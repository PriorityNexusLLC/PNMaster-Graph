"""Helper Inbox: plain board lists typed by a person, turned into checks the system runs on its own.

Drop a Markdown file in Obsidian:  HOOT/Inbox/  (files starting with "_" are ignored, e.g. the template).
Format (forgiving; see HOOT/Inbox/_TEMPLATE - copy me.md):

    # Company: Lockheed Martin
    Source: https://www.lockheedmartin.com/.../board-of-directors.html
    - Jim Taiclet — Chairman, President and CEO
    - David B. Burritt — Director

    # Person: Kelly Ayotte
    Source: https://...
    - Caterpillar — Director
    - Blackstone — Director (former)

For each new or changed file:
  1. every pairing becomes a CLAIM (data/staging/claims_inbox_<file>.json); nothing is treated as fact
  2. every person is looked up in official records (HOOT people sweep, two facts required)
  3. every "# Company:" is added to the HOOT Watchlist and pulled from SEC filings
  4. a check report is written: HOOT/Reports/Claims check - Inbox - <file>.md
Changes are detected by fingerprint, so re-saving an unchanged file does nothing.

    python -I src/ingest/inbox.py
"""
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import audit, paths  # noqa: E402

INBOX = os.path.join(paths.HOOT_NOTES, "Inbox")
DONE = os.path.join(ROOT, "data", "staging", "inbox_done.json")
WATCH = os.path.join(paths.HOOT_NOTES, "Watchlist.md")
HOOT = os.path.join(paths.HOOT_DIR, "hoot.py")
HEAD = re.compile(r"^\s*#{1,3}\s*(company|organization|org|person|people)\s*:\s*(.+?)\s*$", re.I)
SRC = re.compile(r"^\s*(?:source|sources|link)\s*:\s*(\S+)", re.I)
ITEM = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
SPLIT = re.compile(r"\s+[—–-]\s+|\s*:\s+|\s*\|\s*")


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:50] or "file"


def parse(text):
    """-> list of (person, organization, role, source)"""
    pairs, kind, subject, source = [], None, None, ""
    for line in text.splitlines():
        h = HEAD.match(line)
        if h:
            kind = "company" if h.group(1).lower() in ("company", "organization", "org") else "person"
            subject, source = h.group(2).strip(), ""
            continue
        s = SRC.match(line)
        if s:
            source = s.group(1)
            continue
        it = ITEM.match(line)
        if it and subject and not it.group(1).lower().startswith(("source", "http")):
            parts = SPLIT.split(it.group(1), maxsplit=1)
            name, role = parts[0].strip(" *"), (parts[1].strip() if len(parts) > 1 else "")
            if not name:
                continue
            if kind == "company":
                pairs.append((name, subject, role, source))
            else:
                pairs.append((subject, name, role, source))
    return pairs


def hoot(*args):
    r = subprocess.run([sys.executable, "-I", HOOT, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (r.stdout or "") + (r.stderr or "")


def reports():
    """Step 2 (after the autopilot has added what was found): one check report per Inbox file."""
    for f in sorted(os.listdir(INBOX)) if os.path.isdir(INBOX) else []:
        if f.endswith(".md") and not f.startswith("_"):
            cpath = os.path.join(ROOT, "data", "staging", f"claims_inbox_{slug(f[:-3])}.json")
            if os.path.exists(cpath):
                subprocess.run([sys.executable, "-I", os.path.join(ROOT, "src", "ingest", "claims_check.py"), cpath,
                                "--title", f"Inbox - {f[:-3]}"])
    return 0


def main():
    if "--report" in sys.argv:
        return reports()
    os.makedirs(INBOX, exist_ok=True)
    done = json.load(open(DONE, encoding="utf-8")) if os.path.exists(DONE) else {}
    files = [f for f in sorted(os.listdir(INBOX)) if f.endswith(".md") and not f.startswith("_")]
    todo = []
    for f in files:
        text = open(os.path.join(INBOX, f), encoding="utf-8").read()
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if done.get(f) != h:
            todo.append((f, text, h))
    print(f"inbox: {len(files)} file(s) · {len(todo)} new or changed")
    for f, text, h in todo:
        pairs = parse(text)
        if not pairs:
            print(f"  {f}: no lists found (see the template for the format)")
            done[f] = h
            continue
        rows = {}
        for person, org, role, source in pairs:
            rows.setdefault(person, []).append({"org_claimed": org, "role": role, "source": source})
        claims = {"source": f"Helper Inbox: {f} (typed by a helper; every line is a claim to check)",
                  "rows": [{"person_claimed": p, "claims": cs} for p, cs in rows.items()]}
        cpath = os.path.join(ROOT, "data", "staging", f"claims_inbox_{slug(f[:-3])}.json")
        with open(cpath, "w", encoding="utf-8") as fh:
            json.dump(claims, fh, indent=1, ensure_ascii=False)
        companies = sorted({org for _, org, _, _ in pairs if re.search(rf"#+\s*(company|organization|org)\s*:\s*{re.escape(org)}\s*$",
                                                                       text, re.I | re.M)})
        watch = open(WATCH, encoding="utf-8").read() if os.path.exists(WATCH) else ""
        new = [c for c in companies if f"- sec: {c}".lower() not in watch.lower()]
        if new:
            if "## From the helper Inbox" not in watch:
                watch = watch.rstrip() + "\n\n## From the helper Inbox\n"
            watch = watch.rstrip() + "\n" + "".join(f"- sec: {c}\n" for c in new)
            with open(WATCH, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(watch)
        print(f"  {f}: {len(pairs)} pairing(s), {len(rows)} people, {len(companies)} companies ({len(new)} new to the Watchlist)")
        for c in new:                                  # pull the company's SEC filings now (public companies)
            out = hoot("sec", c, "--no-expand")
            print(f"    SEC {c}: {'pulled' if 'Traceback' not in out and '!' not in out[:200] else 'not found as a public company (fine: private or foreign)'}")
        names = ";".join(rows)
        print("    people sweep: " + (hoot("people", "--only", names).strip().splitlines() or ["done"])[-1][:120])
        done[f] = h
        audit.log_event("inbox_processed", file=f, sha256=h, pairings=len(pairs), people=len(rows), companies=companies)
    with open(DONE, "w", encoding="utf-8") as fh:
        json.dump(done, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
