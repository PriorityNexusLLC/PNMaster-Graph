"""NIH RePORTER: federal health grants -> PI, awardee organization, funding institute, dollars."""
from collections import defaultdict

from ..util import log

API = "https://api.reporter.nih.gov/v2/projects/search"


def ingest(http, store, cfg, *, pi=None, org=None, project=None, limit=100, years=None):
    crit = {}
    if pi:
        crit["pi_names"] = [{"any_name": pi}]
    if org:
        crit["org_names"] = [org]
    if project:
        crit["project_nums"] = [project]
    if years:
        crit["fiscal_years"] = years
    if not crit:
        raise SystemExit("nih: give --pi, --org or --project")
    body = {"criteria": crit, "limit": min(limit, 500), "offset": 0,
            "sort_field": "fiscal_year", "sort_order": "desc"}
    d = http.post_json(API, body, ttl=86400)
    res = d.get("results", [])
    total = (d.get("meta") or {}).get("total", len(res))
    log(f"NIH RePORTER: {len(res)} of {total} project-years")

    grants = defaultdict(list)
    for r in res:
        grants[r.get("core_project_num") or r.get("project_num")].append(r)

    for core, rows in grants.items():
        rows.sort(key=lambda r: r.get("fiscal_year") or 0)
        last = rows[-1]
        by_fy = {str(r.get("fiscal_year")): r.get("award_amount") for r in rows if r.get("award_amount")}
        total_amt = sum(v for v in by_fy.values() if v)
        url = f"https://reporter.nih.gov/project-details/{last.get('appl_id')}"
        g = store.upsert_entity("grant", f"NIH {core}: {(last.get('project_title') or '')[:80]}", {"nih_project": core}, {
            "title": last.get("project_title"), "fiscal_years": sorted(by_fy),
            "award_by_fiscal_year": by_fy, "project_start": (last.get("project_start_date") or "")[:10],
            "project_end": (last.get("project_end_date") or "")[:10], "reporter_url": url})
        src = "NIH RePORTER"
        common = dict(source=src, evidence_url=url, filing_date=(last.get("award_notice_date") or "")[:10] or None,
                      start_date=(last.get("project_start_date") or "")[:10] or None,
                      end_date=(last.get("project_end_date") or "")[:10] or None)
        o = last.get("organization") or {}
        if o.get("org_name"):
            oids = {"manual": "nih-org:" + o["org_name"].lower()}
            if o.get("org_ueis"):
                oids["uei"] = o["org_ueis"][0]
            hits = store.find_by_name(o["org_name"], "organization")
            org_e = hits[0] if len(hits) == 1 else store.upsert_entity("organization", o["org_name"].title(), oids,
                                                                        {"city": o.get("org_city"), "state": o.get("org_state")})
            store.add_edge(g, org_e, "grant_awarded_to", amount=total_amt, currency="USD", **common)
        ic = last.get("agency_ic_admin") or {}
        if ic.get("name"):
            ic_e = store.upsert_entity("organization", ic["name"], {"manual": "nih-ic:" + (ic.get("abbreviation") or ic["name"])},
                                       {"is_funder": True, "abbreviation": ic.get("abbreviation")})
            store.add_edge(g, ic_e, "funded_by", amount=total_amt, currency="USD", **common)
        for p in last.get("principal_investigators") or []:
            name = (p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}").strip().title()
            pe = store.upsert_entity("person", name, {"nih_pi": p.get("profile_id")} if p.get("profile_id") else
                                     {"manual": "nih-pi:" + name.lower()})
            store.add_edge(pe, g, "principal_investigator_of", qualifier="contact PI" if p.get("is_contact_pi") else None,
                           **common)
    log(f"  {len(grants)} grants recorded")
    store.commit()
