"""Upgrade the investigator's original records with evidence gathered since, never by overwriting.

1. Identity: add official identifiers (GLEIF LEI, IRS EIN) to entities that lacked or misstated them.
   Existing identifiers are never changed; a contradicted one gets a recorded note.
2. Connections: when SEC/IRS filings show the named human bridge holding a role at BOTH ends of an
   original edge, a new edge with full citations SUPERSEDES it. The investigator's own control type and
   conflict weight are carried over unchanged (they were judgments, not facts).
3. Statutes: edges whose stated source is a statute get a structured statutory citation.
Every change is logged; superseded records stay in the file."""
import json
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import paths  # noqa: E402
sys.path.insert(0, paths.HOOT_DIR)
from engine import audit, store, validator  # noqa: E402
from hootlib.util import person_name_match  # noqa: E402

IDENTITY = [   # (node, key, value, evidence)
    ("ENT_AIRBUS", "gleif_lei", "MINO79WLOO247M1IL051", "https://search.gleif.org/#/record/MINO79WLOO247M1IL051"),
    ("ENT_ARAMCO", "gleif_lei", "5586006WD91QHB7J4X50", "https://search.gleif.org/#/record/5586006WD91QHB7J4X50"),
    ("ENT_OPENAI", "irs_ein", "81-0861541", "https://projects.propublica.org/nonprofits/organizations/810861541"),
    ("ENT_IQT", "irs_ein", "52-2149962", "https://projects.propublica.org/nonprofits/organizations/522149962"),
]
NOTES = {"ENT_IQT": "canonical_id US-EIN-52-2148698 (from the original file) does not exist in IRS data; In-Q-Tel Inc's "
                    "EIN is 52-2149962 (IRS e-file, via ProPublica). The original value is kept unchanged, as recorded."}
STATUTES = {"EDGE_011": ("50 U.S.C. § 4565", "https://www.law.cornell.edu/uscode/text/50/4565",
                         store.now_iso()[:10],    # date the current statutory text was retrieved and checked
                         "statute text: \"The Secretary of the Treasury shall serve as the chairperson of the Committee.\" "
                         "(current text checked on the date recorded)")}


CONSOLIDATE = {"ENT_OPENAI_INC": "ENT_OPENAI", "ENT_IN_Q_TEL_INC": "ENT_IQT"}   # automatic duplicate -> investigator's original


def consolidate(g, gov, nodes, dup_id, target_id, reason=None):
    """Fold an automatic duplicate into the investigator's original node: mark it merged, re-point its
    connections through superseding records. Nothing is deleted."""
    dup = nodes.get(dup_id)
    if not dup or (dup.get("provenance") or {}).get("merged_into") or not (dup.get("provenance") or {}).get("review"):
        return []
    reason = reason or f"same entity as {target_id} (shared official identifier {dup.get('identity')})"
    dup["provenance"].update(merged_into=target_id, merged_at=store.now_iso(), merged_reason=reason)
    audit.log_event("node_consolidated", node_id=dup_id, into=target_id, reason=reason)
    out = [f"consolidated {dup_id} into {target_id}"]
    for e in [x for x in g["edges"] if dup_id in (x["source"], x["target"]) and not (x.get("integrity") or {}).get("superseded_by")]:
        t, i = e["transparency"], e["integrity"]
        payload = {"source": target_id if e["source"] == dup_id else e["source"],
                   "target": target_id if e["target"] == dup_id else e["target"],
                   "relationship_type": e["relationship_type"], "human_bridge": e.get("human_bridge", ""),
                   "transparency": {k: t.get(k) for k in ("verification_source", "source_reference", "citation_url",
                                                          "document_date", "active", "tenure_start", "tenure_end")},
                   "integrity": {k: i.get(k) for k in ("control_type", "control_class", "conflict_weight")},
                   "supersedes": {"edge_id": e["edge_id"], "reason": f"endpoint consolidated: {reason}"},
                   "supplement": {**{k: t.get(k) for k in ("role_title", "percent_of_class", "evidence_window",
                                                           "evidence_trail", "source_sha256", "activity_basis")},
                                  "weight_basis": i.get("weight_basis"),
                                  "provenance": {**(e.get("provenance") or {}), "consolidated_from": dup_id}}}
        out += apply(g, gov, payload)
    return out


