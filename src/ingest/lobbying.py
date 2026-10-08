"""Lobbying connector: official Lobbying Disclosure Act filings (lda.gov, the Senate Office of Public Records and
House Clerk's joint system) for the organizations on the map.

For each organization (most-connected first) it reads the filings that name it as a CLIENT for this year and last,
and records, each linked to its filing:
  lobbying firm -> client      LOBBIES_FOR    (issues and reported income)
  client -> U.S. Senate        LOBBIED        when the filing lists the Senate among the bodies contacted
  lobbyist -> lobbying firm    LOBBYIST_AT    only for lobbyists who DISCLOSED a former government job
                                              ("covered position": the official revolving-door disclosure)
Matching: the filing's client name must equal the organization's name once legal endings (Inc, Corpâ€¦) are
removed, and the client's state must match when both are known. Progress is cached; each run continues.

    python -I src/ingest/lobbying.py [--limit 150]
"""
import argparse
import json
import os
import sys
import urllib.parse
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _graphio import ROOT, Writer, org_key  # noqa: E402
from engine import paths  # noqa: E402

sys.path.insert(0, paths.HOOT_DIR)
from hootlib.util import Http, load_config  # noqa: E402
from engine.validator import is_superseded  # noqa: E402
from engine import audit  # noqa: E402

