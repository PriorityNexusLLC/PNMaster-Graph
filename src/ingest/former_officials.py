"""Former officials who now lobby for organizations on the map: the full list, from the lobbyists' own statutory
disclosures ("covered positions", Lobbying Disclosure Act). Read-only: writes a report, changes no records.

For every lobbyist on the map who disclosed a former government job, each disclosed position is sorted into
Senate · House · Executive branch · Military · Other, the named lawmaker or office is looked up on the map, and the
report shows which former employers are ALREADY on the map (the link is visible in paths) and which are MISSING
(a gap to fill: e.g. a former Representative, or an agency with no node yet).

    python -I src/ingest/former_officials.py
"""
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import paths, store  # noqa: E402
from engine.validator import is_superseded  # noqa: E402
sys.path.insert(0, paths.HOOT_DIR)
from hootlib.util import person_name_match  # noqa: E402  (nicknames: Chuck = Charles)

NOT_NAMES = {"cmte", "committee", "comm", "subcommittee", "office", "staff", "leadership", "majority", "minority"}

REPORT = os.path.join(paths.REPORTS, "Former officials lobbying list.md")
EXEC = re.compile(r"white house|department|dept\.?|agency|administration|office of|commission|bureau|treasury|pentagon|"
                  r"\bdod\b|\bomb\b|\bnsc\b|\bsec\b|\bfda\b|\bcms\b|\bhhs\b|\bepa\b|\bfcc\b|\bftc\b|\bferc\b|federal reserve|"
                  r"\busda\b|\bdoe\b|\bdoj\b|state department|national security|ambassador|secretary|nih\b|nasa|\busaid\b|"
                  r"\bustr\b|trade representative|council of economic|vice president|president\b", re.I)
MIL = re.compile(r"\b(army|navy|air force|marine|usmc|coast guard|space force|national guard|joint chiefs)\b", re.I)
SEN = re.compile(r"\bsen\.|\bsenator\b|\bsenate\b", re.I)
HOU = re.compile(r"\brep\.|\brepresentative\b|\bcongress(man|woman)\b|\bhouse\b|member of congress", re.I)
NAME = re.compile(r"\b(?:Sen\.|Senator|Rep\.|Representative|Congressman|Congresswoman)\s+((?:[A-Z][\w.'-]*\s+){0,3}[A-Z][\w'-]+)")


