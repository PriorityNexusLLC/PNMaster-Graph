"""Linking people named in filings that carry no ID (Form D, Form 990) to people HOOT already knows.

A name alone never establishes identity. A match is accepted as HIGH confidence only when the name
agrees AND a second, independent fact agrees:
    * city: the filing's address city equals the city on the person's own SEC insider filings, or
    * claim: one of the investigator's research documents claims this person holds a seat at this
      organization (two independent sources, the document and the statutory filing, agree).
Anything else is NAME_ONLY (kept as a labelled lead, excluded from loops and from the graph) or
UNLINKED (a new person record with no registry identifier).
"""
import glob
import json
import os
import re

from .util import norm_name, person_name_match

CLAIMS_DIR = os.path.join(os.path.expanduser("~"), "PriorityNexus", "data", "staging")
_claims = None
_people = None


def _org_norm(s):
    s = re.sub(r"\(.*?\)", " ", (s or "").lower()).replace("&", " and ").replace("'", "")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\b(inc|incorporated|corp|corporation|co|company|llc|ltd|plc|the|foundation|fund)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def claims_index():
    """(person name, org) pairs asserted in the investigator's documents (from boards/claims checks)."""
    global _claims
    if _claims is not None:
        return _claims
    pairs = []
    for f in glob.glob(os.path.join(CLAIMS_DIR, "boards_*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:                                  # noqa: BLE001
            continue
        for t in d.get("tables", []):
            for r in t.get("rows", []):
                pairs.append((r["person"], t["company_claimed"], os.path.basename(f)))
    for f in glob.glob(os.path.join(CLAIMS_DIR, "claims_*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:                                  # noqa: BLE001
            continue
        for row in d.get("rows", []):
            for c in row.get("claims", []):
                pairs.append((row["person_claimed"], c["org_claimed"], os.path.basename(f)))
    # the investigator's original graph: people named as the human bridge of an edge sit at both ends
    gp = os.path.join(os.path.dirname(CLAIMS_DIR), "priority_nexus_graph.json")
    if os.path.exists(gp):
        try:
            g = json.load(open(gp, encoding="utf-8"))
            labels = {n["id"]: n["label"] for n in g.get("nodes", [])}
            for e in g.get("edges", []):
                if (e.get("provenance") or {}).get("review"):     # only the investigator's own records
                    continue
                hb = re.sub(r"\(.*?\)", " ", e.get("human_bridge") or "")
                for person in re.findall(r"[A-Z][\w.'\-]*(?:\s+[A-Z][\w.'\-]*)*\s+[A-Z][\w'\-]+", hb):
                    for end in (e.get("source"), e.get("target")):
                        pairs.append((person, labels.get(end, ""), "priority_nexus_graph.json " + e.get("edge_id", "")))
        except Exception:                                  # noqa: BLE001
            pass
    _claims = pairs
    return pairs


def _claimed(name, org):
    on = _org_norm(org)
    for p, o, src in claims_index():
        oo = _org_norm(o)
        if person_name_match(name, p) and (oo == on or (len(oo.split()) >= 1 and len(on) > 3 and (oo in on or on in oo))):
            return src
    return None


def resolve_person(store, name, org_name, city=None, scope=""):
    """-> (entity id, {'confidence': 'high'|'name_only'|'unlinked', 'method': [...]})"""
    global _people
    if _people is None:                      # people with a registry ID, loaded once per run
        _people = [store.entity(r[0]) for r in store.db.execute(
            "SELECT e.id FROM entities e JOIN ids i ON i.entity_id=e.id WHERE e.type='person' AND i.scheme='sec_cik'")]
    cands = []
    for e in _people:
        m = person_name_match(name, e["name"]) or next((person_name_match(name, a) for a in e["aliases"] if person_name_match(name, a)), None)
        if m:
            cands.append((e, m))
    registry = [c for c in cands if c[0]["ids"].get("sec_cik")]
    pool = registry or cands
    if len(pool) == 1:
        e, how = pool[0]
        method = [f"name ({how})"]
        fc = (e["attrs"].get("filing_city") or "").split(",")[0].strip().lower()
        if city and fc and fc == city.strip().lower():
            method.append(f"city {city.title()} matches SEC insider filings")
        src = _claimed(e["name"], org_name) or _claimed(name, org_name)
        if src:
            method.append(f"claimed in your research ({src})")
        conf = "high" if len(method) >= 2 else "name_only"
        if how == "initial" and not src:              # an initial is too weak to pair with a city alone
            conf = "name_only"
        if conf == "name_only":
            # keep the statutory fact on a separate, unlinked record; note the possible match
            nid = store.upsert_entity("person", name, {"manual": f"{scope}:{norm_name(name)}"},
                                      {"possible_match": {"name": e["name"], "sec_cik": e["ids"].get("sec_cik"),
                                                          "why_not_linked": "name only; no second fact agrees"}})
            return nid, {"confidence": "name_only", "method": method, "possible_match": e["canonical_id"]}
        return e["id"], {"confidence": "high", "method": method}
    # same organization's own filings: "Rob Schutz" and "Robert Schutz" are one record, not two
    for eid, val in store.db.execute("SELECT entity_id, value FROM ids WHERE scheme='manual' AND value LIKE ?", (scope + ":%",)).fetchall():
        other = store.db.execute("SELECT name FROM entities WHERE id=?", (eid,)).fetchone()
        if other and person_name_match(name, other[0]):
            store.upsert_entity("person", other[0], {"manual": val}, aliases=[name])
            return eid, {"confidence": "unlinked", "method": ["no unique match in the map",
                                                             f"same person as '{other[0]}' in this organization's filings"]}
    nid = store.upsert_entity("person", name, {"manual": f"{scope}:{norm_name(name)}"})
    info = {"confidence": "unlinked", "method": ["no unique match"]}
    if len(pool) > 1:
        info["ambiguous_with"] = [c[0]["canonical_id"] for c in pool[:5]]
    return nid, info