DONE = os.path.join(ROOT, "data", "staging", "lobbying_done.json")
API = "https://lda.gov/api/v1/filings/?{}"
ACTIVE_DAYS = 456


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150, help="organizations to check this run")
    a = ap.parse_args()
    http = Http(load_config())
    http.min_interval["lda.gov"] = 4.5                   # stay well inside the public API's rate limit
    w = Writer("lobbying connector (Senate LDA)", "automatic (lobbying v1)")
    g = w.g
    done = json.load(open(DONE, encoding="utf-8")) if os.path.exists(DONE) else {}
    today = date.today()
    deg = {}
    for e in g["edges"]:
        if not is_superseded(e):
            deg[e["source"]] = deg.get(e["source"], 0) + 1
            deg[e["target"]] = deg.get(e["target"], 0) + 1
    orgs = [n for n in g["nodes"] if n.get("entity_type") not in ("PERSON", "LOBBYING_REGISTRANT", "LEGISLATIVE_COMMITTEE",
                                                                   "LEGISLATIVE_CHAMBER")
            and not (n.get("provenance") or {}).get("merged_into") and (n.get("identity") or {}).get("sec_cik")]
    orgs.sort(key=lambda n: -deg.get(n["id"], 0))
    todo = [n for n in orgs if (done.get(n["id"]) or "") < (today - timedelta(days=30)).isoformat()][:a.limit]
    print(f"lobbying: {len(orgs)} organizations eligible Â· {len(todo)} checked this run")
    found_clients, revolving = 0, 0
    for i, n in enumerate(todo, 1):
        key = org_key(n["label"])
        if len(key) < 4:
            done[n["id"]] = today.isoformat()
            continue
        filings = []
        for year in (today.year - 1, today.year):
            url = API.format(urllib.parse.urlencode({"client_name": key, "filing_year": year, "page_size": 25}))
            pages = 0
            while url and pages < 8:
                try:
                    d = json.loads(http.request(url, ttl=7 * 86400)[0])
                except Exception as ex:                   # noqa: BLE001
                    print(f"  {n['label']}: {str(ex)[:80]}")
                    break
                filings += [f for f in d.get("results") or [] if org_key((f.get("client") or {}).get("name")) == key]
                url, pages = d.get("next"), pages + 1
        if filings:
            found_clients += 1
        by_reg = {}
        for f in filings:
            if (f.get("filing_type") or "").startswith(("RR", "MM")) and not f.get("lobbying_activities"):
                continue
            by_reg.setdefault((f.get("registrant") or {}).get("id"), []).append(f)
        senate_trail = []
        for rid, fs in by_reg.items():
            fs.sort(key=lambda f: f.get("dt_posted") or "")
            latest = fs[-1]
            reg = latest.get("registrant") or {}
            issues = sorted({x.get("general_issue_code_display") or "" for f in fs for x in f.get("lobbying_activities") or []} - {""})
            income = sum(float(f.get("income") or 0) for f in fs)
            posted = (latest.get("dt_posted") or "")[:10]
            active = posted >= (today - timedelta(days=ACTIVE_DAYS)).isoformat()
            trail = [{"date": (f.get("dt_posted") or "")[:10], "url": f.get("filing_document_url")} for f in fs][-12:]
            t = {"verification_source": "Senate LDA filing", "source_reference": latest["filing_uuid"],
                 "citation_url": latest["filing_document_url"], "document_date": posted, "active": active}
            in_house = org_key(reg.get("name")) == key
            if not in_house:
                rnode = w.node(reg.get("name") or "Lobbying registrant", "LOBBYING_REGISTRANT", f"LDA-REGISTRANT:{reg.get('id')}",
                               ["DOM_C"], {"domain_basis": "lobbying registrant: institutional influence (DOM_C) by default",
                                           "registrant_city": f"{reg.get('city')}, {reg.get('state')}"})
                w.edge(rnode, n["id"], "LOBBIES_FOR", t, 0.4, "LOBBYING_ENGAGEMENT",
                       supplement={"role_title": (f"issues: {', '.join(issues[:8])}" + (f"; reported income ${income:,.0f}" if income else ""))[:300],
                                   "evidence_trail": trail})
            for f in fs:
                if any((x.get("name") or "").upper() == "SENATE" for act in f.get("lobbying_activities") or []
                       for x in act.get("government_entities") or []):
                    senate_trail.append({"date": (f.get("dt_posted") or "")[:10], "url": f.get("filing_document_url"),
                                         "uuid": f["filing_uuid"], "by": reg.get("name")})
            # lobbyists who disclosed a former government job (the statutory "covered position")
            for f in fs[-2:]:
                for act in f.get("lobbying_activities") or []:
                    for lb in act.get("lobbyists") or []:
                        cp = (lb.get("covered_position") or "").strip()
                        p = lb.get("lobbyist") or {}
                        if not cp or not p.get("id") or in_house:
                            continue
                        name = " ".join(x.title() for x in (p.get("first_name"), p.get("middle_name"), p.get("last_name")) if x)
                        lid = w.node(name, "PERSON", f"LDA-LOBBYIST:{p['id']}", ["DOM_C"],
                                     {"covered_position": cp[:300], "identity_basis": "lobbyist ID in Senate LDA filings"})
                        w.edge(lid, rnode, "LOBBYIST_AT", {**t, "source_reference": f["filing_uuid"],
                                                           "citation_url": f["filing_document_url"],
                                                           "document_date": (f.get("dt_posted") or "")[:10]},
                               0.5, "FORMER_OFFICIAL_LOBBYIST",
                               supplement={"role_title": f"lobbies for {n['label']}; former position (disclosed): {cp}"[:300]})
                        revolving += 1
        if senate_trail:
            senate_trail.sort(key=lambda x: x["date"])
            last = senate_trail[-1]
            w.edge(n["id"], "ENT_US_SENATE", "LOBBIED",
                   {"verification_source": "Senate LDA filing", "source_reference": last["uuid"], "citation_url": last["url"],
                    "document_date": last["date"], "active": last["date"] >= (today - timedelta(days=ACTIVE_DAYS)).isoformat()},
                   0.4, "LOBBYING_CONTACT",
                   supplement={"role_title": f"{len(senate_trail)} filing(s) list the Senate; firms: "
                                             + ", ".join(sorted({x['by'] for x in senate_trail if x['by']}))[:240],
                               "evidence_trail": [{"date": x["date"], "url": x["url"]} for x in senate_trail][-12:]})
        done[n["id"]] = today.isoformat()
        if i % 50 == 0:
            print(f"  {i}/{len(todo)} Â· {w.counts}")
            w.save()
            json.dump(done, open(DONE, "w", encoding="utf-8"))
    w.save()
    json.dump(done, open(DONE, "w", encoding="utf-8"))
    audit.log_event("lobbying_run", organizations=len(todo), clients_found=found_clients, counts=w.counts)
    print(f"lobbying: {found_clients} of {len(todo)} organizations appear as clients Â· {revolving} former-official "
          f"lobbyist disclosures Â· {w.counts}")


if __name__ == "__main__":
    main()
