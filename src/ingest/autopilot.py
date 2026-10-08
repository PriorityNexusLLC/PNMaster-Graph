"""Autopilot: move HOOT's verified SEC evidence into the master graph without manual review.

What it adds: only connections that come from statutory SEC filings (Forms 3/4/5, Schedule 13D/13G)
with the filer's SEC ID and an accession number, i.e. exactly what a reviewer could approve.
How it classifies, in order, recorded on every record ("domain_basis"):
    1. your own spreadsheet (data/staging/user_domains.json, from Connections.xlsx)
    2. the company's SEC industry code (SIC) via data/sic_domains.json ("exact" or flagged "nearest")
    3. people: the domains of the companies they file for (+ BRIDGE_HUMAN when 2+ domains)
Weights: HOOT's generic defaults, labelled as automatic. Everything added is marked
provenance.review = "automatic"; the validator still checks every row; nothing is overwritten
(a newer filing *supersedes* an older record, with the reason logged).
Companies it can't classify are skipped and listed in the report, never guessed.
"""
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import paths  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "src", "ingest"))

from engine import staging, store  # noqa: E402
from claims import org_key  # noqa: E402
from hoot_bridge import DEFAULT_HOOT_DB, stage  # noqa: E402

REVIEW = "automatic (autopilot v1)"
with open(os.path.join(ROOT, "data", "other_domains.json"), encoding="utf-8") as _f:
    OTHER = json.load(_f)
REPORT = os.path.join(paths.REPORTS, "Autopilot run.md")


def load_sic_rules():
    with open(os.path.join(ROOT, "data", "sic_domains.json"), encoding="utf-8") as f:
        rules = json.load(f)["rules"]
    return sorted(rules, key=lambda r: r[1] - r[0])          # most specific range first


def sic_lookup(hoot_db):
    """Fetch missing SIC codes through HOOT's cached SEC client (one cached request per company)."""
    sys.path.insert(0, os.path.dirname(hoot_db))
    from hootlib.util import Http, load_config
    http = Http(load_config())

    def get(cik):
        try:
            d = http.json(f"https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json", ttl=30 * 86400)
            return d.get("sic"), d.get("sicDescription")
        except Exception:                                    # noqa: BLE001
            return None, None
    return get


def classify_org(nd, user, rules, sic_get):
    for k in [org_key(nd["label"])] + [org_key(a) for a in nd.get("aliases", [])]:
        if k in user["entities"]:
            u = user["entities"][k]
            return u["domains"], f"your {user['source']} ({', '.join(u['where'][:2])})"
    sic, desc = nd.get("info", {}).get("sic"), nd.get("info", {}).get("industry")
    if not sic and nd["identity"].get("sec_cik"):
        sic, desc = sic_get(nd["identity"]["sec_cik"])
    if sic and str(sic).isdigit():
        n = int(sic)
        for lo, hi, doms, conf, label in rules:
            if lo <= n <= hi:
                basis = f"SEC industry code {sic} ({desc or label}) → {label}"
                return doms, basis + ("" if conf == "exact" else " [nearest fit, no exact domain]")
    info = nd.get("info", {})
    if info.get("ntee_code"):                                   # nonprofits: IRS NTEE classification
        r = OTHER["ntee"].get(info["ntee_code"][0].upper())
        if r:
            return r[0], f"IRS NTEE code {info['ntee_code']} ({r[2]})" + ("" if r[1] == "exact" else " [nearest fit, no exact domain]")
    if info.get("private_issuer") and info.get("industry"):    # private companies: Form D industry group
        r = OTHER["form_d_industry"].get(info["industry"])
        if r:
            return r[0], f"Form D industry group '{info['industry']}'" + ("" if r[1] == "exact" else " [nearest fit, no exact domain]")
    # SEC gives investment vehicles no industry code; their names say what they are
    if nd["identity"].get("sec_cik") and not sic and INVEST.search(nd["label"]):
        return ["DOM_C"], (f"SEC filer with no industry code; name '{nd['label']}' indicates an investment vehicle "
                           "(fund / partnership / sovereign authority) [nearest fit, by name]")
    return None, f"no classification: SIC {sic or 'unknown'} {desc or ''}".strip()


INVEST = __import__("re").compile(r"\b(fund|funds|partners|partnership|l\.?p\.?|capital|invest\w*|trust|authority|"
                                  r"holdings|equity|ventures|asset|advisors?|management|lending|bank|credit|income|"
                                  r"opportunit\w*|acquisition)\b", __import__("re").I)