def main(dry=False):
    g, gov = store.load_graph(), store.load_governance()
    nodes = {n["id"]: n for n in g["nodes"]}
    changes = []
    for dup, target in CONSOLIDATE.items():
        changes += consolidate(g, gov, nodes, dup, target)

    for nid, key, val, url in IDENTITY:
        if nid not in nodes or (nodes[nid].get("identity") or {}).get(key) == val:
            continue
        try:
            node, ids = validator.validate_identity_addition(nid, {key: val}, g, gov)
        except validator.ValidationError as ex:
            changes.append(f"identity {nid}: not added ({ex})")
            continue
        node.setdefault("identity", {}).update(ids)
        node.setdefault("provenance", {}).setdefault("identity_additions", []).append(
            {"added": ids, "at": store.now_iso(), "evidence_url": url, "by": "upgrade_legacy"})
        if nid in NOTES:
            node["provenance"].setdefault("identity_notes", []).append({"at": store.now_iso(), "note": NOTES[nid]})
        audit.log_event("identity_added", node_id=nid, identity=ids, evidence_url=url, note=NOTES.get(nid))
        changes.append(f"identity {nid}: added {key} {val}")

    persons = [n for n in g["nodes"] if n.get("entity_type") == "PERSON"]
    roles = defaultdict(dict)
    for e in g["edges"]:
        if not (e.get("integrity") or {}).get("superseded_by") and e["relationship_type"] in ("DIRECTOR_OF", "OFFICER_OF"):
            prev = roles[e["source"]].get(e["target"])
            if not prev or (e["transparency"].get("document_date") or "") > (prev["transparency"].get("document_date") or ""):
                roles[e["source"]][e["target"]] = e

    legacy = [e for e in g["edges"] if not (e.get("provenance") or {}).get("review")
              and not (e.get("integrity") or {}).get("superseded_by") and not (e.get("transparency") or {}).get("citation_url")]
    for e in legacy:
        if e["edge_id"] in STATUTES:
            ref, url, date, why = STATUTES[e["edge_id"]]
            payload = {"source": e["source"], "target": e["target"], "relationship_type": e["relationship_type"],
                       "human_bridge": e.get("human_bridge", ""),
                       "transparency": {"verification_source": "Statute", "source_reference": ref, "citation_url": url,
                                        "document_date": date, "active": bool(e["transparency"].get("active", True))},
                       "integrity": {"control_type": e["integrity"]["control_type"], "control_class": "hard",
                                     "conflict_weight": e["integrity"]["conflict_weight"]},
                       "supersedes": {"edge_id": e["edge_id"], "reason": f"upgraded with a structured statutory citation: {why}"},
                       "supplement": {"provenance": {"via": "upgrade_legacy", "review": "automatic (statute citation)"},
                                      "weight_basis": "carried over from the investigator's original record"}}
            changes += apply(g, gov, payload)
            continue
        hb = re.sub(r"\(.*?\)", " ", e.get("human_bridge") or "")
        names = re.findall(r"[A-Z][\w.'\-]*(?:\s+[A-Z][\w.'\-]*)*\s+[A-Z][\w'\-]+", hb)
        for nm in names:
            for p in persons:
                if not person_name_match(nm, p["label"]):
                    continue
                a, b = roles[p["id"]].get(e["source"]), roles[p["id"]].get(e["target"])
                if not (a and b):
                    continue
                newest = max((a, b), key=lambda x: x["transparency"].get("document_date") or "")
                t = newest["transparency"]
                both_active = bool(a["transparency"]["active"] and b["transparency"]["active"])
                trail = [{"date": x["transparency"].get("document_date"), "url": x["transparency"].get("citation_url"),
                          "edge": x["edge_id"], "role": f"{x['relationship_type']} {nodes[x['target']]['label']}"} for x in (a, b)]
                payload = {"source": e["source"], "target": e["target"], "relationship_type": e["relationship_type"],
                           "human_bridge": f"{p['label']} ({a['relationship_type'].lower()} at {nodes[e['source']]['label']}; "
                                           f"{b['relationship_type'].lower()} at {nodes[e['target']]['label']})",
                           "transparency": {"verification_source": t["verification_source"], "source_reference": t["source_reference"],
                                            "citation_url": t["citation_url"], "document_date": t["document_date"], "active": both_active},
                           "integrity": {"control_type": e["integrity"]["control_type"], "control_class": "hard",
                                         "conflict_weight": e["integrity"]["conflict_weight"]},
                           "supersedes": {"edge_id": e["edge_id"], "reason":
                                          f"corroborated by filings: {p['label']} holds roles at both ends ({a['edge_id']}, {b['edge_id']})"
                                          + ("" if both_active else "; at least one role is historical")},
                           "supplement": {"evidence_trail": trail, "provenance": {"via": "upgrade_legacy", "person": p["id"],
                                                                                 "review": "automatic (corroboration)"},
                                          "weight_basis": "carried over from the investigator's original record",
                                          "activity_basis": "active only if both roles are active"}}
                changes += apply(g, gov, payload)
                break
            else:
                continue
            break
    if not dry and changes:
        store.save_graph(g)
    for c in changes:
        print(" ", c.encode("ascii", "replace").decode())
    return changes


def apply(g, gov, payload):
    try:
        edge, sup_id, reason = validator.validate_new_edge(payload, g, gov, store.next_edge_id(g))
    except validator.ValidationError as ex:
        return [f"{payload['supersedes']['edge_id']}: not upgraded ({ex})"]
    old = next(x for x in g["edges"] if x["edge_id"] == sup_id)
    old["integrity"].update(superseded_by=edge["edge_id"], superseded_at=edge["integrity"]["recorded_at"], supersede_reason=reason)
    g["edges"].append(edge)
    audit.log_event("edge_recorded", edge=edge, superseded=sup_id, reason=reason)
    return [f"{sup_id} → {edge['edge_id']}: {reason}"]


if __name__ == "__main__":
    main(dry="--dry" in sys.argv)
