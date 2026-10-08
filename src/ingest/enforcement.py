"""Public enforcement records check: every organization and person on the map, against official records that
government agencies have ALREADY published. We report these exactly as the agency states them; we never infer.

  U.S. Department of Justice press releases   resolved civil matters (False Claims Act settlements and judgments,
                                              other civil settlements), complaints filed, criminal outcomes
  HHS-OIG List of Excluded Individuals/Entities   parties excluded from federal health care programs

Every entity gets a dated stamp: "reviewed YYYY-MM-DD against [sources]: no record found / N possible matches".
A name appearing in a record is ONE fact, so every match is POSSIBLE until you tick it in
HOOT/Enforcement confirmations (your tick is the second fact). Only confirmed records can appear in a report,
quoted and linked, with the agency's own wording (e.g. "no determination of liability").

    python -I src/ingest/enforcement.py
"""
import csv
import html
import io
import json
import os
import re
import sys
import urllib.parse
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import paths  # noqa: E402
HOOT_DIR = paths.HOOT_DIR
sys.path.insert(0, HOOT_DIR)
from engine import audit, store  # noqa: E402
from hootlib.util import Http, load_config  # noqa: E402

OUT = os.path.join(ROOT, "data", "enforcement")
STAMPS = os.path.join(OUT, "stamps.json")
RELEASES = os.path.join(OUT, "doj_releases.json")
CONFIRM = os.path.join(os.path.dirname(audit.ANCHOR_PATH), "..", "Enforcement confirmations.md")
REPORT = os.path.join(os.path.dirname(audit.ANCHOR_PATH), "Enforcement records check.md")
DOJ_API = "https://www.justice.gov/api/v1/press_releases.json?pagesize=50&page={}&{}"
DOJ_QUERIES = ["False Claims", "Resolve Allegations", "Settle Allegations"]
LEIE_URL = "https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv"
SUFFIX = r"(inc|incorporated|corp|corporation|co|company|llc|l\.l\.c|lp|l\.p|ltd|limited|plc|holdings?|group|n\.a|sa|ag|nv|se)"
GENERIC = {"target", "block", "visa", "apple", "amazon", "oracle", "meta", "ball", "gap", "best buy", "dollar general",
           "general electric", "united", "american", "national", "first", "international", "global", "capital"}


def kind_of(title):
    t = title.lower()
    if re.search(r"plead(s|ed)? guilty|convicted|sentenced|indicted|charged", t):
        return "criminal case (as described by DOJ)"
    if re.search(r"files? (a )?complaint|sues|intervenes|lawsuit against", t):
        return "complaint filed: allegations, not resolved"
    if re.search(r"judgment|ordered to pay", t):
        return "court judgment (as described by DOJ)"
    if re.search(r"agree|to pay|pays|settle|resolve|recover", t):
        return "civil settlement (as described by DOJ)"
    return "press release"


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def fetch_doj(http):
    have = json.load(open(RELEASES, encoding="utf-8")) if os.path.exists(RELEASES) else {}
    for q in DOJ_QUERIES:
        page = 0
        while True:
            url = DOJ_API.format(page, urllib.parse.urlencode({"parameters[title]": q}))
            try:
                d = json.loads(http.request(url, ttl=7 * 86400)[0])
            except Exception as ex:                       # noqa: BLE001
                print(f"  DOJ {q!r} page {page}: {str(ex)[:80]}")
                break
            rs = d.get("results") or []
            for r in rs:
                if r.get("uuid") in have:
                    continue
                body = clean(r.get("body"))
                have[r["uuid"]] = {
                    "title": clean(r.get("title")), "url": r.get("url"), "number": r.get("number"),
                    "date": datetime.fromtimestamp(int(r.get("date") or 0), timezone.utc).date().isoformat(),
                    "component": ", ".join(re.findall(r"'name': '([^']+)'", str(r.get("component") or ""))),
                    "no_liability": bool(re.search(r"no determination of liability|allegations only", body, re.I)),
                    "text": body[:8000]}
            page += 1
            if len(rs) < 50 or page > 200:
                break
        print(f"  DOJ releases matching {q!r}: {len(have)} total stored")
    os.makedirs(OUT, exist_ok=True)
    with open(RELEASES, "w", encoding="utf-8") as f:
        json.dump(have, f, ensure_ascii=False)
    return have


