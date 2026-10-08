"""Re-run the independent review (second pass) on reports.

    python -I src/ingest/review_reports.py            # every report you edited since its last review, or never reviewed
    python -I src/ingest/review_reports.py PN-...     # one report
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import review  # noqa: E402


def main(argv):
    ids = argv or review.stale_reviews()
    print(f"review: {len(ids)} report(s) to check")
    for fid in ids:
        try:
            r = review.run(fid)
            print(f"  {fid}: {r['verdict']} · sources {r['sources_ok']}/{r['sources']}")
        except Exception as ex:                     # noqa: BLE001
            print(f"  {fid}: review failed ({ex})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
