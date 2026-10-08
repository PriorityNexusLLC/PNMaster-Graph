"""Build tests/public_fixture_graph.json: the investigator's ORIGINAL graph, reduced to what official records confirm.
It is the worked example of the three cores published with the code:

  Identity      the original entities, each with the official identifiers verified since (identity additions)
  Transparency  only the original connections that filings or statutes confirmed, in their sourced form
                (each with its citation); unconfirmed originals and unsourced leadership lists are left out
  Integrity     every connection keeps its fingerprint; the file records what was left out and why

    python -I src/maintenance/build_public_fixture.py
"""
import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import store, validator  # noqa: E402

ORIGINAL = os.path.join(ROOT, "tests", "fixture_graph.json")
OUT = os.path.join(ROOT, "tests", "public_fixture_graph.json")


def main():
    orig, master = store.load_json(ORIGINAL), store.load_graph()
    gov = store.load_governance()
    E = {e["edge_id"]: e for e in master["edges"]}
    N = {n["id"]: n for n in master["nodes"]}
    kept, left_out, confirmed_from = [], [], {}
    for e in orig["edges"]:
        cur = E.get(e["edge_id"])
        while cur and (cur.get("integrity") or {}).get("superseded_by"):
            cur = E.get(cur["integrity"]["superseded_by"])
        if cur and cur is not E.get(e["edge_id"]) and (cur.get("transparency") or {}).get("citation_url"):
            kept.append(copy.deepcopy(cur))                  # exactly as recorded: its fingerprint must still match
            confirmed_from[cur["edge_id"]] = e["edge_id"]
        else:
            left_out.append({"edge_id": e["edge_id"], "source": e["source"], "target": e["target"],
                             "relationship_type": e["relationship_type"],
                             "why": "no primary-source confirmation found in filings or statutes yet"})
    nodes = []
    for n in orig["nodes"]:
        m = copy.deepcopy(N.get(n["id"], n))
        m["leadership"] = []                                  # unsourced lists stay out of the public example
        prov = m.get("provenance") or {}
        m["provenance"] = {k: v for k, v in prov.items() if k in ("identity_additions", "identity_notes", "created_via",
                                                                  "corrections")}
        nodes.append(m)
    ids = {n["id"] for n in nodes}
    kept = [e for e in kept if e["source"] in ids and e["target"] in ids]
    fx = {"schema_metadata": {**orig.get("schema_metadata", {}),
                              "public_example": "The investigator's original graph (37 entities, 15 connections), reduced to "
                                                "what official records confirm: the three cores applied to real data.",
                              "confirmed_from_original": confirmed_from,
                              "left_out": left_out,
                              "leadership_lists": "removed: they carried no source citations"},
          "nodes": nodes, "edges": kept}
    errors = [i for i in validator.audit_graph(fx, gov) if i["severity"] == "error"]
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(fx, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"public fixture: {len(nodes)} entities · {len(kept)} confirmed connections · {len(left_out)} left out · "
          f"{len(errors)} integrity errors")
    for i in errors:
        print(f"  {i['core']} {i['id']}: {i['message'][:120]}")


if __name__ == "__main__":
    main()
