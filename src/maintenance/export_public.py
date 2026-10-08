"""Build the public copy of the project (for GitHub) in a separate folder. Copies ONLY code, public rule tables
and public docs; never data, reports, keys, confirmations or the vault. Then scans every copied file for personal
details and refuses to finish if any are found.

    python -I src/maintenance/export_public.py [destination]     (default: Documents\\PNMaster-Graph)
"""
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import paths  # noqa: E402

PUBLIC_DATA = ["governance.json", "sic_domains.json", "other_domains.json", "committee_domains.json", "regulations.json"]
PUBLIC_DOCS = ["concept-paper.md", "Publishing safely.md"]
TOP = ["CLAUDE.md", "run.cmd", "update_all.cmd", "check_docs.cmd", "auto_reports.cmd", ".gitignore"]
HOOT_SKIP = {"cache", "graph.db", "config.json", "people_sweep.json", "identity_fixes.json", "hoot.db", "__pycache__"}
# investigation-specific lines that stay private
PRIVATE_LINES = re.compile(r"McDonald|mcd_board|corporate names\.md|Connections\.xlsx|ENT_MCDONALDS", re.I)
# personal details that must never appear in the public copy: generic patterns here, personal words in a private
# file (data/private_terms.txt, one per line) that is never exported
LEAKS = [re.compile(p, re.I) for p in (r"[\w.+-]+@(gmail|yahoo|outlook|hotmail|icloud)\.com", r"[A-Z]:\\Users\\[^\\\s]+",
                                        r"api_key\"\s*:\s*\"[A-Za-z0-9]{12,}")]
PUBLIC_CONTACT = ["theaistherapist@gmail.com", "Josie Anderson"]   # chosen by the investigator to be public
_terms = os.path.join(ROOT, "data", "private_terms.txt")
if os.path.exists(_terms):
    LEAKS += [re.compile(re.escape(t.strip()), re.I) for t in open(_terms, encoding="utf-8") if t.strip()]


def copytree(src, dst, skip=()):
    for d, dirs, files in os.walk(src):
        dirs[:] = [x for x in dirs if x not in skip and x != "__pycache__"]
        for f in files:
            if f in skip or f.endswith(".pyc"):
                continue
            rel = os.path.relpath(os.path.join(d, f), src)
            os.makedirs(os.path.dirname(os.path.join(dst, rel)), exist_ok=True)
            shutil.copy2(os.path.join(d, f), os.path.join(dst, rel))


def main():
    dst = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), "Documents", "PNMaster-Graph")
    os.makedirs(dst, exist_ok=True)
    for x in os.listdir(dst):                       # rebuild everything except the git history
        if x == ".git":
            continue
        p = os.path.join(dst, x)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    copytree(os.path.join(ROOT, "src"), os.path.join(dst, "src"))
    copytree(os.path.join(ROOT, "tests"), os.path.join(dst, "tests"), skip={"fixture_graph.json"})
    copytree(paths.HOOT_DIR, os.path.join(dst, "hoot"), skip=HOOT_SKIP)
    os.makedirs(os.path.join(dst, "data"))
    for f in PUBLIC_DATA:
        shutil.copy2(os.path.join(ROOT, "data", f), os.path.join(dst, "data", f))
    os.makedirs(os.path.join(dst, "docs"))
    for f in PUBLIC_DOCS:
        shutil.copy2(os.path.join(ROOT, "docs", f), os.path.join(dst, "docs", f))
    for f in TOP:
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            text = open(p, encoding="utf-8-sig").read()
            if f.endswith(".cmd"):
                text = "\n".join(l for l in text.splitlines() if not PRIVATE_LINES.search(l)) + "\n"
            open(os.path.join(dst, f), "w", encoding="utf-8", newline="\n").write(text)
    shutil.copy2(os.path.join(ROOT, "docs", "PUBLIC_README.md"), os.path.join(dst, "README.md"))

    problems, count = [], 0
    for d, dirs, files in os.walk(dst):
        dirs[:] = [x for x in dirs if x != ".git"]
        for f in files:
            count += 1
            p = os.path.join(d, f)
            try:
                text = open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for allowed in PUBLIC_CONTACT:              # the investigator's chosen public contact is fine
                text = text.replace(allowed, "")
            for rx in LEAKS:
                m = rx.search(text)
                if m:
                    problems.append(f"{os.path.relpath(p, dst)}: {m.group(0)[:40]}")
    if problems:
        print("STOP: personal details found; fix these before uploading:")
        print("\n".join("  " + x for x in problems))
        return 1
    print(f"public copy ready: {dst} · {count} files · personal-details scan clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
