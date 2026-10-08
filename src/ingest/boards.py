"""Check "<Company> Board of Directors" tables in a research document against SEC insider filings.

For every row (person, role) under a heading naming a company:
    CONFIRMED       the company's own Form 3/4/5 filings list this person as director/officer
    CONFLICT        filings list them, but the newest is stale or reports the role ended
    NOT_IN_FILINGS  the company is ingested in HOOT but this person isn't among its filers
                    (wrong, brand-new, or a director who holds no equity; check the DEF 14A)
    NOT_INGESTED    the company is an SEC filer HOOT hasn't pulled yet (command given)
    NO_SEC_COVERAGE the company doesn't file with the SEC (private/nonprofit/foreign)
Nothing here writes to the master graph. Confirmed rows reach it only through hoot_bridge + Review.
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src", "ingest"))
from claims import org_key, person_match  # noqa: E402
from hoot_bridge import ACTIVE_WINDOW_DAYS, DEFAULT_HOOT_DB, hoot_entities, open_hoot, source_type  # noqa: E402

HEAD = re.compile(r"^(?:part\s+[ivx]+:.*?|[ivx]+\.\s*|\d+\\?\.\s*)?(.+?)\s+(?:board of directors|supervisory board|board of trustees|"
                  r"board breakdown|board of directors & .*|board)\b", re.I)


def sec_company_titles(hoot_db):
    """Ticker list from HOOT's download cache (no network). {org_key: (title, ticker)}"""
    vault_hoot = os.path.dirname(hoot_db)
    sys.path.insert(0, vault_hoot)
    try:
        from hootlib.util import Http, load_config
        data = Http(load_config()).json("https://www.sec.gov/files/company_tickers.json", ttl=30 * 86400)
    except Exception:                                      # noqa: BLE001
        return {}
    out = {}
    for r in data.values():
        out.setdefault(org_key(r["title"]), (r["title"], str(r["cik_str"])))    # first listing = primary ticker
    return out


JUNK = re.compile(r"leadership|second-degree|table\b|bridges|breakdown|directorate|\bweb\b|phase|^[ivx]+\.?$|&$|^key\b", re.I)


def parse(text):
    lines = text.splitlines()
    tables = []
    for i in range(len(lines) - 1):
        if not (lines[i].startswith("|") and re.match(r"^\|\s*:?-{3,}", lines[i + 1])):
            continue
        head = [c.strip(" *") for c in lines[i].strip().strip("|").split("|")]
        if not re.search(r"director|name", head[0], re.I) or len(head) > 4:
            continue
        j = i - 1
        while j > 0 and not lines[j].lstrip().startswith("#"):
            j -= 1
        heading = re.sub(r"[\\*#]", "", lines[j]).strip()
        m = HEAD.match(re.sub(r"\(.*?\)", "", heading).strip())
        if not m:
            continue
        company = m.group(1).strip(" :-–—")
        rows, k = [], i + 2
        while k < len(lines) and lines[k].startswith("|"):
            cells = [re.sub(r"[\\*]", "", c).strip() for c in lines[k].strip().strip("|").split("|")]
            name = re.sub(r"\(.*?\)", "", cells[0]).strip()
            if len(name.split()) >= 2 and not re.search(r"\d", name):
                rows.append({"person": name, "role": cells[1] if len(cells) > 1 else "", "line": k + 1})
            k += 1
        tables.append({"heading": heading, "company": company, "line": i + 1, "rows": rows})
    return tables