def main():
    g = store.load_graph()
    nodes = {n["id"]: n for n in g["nodes"] if not (n.get("provenance") or {}).get("merged_into")}
    edges = [e for e in g["edges"] if not is_superseded(e) and e["source"] in nodes and e["target"] in nodes]
    lawmakers = defaultdict(list)                    # surname -> nodes of sitting/former senators and representatives
    names_of = lambda n: [n["label"]] + ((n.get("provenance") or {}).get("aliases_seen_in_sources") or [])
    for e in edges:
        if e["relationship_type"] in ("SENATOR_IN", "REPRESENTATIVE_IN"):
            for nm in names_of(nodes[e["source"]]):
                lawmakers[re.sub(r",?\s+(Jr\.?|Sr\.?|II|III|IV)$", "", nm).split()[-1].lower()].append(e["source"])
    committees = [n for n in nodes.values() if n.get("entity_type") == "LEGISLATIVE_COMMITTEE"]
    gov_nodes = [n for n in nodes.values() if n.get("entity_type") not in ("PERSON", "CORPORATION", "ORGANIZATION",
                                                                            "LOBBYING_REGISTRANT", "PRIVATE_COMPANY", "NONPROFIT",
                                                                            "LEGISLATIVE_COMMITTEE", "FOREIGN_AGENT_REGISTRANT")]
    rows, gaps, cat_count = [], Counter(), Counter()
    seen = set()
    for e in edges:
        if e["relationship_type"] != "LOBBYIST_AT":
            continue
        m = re.match(r"lobbies for (.+?); former position \(disclosed\): (.+)$", (e.get("transparency") or {}).get("role_title") or "")
        if not m:
            continue
        client, cp = m.group(1), m.group(2)
        key = (e["source"], client)
        if key in seen:
            continue
        seen.add(key)
        for seg in [s.strip() for s in re.split(r";|\s\|\s", cp) if s.strip()]:
            cat = "Senate" if SEN.search(seg) else "House" if HOU.search(seg) else "Military" if MIL.search(seg) \
                else "Executive branch" if EXEC.search(seg) else "Other"
            cat_count[cat] += 1
            found, missing = [], []
            for nm in NAME.finditer(seg):
                parts = [x for x in nm.group(1).split() if x.strip(".,") not in ("Jr", "Sr", "II", "III", "IV")]
                if not parts or parts[-1].lower().strip(".") in NOT_NAMES:
                    continue
                hits = list(dict.fromkeys(p for p in lawmakers.get(parts[-1].lower(), [])
                                          if len(parts) == 1 or any(person_name_match(" ".join(parts), x) for x in names_of(nodes[p]))))
                if hits:
                    found += [nodes[p]["label"] for p in hits]
                else:
                    missing.append(nm.group(0))
            low = seg.lower()
            best = None                              # committees: the one named FIRST, in the right chamber
            for n in committees:
                chamber = "senate" if n["label"].startswith("Senate") else "house"
                if chamber not in low:
                    continue
                w0 = next((w for w in re.findall(r"[a-z]+", n["label"].lower()) if len(w) > 3 and w not in
                           ("senate", "house", "committee", "special", "select", "permanent")), None)
                i = low.find(w0) if w0 else -1
                if i >= 0 and (best is None or i < best[0]):
                    best = (i, n["label"])
            if best:
                found.append(best[1])
            for n in gov_nodes:
                words = [w for w in re.findall(r"[a-z]+", n["label"].lower()) if len(w) > 3 and w not in
                         ("united", "states", "committee", "senate", "house", "office", "representatives")]
                if words and all(w in low for w in words[:3]):
                    found.append(n["label"])
            if cat in ("Executive branch", "Military") and not found:
                missing.append(seg[:80])
            for x in missing:
                gaps[(cat, x)] += 1
            rows.append((client, nodes[e["source"]]["label"], nodes[e["target"]]["label"], cat, seg[:180],
                         ", ".join(dict.fromkeys(found)) or "", ", ".join(missing), (e.get("transparency") or {}).get("citation_url")))
    rows.sort(key=lambda r: (r[0], r[3], r[1]))
    L = ["---", "hoot: report", f"generated: {date.today().isoformat()}", "tags: [hoot/report]", "---",
         "# Former officials lobbying list", "",
         "Every lobbyist working for an organization on your map who disclosed a former government job, as required by "
         "the Lobbying Disclosure Act (their \"covered positions\"), taken from the filings themselves. Disclosing a "
         "former position is a legal requirement and not wrongdoing; the point is to see where government and industry "
         "connect.", "",
         f"**{len(seen)} lobbyist–client pairs · {sum(cat_count.values())} disclosed former positions:** "
         + " · ".join(f"{k} {v}" for k, v in cat_count.most_common()), "",
         "**On the map** = the former employer already has a node, so the connection appears in paths and pattern signals. "
         "**Missing** = a gap: the former employer isn't on the map yet (most often a former Representative or Senator "
         "no longer in office, or an agency without a node).", "",
         "## Biggest gaps (former employers not on the map yet)", "", "| Where | Former employer as disclosed | Lobbyists |", "|---|---|---|"]
    L += [f"| {c} | {x} | {k} |" for (c, x), k in gaps.most_common(40)]
    L += ["", "## Full list by client", ""]
    cur = None
    for client, who, firm, cat, seg, found, missing, url in rows:
        if client != cur:
            L += ["", f"### {client}", "", "| Lobbyist | Firm | Where | Former position (disclosed) | On the map | Missing | Filing |",
                  "|---|---|---|---|---|---|---|"]
            cur = client
        L.append(f"| {who} | {firm} | {cat} | {seg.replace('|', '/')} | {found} | {missing} | [filing]({url}) |")
    with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    print(f"former officials: {len(seen)} lobbyist-client pairs · " + " · ".join(f"{k} {v}" for k, v in cat_count.most_common())
          + f" · {len(gaps)} distinct missing former employers -> {REPORT}")


if __name__ == "__main__":
    main()
