"""Shared helpers for connectors that write official records into the master graph. Every write goes through
the validator, is logged in the hash-chained audit log, and never overwrites: a newer filing supersedes the
older record, which stays in the file."""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import audit, store, validator  # noqa: E402

LEGAL = r"(inc|incorporated|corp|corporation|co|company|llc|l\.l\.c|lp|l\.p|ltd|limited|plc|n\.a|sa|ag|nv|se|the)"


def org_key(label):
    s = (label or "").lower().replace("&", " and ")
    s = re.sub(r"\(.*?\)|/.*$", " ", s)
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(rf"\b{LEGAL}\b", " ", s)
    s = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()
    words = s.split()
    if len(words) >= 3 and words[-1] in ("group", "holdings", "holding"):
        s = " ".join(words[:-1])
    return s


class Writer:
    def __init__(self, via, review):
        self.g, self.gov = store.load_graph(), store.load_governance()
        self.via, self.review = via, review
        self.by_cid = {(n.get("identity") or {}).get("canonical_id"): n["id"] for n in self.g["nodes"]
                       if (n.get("identity") or {}).get("canonical_id")}
        self.ids = {n["id"] for n in self.g["nodes"]}
        self.counts = {"nodes": 0, "edges": 0, "updated": 0, "unchanged": 0}

    def node(self, label, etype, cid, domains, provenance=None):
        if cid in self.by_cid:
            return self.by_cid[cid]
        base = "ENT_" + re.sub(r"[^A-Z0-9]+", "_", label.upper()).strip("_")[:50]
        nid, k = base, 2
        while nid in self.ids:
            nid = f"{base[:56]}_{k}"
            k += 1
        n = validator.validate_new_node({"id": nid, "label": label, "entity_type": etype, "domains": domains,
                                         "identity": {"canonical_id": cid},
                                         "provenance": {"created_via": self.via, "review": self.review, **(provenance or {})}},
                                        self.g, self.gov)
        self.g["nodes"].append(n)
        self.by_cid[cid] = nid
        self.ids.add(nid)
        audit.log_event("node_recorded", node=n)
        self.counts["nodes"] += 1
        return nid

    def edge(self, src, tgt, rtype, transparency, weight, ctype, cclass="soft", supplement=None):
        live = next((e for e in self.g["edges"] if e["source"] == src and e["target"] == tgt
                     and e["relationship_type"] == rtype and not (e.get("integrity") or {}).get("superseded_by")), None)
        payload = {"source": src, "target": tgt, "relationship_type": rtype, "human_bridge": "",
                   "transparency": transparency,
                   "integrity": {"control_type": ctype, "control_class": cclass, "conflict_weight": weight},
                   "supplement": {"weight_basis": "automatic default by relation type (not a Priority Nexus threshold); not reviewed",
                                  "provenance": {"via": self.via, "review": self.review}, **(supplement or {})}}
        if live:
            old_date = (live.get("transparency") or {}).get("document_date") or ""
            if (transparency.get("document_date") or "") <= old_date:
                self.counts["unchanged"] += 1
                return live
            payload["supersedes"] = {"edge_id": live["edge_id"], "reason": f"newer filing dated {transparency['document_date']}"}
        e, sup, reason = validator.validate_new_edge(payload, self.g, self.gov, store.next_edge_id(self.g))
        if sup:
            old = next(x for x in self.g["edges"] if x["edge_id"] == sup)
            old["integrity"].update(superseded_by=e["edge_id"], superseded_at=e["integrity"]["recorded_at"], supersede_reason=reason)
            self.counts["updated"] += 1
        else:
            self.counts["edges"] += 1
        self.g["edges"].append(e)
        audit.log_event("edge_recorded", edge=e, superseded=sup, reason=reason or None)
        return e

    def save(self):
        if self.counts["nodes"] or self.counts["edges"] or self.counts["updated"]:
            store.save_graph(self.g)