def check(doc_path, hoot_db=DEFAULT_HOOT_DB):
    tables = parse(open(doc_path, encoding="utf-8").read())
    db = open_hoot(hoot_db)
    ents = hoot_entities(db)
    edges = [dict(r) for r in db.execute("SELECT * FROM edges")]
    orgs = {}
    for i, e in ents.items():
        if e["type"] == "organization" and e["ids"].get("sec_cik"):
            orgs.setdefault(org_key(e["name"]), i)
            for (a,) in db.execute("SELECT alias FROM aliases WHERE entity_id=?", (i,)):
                orgs.setdefault(org_key(a), i)
    filers = defaultdict(list)                              # org -> primary-source person edges
    for e in edges:
        if source_type(e["source"]) == "Form 3/4/5" and ents[e["src"]]["type"] == "person":
            filers[e["dst"]].append(e)
    sec = sec_company_titles(hoot_db)
    today = date.today()
    out, confirmed_people = [], defaultdict(dict)

    for t in tables:
        key = org_key(t["company"])
        oid = orgs.get(key)
        res = {"company_claimed": t["company"], "heading": t["heading"], "doc_line": t["line"], "rows": []}
        if oid is not None and not ents[oid]["attrs"].get("key_filings"):
            oid = None                      # only seen through someone else's filings; its own board wasn't pulled
        if oid is None:
            if JUNK.search(t["company"]) or len(key) < 3:
                res["company_status"] = "UNRESOLVED_TABLE"
                res["note"] = "the heading doesn't name a single company; a person must say which board this is"
            elif key in sec:
                res["company_status"] = "NOT_INGESTED"
                res["action"] = f"hoot sec {sec[key][1]}"
            else:
                res["company_status"] = "NO_SEC_COVERAGE"
            res["rows"] = [{**r, "status": res["company_status"]} for r in t["rows"]]
            out.append(res)
            continue
        org = ents[oid]
        res["company_status"] = "INGESTED"
        res["company_in_filings"] = org["name"]
        res["sec_cik"] = org["ids"]["sec_cik"]
        for r in t["rows"]:
            hits = [e for e in filers[oid] if person_match(r["person"], ents[e["src"]]["name"])]
            people = {e["src"] for e in hits}
            if len(people) > 1:
                out_row = {**r, "status": "AMBIGUOUS", "candidates": sorted(ents[p]["name"] for p in people)}
            elif not hits:
                w = org["attrs"].get("insider_window") or {}
                out_row = {**r, "status": "NOT_IN_FILINGS",
                           "note": f"not among {org['name']}'s insider filers in the window checked "
                                   f"({w.get('from', '?')} → {w.get('to', '?')}); check the latest DEF 14A"}
            else:
                newest = max(hits, key=lambda e: e["last_seen"] or "")
                last = newest["last_seen"] or newest["filing_date"]
                stale = last and (today - date.fromisoformat(last)).days > ACTIVE_WINDOW_DAYS
                pid = newest["src"]
                out_row = {**r, "status": "CONFLICT" if (stale or newest["is_current"] == 0) else "CONFIRMED",
                           "name_in_filings": ents[pid]["name"], "person_cik": ents[pid]["ids"].get("sec_cik"),
                           "relations": sorted({e["relation"] + (f" ({e['qualifier']})" if e["qualifier"] else "") for e in hits}),
                           "first_filing": min((e["first_seen"] or "") for e in hits), "last_filing": last,
                           "citation": newest["evidence_url"]}
                if out_row["status"] == "CONFLICT":
                    out_row["note"] = f"newest insider filing {last}; confirm in the latest DEF 14A"
                confirmed_people[ents[pid]["ids"].get("sec_cik")][org["name"]] = out_row
            res["rows"].append(out_row)
        out.append(res)

    bridges = []
    for cik, by_org in confirmed_people.items():
        if len(by_org) >= 2:
            any_row = next(iter(by_org.values()))
            bridges.append({"person": any_row["name_in_filings"], "sec_cik": cik,
                            "organizations": [{"org": o, "status": r["status"], "last_filing": r["last_filing"],
                                               "citation": r["citation"]} for o, r in sorted(by_org.items())]})
    bridges.sort(key=lambda b: -len(b["organizations"]))
    return {"document": os.path.basename(doc_path), "checked_on": today.isoformat(), "tables": out, "verified_bridges": bridges}


