"""People sweep: for every person named anywhere in the investigation, look for an official record and
follow it to every organization they are tied to.

Sources of names: the master graph's leadership lists and human bridges, the investigator's research
documents (claims checks), and people found in Form D / Form 990 filings who aren't linked yet.

For each name:
  1. SEC search by name -> candidate filers (CIK, mailing address, filing history)
  2. accept a candidate only with a SECOND FACT: its SEC address names an organization the investigator
     ties the person to, or its SEC filing career includes one of those organizations
  3. accepted -> career sweep (every company the person has filed for)
  4. no SEC record -> LittleSis lookup as a LEAD (secondary source), noting which of the person's
     claimed organizations it mentions
Report: HOOT/Reports/People sweep.md. Progress is cached, so each run continues where the last stopped."""
import json
import os
import re
import urllib.parse
from collections import defaultdict

from . import yamlish
from .resolve import _org_norm, claims_index
from .sources import sec
from .util import HOOT_DIR, NICKNAMES, log, norm_name, person_name_match, sec_person_name, today


def name_variants(name):
    """The name as given, plus the maiden-name form when a middle word looks like a surname
    ('Dina Powell McCormick' also files as 'Dina Powell')."""
    toks = [t for t in re.sub(r"\(.*?\)", " ", name).replace(",", " ").split()]
    out = [name]
    if len(toks) >= 3 and len(toks[-2]) > 2 and toks[-2][0].isupper() and "." not in toks[-2]:
        out.append(f"{toks[0]} {toks[-2]}")
    return out

DONE = os.path.join(HOOT_DIR, "people_sweep.json")
GRAPH = os.path.join(os.path.expanduser("~"), "PriorityNexus", "data", "priority_nexus_graph.json")
class _People(defaultdict):
    prio = {}


LEAD_ROLE = re.compile(r"^\s*(.+?)\s*\((.+)\)\s*$")
NOT_A_NAME = re.compile(r"\b(equity|inc|corp|group|fund|capital|holdings?|partners|llc|lp|ltd|plc|board|committee|director|"
                        r"chair|ceo|cfo|president|trust|foundation|university|bank|council|13f|def|sec)\b", re.I)


def plausible_name(s):
    toks = s.split()
    return 2 <= len(toks) <= 6 and not NOT_A_NAME.search(s) and not re.search(r"\d", s) and len(toks[-1]) > 1


def targets(store):
    """{display name: set(org names the investigator ties them to)}"""
    people = _People(set)
    prio = {}
    if os.path.exists(GRAPH):
        g = json.load(open(GRAPH, encoding="utf-8"))
        for n in g.get("nodes", []):
            for entry in n.get("leadership") or []:
                m = LEAD_ROLE.match(entry)
                name = (m.group(1) if m else entry).strip()
                if plausible_name(name):
                    people[name].add(n["label"])
                    prio[name] = 0                      # your leadership lists first
    for person, org, _src in claims_index():
        if plausible_name(person):
            people[person].add(org)
            prio[person] = min(prio.get(person, 9), 1 if _src.startswith("priority_nexus_graph") else 2)
    # unlinked people from Form D / 990 filings: their organization is the one in the filing
    for eid, val in store.db.execute("SELECT entity_id, value FROM ids WHERE scheme='manual' AND (value LIKE 'formd:%' OR value LIKE 'ein:%')"):
        e = store.entity(eid)
        if e and e["type"] == "person":
            for ed in store.edges("src=?", (eid,)):
                people[e["name"]].add(store.entity(ed["dst"])["name"])
                prio.setdefault(e["name"], 3)
    people.prio = prio
    return people


def _bare(s):
    return _org_norm(s)


def _queries(toks):
    """EDGAR name searches to try, in order: surname + first two letters of each first-name form (Bill -> William),
    then a maiden name kept before the married one ('Dina Powell McCormick' -> 'Powell Di')."""
    first = toks[0].lower().strip(".")
    forms = [first] + [f for f in {NICKNAMES.get(first)} if f]
    surnames = [toks[-1]] + ([toks[-2]] if len(toks) >= 3 and len(toks[-2]) > 2 and toks[-2][0].isupper() else [])
    out = []
    for s in surnames:
        for f in forms:
            q = f"{s} {f[:2]}".replace(".", "")
            if q.lower() not in [x.lower() for x in out]:
                out.append(q)
    return out


def _search(http, q):
    url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=" + urllib.parse.quote(q) +
           "&type=&dateb=&owner=only&count=40&output=atom")
    try:
        return http.request(url, ttl=30 * 86400)[0].decode("latin-1")
    except Exception:                                          # noqa: BLE001
        return ""


