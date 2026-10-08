"""Private companies (SEC Form D) and nonprofits (IRS Form 990): the people HOOT can't reach through
insider filings. Both are statutory filings; people in them carry no ID, so every person link goes
through resolve.resolve_person and records how confident the identity match is."""
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from ..resolve import resolve_person
from ..util import log, norm_name, smart_title
from .sec import _strip_ns, _t, doc_urls, pad, recent_rows, submissions

EFTS = "https://efts.sec.gov/LATEST/search-index"
PP = "https://projects.propublica.org/nonprofits/api/v2"
GT_XML = "https://gt990datalake-rawdata.s3.amazonaws.com/EfileData/XmlFiles/{oid}_public.xml"


def _bare(name):
    """Name with legal-form words removed, for exact comparison: 'Cibo Technologies, Inc.' -> 'cibo technologies'."""
    n = norm_name(re.sub(r"\(.*?\)", " ", name or ""))
    n = re.sub(r"\b(inc|incorporated|corp|corporation|co|company|llc|l l c|ltd|limited|lp|l p|plc|the)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _recent(d, days):
    return bool(d) and d >= (date.today() - timedelta(days=days)).isoformat()


# ================================================================ Form D (private companies)
def find_formd_issuer(http, query):
    q = urllib.parse.quote(f'"{query}"')
    d = http.json(f"{EFTS}?q={q}&forms=D", ttl=7 * 86400)
    found = {}
    for h in d.get("hits", {}).get("hits", []):
        for dn, cik in zip(h["_source"].get("display_names", []), h["_source"].get("ciks", [])):
            name = re.sub(r"\s*\(CIK.*$", "", dn).strip()
            found[cik] = name
    key = _bare(query)
    exact = [c for c, n in found.items() if _bare(n) == key]       # exact name only: full-text hits can be unrelated
    if len(exact) == 1:
        return exact[0]
    if not exact and found:
        raise SystemExit(f"No Form D filer is named {query!r} (full-text hits were other companies: "
                         + "; ".join(list(found.values())[:5]) + ")")
    if not found:
        raise SystemExit(f"No Form D filer matches {query!r}. It may never have raised money under Reg D, or files under another legal name.")
    raise SystemExit("Several Form D filers match; re-run with the CIK:\n" + "\n".join(f"  CIK {int(c):<9} {n}" for c, n in found.items()))


def ingest_formd(http, store, cfg, query, max_filings=6):
    cik = pad(query) if query.strip().isdigit() else find_formd_issuer(http, query)
    sub = submissions(http, cik)
    rows = [r for r in recent_rows(sub) if r["form"] in ("D", "D/A")][:max_filings]
    if not rows:
        raise SystemExit(f"{sub.get('name')} has no Form D filings")
    log(f"Form D: {sub.get('name')} (CIK {int(cik)}): {len(rows)} filings")
    org = None
    stats = {"high": 0, "name_only": 0, "unlinked": 0}
    for r in reversed(rows):                                   # oldest first; newest facts win
        url, index = doc_urls(cik, r)
        b, sha = http.request(url, ttl=None)
        root = _strip_ns(ET.fromstring(b))
        name = _t(root, ".//primaryIssuer/entityName") or sub.get("name")
        org = store.upsert_entity("organization", smart_title(name), {"sec_cik": cik}, {
            "private_issuer": True, "industry": _t(root, ".//industryGroup/industryGroupType"),
            "jurisdiction": _t(root, ".//primaryIssuer/jurisdictionOfInc"),
            "year_of_inc": _t(root, ".//yearOfInc/value"), "entity_type": _t(root, ".//primaryIssuer/entityType"),
            "city": _t(root, ".//primaryIssuer/issuerAddress/city"),
            "form_d_latest": {"date": r["filingDate"], "total_offering": _t(root, ".//totalOfferingAmount"),
                              "total_sold": _t(root, ".//totalAmountSold"), "filing": index}})
        for rp in root.findall(".//relatedPersonsList/relatedPersonInfo"):
            first, mid, last = (_t(rp, f"relatedPersonName/{k}") or "" for k in ("firstName", "middleName", "lastName"))
            pname = smart_title(" ".join(x for x in (first, mid, last) if x).upper())
            city = _t(rp, "relatedPersonAddress/city")
            pid, match = resolve_person(store, pname, name, city=city, scope=f"formd:{int(cik)}")
            stats[match["confidence"]] += 1
            rels = [x.text.strip() for x in rp.findall("relatedPersonRelationshipList/relationship") if x.text]
            clar = _t(rp, "relationshipClarification")
            for rel in rels:
                relation = {"Director": "director_of", "Executive Officer": "officer_of"}.get(rel, "related_to")
                store.add_edge(pid, org, relation, qualifier=clar if relation != "director_of" else None,
                               source="SEC Form D", evidence_url=index, filing_date=r["filingDate"], source_sha256=sha,
                               is_current=1 if _recent(r["filingDate"], 456) else 0,
                               attrs={"identity_match": match, "city_in_filing": city, "form_d_role": rel,
                                      "document_url": url})
    store.commit()
    log(f"  people: {stats['high']} linked with high confidence · {stats['name_only']} name-only (kept as leads) · "
        f"{stats['unlinked']} new (no match)")
    return org


# ================================================================ Form 990 (nonprofits)
def find_nonprofit(http, query):
    q = re.sub(r"\D", "", query)
    if len(q) == 9:
        return q
    d = http.json(f"{PP}/search.json?q={urllib.parse.quote(query)}", ttl=7 * 86400)
    orgs = d.get("organizations", [])
    key = _bare(query)
    exact = [o for o in orgs if _bare(o["name"]) == key]
    if len(exact) == 1:
        return str(exact[0]["ein"]).zfill(9)
    if not exact and orgs:
        raise SystemExit("No nonprofit is named exactly that; closest IRS names (re-run with the EIN if one is right):\n" + "\n".join(
            f"  EIN {str(o['ein']).zfill(9)}  {o['name']}  ({o.get('city')}, {o.get('state')})" for o in orgs[:10]))
    if not orgs:
        raise SystemExit(f"No nonprofit matches {query!r} in IRS data (ProPublica Nonprofit Explorer)")
    raise SystemExit("Several nonprofits match; re-run with the EIN:\n" + "\n".join(
        f"  EIN {str(o['ein']).zfill(9)}  {o['name']}  ({o.get('city')}, {o.get('state')})" for o in orgs[:15]))


FLAG = lambda x: (x or "").strip().lower() in ("x", "1", "true")


# ---------------------------------------------------------------- read-only peeks (for choosing between same-named orgs)
def peek_formd_people(http, cik):
    sub = submissions(http, cik)
    rows = [r for r in recent_rows(sub) if r["form"] in ("D", "D/A")][:3]
    names = set()
    for r in rows:
        url, _ = doc_urls(cik, r)
        root = _strip_ns(ET.fromstring(http.request(url, ttl=None)[0]))
        for rp in root.findall(".//relatedPersonsList/relatedPersonInfo"):
            names.add(" ".join(x for x in (_t(rp, f"relatedPersonName/{k}") for k in ("firstName", "middleName", "lastName")) if x))
    return sub.get("name"), sorted(names)


def peek_990_people(http, ein):
    meta = http.json(f"{PP}/organizations/{int(ein)}.json", ttl=7 * 86400)["organization"]
    oid = meta.get("latest_object_id")
    if not oid:
        return meta.get("name"), []
    root = _strip_ns(ET.fromstring(http.request(GT_XML.format(oid=oid), ttl=90 * 86400)[0]))
    names = {el.find("PersonNm").text.strip() for el in root.iter()
             if el.find("PersonNm") is not None and el.find("TitleTxt") is not None and el.find("PersonNm").text}
    return meta.get("name"), sorted(names)


def formd_candidates(http, query):
    q = urllib.parse.quote(f'"{query}"')
    d = http.json(f"{EFTS}?q={q}&forms=D", ttl=7 * 86400)
    out = {}
    for h in d.get("hits", {}).get("hits", []):
        for dn, cik in zip(h["_source"].get("display_names", []), h["_source"].get("ciks", [])):
            out[cik] = re.sub(r"\s*\(CIK.*$", "", dn).strip()
    return out


def nonprofit_candidates(http, query):
    out = {}
    for page in range(0, 3):                                  # chapters with the same name can crowd page 1
        try:
            d = http.json(f"{PP}/search.json?q={urllib.parse.quote(query)}&page={page}", ttl=7 * 86400)
        except Exception:                                     # noqa: BLE001
            break
        orgs = d.get("organizations", [])
        out.update({str(o["ein"]).zfill(9): f"{o['name']} ({o.get('city')}, {o.get('state')})" for o in orgs})
        if len(orgs) < 25:
            break
    return out


def ingest_990(http, store, cfg, query):
    ein = find_nonprofit(http, query)
    meta = http.json(f"{PP}/organizations/{int(ein)}.json", ttl=7 * 86400)["organization"]
    oid = meta.get("latest_object_id")
    if not oid:
        raise SystemExit(f"{meta.get('name')}: no e-filed 990 available (small filers may file 990-N, which lists no board)")
    xml_url = GT_XML.format(oid=oid)
    b, sha = http.request(xml_url, ttl=90 * 86400)
    root = _strip_ns(ET.fromstring(b))
    tax_end = _t(root, ".//TaxPeriodEndDt") or ""
    filed = (_t(root, ".//ReturnTs") or "")[:10] or tax_end
    rtype = _t(root, ".//ReturnTypeCd")
    ein_fmt = f"{ein[:2]}-{ein[2:]}"
    page = f"https://projects.propublica.org/nonprofits/organizations/{int(ein)}"
    name = smart_title((meta.get("name") or "").upper())
    org = store.upsert_entity("organization", name, {"ein": ein}, {
        "nonprofit": True, "ntee_code": meta.get("ntee_code"), "city": (meta.get("city") or "").title(),
        "state": meta.get("state"), "form_990": {"type": rtype, "tax_period_end": tax_end, "filed": filed, "page": page},
        "part_vi": {"family_or_business_relationships_among_officers": FLAG(_t(root, ".//FamilyOrBusinessRlnInd")),
                    "conflict_of_interest_policy": FLAG(_t(root, ".//ConflictOfInterestPolicyInd")),
                    "annual_conflict_disclosure": FLAG(_t(root, ".//AnnualDisclosureCoveredPrsnInd")),
                    "regular_monitoring": FLAG(_t(root, ".//RegularMonitoringEnfrcInd"))}})
    current = _recent(tax_end, 900)                           # 990s lag a year or more
    common = dict(source=f"IRS Form 990 ({tax_end[:4]})", evidence_url=page, filing_date=filed or None,
                  source_sha256=sha)
    stats = {"high": 0, "name_only": 0, "unlinked": 0}
    seen = set()
    for el in root.iter():
        kids = {c.tag: (c.text or "").strip() for c in el}
        pname = kids.get("PersonNm")
        if not pname or "TitleTxt" not in kids or norm_name(pname) in seen:
            continue
        seen.add(norm_name(pname))
        nice = smart_title(pname.upper())
        pid, match = resolve_person(store, nice, name, scope=f"ein:{ein}")
        stats[match["confidence"]] += 1
        director = FLAG(kids.get("IndividualTrusteeOrDirectorInd")) or FLAG(kids.get("InstitutionalTrusteeInd")) \
            or re.search(r"director|trustee|chair|board", kids.get("TitleTxt", ""), re.I)
        officer = FLAG(kids.get("OfficerInd")) or FLAG(kids.get("KeyEmployeeInd"))
        former = FLAG(kids.get("FormerOfcrDirectorTrusteeInd"))
        comp = kids.get("ReportableCompFromOrgAmt") or kids.get("CompensationAmt")
        attrs = {"identity_match": match, "title": kids.get("TitleTxt"), "hours_per_week": kids.get("AverageHoursPerWeekRt"),
                 "compensation_usd": comp, "comp_from_related_orgs_usd": kids.get("ReportableCompFromRltdOrgAmt"),
                 "document_url": xml_url, "reference": f"{ein_fmt} / {tax_end[:4]}"}
        for rel in (["director_of"] if director else []) + (["officer_of"] if officer else []) or ["related_to"]:
            store.add_edge(pid, org, rel, qualifier=kids.get("TitleTxt") if rel != "director_of" else None,
                           is_current=0 if former else (1 if current else 0), attrs=attrs, **common)
    # Schedule R: related organizations, identified by EIN
    for grp in root.iter():
        kids = {c.tag: c for c in grp}
        rein = kids.get("EIN")
        nm = grp.find(".//BusinessNameLine1Txt")
        if rein is None or nm is None or grp.tag.startswith("Return") or not (grp.tag.startswith("IdRelated") or "Related" in grp.tag):
            continue
        r_ein = re.sub(r"\D", "", rein.text or "")
        if len(r_ein) != 9 or r_ein == ein:
            continue
        rid = store.upsert_entity("organization", smart_title((nm.text or "").upper()), {"ein": r_ein})
        store.add_edge(org, rid, "related_to", qualifier=f"Schedule R ({grp.tag})", is_current=1 if current else 0,
                       attrs={"document_url": xml_url, "reference": f"{ein_fmt} / {tax_end[:4]}"}, **common)
    store.commit()
    pv = store.entity(org)["attrs"]["part_vi"]
    log(f"Form 990: {name} (EIN {ein_fmt}, tax year {tax_end[:4]}): {len(seen)} people · "
        f"{stats['high']} linked (high) · {stats['name_only']} name-only leads · {stats['unlinked']} new")
    if pv["family_or_business_relationships_among_officers"]:
        log("  ! Part VI line 2: the organization reports family/business relationships among its officers/directors")
    return org
