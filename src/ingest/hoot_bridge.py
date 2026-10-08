"""HOOT -> Priority Nexus bridge.

Stages primary-source connections from HOOT's evidence store (Obsidian vault, .hoot/graph.db) as a
*review batch*. Nothing reaches data/priority_nexus_graph.json until a person assigns domains and
conflict weights and approves each edge; then every row still passes the normal validator.

Integrity rules this module enforces:
  * Identity: entities are matched to the master graph by sec_cik / gleif_lei / doi only, never by
    name. Entities with no exportable identifier are reported as blocked, not given a made-up one.
  * Transparency: only statutory/primary sources are staged (SEC Forms 3/4/5, Schedule 13D/13G,
    13F-HR, NIH RePORTER). LittleSis/OpenAlex/manual ties become *leads*, never verified edges.
    Source types are mapped literally; nothing is relabelled to fit the allowed list.
  * Integrity: HOOT's weights are passed as *suggestions* only. Domains are never guessed for
    organizations. Existing master edges are never modified; HOOT evidence that supports one is
    reported as a corroboration.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import paths  # noqa: E402

from engine import store  # noqa: E402

DEFAULT_HOOT_DB = paths.HOOT_DB
STAGING = os.path.join(ROOT, "data", "staging")
ACTIVE_WINDOW_DAYS = 456          # ~15 months: directors/officers normally file at least yearly

ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")

# HOOT relation -> (relationship_type, control_type)
REL = {
    "director_of": ("DIRECTOR_OF", "FIDUCIARY_BOARD_SEAT"),
    "officer_of": ("OFFICER_OF", "EXECUTIVE_OFFICER"),
    "ten_percent_owner_of": ("TEN_PERCENT_OWNER_OF", "VOTING_EQUITY"),
    "beneficial_owner_of": ("BENEFICIAL_OWNER_OF", "VOTING_EQUITY"),
    "holds_shares_of": ("INSTITUTIONAL_HOLDING_IN", "PASSIVE_EQUITY"),
    "principal_investigator_of": ("PRINCIPAL_INVESTIGATOR_OF", "RESEARCH_GRANT"),
    "grant_awarded_to": ("GRANT_AWARDED_TO", "RESEARCH_GRANT"),
    "funded_by": ("FUNDED_BY", "RESEARCH_GRANT"),
}


def source_type(hoot_source):
    s = (hoot_source or "").upper()
    if s.startswith("SEC FORM D"):
        return "Form D"
    if s.startswith("IRS FORM 990"):
        return "Form 990"
    if s.startswith("UK COMPANIES HOUSE"):
        return "UK Companies House"
    if re.match(r"^SEC FORM [345](/A)?$", s):
        return "Form 3/4/5"
    if s.startswith("SEC SCHEDULE 13D") or s.startswith("SEC SCHEDULE 13G"):
        return "Schedule 13D/13G"
    if s.startswith("SEC 13F-HR"):
        return "13F-HR"
    if s.startswith("NIH REPORTER"):
        return "NIH RePORTER Grant ID"
    return None                                   # not a primary source -> lead


def open_hoot(path):
    if not os.path.exists(path):
        raise SystemExit(f"HOOT database not found at {path}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def hoot_entities(db):
    ents = {}
    for r in db.execute("SELECT id, type, name, canonical_id, attrs FROM entities"):
        ents[r["id"]] = dict(r, attrs=json.loads(r["attrs"] or "{}"), ids={})
    for r in db.execute("SELECT scheme, value, entity_id FROM ids"):
        if r["entity_id"] in ents:
            ents[r["entity_id"]]["ids"][r["scheme"]] = r["value"]
    return ents


def export_identity(ent):
    """Only real registry identifiers. Returns {} when none exist (entity is then blocked)."""
    ids, out = ent["ids"], {}
    if ids.get("sec_cik"):
        out["sec_cik"] = ids["sec_cik"].zfill(10)
    if ids.get("lei"):
        out["gleif_lei"] = ids["lei"]
    if ids.get("doi"):
        out["doi"] = ids["doi"]
    if not out and ids.get("uk_company"):              # UK Companies House company number
        out["canonical_id"] = f"UKCH:{ids['uk_company']}"
    if not out and ids.get("uk_officer"):              # UK Companies House officer ID
        out["canonical_id"] = f"UKCH-OFFICER:{ids['uk_officer'].upper()}"
        out["uk_officer_id"] = ids["uk_officer"]            # exact, case-sensitive registry ID
    if ids.get("ein"):                                  # IRS Employer Identification Number (nonprofits)
        out["irs_ein"] = f"{ids['ein'][:2]}-{ids['ein'][2:]}"
    if not out:
        if ids.get("orcid"):
            out["canonical_id"] = "ORCID:" + ids["orcid"].upper()
        elif ids.get("nih_project"):
            out["canonical_id"] = "NIH-PROJECT:" + ids["nih_project"].upper()
        elif ids.get("nih_pi"):
            out["canonical_id"] = "NIH-PI:" + str(ids["nih_pi"])
    return out


def master_index(graph):
    idx = {}
    merged = {n["id"]: n["provenance"]["merged_into"] for n in graph["nodes"] if (n.get("provenance") or {}).get("merged_into")}
    for n in sorted(graph["nodes"], key=lambda n: n["id"] in merged):      # originals win over consolidated duplicates
        for k in ("sec_cik", "gleif_lei", "doi", "canonical_id", "irs_ein"):
            v = (n.get("identity") or {}).get(k)
            if v:
                idx.setdefault((k, str(v)), merged.get(n["id"], n["id"]))   # a duplicate's IDs point to its original
    return idx


def slug_id(label, taken, ident):
    base = "ENT_" + re.sub(r"[^A-Z0-9]+", "_", label.upper()).strip("_")[:44]
    cand = base
    if cand in taken:
        tail = re.sub(r"\D", "", next(iter(ident.values()), ""))[-4:] or "X"
        cand = f"{base}_{tail}"
    n = 2
    while cand in taken:
        cand = f"{base}_{n}"
        n += 1
    taken.add(cand)
    return cand


def stage(hoot_db, centers, hops=2, out_dir=STAGING):
    db = open_hoot(hoot_db)
    ents = hoot_entities(db)
    edges = [dict(r, attrs=json.loads(r["attrs"] or "{}")) for r in db.execute("SELECT * FROM edges")]
    graph = store.load_graph()
    midx = master_index(graph)
    by_label = {n["id"]: n for n in graph["nodes"]}
    taken = set(by_label)

    # resolve centers: master node ids or HOOT canonical ids, matched through identifiers only
    start = set()
    for c in centers:
        if c in by_label:
            ident = by_label[c].get("identity") or {}
            for k, scheme in (("sec_cik", "sec_cik"), ("gleif_lei", "lei"), ("doi", "doi")):
                if ident.get(k):
                    r = db.execute("SELECT entity_id FROM ids WHERE scheme=? AND value=?", (scheme, ident[k])).fetchone()
                    if r:
                        start.add(r[0])
        else:
            r = db.execute("SELECT id FROM entities WHERE canonical_id=?", (c,)).fetchone()
            if r:
                start.add(r[0])
        if not start:
            raise SystemExit(f"{c}: no HOOT entity shares an identifier with it (ingest it in HOOT first)")

    adj = {}
    for e in edges:
        adj.setdefault(e["src"], []).append(e)
        adj.setdefault(e["dst"], []).append(e)
    if not centers:                                # "everything": every entity, every primary edge
        start = set(ents)
        hops = 1
    dist = {s: 0 for s in start}
    frontier = list(start) if centers else []
    for h in range(1, hops + 1):
        nxt = []
        for u in frontier:
            for e in adj.get(u, []):
                v = e["dst"] if e["src"] == u else e["src"]
                if v not in dist:
                    dist[v] = h
                    nxt.append(v)
        frontier = nxt
    scope_edges = [e for e in edges if e["src"] in dist and e["dst"] in dist
                   and min(dist[e["src"]], dist[e["dst"]]) < hops]

    batch = {"batch_id": "B" + store.now_iso().replace(":", "").replace("-", "")[:15], "created_at": store.now_iso(),
             "hoot_db": hoot_db, "centers": centers, "hops": hops,
             "instructions": "Assign domains to every NEW node used by an edge you approve, set each approved edge's "
                             "conflict_weight (or accept the suggestion), then approve in the app's Review tab.",
             "nodes": {}, "edges": [], "corroborations": [], "leads": [], "blocked": []}
    today = date.today()

    def node_entry(hid):
        if hid in batch["nodes"]:
            return batch["nodes"][hid]
        ent = ents[hid]
        ident = export_identity(ent)
        if not ident:
            return None
        existing = next((midx[(k, v)] for k, v in ident.items() if (k, v) in midx), None)
        etype = {"person": "PERSON", "grant": "RESEARCH_GRANT", "work": "SCIENTIFIC_STUDY"}.get(ent["type"], "ORGANIZATION")
        if ent["type"] == "organization" and ident.get("sec_cik") and ent["attrs"].get("industry"):
            etype = "CORPORATION"
        if ent["attrs"].get("private_issuer"):
            etype = "PRIVATE_COMPANY"
        if ent["attrs"].get("nonprofit"):
            etype = "NONPROFIT"
        nd = {"key": ent["canonical_id"], "status": "existing" if existing else "new",
              "master_id": existing or slug_id(ent["name"], taken, ident), "label": ent["name"],
              "entity_type": etype, "identity": ident, "domains": [], "suggested_domains": [],
              "info": {k: ent["attrs"].get(k) for k in ("sic", "industry", "ntee_code", "private_issuer", "nonprofit",
                                                        "jurisdiction", "state_of_incorporation", "tickers")
                       if ent["attrs"].get(k)},
              "aliases": [a[0] for a in db.execute("SELECT alias FROM aliases WHERE entity_id=?", (hid,))]}
        if existing:
            nd["domains"] = list(by_label[existing].get("domains") or [])
        batch["nodes"][hid] = nd
        return nd

    for e in scope_edges:
        st = source_type(e["source"])
        a, b = ents[e["src"]], ents[e["dst"]]
        desc = f"{a['name']} {e['relation']} {b['name']}"
        if not st or e["relation"] not in REL:
            batch["leads"].append({"description": desc, "source": e["source"], "evidence_url": e["evidence_url"],
                                   "why_not_verified": "secondary/aggregator source; confirm against a primary document"})
            continue
        conf = (e["attrs"].get("identity_match") or {}).get("confidence")
        if conf == "name_only":
            batch["leads"].append({"description": desc, "source": e["source"], "evidence_url": e["evidence_url"],
                                   "why_not_verified": "person matched by name only; no second fact agrees"})
            continue
        na, nb = node_entry(e["src"]), node_entry(e["dst"])
        if not na or not nb:
            missing = [x["name"] for x, n in ((a, na), (b, nb)) if not n]
            batch["blocked"].append({"description": desc, "reason": f"no registry identifier for {', '.join(missing)}"})
            continue
        acc = ACCESSION.search(e["evidence_url"] or "")
        ref = acc.group(0) if acc else None
        if st in ("Form 990", "UK Companies House"):
            ref = e["attrs"].get("reference")
        if st == "NIH RePORTER Grant ID":
            ref = (ents[e["src"]]["ids"].get("nih_project") or ents[e["dst"]]["ids"].get("nih_project") or "").upper() or None
        if not ref:
            batch["blocked"].append({"description": desc, "reason": "evidence URL carries no filing/grant reference"})
            continue
        rtype, ctype = REL[e["relation"]]
        last = e["last_seen"] or e["filing_date"]
        if e["is_current"] == 0 or (e["percent"] is not None and e["percent"] == 0):
            active, basis = False, "filing reports the position ended / 0% ownership"
        elif last and date.fromisoformat(last) < today - timedelta(days=ACTIVE_WINDOW_DAYS):
            active, basis = False, f"no {st} filing since {last}; treated as historical until a newer filing shows it"
        else:
            active, basis = True, f"most recent {st} filing {last}"
        ev = e["attrs"].get("evidence", [])
        batch["edges"].append({
            "key": e["id"], "source_key": na["key"], "target_key": nb["key"],
            "source_label": a["name"], "target_label": b["name"],
            "relationship_type": rtype,
            "transparency": {"verification_source": st, "source_reference": ref, "citation_url": e["evidence_url"],
                             "document_date": e["filing_date"], "active": active},
            "supplement": {
                "role_title": e["qualifier"] if e["relation"] == "officer_of" else None,
                "percent_of_class": e["percent"],
                "evidence_window": {"first_filing": e["first_seen"], "last_filing": e["last_seen"],
                                    "filings_seen": len(ev),
                                    "note": "dates of filings that report the role, not appointment dates"},
                "evidence_trail": [{"date": x.get("date"), "url": x.get("url"), "sha256": x.get("sha256")} for x in ev][-12:],
                "source_sha256": e["source_sha256"], "activity_basis": basis,
                "provenance": {"via": "HOOT bridge", "hoot_edge": e["id"], "hoot_verification_hash": e["verification_hash"]}},
            "integrity": {"control_type": ctype, "control_class": e["control"],
                          "suggested_conflict_weight": round(float(e["conflict_weight"]), 2),
                          "suggestion_basis": "HOOT default by relation type (halved if former, scaled by % owned); not a Priority Nexus threshold"},
            "decision": {"approve": False, "conflict_weight": None},
        })

    # suggested domains for people: the domains of the master organizations they hold roles at
    org_domains = {}
    for ed in batch["edges"]:
        for k_from, k_to in ((ed["source_key"], ed["target_key"]), (ed["target_key"], ed["source_key"])):
            nd = next(n for n in batch["nodes"].values() if n["key"] == k_to)
            if nd["status"] == "existing":
                org_domains.setdefault(k_from, set()).update(nd["domains"])
    for nd in batch["nodes"].values():
        if nd["status"] == "new" and nd["entity_type"] == "PERSON" and org_domains.get(nd["key"]):
            doms = sorted(d for d in org_domains[nd["key"]] if d.startswith("DOM_"))
            if len(doms) >= 2:
                doms.append("BRIDGE_HUMAN")
            nd["suggested_domains"] = doms
            nd["suggestion_basis"] = "domains of the already-classified organizations this person files for"

    # organizations: the investigator's own earlier classification, if any (labelled, never auto-applied)
    ud_path = os.path.join(ROOT, "data", "staging", "user_domains.json")
    if os.path.exists(ud_path):
        sys.path.insert(0, os.path.join(ROOT, "src", "ingest"))
        from claims import org_key
        with open(ud_path, encoding="utf-8") as f:
            ud = json.load(f)
        for nd in batch["nodes"].values():
            if nd["status"] == "new" and nd["entity_type"] != "PERSON":
                hit = next((ud["entities"][k] for k in [org_key(nd["label"])] + [org_key(a) for a in nd.get("aliases", [])]
                            if k in ud["entities"]), None)
                if hit:
                    nd["suggested_domains"] = hit["domains"]
                    nd["suggestion_basis"] = f"your {ud['source']} ({', '.join(hit['where'][:2])})"

    # corroborate existing master interlocks: a person with filed roles at both endpoints
    roles = {}
    for ed in batch["edges"]:
        if ed["relationship_type"] in ("DIRECTOR_OF", "OFFICER_OF"):
            roles.setdefault(ed["source_key"], []).append(ed)
    key_to_master = {n["key"]: n["master_id"] for n in batch["nodes"].values()}
    for me in graph["edges"]:
        for pk, eds in roles.items():
            tgts = {key_to_master[x["target_key"]]: x for x in eds}
            if me["source"] in tgts and me["target"] in tgts:
                pa, pb = tgts[me["source"]], tgts[me["target"]]
                batch["corroborations"].append({
                    "master_edge": me["edge_id"], "person": pa["source_label"],
                    "evidence": [{"org": x["target_label"], "role": x["relationship_type"], "active": x["transparency"]["active"],
                                  "filing": x["transparency"]["citation_url"], "date": x["transparency"]["document_date"]}
                                 for x in (pa, pb)],
                    "note": "independent SEC evidence for this interlock; the master edge itself is not modified"})

    batch["nodes"] = list(batch["nodes"].values())
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{batch['batch_id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(batch, f, indent=2, ensure_ascii=False)
    return path, batch


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("centers", nargs="*", help="master node ids (e.g. ENT_MCDONALDS) or HOOT canonical ids; none = everything")
    ap.add_argument("--hops", type=int, default=2)
    ap.add_argument("--hoot-db", default=DEFAULT_HOOT_DB)
    a = ap.parse_args()
    path, b = stage(a.hoot_db, a.centers, a.hops)
    new = sum(1 for n in b["nodes"] if n["status"] == "new")
    print(f"staged {path}\n  {len(b['edges'])} primary-source edges · {new} new / {len(b['nodes']) - new} existing entities"
          f"\n  {len(b['corroborations'])} corroborations of existing edges · {len(b['leads'])} leads · {len(b['blocked'])} blocked")


if __name__ == "__main__":
    main()
