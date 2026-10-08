"""Pattern signals: connections that no single document states, found by combining official records already on
the map. Each signal lists the records it rests on and is worded as what those records show, never as motive.

  1 Legislature to boardroom   a former senator (Congress.gov) now holds a filed board or officer seat
  2 Oversight overlap          a sitting senator sits on a committee whose jurisdiction covers a sector where
                               the same person holds or held a filed corporate role
  3 Lobbyist-lawmaker link     a lobbyist who disclosed working for a sitting senator (or a Senate committee)
                               lobbies for a company in a sector that senator's committee oversees
  4 Enforcement proximity      a company shares a current director with a company that has a CONFIRMED public
                               enforcement record (confirmed by the investigator; possible matches never count)

Signals for review, not findings. Committee jurisdictions come from data/committee_domains.json (a labelled
classification). People count as one person only when official IDs or the investigator's confirmation join them."""
import json
import os
import re
from collections import defaultdict

from . import paths
from .store import DATA
from .validator import is_superseded

ROLE = ("DIRECTOR_OF", "OFFICER_OF", "TEN_PERCENT_OWNER_OF", "BENEFICIAL_OWNER_OF")


def _same_person(a, b):
    """Nickname-aware name match (Chuck = Charles), using HOOT's matcher when available."""
    try:
        import sys
        sys.path.insert(0, paths.HOOT_DIR)
        from hootlib.util import person_name_match
        return bool(person_name_match(a, b))
    except ImportError:
        return a.split()[0][:3].lower() == b.split()[0][:3].lower() and a.split()[-1].lower() == b.split()[-1].lower()
_cache = {}


