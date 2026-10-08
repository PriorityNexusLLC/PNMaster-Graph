"""Check a research document's affiliation claims against primary-source evidence in HOOT.

Input: a Markdown/plain-text export of a document whose tables look like
    | Director | <Org> Role | Current External Affiliations | Past Experience |
with bullet items "Org (role)". Every claim gets a status; none is written to the master graph.

    CONFIRMED      an SEC filing shows this person in this role at this organization (cited)
    CONFLICT       filings show the role but contradict the claim's timing (e.g. claim says current,
                   newest filing is older than the activity window or reports it ended)
    NO_SEC_COVERAGE  person found in filings, but this organization doesn't appear in their filings.
                   Common for private companies, nonprofits, universities and foreign issuers:
                   it is NOT evidence the claim is false; it needs a DEF 14A, Form 990 or registry.
    PERSON_NOT_FOUND  no insider at the center organization matches this name
Also lists roles found in filings that the document does not mention.

Person lookup is by name only *among insiders who filed at the center organization*, and every
match is shown with its SEC CIK so a human can confirm it. Names never become identifiers.
"""
import argparse
import json
import os
import re
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "ingest"))

from hoot_bridge import (ACTIVE_WINDOW_DAYS, DEFAULT_HOOT_DB, hoot_entities, open_hoot,  # noqa: E402
                         source_type)

SUFFIX = r"\b(incorporated|inc|corporation|corp|company|co|ltd|limited|llc|lp|plc|sa|ag|nv|the)\b"
NICK = {"chris": "christopher", "mike": "michael", "jim": "james", "bob": "robert", "bill": "william",
        "tom": "thomas", "joe": "joseph", "dan": "daniel", "dave": "david", "kathy": "kathleen", "margo": "margaret",
        "liz": "elizabeth", "beth": "elizabeth", "tony": "anthony", "rick": "richard", "steve": "steven", "ed": "edward"}