def to_markdown(r):
    tally = defaultdict(int)
    for t in r["tables"]:
        for row in t["rows"]:
            tally[row["status"]] += 1
    L = [f"# Board tables check: {r['document']}", "", f"Checked {r['checked_on']} against SEC insider filings (Forms 3/4/5) in HOOT.", "",
         "| Status | Rows |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(tally.items())] + ["",
         "*CONFIRMED = the company's own filings list this person. NOT_IN_FILINGS ≠ false: some directors hold no equity "
         "or joined recently; check the DEF 14A. NO_SEC_COVERAGE = private, nonprofit or foreign: needs a different primary source.*", ""]
    if r["verified_bridges"]:
        L += ["## Verified human bridges (confirmed at 2+ companies by filings)", ""]
        for b in r["verified_bridges"]:
            L.append(f"- **{b['person']}** (CIK {b['sec_cik']}): " + "; ".join(
                f"[{o['org']}]({o['citation']}) {'✓' if o['status'] == 'CONFIRMED' else '⚠ ' + o['status']} (last filing {o['last_filing']})"
                for o in b["organizations"]))
        L.append("")
    todo = sorted({t["action"] for t in r["tables"] if t.get("action")})
    if todo:
        L += ["## Companies to ingest next (SEC filers not yet in HOOT)", ""] + [f"- `{a}`" for a in todo] + [""]
    for t in r["tables"]:
        L.append(f"## {t['company_claimed']}  <small>(doc line {t['doc_line']})</small>")
        if t["company_status"] != "INGESTED":
            L += [f"**{t['company_status']}**" + (f": run `{t['action']}`" if t.get("action") else "") + f" · {len(t['rows'])} people listed", ""]
            continue
        L.append(f"Matched SEC filer **{t['company_in_filings']}** (CIK {t['sec_cik']})")
        for row in t["rows"]:
            s = f"- **{row['status']}** · {row['person']}" + (f" ({row['role']})" if row["role"] else "")
            if row.get("name_in_filings"):
                s += f" → {row['name_in_filings']} (CIK {row['person_cik']}), {', '.join(row['relations'])}, filings {row['first_filing']}→{row['last_filing']} · [filing]({row['citation']})"
            if row.get("candidates"):
                s += " · candidates: " + ", ".join(row["candidates"])
            if row.get("note"):
                s += f" · *{row['note']}*"
            L.append(s)
        L.append("")
    return "\n".join(L) + "\n"


PAIR = re.compile(r"^\s*[*-]\s+\*\*(?P<leaf>.+?):\*\*\s+Connected solely through\s+(?:former \w+\s+)?\*\*(?P<person>.+?)\*\*"
                  r"(?:\s*\((?P<role>[^)]*)\))?(?:\s+on the\s+\*\*(?P<hub>.+?)\*\*\s+board)?", re.I)


def pairs_as_tables(text):
    """'**A:** Connected solely through **Person** on the **B** board' -> one-row tables for A and B."""
    tables = []
    for n, line in enumerate(text.splitlines(), 1):
        m = PAIR.match(line)
        if not m:
            continue
        person = re.sub(r"\(.*?\)", "", m.group("person")).strip()
        leaf = m.group("leaf")
        leaves = re.split(r"(?<=\b(?:Inc|Corp|plc|Ltd|LLC)\.?)\s+&\s+", leaf)   # "Progyny, Inc. & Surgery Partners"
        targets = [(x, m.group("role") or "claimed link") for x in leaves] + [(m.group("hub"), "board member (claimed)")]
        for org, role in targets:
            if org:
                org = re.sub(r"\(.*?\)", "", org.replace("\\", "")).split("/")[0].strip()
                tables.append({"heading": line.strip()[:120], "company": org, "line": n,
                               "rows": [{"person": person, "role": role, "line": n}]})
    return tables


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("document")
    ap.add_argument("--hoot-db", default=DEFAULT_HOOT_DB)
    ap.add_argument("--report")
    ap.add_argument("--pairs", action="store_true", help="document lists '**A:** Connected solely through **P** on the **B** board'")
    a = ap.parse_args()
    if a.pairs:
        global parse
        parse = pairs_as_tables
    r = check(a.document, a.hoot_db)
    out = os.path.join(ROOT, "data", "staging", "boards_" + re.sub(r"\W+", "_", os.path.splitext(os.path.basename(a.document))[0])[:60] + ".json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(r, f, indent=2, ensure_ascii=False)
    if a.report:
        with open(a.report, "w", encoding="utf-8", newline="\n") as f:
            f.write(to_markdown(r))
    tally = defaultdict(int)
    for t in r["tables"]:
        for row in t["rows"]:
            tally[row["status"]] += 1
    print(f"{len(r['tables'])} board tables · " + " · ".join(f"{k} {v}" for k, v in sorted(tally.items())) +
          f" · {len(r['verified_bridges'])} verified bridges")


if __name__ == "__main__":
    main()