def sec_candidates(http, name):
    toks = [t for t in re.sub(r"\(.*?\)", " ", name).replace(",", " ").split() if t.lower().strip(".") not in ("jr", "sr", "ii", "iii", "dr", "sir")]
    if len(toks) < 2:
        return []
    out, seen = [], set()
    for q in _queries(toks):
        for c in _parse_candidates(_search(http, q)):
            if c["cik"] not in seen:
                seen.add(c["cik"])
                out.append(c)
    return out[:16]


def _parse_candidates(x):
    if not x:
        return []
    out = []
    one = re.search(r"<conformed-name>([^<]+)</conformed-name>", x)
    if one:                                                    # exactly one match: the page is that filer
        cik = re.search(r"<cik>(\d+)</cik>", x).group(1)
        addr = " ".join(re.findall(r"<(?:street1|street2|city)>([^<]+)<", x))
        out.append({"cik": cik, "address": addr})
    else:
        for ent in re.findall(r"<entry[\s\S]*?</entry>", x):
            cik = re.search(r"<cik>(\d+)</cik>", ent)
            if cik:
                out.append({"cik": cik.group(1), "address": " ".join(re.findall(r"<(?:street1|street2|city)>([^<]+)<", ent)),
                            "last": (re.search(r"<last-date>([^<]+)<", ent) or [None, None])[1]})
    return out[:12]


def career_names(http, cik):
    url = f"https://www.sec.gov/cgi-bin/own-disp?action=getowner&CIK={sec.pad(cik)}"
    try:
        html = http.request(url, ttl=30 * 86400)[0].decode("utf-8", "replace")
    except Exception:                                          # noqa: BLE001
        return []
    return [re.sub(r"<[^>]+>", " ", n).split("Current Name:")[-1].strip() for n, *_ in sec.OWN_ROW.findall(html)]


def second_fact(cand, orgs, careers):
    addr = _bare(cand.get("address") or "")
    tight = lambda s: re.sub(r"[^a-z0-9]", "", _bare(s))         # 'J P Morgan Chase' == 'JPMorgan Chase'
    for org in orgs:
        o = _bare(org)
        words = [w for w in o.split() if len(w) >= 4]
        if words and all(w in addr for w in words[:2]):
            return f"SEC mailing address ({cand['address'].strip()}) names {org}"
        for c in careers:
            co = _bare(c)
            ot, ct = tight(org), tight(c)
            if o and (o == co or ot == ct or (len(ot) > 5 and (ot in ct or ct in ot))):
                return f"SEC filing career includes {c} (you tie them to {org})"
    return None


def run(http, store, cfg, limit=150, renderer=None, only=None):
    """`only`: a list of names to look up now (e.g. a new document's people), even if checked before."""
    done = json.load(open(DONE, encoding="utf-8")) if os.path.exists(DONE) else {}
    people = targets(store)
    have_cik = {norm_name(e["name"]) for e in store.all_entities() if e["type"] == "person" and e["ids"].get("sec_cik")}
    if only:
        want = {norm_name(x) for x in only}
        todo = [p for p in people if norm_name(p) in want]
    else:
        todo = [p for p in sorted(people, key=lambda x: (people.prio.get(x, 9), x))
                if norm_name(p) not in done and norm_name(p) not in have_cik][:limit]
    log(f"people sweep: {len(people)} people named · {len(todo)} to look up this run")
    for i, name in enumerate(todo, 1):
        orgs = sorted(people[name])
        rec = {"name": name, "orgs": orgs[:8], "checked": today()}
        cands = []
        for c in sec_candidates(http, name):
            try:
                sub = sec.submissions(http, c["cik"])
            except Exception:                                  # noqa: BLE001
                continue
            sec_name = sec_person_name(sub.get("name") or "")
            if any(person_name_match(v, sec_name) for v in name_variants(name)):
                c["sec_name"] = sec_name
                cands.append(c)
        accepted = []
        for c in cands:
            careers = career_names(http, c["cik"])
            fact = second_fact(c, orgs, careers)
            if fact:
                accepted.append((c, fact, careers))
        if len(accepted) == 1:
            c, fact, careers = accepted[0]
            pid = store.upsert_entity("person", c["sec_name"], {"sec_cik": sec.pad(c["cik"])},
                                      {"identity_basis": f"name + {fact}", "people_sweep": today()}, aliases=[name])
            n_new = sec.person_careers(http, store, c["cik"])
            rec.update(status="found", cik=c["cik"], basis=fact, companies=len(careers), new_links=n_new)
        elif accepted:
            rec.update(status="ambiguous", candidates=[{"cik": c["cik"], "why": f} for c, f, _ in accepted])
        elif cands:
            rec.update(status="possible", candidates=[{"cik": c["cik"], "sec_name": c["sec_name"], "address": c["address"],
                                                      "sec_companies": career_names(http, c["cik"])[:10]} for c in cands[:4]],
                       note="SEC filer(s) with this name, but nothing ties them to the organizations you name")
        else:
            rec.update(status="no_sec_record")
            rec.update(littlesis_lead(http, name, orgs))
        done[norm_name(name)] = rec
        if i % 10 == 0:
            store.commit()
            json.dump(done, open(DONE, "w", encoding="utf-8"), indent=1)
            log(f"  {i}/{len(todo)}")
    store.commit()
    json.dump(done, open(DONE, "w", encoding="utf-8"), indent=1)
    if renderer:
        renderer.render()
        write_report(renderer, cfg, done)
    c = defaultdict(int)
    for r in done.values():
        c[r["status"]] += 1
    log("people sweep: " + " · ".join(f"{k} {v}" for k, v in sorted(c.items())))


