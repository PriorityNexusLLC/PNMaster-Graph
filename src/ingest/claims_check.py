"""Check a claims file (data/staging/claims_*.json: person -> organizations a document asserts) against the
master graph's official records, and write a plain report to Obsidian.

Each claim gets one status:
  CONFIRMED            an official record (SEC filing, Senate roster, Congress.gov) ties this person to it
  NOT IN RECORDS       the person was found, but no official record on file ties them to this organization
  PERSON NOT FOUND     no official record for this person yet (or only possible name matches)
  OUTSIDE COVERAGE     the organization files nothing we read (private firm, foreign company, government agency
                       without a roster connector yet): needs another kind of source

    python -I src/ingest/claims_check.py data/staging/claims_senate_119.json --title "Senate 119th Congress"
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import paths  # noqa: E402
sys.path.insert(0, paths.HOOT_DIR)
from engine import audit, store  # noqa: E402
from engine.validator import is_superseded  # noqa: E402
from hootlib.people import name_variants  # noqa: E402
from hootlib.util import person_name_match  # noqa: E402

STOP = {"inc", "corp", "corporation", "co", "company", "the", "llc", "lp", "ltd", "plc", "group", "holdings", "holding",
        "us", "u", "s", "of", "and"}


def org_words(s):
    return [w for w in re.sub(r"[^a-z0-9 ]", " ", s.lower().replace("&", " and ")).split() if w not in STOP]


def org_match(claimed, label):
    a, b = org_words(claimed), org_words(label)
    if not a or not b:
        return False
    tight = lambda ws: "".join(ws)
    return tight(a) == tight(b) or tight(a) in tight(b) and len(tight(a)) >= 5 or set(a) <= set(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("claims")
    ap.add_argument("--title", default=None)
    a = ap.parse_args()
    claims = json.load(open(a.claims, encoding="utf-8"))
    g = store.load_graph()
    nodes = {n["id"]: n for n in g["nodes"] if not (n.get("provenance") or {}).get("merged_into")}
    persons = [n for n in nodes.values() if n.get("entity_type") == "PERSON"]
    edges = [e for e in g["edges"] if not is_superseded(e) and e["source"] in nodes and e["target"] in nodes]
    out_of = {}
    for e in edges:
        out_of.setdefault(e["source"], []).append(e)

    def find_people(name):
        hits = []
        for p in persons:
            names = [p["label"]] + ((p.get("provenance") or {}).get("aliases_seen_in_sources") or [])
            if any(person_name_match(v, x) in ("full", "first_last") for v in name_variants(name) for x in names):
                hits.append(p)
        return hits

    rows, counts = [], {}
    for row in claims["rows"]:
        who = row["person_claimed"]
        found = find_people(who)
        for c in row["claims"]:
            org = c["org_claimed"]
            status, detail = "PERSON NOT FOUND", "no official record found for this person yet"
            if found:
                status, detail = "NOT IN RECORDS", "person found (" + ", ".join(p["label"] for p in found) + \
                                 "), but no official record on file ties them to this organization"
                for p in found:
                    for e in out_of.get(p["id"], []):
                        tgt = nodes[e["target"]]
                        if org_match(org, tgt["label"]) or (org_words(org) == ["senate"] and e["target"] == "ENT_US_SENATE") \
                                or (org == "U.S. Senate" and e["target"] == "ENT_US_SENATE"):
                            t = e.get("transparency") or {}
                            status = "CONFIRMED"
                            detail = (f"{p['label']}: {e['relationship_type'].replace('_', ' ').lower()} {tgt['label']}"
                                      f"{' (' + t['role_title'] + ')' if t.get('role_title') else ''} · "
                                      f"{'current' if t.get('active') is not False else 'historical'} · "
                                      f"[{t.get('verification_source')} {t.get('source_reference') or ''}]({t.get('citation_url')})")
                            break
                    if status == "CONFIRMED":
                        break
            if status != "CONFIRMED" and not any(org_match(org, n["label"]) for n in nodes.values() if n.get("entity_type") != "PERSON"):
                status = "OUTSIDE COVERAGE" if status == "NOT IN RECORDS" or not found else status
                if status == "OUTSIDE COVERAGE":
                    detail = "this organization isn't in any official record we read yet (private firm, agency or foreign company)"
            counts[status] = counts.get(status, 0) + 1
            rows.append((who, org, status, detail))

    # committee seats for everyone confirmed as a senator
    seats = []
    for row in claims["rows"]:
        for p in find_people(row["person_claimed"]):
            for e in out_of.get(p["id"], []):
                if e["relationship_type"] == "COMMITTEE_MEMBER_OF" and (e.get("transparency") or {}).get("active") is not False:
                    seats.append((p["label"], nodes[e["target"]]["label"], (e.get("transparency") or {}).get("role_title") or "Member"))

    title = a.title or claims.get("source", os.path.basename(a.claims))
    L = ["---", "hoot: report", f"generated: {store.now_iso()[:10]}", "tags: [hoot/report]", "---",
         f"# Claims check: {title}", "",
         f"Source document: {claims.get('source', '')}. Every pairing it makes was checked against official records "
         "(SEC filings, senate.gov rosters, Congress.gov). **CONFIRMED** means an official record says so, with the link. "
         "Anything else is still a claim.", "",
         " · ".join(f"**{k}** {v}" for k, v in sorted(counts.items())), "",
         "| Person | Organization claimed | Status | Official record |", "|---|---|---|---|"]
    L += [f"| {w} | {o} | {s} | {d} |" for w, o, s, d in rows]
    if seats:
        L += ["", "## Current Senate committee seats (senate.gov)", "", "| Senator | Committee | Position |", "|---|---|---|"]
        L += [f"| {a_} | {b} | {c} |" for a_, b, c in sorted(set(seats))]
    L += ["", "Not covered by these records yet: spouses' roles unless they file themselves; private firms (e.g. "
          "Bridgewater, BCG); advisory councils; executive-branch appointments (Treasury, Fed, Pentagon), which need a "
          "Congress.gov nominations connector."]
    path = os.path.join(os.path.dirname(audit.ANCHOR_PATH), f"Claims check - {title}.md")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    print(f"claims check: " + " · ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f" -> {path}")


if __name__ == "__main__":
    main()
