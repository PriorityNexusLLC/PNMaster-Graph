"""Rulebook: verify every official link in data/regulations.json and write the Obsidian note
HOOT/Rulebook (laws by domain). Each link is opened; the page must show the cited section (the rule's "check"
text). Results are stamped with the date, so the rulebook says which citations were checked and when.

    python -I src/maintenance/rulebook.py
"""
import json
import os
import sys
import urllib.request
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import audit, paths  # noqa: E402

RULES = os.path.join(ROOT, "data", "regulations.json")
CHECKED = os.path.join(ROOT, "data", "regulations_check.json")
NOTE = os.path.join(paths.HOOT_NOTES, "Rulebook (laws by domain).md")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PriorityNexus-rulebook-check/1.0", "Accept": "text/html,*/*"}


def verify(rule):
    try:
        with urllib.request.urlopen(urllib.request.Request(rule["url"], headers=UA), timeout=40) as r:
            text = r.read(3_000_000).decode("utf-8", "replace")
        if rule.get("check") and rule["check"].lower() not in text.lower():
            return "opened; cited section not found on the page"
        return "verified"
    except Exception as ex:                              # noqa: BLE001
        return f"could not open ({str(ex)[:60]})"


def main():
    data = json.load(open(RULES, encoding="utf-8"))
    today = date.today().isoformat()
    results = {}
    for r in data["rules"]:
        results[r["id"]] = verify(r)
        print(f"  {results[r['id']][:20]:20} {r['id']}")
    with open(CHECKED, "w", encoding="utf-8") as f:
        json.dump({"checked": today, "results": results}, f, indent=1)
    ok = sum(1 for v in results.values() if v == "verified")
    L = ["---", "hoot: guide", f"generated: {today}", "tags: [hoot/system]", "---",
         "# Rulebook: laws, regulations and standards by domain", "",
         "What each domain's organizations are expected to follow, in plain words, with **what meeting it well looks "
         "like**. The purpose is to help organizations improve, not only to flag them. Each rule links to its official "
         f"text; {ok} of {len(results)} links were opened and confirmed to show the cited section on {today} "
         "(others: see the note next to them). General information, not legal advice: the official text governs.", "",
         "Columns: **Requires** (what the rule asks) · **Doing it well** (practical steps that go beyond the minimum) · "
         "**Our checks** (which Priority Nexus checks relate).", ""]
    coi = data.get("conflicts_of_interest")
    if coi:
        L += ["## Conflicts of interest", "", f"**What it is:** {coi['definition']}", "", f"**Key point:** {coi['principle']}", "",
              "**Three degrees:** " + " · ".join(f"*{k}*: {v}" for k, v in coi["degrees"].items()), "",
              "**Standard fixes:** " + " → ".join(coi["fixes"]), "",
              "| Type | Example | Visible in official records? |", "|---|---|---|"]
        L += [f"| {t['type']} | {t['example']} | {t['visible']} |" for t in coi["types"]]
        L += ["", "**Where each field defines it:**", ""]
        for r in [r for r in data["rules"] if r.get("coi")]:
            st = results.get(r["id"], "")
            L.append(f"- **{r['name']}** ({r['citation']}): covers *{r['coi']}*. {r['requires']} "
                     f"[official text]({r['url']}){'' if st == 'verified' else f' ({st})'}")
        L.append("")
    order = ["ALL", "DOM_A", "DOM_B", "DOM_C", "DOM_D", "DOM_E", "DOM_F", "DOM_G", "INTL"]
    for d in order:
        rules = [r for r in data["rules"] if d in r["domains"] and (d == "ALL" or "ALL" not in r["domains"] or d == "INTL")]
        if d == "INTL":
            rules = [r for r in data["rules"] if "INTL" in r["domains"]]
        if not rules:
            continue
        L += [f"## {data['domains'][d]}", ""]
        for r in rules:
            status = results.get(r["id"], "")
            L += [f"### {r['name']}",
                  f"*{r['citation']}* · enforced by {r['regulator']} · {r['type']} · "
                  f"[official text]({r['url']}){'' if status == 'verified' else f' ({status})'}", "",
                  f"- **Requires:** {r['requires']}", f"- **Doing it well:** {r['helps']}"]
            if r.get("our_checks"):
                L.append(f"- **Our checks:** {', '.join(r['our_checks'])}")
            L.append("")
    L += ["---", "Data file for future tools: `PriorityNexus/data/regulations.json` (one entry per rule: requirements, "
          "good practice, related checks, official link)."]
    with open(NOTE, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    audit.log_event("rulebook_checked", rules=len(results), verified=ok)
    print(f"rulebook: {ok}/{len(results)} official links verified -> {NOTE}")


if __name__ == "__main__":
    main()