def fetch_leie(http):
    b, sha = http.request(LEIE_URL, ttl=7 * 86400)
    rows = list(csv.DictReader(io.StringIO(b.decode("latin-1"))))
    return rows, sha


LEGAL = r"(inc|incorporated|corp|corporation|co|company|llc|l\.l\.c|lp|l\.p|ltd|limited|plc|n\.a|sa|ag|nv|se)"
# words that may follow a company's name in a headline without making it a DIFFERENT company
NEXT_OK = {"agrees", "agree", "to", "pays", "pay", "will", "has", "have", "settles", "settle", "resolves", "resolve",
           "and", "of", "in", "for", "pleads", "admits", "enters", "reaches", "must", "ordered", "sentenced", "is",
           "was", "subsidiary", "subsidiaries", "affiliate", "affiliates", "unit", "s", "inc", "incorporated", "corp",
           "corporation", "co", "company", "llc", "lp", "ltd", "limited", "plc", "the", "on", "over", "with", "after",
           "under", "files", "filed", "sued", "admit", "agreed", "paid", "settled", "resolved"}


def org_key(label):
    s = label.lower().replace("&", " and ")
    s = re.sub(r"\(.*?\)|/.*$", " ", s)                     # 'Walmart Inc. / Madrone Capital' -> 'walmart inc.'
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(rf"\b{LEGAL}\b", " ", s)
    s = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()
    words = s.split()
    if len(words) >= 3 and words[-1] in ("group", "holdings", "holding"):   # 'American Airlines Group' -> 'american airlines'
        s = " ".join(words[:-1])
    return s


def distinct_hits(pat, text):
    """Matches of a company name that are not the start of a DIFFERENT company's name
    ('Blackstone Medical', 'American International Biotechnology')."""
    n = 0
    for m in pat.finditer(text):
        nxt = re.match(r"[\s,'’-]*([A-Za-z]+)", text[m.end():])
        if nxt and nxt.group(1)[0].isupper() and nxt.group(1).lower() not in NEXT_OK:
            continue
        n += 1
    return n


def org_pattern(label):
    """Distinctive names match on their own; generic ones ('Target', 'Block') only with a company suffix."""
    k = org_key(label)
    if not k or len(k) < 4:
        return None, False
    words = k.split()
    esc = r"\s+".join(map(re.escape, words))
    if k in GENERIC or (len(words) == 1 and len(k) < 8):
        return re.compile(rf"\b{esc}\s+{LEGAL}\b", re.I), True
    return re.compile(rf"\b{esc}\b", re.I), False


def person_pattern(label):
    toks = [t for t in re.sub(r"[.,]", " ", label).split() if t.lower() not in ("jr", "sr", "ii", "iii", "iv", "phd", "md")]
    if len(toks) < 2 or len(toks[0]) < 2 or len(toks[-1]) < 3:
        return None
    first, last = map(re.escape, (toks[0], toks[-1]))
    return re.compile(rf"\b{first}\s+(?:[A-Z]\.?\s+|[A-Z][a-z]+\s+)?{last}\b")