def _load(name, default):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def compute(graph):
    nodes = {n["id"]: n for n in graph["nodes"] if not (n.get("provenance") or {}).get("merged_into")}
    edges = [e for e in graph["edges"] if not is_superseded(e) and e["source"] in nodes and e["target"] in nodes]
    jur = _load("committee_domains.json", {"committees": {}})
    stamps = (_load(os.path.join("enforcement", "stamps.json"), {}).get("entities") or {})
    out_e, in_e = defaultdict(list), defaultdict(list)
    for e in edges:
        out_e[e["source"]].append(e)
        in_e[e["target"]].append(e)
    t = lambda e: e.get("transparency") or {}
    cite = lambda e: {"edge_id": e["edge_id"], "source": t(e).get("verification_source"), "url": t(e).get("citation_url"),
                      "date": t(e).get("document_date")}
    doms = lambda nid: {d for d in nodes[nid].get("domains") or [] if d.startswith("DOM_")}
    ccode = lambda nid: str((nodes[nid].get("identity") or {}).get("canonical_id", "")).split(":")[-1]
    def committee_doms(nid):
        hit = jur["committees"].get(ccode(nid))
        if hit:
            return set(hit.get("domains") or [])
        label = nodes[nid]["label"]
        if label.startswith("House "):                  # House committees: matched by name (Rule X classification)
            return {d for k, ds in (jur.get("house_by_name") or {}).items() if k.lower() in label.lower() for d in ds}
        return set()
    signals = []

    senators = {}                                       # person -> (senate edge, active?)
    for e in edges:
        if e["relationship_type"] == "SENATOR_IN":
            senators[e["source"]] = e
    seats = defaultdict(list)                           # sitting senator -> committee seat edges
    for e in edges:
        if e["relationship_type"] == "COMMITTEE_MEMBER_OF" and t(e).get("active") is not False:
            seats[e["source"]].append(e)

    # 1 + 2
    for p, se in senators.items():
        corp = [e for e in out_e[p] if e["relationship_type"] in ROLE]
        if t(se).get("active") is False:
            years = re.findall(r"(\d{4})", t(se).get("role_title") or "")
            left = int(years[-1]) if years else None
            for e in corp:
                if t(e).get("active") is False:
                    continue
                first = ((t(e).get("evidence_window") or {}).get("first_filing") or t(e).get("document_date") or "")[:4]
                after = f"; earliest filing on record for the seat: {first}" + (
                    f" ({int(first) - left} year(s) after the Senate service on record ended)" if left and first.isdigit() and int(first) >= left else "")
                signals.append({"type": "Legislature to boardroom", "subjects": [p, e["target"]],
                                "text": f"Congress.gov records {nodes[p]['label']} as a former U.S. Senator "
                                        f"({t(se).get('role_title')}); filings report a current "
                                        f"{e['relationship_type'].replace('_', ' ').lower()} seat at {nodes[e['target']]['label']}{after}.",
                                "evidence": [cite(se), cite(e)]})
        else:
            for s in seats[p]:
                cd = committee_doms(s["target"])
                for e in corp:
                    shared = cd & doms(e["target"])
                    if shared:
                        signals.append({"type": "Oversight overlap", "subjects": [p, s["target"], e["target"]],
                                        "text": f"The senate.gov roster lists {nodes[p]['label']} on the {nodes[s['target']]['label']} "
                                                f"({t(s).get('role_title') or 'Member'}); filings report "
                                                f"{'a current' if t(e).get('active') is not False else 'a past'} "
                                                f"{e['relationship_type'].replace('_', ' ').lower()} seat at {nodes[e['target']]['label']}, "
                                                f"in a sector ({', '.join(sorted(d[-1] for d in shared))}) within that committee's "
                                                f"jurisdiction (classification: {jur.get('basis', 'committee_domains.json')}).",
                                        "evidence": [cite(s), cite(e)]})

    # 3 lobbyist-lawmaker link
    sitting = {p: nodes[p]["label"] for p, se in senators.items() if t(se).get("active") is not False}
    by_last = defaultdict(list)
    for p, lab in sitting.items():
        by_last[lab.split()[-1].lower()].append(p)
    com_words = {"armed services": "SSAS00", "banking": "SSBK00", "finance committee": "SSFI00", "energy and natural": "SSEG00",
                 "commerce": "SSCM00", "help committee": "SSHR00", "health, education": "SSHR00", "agriculture": "SSAF00",
                 "foreign relations": "SSFR00", "intelligence": "SLIN00", "appropriations": "SSAP00",
                 "homeland security": "SSGA00", "judiciary": "SSJU00"}
    committee_node = {ccode(n): n for n in nodes if nodes[n].get("entity_type") == "LEGISLATIVE_COMMITTEE"}
    house_committees = [n for n in nodes if nodes[n].get("entity_type") == "LEGISLATIVE_COMMITTEE" and nodes[n]["label"].startswith("House ")]
    by_last_rep = defaultdict(list)
    names_of = lambda n: [n["label"]] + ((n.get("provenance") or {}).get("aliases_seen_in_sources") or [])
    for e in edges:
        if e["relationship_type"] == "REPRESENTATIVE_IN" and t(e).get("active") is not False:
            for nm in names_of(nodes[e["source"]]):
                by_last_rep[re.sub(r",?\s+(Jr\.?|Sr\.?|II|III|IV)$", "", nm).split()[-1].lower()].append(e["source"])
    for e in edges:
        if e["relationship_type"] != "LOBBYIST_AT":
            continue
        role = t(e).get("role_title") or ""
        m = re.match(r"lobbies for (.+?); former position \(disclosed\): (.+)$", role)
        if not m:
            continue
        client_label, cp = m.group(1), m.group(2)
        client = next((x for x in nodes if nodes[x]["label"] == client_label and nodes[x].get("entity_type") != "PERSON"), None)
        if not client:
            continue
        linked = []
        for title, kind, table in ((r"Sen\.|Senator", "senator", by_last),
                                   (r"Rep\.|Representative|Congressman|Congresswoman", "representative", by_last_rep)):
            for sm in re.finditer(rf"\b(?:{title})\s+((?:[A-Z][\w.'-]*\s+){{0,3}}[A-Z][\w'-]+)", cp):
                nm = [x for x in sm.group(1).split() if x.strip(".,") not in ("Jr", "Sr", "II", "III", "IV")]
                if not nm or nm[-1].lower().strip(".") in ("cmte", "committee", "comm", "subcommittee", "office", "staff"):
                    continue
                for p in dict.fromkeys(table.get(nm[-1].lower(), [])):
                    if len(nm) == 1 or any(_same_person(" ".join(nm), x) for x in names_of(nodes[p])):
                        linked.append((kind, p))
        low = cp.lower()
        if "senate" in low:
            for w_, code in com_words.items():
                if w_ in low and code in committee_node:
                    linked.append(("committee", committee_node[code]))
        if "house" in low and "committee" in low:
            for k in (jur.get("house_by_name") or {}):
                if k.lower() in low:
                    for c in house_committees:
                        if k.lower() in nodes[c]["label"].lower():
                            linked.append(("committee", c))
        for kind, x in dict.fromkeys(linked):
            coms = [s["target"] for s in seats[x]] if kind in ("senator", "representative") else [x]
            over = [c for c in coms if committee_doms(c) & doms(client)]
            if not over:
                continue
            who = nodes[e["source"]]["label"]
            signals.append({"type": "Lobbyist-lawmaker link", "subjects": [e["source"], x, client] + over[:2],
                            "text": f"A Lobbying Disclosure Act filing reports that {who}, who disclosed a former position "
                                    f"(\"{cp[:160]}\"), lobbies for {nodes[client]['label']} through {nodes[e['target']]['label']}. "
                                    + ({"senator": f"The senate.gov roster lists {nodes[x]['label']} on ",
                                        "representative": f"The House Clerk's list shows {nodes[x]['label']} on "}.get(kind, "That is "))
                                    + ", ".join(nodes[c]["label"] for c in over[:3])
                                    + f", whose jurisdiction covers {nodes[client]['label']}'s sector (classification: committee_domains.json).",
                            "evidence": [cite(e)] + [cite(s) for s in seats.get(x, []) if s["target"] in over][:2]})

    # 4 enforcement proximity (confirmed records only)
    confirmed = {i for i, s in stamps.items() if s.get("confirmed") and i in nodes}
    for a in confirmed:
        for d in in_e[a]:
            if d["relationship_type"] != "DIRECTOR_OF" or t(d).get("active") is False:
                continue
            for d2 in out_e[d["source"]]:
                b = d2["target"]
                if d2["relationship_type"] == "DIRECTOR_OF" and b != a and t(d2).get("active") is not False:
                    rec = stamps[a]["confirmed"][0]
                    signals.append({"type": "Enforcement proximity", "subjects": [b, d["source"], a],
                                    "text": f"Filings report {nodes[d['source']]['label']} as a current director of both "
                                            f"{nodes[b]['label']} and {nodes[a]['label']}. {nodes[a]['label']} has a public "
                                            f"enforcement record confirmed by the investigator ({rec['source']}, {rec['date']}: "
                                            f"\"{rec['title']}\").",
                                    "evidence": [cite(d), cite(d2), {"source": rec["source"], "url": rec["url"], "date": rec["date"]}]})

    seen, uniq = set(), []
    for s in signals:
        k = (s["type"], tuple(s["subjects"]))
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    pending = 0
    conf = os.path.join(paths.HOOT_NOTES, "Identity confirmations.md")
    if os.path.exists(conf):
        pending = len(re.findall(r"- \[ \]", open(conf, encoding="utf-8").read()))
    return {"signals": uniq, "counts": dict(sorted(((k, sum(1 for s in uniq if s["type"] == k)) for k in
                                                    {s["type"] for s in uniq}))),
            "pending_identity_confirmations": pending,
            "basis": "official records on the map only; signals for review, not findings"}


def cached(graph_path, load):
    m = os.path.getmtime(graph_path)
    if _cache.get("m") != m:
        _cache.update(m=m, res=compute(load()))
    return _cache["res"]


def for_entity(res, eid):
    return [s for s in res["signals"] if eid in s["subjects"]]
