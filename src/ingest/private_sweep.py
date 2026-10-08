"""Look up every organization your documents name that SEC insider filings don't cover.

Order:  1. public company?  (exact SEC name)            -> added to the HOOT Watchlist
        2. private company? (SEC Form D, exact name)     -> ingested
        3. nonprofit?       (IRS Form 990, exact name)   -> ingested
        4. several organizations share the name?         -> each candidate's filing is opened and the one whose
           board lists people your documents name for it is chosen (two facts agree). Otherwise: left for you.
Nothing is guessed. Report: Obsidian HOOT/Reports/Private & nonprofit sweep."""
import glob
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import paths  # noqa: E402
VAULT = paths.VAULT
sys.path.insert(0, os.path.join(VAULT, ".hoot"))
from hootlib.sources import private  # noqa: E402
from hootlib.util import Http, load_config, person_name_match  # noqa: E402

HOOT = os.path.join(VAULT, ".hoot", "hoot.py")
WATCH = os.path.join(VAULT, "HOOT", "Watchlist.md")
REPORT = os.path.join(VAULT, "HOOT", "Reports", "Private & nonprofit sweep.md")
DONE = os.path.join(ROOT, "data", "staging", "private_sweep_done.json")
FOREIGN = re.compile(r"saudi|aramco|bmw|mclaren|piramal|children's investment|ciff|airbus|gsk|nestl|unilever|compass group|diageo|"
                     r"kimberly-clark de mexico|indra|sepi|sogepa|rolls-royce|carrefour|ahold", re.I)
bare = private._bare
# corporate renames, each verifiable in EDGAR (the old filer's history ends where the successor's begins)
RENAMED = {"Google": "Google Inc. (CIK 1288776) reorganized into Alphabet Inc. (CIK 1652044) in 2015; covered by Alphabet on the Watchlist"}


def claims():
    """{org name as written: set(person names your documents put on it)}"""
    out = {}
    for f in glob.glob(os.path.join(ROOT, "data", "staging", "boards_*.json")):
        for t in json.load(open(f, encoding="utf-8")).get("tables", []):
            out.setdefault(t["company_claimed"].strip(), set()).update(r["person"] for r in t["rows"])
    for f in glob.glob(os.path.join(ROOT, "data", "staging", "claims_*.json")):
        for row in json.load(open(f, encoding="utf-8")).get("rows", []):
            for c in row.get("claims", []):
                out.setdefault(c["org_claimed"].strip(), set()).add(row["person_claimed"])
    return out


def targets():
    names = set()
    for f in glob.glob(os.path.join(ROOT, "data", "staging", "boards_*.json")):
        for t in json.load(open(f, encoding="utf-8")).get("tables", []):
            if t.get("company_status") == "NO_SEC_COVERAGE":
                names.add(t["company_claimed"].strip())
    for f in glob.glob(os.path.join(ROOT, "data", "staging", "claims_*.json")):
        for row in json.load(open(f, encoding="utf-8")).get("rows", []):
            for c in row.get("claims", []):
                if c["status"] == "NO_SEC_COVERAGE":
                    names.add(c["org_claimed"].strip())
    return sorted(n for n in names if len(n) > 2)


def hoot(*args):
    p = subprocess.run(["python", "-I", HOOT, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout + p.stderr).strip()


def summary(out):
    line = next((l for l in out.splitlines() if l.startswith(("Form D:", "Form 990:"))), "")
    ppl = next((l.strip() for l in out.splitlines() if "people:" in l or "linked" in l), "")
    return f"{line} {ppl}".strip()


def add_to_watchlist(cik, name):
    text = open(WATCH, encoding="utf-8").read()
    if re.search(rf"^- sec: {int(cik)}\s*$", text, re.M):
        return
    marker = "## Public companies found by the private/nonprofit sweep"
    if marker not in text:
        text = text.replace("## Source formats", f"{marker}\n\n## Source formats")
    text = text.replace(f"{marker}\n", f"{marker}\n\n- sec: {int(cik)}", 1)
    open(WATCH, "w", encoding="utf-8", newline="\n").write(text)


def choose(http, name, claimed):
    """Open each same-named candidate's filing; keep the one listing people your documents name."""
    scored = []
    key = bare(name)

    def safe(fn):
        try:
            return fn(http, name)
        except Exception:                                    # noqa: BLE001  (404 = no candidates)
            return {}
    for kind, cands, peek in (("formd", safe(private.formd_candidates), private.peek_formd_people),
                              ("nonprofit", safe(private.nonprofit_candidates), private.peek_990_people)):
        exact = [(cid, label) for cid, label in cands.items() if bare(label) == key]   # exactly this name, nothing more
        if kind == "nonprofit" and len(exact) > 10:
            # many same-named chapters: check the largest first (headquarters dwarf local chapters), by IRS revenue
            def revenue(cid):
                try:
                    return http.json(f"{private.PP}/organizations/{int(cid)}.json", ttl=30 * 86400)["organization"].get("revenue_amount") or 0
                except Exception:                            # noqa: BLE001
                    return 0
            exact.sort(key=lambda x: -revenue(x[0]))
        for cid, label in exact[:10]:
            try:
                _, people = peek(http, cid)
            except Exception:                                # noqa: BLE001
                continue
            hits = sorted({p for p in people for c in claimed if person_name_match(p, c)})
            scored.append((len(hits), kind, cid, label, hits))
    scored.sort(key=lambda x: -x[0])
    if scored and scored[0][0] >= 1 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0], scored
    return None, scored