def main():
    http = Http(load_config())
    g = store.load_graph()
    today = date.today().isoformat()
    print("enforcement check: fetching official records…")
    releases = fetch_doj(http)
    leie, leie_sha = fetch_leie(http)
    leie_date = today

    leie_orgs, leie_people = {}, {}
    for r in leie:
        if r.get("BUSNAME"):
            leie_orgs.setdefault(org_key(r["BUSNAME"]), []).append(r)
        elif r.get("LASTNAME"):
            leie_people.setdefault((r["LASTNAME"].strip().lower(), (r.get("FIRSTNAME") or "").strip().lower()), []).append(r)

    person_state = {}                                    # SEC CIK -> state of the person's SEC filing address (HOOT)
    try:
        import sqlite3
        db = sqlite3.connect(f"file:{os.path.join(HOOT_DIR, 'graph.db')}?mode=ro", uri=True)
        for cik, attrs in db.execute("SELECT i.value, e.attrs FROM ids i JOIN entities e ON e.id=i.entity_id "
                                     "WHERE i.scheme='sec_cik' AND e.type='person'"):
            m = re.search(r",\s*([A-Z]{2})\b", (json.loads(attrs or "{}").get("filing_city") or ""))
            if m:
                person_state[cik.zfill(10)] = m.group(1)
    except Exception as ex:                              # noqa: BLE001
        print(f"  person states unavailable ({ex}); exclusion-list person matches skipped")

    index = {}                                           # word -> releases containing it (fast candidate lookup)
    for uid, r in releases.items():
        for w in set(re.findall(r"[a-z0-9]+", (r["title"] + " " + r["text"]).lower())):
            index.setdefault(w, set()).add(uid)

    def candidates(words):
        sets = [index.get(w, set()) for w in words if w]
        return set.intersection(*sets) if sets else set()

    stamps, matches = {}, []
    live = [n for n in g["nodes"] if not (n.get("provenance") or {}).get("merged_into")]
    for n in live:
        person = n.get("entity_type") == "PERSON"
        found = []
        names = [n["label"]] + [a for a in (n.get("provenance") or {}).get("aliases_seen_in_sources") or [] if not a.isupper()]
        for nm in names:
            pat = person_pattern(nm) if person else org_pattern(nm)[0]
            if not pat:
                continue
            toks = re.findall(r"[a-z0-9]+", nm.lower())
            words = [toks[0], toks[-1]] if person and len(toks) >= 2 else re.findall(r"[a-z0-9]+", org_key(nm))
            for uid in candidates(words):
                r = releases[uid]
                hits = (lambda s: len(pat.findall(s))) if person else (lambda s: distinct_hits(pat, s))
                # the headline, or at least two mentions in the text (one mention is usually a passing reference)
                where = "title" if hits(r["title"]) else ("text" if hits(r["text"]) >= 2 else None)
                if where and not any(f.get("uuid") == uid or (f.get("title"), f.get("date")) == (r["title"], r["date"])
                                     for f in found):
                    found.append({"source": "DOJ press release", "uuid": uid, "where": where, "title": r["title"],
                                  "date": r["date"], "url": r["url"], "kind": kind_of(r["title"]),
                                  "no_liability": r["no_liability"], "name_matched": nm})
            if person:
                toks = [t for t in re.sub(r"[.,]", " ", nm).split() if t.lower() not in ("jr", "sr", "ii", "iii", "iv")]
                cik = (n.get("identity") or {}).get("sec_cik")
                st = person_state.get(cik)
                mid = toks[1][0].upper() if len(toks) >= 3 else None
                for x in leie_people.get((toks[-1].lower(), toks[0].lower()), []) if len(toks) >= 2 else []:
                    # a shared name is not enough: the same state as their SEC address, and no clashing middle initial
                    xm = (x.get("MIDNAME") or "").strip()[:1].upper()
                    if not st or (x.get("STATE") or "").upper() != st or (mid and xm and mid != xm):
                        continue
                    found.append({"source": "HHS-OIG exclusion list", "where": "name", "title":
                                  f"{x.get('FIRSTNAME')} {x.get('MIDNAME') or ''} {x.get('LASTNAME')}".replace("  ", " ").strip(),
                                  "date": x.get("EXCLDATE"), "url": "https://exclusions.oig.hhs.gov/",
                                  "kind": f"exclusion type {x.get('EXCLTYPE')}" + (f", reinstated {x['REINDATE']}" if (x.get("REINDATE") or "0") not in ("", "00000000") else ""),
                                  "state": x.get("STATE"), "detail": f"{x.get('GENERAL') or ''} {x.get('SPECIALTY') or ''}".strip(),
                                  "name_matched": nm, "why": f"same name and state ({st}) as their SEC filing address"})
            else:
                for x in leie_orgs.get(org_key(nm), []):
                    found.append({"source": "HHS-OIG exclusion list", "where": "name", "title": x.get("BUSNAME"),
                                  "date": x.get("EXCLDATE"), "url": "https://exclusions.oig.hhs.gov/",
                                  "kind": f"exclusion type {x.get('EXCLTYPE')}", "state": x.get("STATE"),
                                  "detail": x.get("GENERAL") or "", "name_matched": nm})
        stamps[n["id"]] = {"reviewed": today, "label": n["label"],
                           "sources": [f"DOJ press releases ({len(releases)} on civil fraud and settlements)",
                                       f"HHS-OIG exclusion list (downloaded {leie_date})"],
                           "possible": len(found),
                           "result": "no record found" if not found else f"{len(found)} possible match(es): not confirmed"}
        for f in found:
            matches.append((n, f))

    # keep the investigator's ticks; a tick confirms that record is about that entity
    old = open(CONFIRM, encoding="utf-8").read() if os.path.exists(CONFIRM) else ""
    ticked = set(re.findall(r"- \[[xX]\] .*?`(ENT_[A-Z0-9_]+) \| ([^`]+)`", old))
    confirmed = {}
    for n, f in matches:
        key = (n["id"], f.get("uuid") or f"LEIE:{f['title']}:{f['date']}")
        if key in ticked:
            confirmed.setdefault(n["id"], []).append(f)
    for eid, fs in confirmed.items():
        stamps[eid]["confirmed"] = fs
        stamps[eid]["result"] = f"{len(fs)} confirmed public record(s)" + (
            f"; {stamps[eid]['possible'] - len(fs)} other possible match(es) not confirmed" if stamps[eid]["possible"] > len(fs) else "")
    os.makedirs(OUT, exist_ok=True)
    with open(STAMPS, "w", encoding="utf-8") as fh:
        json.dump({"reviewed": today, "leie_sha256": leie_sha, "doj_releases": len(releases), "entities": stamps}, fh,
                  ensure_ascii=False, indent=0)

    strength = lambda f: (0 if f["where"] == "title" else 1 if f["source"].startswith("DOJ") else 2)
    matches.sort(key=lambda x: (strength(x[1]), x[0]["entity_type"] == "PERSON", x[0]["label"]))
    C = ["# Enforcement confirmations", "",
         "Each line is an official record (a Justice Department press release or the HHS-OIG exclusion list) whose text "
         "contains a name on your map. **A matching name is not proof it is the same organization or person.** Open the "
         "link; tick the box only if the record is clearly about that entity (same company, same location, same people). "
         "Unticked lines never appear in a report. Your ticks are kept between runs.", ""]
    for n, f in matches:
        key = f.get("uuid") or f"LEIE:{f['title']}:{f['date']}"
        mark = "x" if (n["id"], key) in ticked else " "
        where = {"title": "named in the headline", "text": "named in the text", "name": "same name"}[f["where"]]
        C.append(f"- [{mark}] **{n['label']}** · {f['source']}, {f['date']}: \"{f['title']}\" ({f['kind']}; {where}"
                 + (f"; state {f['state']}" if f.get("state") else "") + f") [open]({f['url']}) `{n['id']} | {key}`")
    if not matches:
        C.append("- nothing to confirm")
    with open(CONFIRM, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(C) + "\n")

    orgs = sum(1 for n in live if n.get("entity_type") != "PERSON")
    hit_entities = {n["id"] for n, _ in matches}
    R = ["---", "hoot: report", f"generated: {today}", "tags: [hoot/report]", "---", "# Enforcement records check", "",
         f"Every organization ({orgs}) and person ({len(live) - orgs}) on the map was checked on {today} against official "
         f"records agencies have already published: {len(releases)} Justice Department press releases on civil fraud "
         "matters and settlements, and the HHS-OIG List of Excluded Individuals/Entities.", "",
         f"- **No record found:** {len(live) - len(hit_entities)} entities",
         f"- **Possible matches to confirm:** {len(matches)} records for {len(hit_entities)} entities: see "
         "[[Enforcement confirmations]]",
         f"- **Confirmed by you:** {sum(len(v) for v in confirmed.values())} records for {len(confirmed)} entities", "",
         "How to read a match: *named in the headline* is the strongest; *named in the text* may be a passing mention "
         "(a customer, a partner, an earlier employer); *same name* on the exclusion list is the weakest, because "
         "many people share a name. A settlement press release describes allegations the parties resolved; when DOJ says "
         "there was no determination of liability, the report says so too.", "",
         "Each entity's dated stamp shows in the app's details panel (\"Public records check\")."]
    with open(REPORT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(R) + "\n")
    audit.log_event("enforcement_review", entities=len(live), doj_releases=len(releases), leie_sha256=leie_sha,
                    possible=len(matches), confirmed=sum(len(v) for v in confirmed.values()))
    print(f"enforcement check: {len(live)} entities stamped · {len(matches)} possible matches for {len(hit_entities)} "
          f"entities · {sum(len(v) for v in confirmed.values())} confirmed -> {REPORT}")


if __name__ == "__main__":
    main()
