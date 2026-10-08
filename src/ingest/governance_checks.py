"""Nightly governance checks (run by update_all.cmd after every write of the day):

1. Re-check every fingerprint in the hash-chained audit log, and that the graph file matches its last recorded save.
2. Anchor today's fingerprint outside the data folder (Obsidian: HOOT/Reports/Audit anchors).
3. Balance check: independence, concentration, single points of failure, tenure (HOOT/Reports/Balance check).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import audit, balance, store  # noqa: E402


def main():
    v = audit.verify()
    print(f"audit chain: {'intact' if v['ok'] else 'ALTERED (' + str(v['break_count']) + ' problems; see the Integrity tab)'}"
          f" · {v['entries']} entries · graph file: {v['graph']['message']}")
    for b in v["breaks"][:10]:
        print(f"  line {b['line']}: {b['problem']}")
    a = audit.anchor()
    print(f"anchored fingerprint {a['tip'][:16]}… (line {a['line']}) -> {a['anchor_file']}")

    res = balance.compute(store.load_graph())
    vault = os.path.dirname(audit.ANCHOR_PATH)
    path = os.path.join(vault, "Balance check.md")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(balance.report_markdown(res))
    print(f"balance: {len(res['independence']['flags'])} boards flagged · "
          f"{sum(d['flag'] for d in res['concentration']['domains'])} concentrated domains · "
          f"{res['single_points'].get('count', 0)} single points · {len(res['tenure']['flags'])} long tenures -> {path}")
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