def run(hoot_db=DEFAULT_HOOT_DB, centers=()):
    path, batch = stage(hoot_db, list(centers))
    user = {"source": "Connections.xlsx", "entities": {}}
    up = os.path.join(ROOT, "data", "staging", "user_domains.json")
    if os.path.exists(up):
        with open(up, encoding="utf-8") as f:
            user = json.load(f)
    rules, sic_get = load_sic_rules(), sic_lookup(hoot_db)
    graph = store.load_graph()
    nodes = {n["key"]: n for n in batch["nodes"]}

    dom, basis, unclassified = {}, {}, []
    for k, nd in nodes.items():
        if nd["status"] == "existing":
            dom[k] = nd["domains"]
        elif nd["entity_type"] != "PERSON":
            d, b = classify_org(nd, user, rules, sic_get)
            if d:
                dom[k], basis[k] = d, b
            else:
                unclassified.append((nd["label"], b))

    # people inherit the domains of the classified companies they file for
    person_orgs = defaultdict(set)
    for e in batch["edges"]:
        for p, o in ((e["source_key"], e["target_key"]), (e["target_key"], e["source_key"])):
            if nodes[p]["entity_type"] == "PERSON" and nodes[o]["entity_type"] != "PERSON" and o in dom:
                person_orgs[p].add(o)
    for p, orgs in person_orgs.items():
        if nodes[p]["status"] == "existing":
            continue
        ds = sorted({d for o in orgs for d in dom[o] if d.startswith("DOM_")})
        if ds:
            dom[p] = ds + (["BRIDGE_HUMAN"] if len(ds) >= 2 else [])
            basis[p] = "inherited from filed roles at: " + ", ".join(sorted(nodes[o]["label"] for o in orgs))[:300]

    # newest filing wins via explicit supersede, never by overwriting
    key_to_id = {n["key"]: n["master_id"] for n in batch["nodes"]}
    live = {(e["source"], e["target"], e["relationship_type"]): e for e in graph["edges"]
            if not (e.get("integrity") or {}).get("superseded_by")}
    decisions = {"review": REVIEW, "nodes": {k: {"domains": dom[k], "basis": basis.get(k)} for k in dom if nodes[k]["status"] == "new"},
                 "edges": {}}
    skipped, seen_triples = 0, set()
    for e in sorted(batch["edges"], key=lambda x: x["transparency"]["document_date"] or "", reverse=True):
        if e["source_key"] not in dom or e["target_key"] not in dom:
            skipped += 1
            continue
        triple = (key_to_id[e["source_key"]], key_to_id[e["target_key"]], e["relationship_type"])
        if triple in seen_triples:                 # e.g. two officer titles: keep the newest filing's record
            continue
        seen_triples.add(triple)
        d = {"approve": True, "conflict_weight": e["integrity"]["suggested_conflict_weight"],
             "weight_basis": "automatic: HOOT default by relation type (halved if historical, scaled by % owned); not reviewed"}
        old = live.get((key_to_id[e["source_key"]], key_to_id[e["target_key"]], e["relationship_type"]))
        if old:
            ot, oi = old.get("transparency") or {}, old.get("integrity") or {}
            was_auto = ((old.get("provenance") or {}).get("review") or "").startswith("automatic")
            newer = (e["transparency"]["document_date"] or "") > (ot.get("document_date") or "")
            facts_changed = newer and (ot.get("active") != e["transparency"]["active"]
                                       or ot.get("source_reference") != e["transparency"]["source_reference"])
            # a person's decisions are never touched; automatic records are corrected only by an explicit supersede
            weight_fix = was_auto and abs((oi.get("conflict_weight") or 0) - d["conflict_weight"]) > 0.001
            status_fix = was_auto and ot.get("active") != e["transparency"]["active"]
            if not (facts_changed or weight_fix or status_fix):
                continue
            if facts_changed:
                reason = (f"autopilot: newer {e['transparency']['verification_source']} filing "
                          f"{e['transparency']['document_date']} (record was {ot.get('document_date')})")
            else:
                reason = (f"autopilot correction: weight {oi.get('conflict_weight')} → {d['conflict_weight']}, "
                          f"active {ot.get('active')} → {e['transparency']['active']} (historical ties are halved; "
                          "earlier automatic record used the unhalved default)")
            d["supersedes"] = {"edge_id": old["edge_id"], "reason": reason}
        decisions["edges"][e["key"]] = d

    res = staging.apply(batch["batch_id"], decisions) if decisions["edges"] else \
        {"nodes_added": [], "edges_added": [], "rejected": [], "already_present": []}
    write_report(batch, res, unclassified, skipped, basis, nodes)
    return res, unclassified


def write_report(batch, res, unclassified, skipped, basis, nodes):
    nearest = [(nodes[k]["label"], b) for k, b in basis.items() if "nearest fit" in (b or "")]
    L = ["---", 'hoot: "report"', f'generated: "{store.now_iso()[:10]}"', "tags:", '  - "hoot/report"', "---",
         "# Autopilot run", "",
         f"Batch `{batch['batch_id']}` · {len(batch['edges'])} SEC-backed connections considered.", "",
         f"- **Added:** {len(res['nodes_added'])} entities, {len(res['edges_added'])} connections",
         f"- **Updated by newer filings:** {len(res.get('superseded', []))} (old records kept, marked superseded)",
         f"- **Already in the graph, unchanged:** {len(res.get('already_present', []))}",
         f"- **Skipped, company not classifiable:** {skipped} connections ({len(unclassified)} companies, listed below)",
         f"- **Rejected by the validator:** {len(res['rejected'])}", "",
         "Every record added here is marked `review: automatic`, with the rule that classified it in `domain_basis`. "
         "Edit `PriorityNexus\\data\\sic_domains.json` to change a rule.", ""]
    if nearest:
        L += [f"## Classified by nearest fit ({len(nearest)}): worth a glance someday", ""] + \
             [f"- {n}: {b}" for n, b in sorted(nearest)[:80]] + [""]
    if unclassified:
        L += ["## Not classified (skipped, nothing guessed)", ""] + [f"- {n}: {b}" for n, b in sorted(unclassified)[:80]] + [""]
    if res["rejected"]:
        L += ["## Rejected rows", ""] + [f"- {r['row']}: {'; '.join(e['message'] for e in r['errors'])}" for r in res["rejected"][:50]]
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")


def main():
    res, uncl = run(centers=sys.argv[1:])
    print(f"autopilot: +{len(res['nodes_added'])} entities, +{len(res['edges_added'])} connections, "
          f"{len(res.get('superseded', []))} updated, {len(res.get('already_present', []))} unchanged, "
          f"{len(uncl)} companies unclassified, {len(res['rejected'])} rejected · report: {REPORT}")


if __name__ == "__main__":
    main()
