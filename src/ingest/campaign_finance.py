"""Campaign-finance connector: Federal Election Commission records (api.open.fec.gov).

For every sitting senator on the map (and former senators you watch): their official FEC candidate ID and principal
campaign committee, then contributions that committee received FROM OTHER POLITICAL COMMITTEES (FEC Form 3,
line 11C). Each contributing PAC is looked up; when the organization the FEC lists as its sponsor ("connected
organization") is an organization on the map, the map records:

  organization -> senator    CORPORATE_PAC_CONTRIBUTED_TO   "via <PAC> (FEC Cxxxxxxxx): $total in <cycle>"

Matching: senator by surname + state + office (Senate) in the FEC's own candidate search, then first name;
sponsor by the FEC's connected-organization name equal to the map organization's name (legal endings removed).

Needs a free api.data.gov key (the same key also unlocks Congress.gov): add it to .hoot/config.json as
"api_data_gov_key". With the demo key only a few senators can be read per hour.

    python -I src/ingest/campaign_finance.py [--limit 100] [--cycle 2026]
"""
import argparse
import json
import os
import sys
import urllib.parse
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _graphio import Writer, org_key  # noqa: E402
from engine import paths  # noqa: E402

sys.path.insert(0, paths.HOOT_DIR)
from hootlib.util import Http, load_config, person_name_match  # noqa: E402
from engine import audit  # noqa: E402

API = "https://api.open.fec.gov/v1"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--cycle", type=int, default=date.today().year + (date.today().year % 2))
    a = ap.parse_args()
    cfg = load_config()
    key = cfg.get("api_data_gov_key") or cfg.get("fec_api_key") or "DEMO_KEY"
    http = Http(cfg)
    http.min_interval["api.open.fec.gov"] = 0.5 if key != "DEMO_KEY" else 2.0
    get = lambda path, **q: json.loads(http.request(f"{API}{path}?" + urllib.parse.urlencode({**q, "api_key": key}, doseq=True),
                                                    ttl=7 * 86400)[0])
    w = Writer("campaign-finance connector (FEC)", "automatic (fec v1)")
    nodes = {n["id"]: n for n in w.g["nodes"] if not (n.get("provenance") or {}).get("merged_into")}
    keys = {}
    for n in nodes.values():
        if n.get("entity_type") not in ("PERSON", "LEGISLATIVE_COMMITTEE", "LEGISLATIVE_CHAMBER"):
            k = org_key(n["label"])
            if len(k) >= 5:
                keys.setdefault(k, n["id"])
    senators = []
    for e in w.g["edges"]:
        if e["relationship_type"] == "SENATOR_IN" and e["source"] in nodes and not (e.get("integrity") or {}).get("superseded_by"):
            role = (e.get("transparency") or {}).get("role_title") or ""
            st = role.split("for ")[-1][:2] if "Senator for" in role else None
            senators.append((e["source"], st))
    print(f"campaign finance: {len(senators)} senators on the map · cycle {a.cycle} · key {'personal' if key != 'DEMO_KEY' else 'DEMO (limited)'}")
    pac_sponsor, made = {}, 0
    for sid, state in senators[:a.limit]:
        label = nodes[sid]["label"]
        try:
            q = {"name": label.split()[-1], "office": "S", "per_page": 20}
            if state:
                q["state"] = state
            cands = [c for c in get("/candidates/search/", **q).get("results") or []
                     if person_name_match(label, " ".join(reversed([x.strip() for x in (c.get("name") or "").split(",", 1)])))]
            if len(cands) != 1:
                print(f"  {label}: {len(cands)} FEC candidate match(es); skipped (needs exactly one)")
                continue
            cand = cands[0]
            pcc = [c for c in get(f"/candidate/{cand['candidate_id']}/committees/", designation="P").get("results") or []]
            if not pcc:
                continue
            cid = pcc[0]["committee_id"]
            totals, pages, last = defaultdict(float), 0, {}
            while pages < 10:
                res = get("/schedules/schedule_a/", committee_id=cid, two_year_transaction_period=a.cycle,
                          line_number="F3-11C", per_page=100, **last)
                for r in res.get("results") or []:
                    if r.get("contributor_id"):
                        totals[(r["contributor_id"], r.get("contributor_name") or "")] += float(r.get("contribution_receipt_amount") or 0)
                li = (res.get("pagination") or {}).get("last_indexes") or {}
                if not li or len(res.get("results") or []) < 100:
                    break
                last, pages = li, pages + 1
        except Exception as ex:                          # noqa: BLE001
            print(f"  {label}: FEC lookup stopped ({str(ex)[:80]})")
            if "429" in str(ex) or "OVER_RATE_LIMIT" in str(ex):
                break
            continue
        for (pac_id, pac_name), amount in totals.items():
            if pac_id not in pac_sponsor:
                try:
                    c = (get(f"/committee/{pac_id}/").get("results") or [{}])[0]
                    pac_sponsor[pac_id] = (c.get("connected_organization_name") or "", c.get("name") or pac_name)
                except Exception:                        # noqa: BLE001
                    pac_sponsor[pac_id] = ("", pac_name)
            conn, pname = pac_sponsor[pac_id]
            org = keys.get(org_key(conn)) if conn else None
            if not org or amount <= 0:
                continue
            w.edge(org, sid, "CORPORATE_PAC_CONTRIBUTED_TO",
                   {"verification_source": "FEC filing", "source_reference": pac_id,
                    "citation_url": f"https://www.fec.gov/data/committee/{pac_id}/", "document_date": date.today().isoformat(),
                    "active": True}, 0.4, "CAMPAIGN_CONTRIBUTION",
                   supplement={"role_title": f"via {pname} ({pac_id}), connected organization as filed: {conn}; "
                                             f"${amount:,.0f} to {label}'s principal campaign committee ({cid}) in the "
                                             f"{a.cycle - 1}–{a.cycle} cycle"[:300]})
            made += 1
        print(f"  {label}: {len(totals)} contributing committees · {made} corporate-sponsored on the map so far")
    w.save()
    audit.log_event("campaign_finance_run", cycle=a.cycle, senators=min(len(senators), a.limit), edges=made, counts=w.counts)
    print(f"campaign finance: {made} corporate PAC contribution links · {w.counts}")


if __name__ == "__main__":
    main()
