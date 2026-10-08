"""UK Companies House: statutory register of every UK company's directors and officers (Companies Act 2006).
Officers carry a Companies House officer ID, a real registry identifier that follows a person across
their UK appointments. Needs a free API key (config: companies_house_api_key)."""
import re
import urllib.error
import urllib.parse

from ..resolve import resolve_person
from ..util import log, norm_name, smart_title

API = "https://api.company-information.service.gov.uk"
WEB = "https://find-and-update.company-information.service.gov.uk"
ROLE = {"director": "director_of", "corporate-director": "director_of", "nominee-director": "director_of",
        "secretary": "officer_of", "corporate-secretary": "officer_of", "llp-designated-member": "director_of",
        "llp-member": "director_of", "member-of-a-management-organ": "director_of", "judicial-factor": "related_to"}


def _need_key(cfg):
    if not cfg.get("companies_house_api_key"):
        raise SystemExit("UK Companies House needs a free API key: see HOOT/Foreign registries.md, then put it in "
                         ".hoot/config.json as \"companies_house_api_key\"")


def _bare(n):
    return re.sub(r"\s+", " ", re.sub(r"\b(limited|ltd|plc|llp|the)\b", " ", norm_name(n))).strip()


def _person_name(ch):
    """'WALSH, Paul Steven' -> 'Paul Steven Walsh'"""
    if "," in ch:
        last, first = ch.split(",", 1)
        return smart_title(f"{first.strip()} {last.strip()}".upper())
    return smart_title(ch.upper())


def find_company(http, query):
    q = query.strip().upper()
    if re.fullmatch(r"[A-Z0-9]{8}", q):
        return q
    d = http.json(f"{API}/search/companies?q={urllib.parse.quote(query)}&items_per_page=20", ttl=7 * 86400)
    items = d.get("items", [])
    exact = [i for i in items if _bare(i["title"]) == _bare(query)]
    active = [i for i in exact if i.get("company_status") == "active"]
    pick = active if len(active) == 1 else exact
    if len(pick) == 1:
        return pick[0]["company_number"]
    if not items:
        raise SystemExit(f"No UK company named {query!r}")
    raise SystemExit("Several UK companies match; re-run with the company number:\n" + "\n".join(
        f"  {i['company_number']}  {i['title']}  [{i.get('company_status')}]  {i.get('address_snippet', '')[:50]}" for i in items[:12]))


def ingest(http, store, cfg, query, appointments=True):
    _need_key(cfg)
    num = find_company(http, query)
    prof = http.json(f"{API}/company/{num}", ttl=7 * 86400)
    name = smart_title(prof["company_name"].upper())
    org = store.upsert_entity("organization", name, {"uk_company": num}, {
        "jurisdiction": "GB", "uk_status": prof.get("company_status"), "uk_sic": prof.get("sic_codes"),
        "incorporated": prof.get("date_of_creation"), "uk_register_url": f"{WEB}/company/{num}"})
    log(f"Companies House: {name} ({num}, {prof.get('company_status')})")
    n_off = officers(http, store, cfg, num, org, name, follow=appointments)
    store.commit()
    log(f"  {n_off} officer appointments recorded")
    return org


def officers(http, store, cfg, num, org, org_name, follow=False):
    d = http.json(f"{API}/company/{num}/officers?items_per_page=100", ttl=7 * 86400)
    evidence = f"{WEB}/company/{num}/officers"
    n = 0
    for o in d.get("items", []):
        role = ROLE.get(o.get("officer_role"), "related_to")
        oid = ((o.get("links") or {}).get("officer") or {}).get("appointments", "").split("/")[2:3]
        oid = oid[0] if oid else None
        corporate = o.get("officer_role", "").startswith("corporate")
        pname = smart_title(o["name"].upper()) if corporate else _person_name(o["name"])
        if corporate:
            pid = store.upsert_entity("organization", pname, {"uk_officer": oid} if oid else {"manual": f"ukch:{norm_name(pname)}"})
            match = {"confidence": "registry", "method": ["Companies House officer ID"]}
        else:
            known = store.find("uk_officer", oid) if oid else None
            if known:
                pid, match = known, {"confidence": "registry", "method": ["Companies House officer ID"]}
            else:
                pid, match = resolve_person(store, pname, org_name, scope=f"ukch:{num}")
                if match["confidence"] == "high" and oid:
                    store.db.execute("INSERT OR IGNORE INTO ids VALUES('uk_officer', ?, ?)", (oid, pid))
                    match["method"].append("Companies House officer ID attached")
                elif oid:                                     # a registry ID of its own: give the record that identity
                    store.db.execute("INSERT OR IGNORE INTO ids VALUES('uk_officer', ?, ?)", (oid, pid))
                    match = {"confidence": "registry", "method": ["Companies House officer ID (not linked to other sources)"]}
            dob = o.get("date_of_birth") or {}
            if dob:
                store.set_attr(pid, "uk_birth_month", f"{dob.get('year')}-{str(dob.get('month')).zfill(2)}")
        store.add_edge(pid, org, role, qualifier=o.get("occupation") if role != "director_of" else None,
                       source="UK Companies House", evidence_url=evidence, filing_date=o.get("appointed_on"),
                       start_date=o.get("appointed_on"), end_date=o.get("resigned_on"),
                       is_current=0 if o.get("resigned_on") else 1,
                       attrs={"identity_match": match, "reference": num, "uk_role": o.get("officer_role"),
                              "nationality": o.get("nationality"), "residence": o.get("country_of_residence")})
        n += 1
        if follow and oid and not o.get("resigned_on") and not corporate:
            appointments(http, store, oid, pid)
    return n


def appointments(http, store, officer_id, pid):
    """The officer's other UK appointments: the UK equivalent of the SEC career sweep."""
    try:
        d = http.json(f"{API}/officers/{officer_id}/appointments?items_per_page=50", ttl=30 * 86400)
    except urllib.error.HTTPError:
        return
    for a in d.get("items", []):
        to = a.get("appointed_to") or {}
        if not to.get("company_number"):
            continue
        org = store.upsert_entity("organization", smart_title((to.get("company_name") or to["company_number"]).upper()),
                                  {"uk_company": to["company_number"]}, {"jurisdiction": "GB", "uk_status": to.get("company_status")})
        store.add_edge(pid, org, ROLE.get(a.get("officer_role"), "related_to"), source="UK Companies House",
                       evidence_url=f"{WEB}/officers/{officer_id}/appointments", filing_date=a.get("appointed_on"),
                       start_date=a.get("appointed_on"), end_date=a.get("resigned_on"),
                       is_current=0 if a.get("resigned_on") else 1,
                       attrs={"identity_match": {"confidence": "registry", "method": ["Companies House officer ID"]},
                              "reference": to["company_number"], "uk_role": a.get("officer_role")})