def littlesis_lead(http, name, orgs):
    try:
        d = http.json(f"https://littlesis.org/api/entities/search?q={urllib.parse.quote(name)}", ttl=30 * 86400)
    except Exception:                                          # noqa: BLE001
        return {}
    hits = [h for h in d.get("data", []) if h["attributes"].get("primary_ext") == "Person"
            and person_name_match(name, h["attributes"]["name"])]
    if not hits:
        return {}
    h = hits[0]
    lsid = h["id"]
    try:
        rels = http.json(f"https://littlesis.org/api/entities/{lsid}/relationships?page=1", ttl=30 * 86400).get("data", [])
    except Exception:                                          # noqa: BLE001
        rels = []
    linked = sorted({re.sub(r"^.*?/(?:org|person)/\d+-", "", r.get("related") or "").replace("_", " ") for r in rels})[:40]
    agree = [o for o in orgs if any(_bare(o) and (_bare(o) in _bare(x) or _bare(x) in _bare(o)) for x in linked if len(_bare(x)) > 3)]
    return {"littlesis": {"id": lsid, "url": f"https://littlesis.org/entities/{lsid}", "blurb": h["attributes"].get("blurb"),
                          "organizations": linked, "agrees_with_your_orgs": agree,
                          "note": "secondary source: leads to verify, not proof"}}


def write_report(renderer, cfg, done):
    rows = list(done.values())
    L = ["# People sweep", "",
         "Everyone named in your leadership lists, original graph and research documents, looked up for an official "
         "record. A record is accepted only when a **second fact** agrees (its SEC address or filing career names an "
         "organization you tie the person to). Accepted people then get a full career sweep.", ""]
    sec_ = lambda r: [r2 for r2 in rows if r2["status"] == r]
    found = sec_("found")
    L += [f"## Found and expanded ({len(found)})", ""] + [
        f"- **{r['name']}** (SEC CIK {int(r['cik'])}): {r['basis']} · {r.get('companies', 0)} companies in SEC career, "
        f"{r.get('new_links', 0)} new links" for r in sorted(found, key=lambda x: x["name"])] + [""]
    nos = sec_("no_sec_record")
    L += [f"## No SEC record: officials, private figures ({len(nos)})", "",
          "Not SEC insiders. LittleSis (a secondary, crowd-researched source) shown as **leads**; ✓ marks where it agrees "
          "with an organization you named. Each lead still needs an official record (appointment, Form 990, registry).", ""]
    for r in sorted(nos, key=lambda x: x["name"]):
        ls = r.get("littlesis")
        if ls:
            agree = ", ".join(ls["agrees_with_your_orgs"]) or "none of yours"
            L.append(f"- **{r['name']}** ({'; '.join(r['orgs'][:3])}): [LittleSis]({ls['url']}) agrees on: {agree} · "
                     f"also lists: {', '.join(ls['organizations'][:12])}")
        else:
            L.append(f"- **{r['name']}** ({'; '.join(r['orgs'][:3])}): no SEC or LittleSis record")
    pos = sec_("possible") + sec_("ambiguous")
    L += ["", f"## Possible matches, not accepted ({len(pos)})", "",
          "Same name in SEC records, but nothing ties that record to the organizations you named. Listed as **leads**: "
          "if one of these companies also appears in your research for this person, it becomes a confirmed match next run.", ""]
    L += [f"- **{r['name']}** (you tie them to: {'; '.join(r['orgs'][:2])}): " + "; ".join(
        f"SEC filer {c.get('sec_name', '')} (CIK {int(c['cik'])}) files for: {', '.join(c.get('sec_companies', [])[:6]) or c.get('why', '?')}"
        for c in r.get("candidates", [])[:3]) for r in sorted(pos, key=lambda x: x["name"])]
    rel = f"{cfg.get('notes_folder', 'HOOT')}/Reports/People sweep.md"
    renderer._write(rel, yamlish.dump({"hoot": "report", "generated": today(), "tags": ["hoot/report"]}) + "\n".join(L) + "\n")
