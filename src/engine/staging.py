"""Review batches (data/staging/*.json) -> master graph, one approved row at a time.

Every approved node/edge goes through the same validator as the manual forms. Rows that fail are
reported back, never forced in. Applying a batch twice is safe: duplicates are refused (409)."""
import json
import os
import re

from . import audit, store, validator

STAGING = os.path.join(store.DATA, "staging")
BATCH_RE = re.compile(r"^B[0-9T]{6,20}$")


def list_batches():
    if not os.path.isdir(STAGING):
        return []
    out = []
    for fn in sorted(os.listdir(STAGING), reverse=True):
        if fn.endswith(".json") and BATCH_RE.match(fn[:-5]):
            with open(os.path.join(STAGING, fn), encoding="utf-8") as f:
                b = json.load(f)
            out.append({"batch_id": b["batch_id"], "created_at": b["created_at"], "centers": b["centers"],
                        "edges": len(b["edges"]), "applied": len(b.get("applied", []))})
    return out


def load_batch(batch_id):
    if not BATCH_RE.match(batch_id or ""):
        raise validator.ValidationError([{"field": "batch_id", "message": "invalid batch id"}], 400)
    path = os.path.join(STAGING, batch_id + ".json")
    if not os.path.exists(path):
        raise validator.ValidationError([{"field": "batch_id", "message": "batch not found"}], 404)
    with open(path, encoding="utf-8") as f:
        return json.load(f), path


def apply(batch_id, decisions):
    """decisions = {"nodes": {key: {"domains": [...]}},
                    "edges": {key: {"approve": true, "conflict_weight": 0.9, "weight_basis": "..."}}}"""
    batch, path = load_batch(batch_id)
    gov = store.load_governance()
    graph = store.load_graph()
    nodes = {n["key"]: n for n in batch["nodes"]}
    results = {"nodes_added": [], "edges_added": [], "rejected": [], "already_present": []}
    approved = [e for e in batch["edges"] if (decisions.get("edges") or {}).get(e["key"], {}).get("approve")]

    for e in approved:
        for k in (e["source_key"], e["target_key"]):
            nd = nodes[k]
            if nd["status"] != "new" or any(n["id"] == nd["master_id"] for n in graph["nodes"]):
                continue
            nd_dec = (decisions.get("nodes") or {}).get(k) or {}
            doms = nd_dec.get("domains") or []
            review = decisions.get("review", "human")
            try:
                node = validator.validate_new_node({
                    "id": nd["master_id"], "label": nd["label"], "entity_type": nd["entity_type"], "domains": doms,
                    "identity": nd["identity"],
                    "provenance": {"created_via": "HOOT bridge " + ("autopilot" if review != "human" else "review"),
                                   "review": review, "domain_basis": nd_dec.get("basis") or "set by reviewer",
                                   "batch_id": batch_id, "hoot_key": k,
                                   "aliases_seen_in_sources": nd.get("aliases", [])[:10]}}, graph, gov)
            except validator.ValidationError as ex:
                results["rejected"].append({"row": f"node {nd['label']}", "errors": ex.errors})
                continue
            graph["nodes"].append(node)
            results["nodes_added"].append(node["id"])
            audit.log_event("node_recorded", node=node, batch=batch_id)

    key_to_id = {n["key"]: n["master_id"] for n in batch["nodes"]}
    present = {n["id"] for n in graph["nodes"]}
    for e in approved:
        d = decisions["edges"][e["key"]]
        src, tgt = key_to_id[e["source_key"]], key_to_id[e["target_key"]]
        if src not in present or tgt not in present:
            results["rejected"].append({"row": f"edge {e['source_label']} → {e['target_label']}",
                                        "errors": [{"field": "nodes", "message": "an endpoint was not added (see node errors)"}]})
            continue
        sup = dict(e["supplement"])
        basis = (d.get("weight_basis") or "").strip()
        sup["weight_basis"] = basis or ("reviewer accepted HOOT suggestion"
                                        if d.get("conflict_weight") == e["integrity"]["suggested_conflict_weight"]
                                        else "set by reviewer")
        sup["provenance"] = {**(sup.get("provenance") or {}), "batch_id": batch_id,
                             "review": decisions.get("review", "human")}
        payload = {"source": src, "target": tgt, "relationship_type": e["relationship_type"], "human_bridge": "",
                   "supersedes": d.get("supersedes"),
                   "transparency": e["transparency"],
                   "integrity": {"control_type": e["integrity"]["control_type"],
                                 "control_class": e["integrity"]["control_class"],
                                 "conflict_weight": d.get("conflict_weight")},
                   "supplement": sup}
        try:
            edge, sup_id, reason = validator.validate_new_edge(payload, graph, gov, store.next_edge_id(graph))
        except validator.ValidationError as ex:
            dup = ex.status == 409
            results["already_present" if dup else "rejected"].append(
                {"row": f"edge {e['source_label']} {e['relationship_type']} {e['target_label']}", "errors": ex.errors})
            continue
        if sup_id:
            old = next(x for x in graph["edges"] if x["edge_id"] == sup_id)
            old.setdefault("integrity", {}).update(superseded_by=edge["edge_id"],
                                                   superseded_at=edge["integrity"]["recorded_at"], supersede_reason=reason)
            results.setdefault("superseded", []).append(sup_id)
        graph["edges"].append(edge)
        results["edges_added"].append(edge["edge_id"])
        audit.log_event("edge_recorded", edge=edge, batch=batch_id)

    if results["nodes_added"] or results["edges_added"]:
        store.save_graph(graph)
        batch.setdefault("applied", []).append({"at": store.now_iso(), "review": decisions.get("review", "human"),
                                                "nodes_added": results["nodes_added"], "edges_added": results["edges_added"],
                                                "rejected": len(results["rejected"])})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(batch, f, indent=2, ensure_ascii=False)
    if results["rejected"]:
        audit.log_event("rejected", endpoint=f"staging/{batch_id}", errors=results["rejected"])
    return results
