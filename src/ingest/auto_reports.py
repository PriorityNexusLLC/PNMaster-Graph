"""Auto-reports: rank every entity by how many balance flags it carries, highest first, then draft AND review a
report for the top ones. Runs in the background (auto_reports.cmd, or nightly from update_all.cmd) and keeps
everything in its own folder:

  Obsidian: HOOT/Reports/Auto reports/   (index note, each draft, its reviewed version and review notes)
  data/reports/                          (the same files, served by the app at /reports/...)

An entity is re-drafted only when its records or flags changed since the last run, so nightly runs are quick.
Flags counted (each tied to a published standard, see engine/gates.py):
  board independence · long tenure · over-boarding · single point of failure · top holder in a concentrated domain

    python -I src/ingest/auto_reports.py [--top 10] [--dry]
"""
import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import audit, balance, report, review, store  # noqa: E402
from engine.validator import is_superseded  # noqa: E402

STATE = os.path.join(report.REPORT_DIR, "auto_state.json")


def rank(graph):
    bal = balance.compute(graph)
    flags = defaultdict(Counter)
    for f in bal["independence"]["flags"]:
        flags[f["org"]]["board independence"] += 1
        for t in f["ties"]:
            flags[t["person"]]["board independence"] += 1
    for f in bal["tenure"]["flags"]:
        flags[f["person"]]["long tenure"] += 1
        flags[f["org"]]["long tenure"] += 1
    for f in bal["overboarding"]["flags"]:
        flags[f["person"]]["over-boarding"] += 1
    for f in bal["single_points"]["flags"]:
        flags[f["id"]]["single point"] += 1
    for d in bal["concentration"]["domains"]:
        if d["flag"]:
            for h in d["top_holders"][:3]:
                flags[h["id"]]["concentration"] += 1
    nodes = {n["id"]: n for n in graph["nodes"] if not (n.get("provenance") or {}).get("merged_into")}
    deg = Counter()
    for e in graph["edges"]:
        if not is_superseded(e):
            deg[e["source"]] += 1
            deg[e["target"]] += 1
    ranked = [(i, c) for i, c in flags.items() if i in nodes]
    ranked.sort(key=lambda x: (-sum(x[1].values()), -len(x[1]), -deg[x[0]], nodes[x[0]]["label"]))
    return ranked, nodes


def fingerprint(graph, eid, flags):
    rel = sorted((e["edge_id"], (e.get("integrity") or {}).get("verification_hash") or "", bool(is_superseded(e)))
                 for e in graph["edges"] if eid in (e["source"], e["target"]))
    return hashlib.sha256(json.dumps([rel, sorted(flags.items())]).encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description="Auto-reports, most flags first")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--dry", action="store_true", help="only show the ranking")
    ap.add_argument("--force", action="store_true", help="re-draft even if nothing changed (e.g. after a template update)")
    a = ap.parse_args()
    graph = store.load_graph()
    ranked, nodes = rank(graph)
    state = {}
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    print(f"auto-reports: {len(ranked)} entities carry at least one flag; reporting on the top {a.top}")
    rows = []
    for i, (eid, fl) in enumerate(ranked[:a.top], 1):
        fp = fingerprint(graph, eid, fl)
        prev = state.get(eid) or {}
        label = nodes[eid]["label"]
        print(f"  {i:2}. {sum(fl.values())} flag(s) · {label} ({', '.join(f'{k} {v}' for k, v in fl.items())})")
        if a.dry:
            continue
        if not a.force and prev.get("fingerprint") == fp and prev.get("report_id") and \
                (review.status(prev["report_id"]).get("state") == "done"):
            rid, note = prev["report_id"], "unchanged since last run"
        else:
            r = report.draft(graph, eid, folder="auto")
            rid, note = r["id"], "new draft"
            try:
                review.run(rid)
            except Exception as ex:                 # noqa: BLE001  keep going; the error is in the sidecar
                print(f"      review failed: {ex}")
        st = review.status(rid)
        state[eid] = {"fingerprint": fp, "report_id": rid, "flags": dict(fl)}
        rows.append((i, eid, label, fl, rid, st.get("verdict") or st.get("state"), note))
        print(f"      {note}: {rid} · {st.get('verdict') or st.get('state')}")
    if a.dry:
        return 0
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)

    vault_dir = os.path.join(os.path.dirname(audit.ANCHOR_PATH), "Auto reports")
    os.makedirs(vault_dir, exist_ok=True)
    L = ["---", "hoot: report", f"generated: {store.now_iso()[:10]}", "tags: [hoot/report]", "---",
         "# Auto reports index", "",
         "Entities ranked by how many balance flags they carry, most first. Each was drafted, then independently "
         "reviewed. **Nothing here is published.** Start with the top one, read its review notes, do the right of "
         "reply, and re-check. Flags are signals measured against published standards, not findings.", "",
         "| # | Entity | Flags | Verdict | Draft | Reviewed (v2) | Review notes |", "|---|---|---|---|---|---|---|"]
    for i, eid, label, fl, rid, verdict, note in rows:
        L.append(f"| {i} | {label} | {sum(fl.values())}: {', '.join(f'{k} {v}' for k, v in fl.items())} | {verdict} | "
                 f"[[{rid}]] | [[{rid} (reviewed)]] | [[{rid} (review notes)]] |")
    L += ["", "Flag meanings: **board independence** (NYSE/Nasdaq majority-independent rule) · **long tenure** (UK "
          "Corporate Governance Code, more than nine years) · **over-boarding** (ISS: more than five public boards, or a "
          "CEO on more than two outside boards) · **single point** (part of the map connects only through this entity) "
          "· **concentration** (top holder where blocks are highly concentrated, DOJ/FTC HHI over 1,800).",
          "", "Re-runs nightly with update_all; an entity is re-drafted only when its records or flags change."]
    with open(os.path.join(vault_dir, "Auto reports index.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    audit.log_event("auto_reports_run", ranked=len(ranked), reported=[r[4] for r in rows])
    print(f"index: {os.path.join(vault_dir, 'Auto reports index.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