def org_key(s):
    s = re.sub(r"\s*/[a-z]{2,3}/?\s*$", "", (s or "").lower().strip())       # EDGAR state tag: /MD/, /DE/
    s = re.sub(r"\(.*?\)", " ", s).replace("&", " and ").replace("'", "").replace("’", "")
    s = re.sub(r"\bholdings?\b\s*$", " ", re.sub(SUFFIX, " ", s).strip())     # trailing "Holding(s)" wrapper only
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(SUFFIX, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def org_match(a, b):
    ka, kb = org_key(a), org_key(b)
    if not ka or not kb:
        return False
    # exact after removing legal-form words only: "Kimberly-Clark de Mexico" must never match
    # "Kimberly-Clark Corp" (different legal entities). A missed match costs a manual check;
    # a false match would fabricate evidence.
    return ka == kb


def name_parts(n):
    n = re.sub(r"\((.*?)\)", r" \1 ", n or "")           # keep nicknames as extra tokens
    toks = [t.strip(".,").lower() for t in n.split() if t.strip(".,")]
    toks = [t for t in toks if t not in ("jr", "sr", "ii", "iii", "iv", "dr", "sir", "dame")]
    return toks


def person_match(claim_name, sec_name):
    c, s = name_parts(claim_name), name_parts(sec_name)
    if not c or not s or c[-1] != s[-1]:                  # surname must match exactly
        return False
    firsts_c = {NICK.get(t, t) for t in c[:-1] if len(t) > 1}
    firsts_s = {NICK.get(t, t) for t in s[:-1] if len(t) > 1}
    return bool(firsts_c & firsts_s) or any(a[:3] == b[:3] for a in firsts_c for b in firsts_s)


def parse_items(cell):
    items = [x.strip() for x in re.split(r"\\\*|(?:^|\s)\*\s", cell or "") if x.strip()]
    out = []
    for it in items:
        m = re.match(r"^(.*?)\s*\((.*)\)\s*$", it)
        out.append({"org": (m.group(1) if m else it).strip(), "role": (m.group(2) if m else "").strip()})
    return out


def parse_tables(text):
    """Rows of 4-column pipe tables: person | center role | current affiliations | past."""
    claims = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or set("".join(cells)) <= set(":- ") or cells[0].lower() in ("director", "name", "board member"):
            continue
        person = re.sub(r"[*_]", "", cells[0]).strip()
        if not person or len(person.split()) < 2:
            continue
        claims.append({"person": person, "center_role": cells[1],
                       "current": parse_items(cells[2]), "past": parse_items(cells[3]) if len(cells) > 3 else []})
    return claims


def check(doc_path, center_cik, hoot_db=DEFAULT_HOOT_DB):
    with open(doc_path, encoding="utf-8") as f:
        text = f.read()
    rows = parse_tables(text)
    db = open_hoot(hoot_db)
    ents = hoot_entities(db)
    edges = [dict(r) for r in db.execute("SELECT * FROM edges")]
    center = next((i for i, e in ents.items() if e["ids"].get("sec_cik") == center_cik.zfill(10)), None)
    if center is None:
        raise SystemExit(f"center CIK {center_cik} not in HOOT; run `hoot sec {center_cik}` first")
    insiders = {e["src"] for e in edges if e["dst"] == center and ents[e["src"]]["type"] == "person"
                and source_type(e["source"])}
    today = date.today()
    results, matched_persons = [], set()

    for row in rows:
        cands = [p for p in insiders if person_match(row["person"], ents[p]["name"])]
        base = {"person_claimed": row["person"], "center_role_claimed": row["center_role"]}
        if len(cands) != 1:
            results.append({**base, "status": "PERSON_NOT_FOUND" if not cands else "AMBIGUOUS_PERSON",
                            "candidates": [{"name": ents[p]["name"], "sec_cik": ents[p]["ids"].get("sec_cik")} for p in cands]})
            continue
        pid = cands[0]
        matched_persons.add(pid)
        pe = ents[pid]
        filed = [e for e in edges if e["src"] == pid and source_type(e["source"])]
        person = {"name_in_filings": pe["name"], "sec_cik": pe["ids"].get("sec_cik")}
        claims_out, claimed_orgs = [], []
        for when, items in (("current", row["current"]), ("past", row["past"])):
            for it in items:
                hits = [e for e in filed if org_match(it["org"], ents[e["dst"]]["name"])]
                claimed_orgs += [h["dst"] for h in hits]
                c = {"org_claimed": it["org"], "role_claimed": it["role"], "claimed_as": when}
                if not hits:
                    c["status"] = "NO_SEC_COVERAGE"
                    c["note"] = "not in this person's SEC insider filings checked; needs DEF 14A / Form 990 / registry"
                else:
                    newest = max(hits, key=lambda e: e["last_seen"] or "")
                    last = newest["last_seen"] or newest["filing_date"]
                    stale = last and (today - date.fromisoformat(last)).days > ACTIVE_WINDOW_DAYS
                    ended = newest["is_current"] == 0
                    if when == "current" and (stale or ended):
                        c["status"] = "CONFLICT"
                        c["note"] = (f"claimed current, but the newest insider filing for this role is {last}"
                                     + (" and reports it ended" if ended else "")
                                     + "; confirm in the company's latest DEF 14A before treating either as settled")
                    else:
                        c["status"] = "CONFIRMED"
                    c["evidence"] = [{"org_in_filings": ents[h["dst"]]["name"], "org_sec_cik": ents[h["dst"]]["ids"].get("sec_cik"),
                                      "relation": h["relation"], "title": h["qualifier"], "first_filing": h["first_seen"],
                                      "last_filing": h["last_seen"], "citation": h["evidence_url"]} for h in hits]
                claims_out.append(c)
        center_hits = [e for e in filed if e["dst"] == center]
        unmentioned = [{"org_in_filings": ents[e["dst"]]["name"], "org_sec_cik": ents[e["dst"]]["ids"].get("sec_cik"),
                        "relation": e["relation"], "last_filing": e["last_seen"], "citation": e["evidence_url"]}
                       for e in filed if e["dst"] != center and e["dst"] not in claimed_orgs]
        results.append({**base, "status": "PERSON_MATCHED", "person": person,
                        "center_role_in_filings": [{"relation": e["relation"], "title": e["qualifier"], "first_filing": e["first_seen"],
                                                    "last_filing": e["last_seen"], "citation": e["evidence_url"]} for e in center_hits],
                        "claims": claims_out, "filed_roles_not_in_document": unmentioned})

    not_in_doc = [{"name": ents[p]["name"], "sec_cik": ents[p]["ids"].get("sec_cik"),
                   "relations": sorted({e["relation"] for e in edges if e["src"] == p and e["dst"] == center}),
                   "last_filing": max((e["last_seen"] or "") for e in edges if e["src"] == p and e["dst"] == center)}
                  for p in insiders - matched_persons]
    return {"document": os.path.basename(doc_path), "center": ents[center]["name"], "center_cik": center_cik.zfill(10),
            "checked_on": today.isoformat(), "rows": results,
            "insiders_not_in_document": sorted(not_in_doc, key=lambda x: x["last_filing"], reverse=True)}


def to_markdown(r):
    L = [f"# Claims check: {r['document']}", "",
         f"Center: **{r['center']}** (SEC CIK {r['center_cik']}) · checked {r['checked_on']} against SEC insider filings in HOOT.", "",
         "**CONFIRMED** = an SEC filing shows it (linked) · **CONFLICT** = filings contradict the timing · "
         "**NO_SEC_COVERAGE** = not in SEC insider filings; this is *not* evidence the claim is false, it needs a proxy statement, Form 990 or registry · "
         "**PERSON_NOT_FOUND** = no McDonald's insider with that name in the filings checked.", ""]
    counts = {}
    for row in r["rows"]:
        for c in row.get("claims", []):
            counts[c["status"]] = counts.get(c["status"], 0) + 1
    L += ["| Status | Claims |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(counts.items())] + [""]
    for row in r["rows"]:
        L.append(f"## {row['person_claimed']}")
        if row["status"] != "PERSON_MATCHED":
            L += [f"**{row['status']}**" + (": candidates " + ", ".join(f"{c['name']} (CIK {c['sec_cik']})" for c in row["candidates"]) if row.get("candidates") else ""), ""]
            continue
        p = row["person"]
        L.append(f"Matched to SEC filer **{p['name_in_filings']}** (CIK {p['sec_cik']}). Confirm this is the same person.")
        for c in row["center_role_in_filings"]:
            L.append(f"- Center role in filings: {c['relation']}{' (' + c['title'] + ')' if c['title'] else ''}, filings {c['first_filing']} → {c['last_filing']} · [filing]({c['citation']})")
        for c in row["claims"]:
            line = f"- **{c['status']}** · {c['org_claimed']}" + (f" ({c['role_claimed']})" if c["role_claimed"] else "") + f" · claimed {c['claimed_as']}"
            if c.get("evidence"):
                ev = c["evidence"][0]
                line += f" → {ev['relation']} at {ev['org_in_filings']}, filings {ev['first_filing']} → {ev['last_filing']} · [filing]({ev['citation']})"
            if c.get("note"):
                line += f" · *{c['note']}*"
            L.append(line)
        for u in row["filed_roles_not_in_document"]:
            L.append(f"- ➕ In filings, not in document: {u['relation']} at {u['org_in_filings']} (last filing {u['last_filing']}) · [filing]({u['citation']})")
        L.append("")
    if r["insiders_not_in_document"]:
        L += ["## Insiders in McDonald's filings who are not in the document", ""]
        L += [f"- {x['name']} (CIK {x['sec_cik']}): {', '.join(x['relations'])}, last filing {x['last_filing']}" for x in r["insiders_not_in_document"]]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("document", help="Markdown or text export of the research document")
    ap.add_argument("--center-cik", required=True, help="SEC CIK of the organization the document is about")
    ap.add_argument("--hoot-db", default=DEFAULT_HOOT_DB)
    ap.add_argument("--report", help="also write a Markdown report here (e.g. into the Obsidian vault)")
    a = ap.parse_args()
    r = check(a.document, a.center_cik, a.hoot_db)
    os.makedirs(os.path.join(ROOT, "data", "staging"), exist_ok=True)
    out = os.path.join(ROOT, "data", "staging", "claims_" + re.sub(r"\W+", "_", os.path.splitext(os.path.basename(a.document))[0]) + ".json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(r, f, indent=2, ensure_ascii=False)
    md = to_markdown(r)
    if a.report:
        with open(a.report, "w", encoding="utf-8", newline="\n") as f:
            f.write(md)
    print(f"wrote {out}" + (f" and {a.report}" if a.report else ""))
    for row in r["rows"]:
        st = [c["status"] for c in row.get("claims", [])]
        print(f"  {row['person_claimed']:<36} {row['status']:<17} " + ", ".join(f"{s}×{st.count(s)}" for s in sorted(set(st))))


if __name__ == "__main__":
    main()