def main():
    http = Http(load_config())
    tick = http.json("https://www.sec.gov/files/company_tickers.json", ttl=30 * 86400)
    public = {}
    for r in tick.values():
        public.setdefault(bare(r["title"]), (str(r["cik_str"]), r["title"]))
    done = json.load(open(DONE, encoding="utf-8")) if os.path.exists(DONE) else {}
    claimed = claims()
    results = []
    for name in targets():
        if name in done and done[name]["status"] in ("found", "public", "foreign", "chosen"):
            results.append((name, done[name]))
            continue
        if name in RENAMED:
            r = {"status": "public", "detail": RENAMED[name]}
        elif FOREIGN.search(name):
            r = {"status": "foreign", "detail": "outside US filings: see the Foreign registries report"}
        elif bare(name) in public:
            cik, title = public[bare(name)]
            add_to_watchlist(cik, title)
            r = {"status": "public", "detail": f"public company {title} (CIK {cik}): added to the Watchlist for SEC insider filings"}
        else:
            r = None
            for kind in ("formd", "nonprofit"):
                code, out = hoot(kind, name)
                if code == 0 and ("Form D:" in out or "Form 990:" in out):
                    r = {"status": "found", "source": kind, "detail": summary(out)}
                    break
            if r is None:
                best, scored = choose(http, name, claimed.get(name, set()))
                if best:
                    n, kind, cid, label, hits = best
                    code, out = hoot(kind, cid)
                    r = {"status": "chosen", "source": kind, "detail": f"{label}: chosen because its filing lists "
                         f"{', '.join(hits)}, whom your documents place there · {summary(out)}"}
                elif scored:
                    r = {"status": "ambiguous", "detail": "; ".join(
                        f"{'CIK' if k == 'formd' else 'EIN'} {c} {lbl} ({n} of your people)" for n, k, c, lbl, _ in scored[:5])}
                else:
                    r = {"status": "not_found", "detail": "no public listing, Form D or e-filed 990 under this name"}
        done[name] = r
        results.append((name, r))
        print(f"  {r['status']:<10} {name}")
    json.dump(done, open(DONE, "w", encoding="utf-8"), indent=1)

    L = ["---", 'hoot: "report"', "tags:", '  - "hoot/report"', "---", "# Private & nonprofit sweep", "",
         "Organizations your documents name that SEC insider filings don't cover. People found are linked to known "
         "people only when a second fact agrees; otherwise they stay separate records.", ""]
    for st, title in (("found", "Found by exact name"), ("chosen", "Several shared the name: chosen by matching your people"),
                      ("public", "Actually public companies (added to Watchlist)"),
                      ("ambiguous", "Still undecided: none of the candidates' filings lists your people"),
                      ("not_found", "Not found (other legal name, never filed, or not US)"), ("foreign", "Foreign")):
        rows = [(n, r) for n, r in results if r["status"] == st]
        if rows:
            L += [f"## {title} ({len(rows)})", ""] + [f"- **{n}**: {r['detail']}".replace("\n", " · ") for n, r in rows] + [""]
    with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    c = {s: sum(1 for _, r in results if r["status"] == s) for s in ("found", "chosen", "public", "ambiguous", "not_found", "foreign")}
    print("private sweep: " + " · ".join(f"{k} {v}" for k, v in c.items()) + f" · report: {REPORT}")


if __name__ == "__main__":
    main()
