"""Keep the master graph's identities in step with HOOT's identity work. Additive and logged; never silent.

1. Also-known-as: every name a source has used for an entity (Margo H. Georgiadis, J.W. Marriott, Jr., ...) is
   recorded on the node under provenance.aliases_seen_in_sources, so searches find the host record by any of them.
   Names are only ever added.
2. Corrections: an automatic node filed as a PERSON whose HOOT record (same official ID) is now an organization
   is corrected, with the old values kept in provenance.corrections. Investigator-made nodes are never touched.
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import paths  # noqa: E402
from engine import audit, store  # noqa: E402

HOOT_DB = paths.HOOT_DB


def _key(s):
    return " ".join(sorted(re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()))


def useful_aliases(label, aliases):
    """Drop aliases that are only the label reordered or re-punctuated (SEC writes 'GEORGIADIS MARY M')."""
    seen, out = {_key(label)}, []
    for a in aliases:
        k = _key(a)
        if a and k not in seen:
            seen.add(k)
            out.append(a)
    return out


def main(dry=False):
    db = sqlite3.connect(f"file:{HOOT_DB}?mode=ro", uri=True)
    by_cik = {}
    for eid, etype, name, cik in db.execute(
            "SELECT e.id, e.type, e.name, i.value FROM entities e JOIN ids i ON i.entity_id=e.id AND i.scheme='sec_cik'"):
        by_cik[cik.zfill(10)] = (eid, etype, name)
    aliases = {}
    for eid, al in db.execute("SELECT entity_id, alias FROM aliases"):
        aliases.setdefault(eid, []).append(al)

    g = store.load_graph()
    added = fixed = 0
    for n in g["nodes"]:
        prov = n.get("provenance") or {}
        cik = (n.get("identity") or {}).get("sec_cik")
        if prov.get("merged_into") or not cik or cik.zfill(10) not in by_cik:
            continue
        eid, etype, name = by_cik[cik.zfill(10)]

        have = prov.get("aliases_seen_in_sources") or []
        new = [a for a in useful_aliases(n["label"], have + sorted(aliases.get(eid, []))) if a not in have]
        if new:
            added += 1
            if not dry:
                n.setdefault("provenance", {})["aliases_seen_in_sources"] = have + new
                audit.log_event("aliases_added", node_id=n["id"], aliases=new, source="HOOT identity store")

        if n.get("entity_type") == "PERSON" and etype == "organization" and prov.get("review"):
            fixed += 1
            if not dry:
                was = {k: n.get(k) for k in ("entity_type", "label", "domains")}
                n["entity_type"] = "ORGANIZATION"
                n["domains"] = [d for d in n.get("domains") or [] if d != "BRIDGE_HUMAN"]
                now = {k: n.get(k) for k in ("entity_type", "label", "domains")}
                basis = (f"name '{name}' is an organization (fund/GP/partnership form); it had been filed as a person "
                         f"from an ownership filing")
                n["provenance"].setdefault("corrections", []).append({"at": store.now_iso(), "was": was, "now": now, "basis": basis})
                audit.log_event("node_corrected", node_id=n["id"], was=was, now=now, basis=basis)
    joined = 0 if dry else apply_confirmations(g)
    if not dry and (added or fixed or joined):
        store.save_graph(g)
    if joined:
        print(f"identity sync: joined {joined} senator record(s) to their SEC record (your confirmations)")
    print(f"identity sync: {added} entities gained also-known-as names · {fixed} corrected person -> organization"
          + (" (dry run)" if dry else ""))


CONFIRM = os.path.join(os.path.dirname(audit.ANCHOR_PATH), "..", "Identity confirmations.md")


def apply_confirmations(g):
    """Ticked boxes in HOOT/Identity confirmations: fold the senator (Bioguide) record into the SEC filer's record,
    keeping both IDs. The investigator's confirmation is the second fact and is logged."""
    if not os.path.exists(CONFIRM):
        return 0
    sys.path.insert(0, os.path.join(ROOT, "src", "ingest"))
    from upgrade_legacy import consolidate
    from engine import validator
    gov = store.load_governance()
    text = open(CONFIRM, encoding="utf-8").read()
    n = 0
    for bid, cik in re.findall(r"- \[[xX]\] .*?`(BIOGUIDE:[A-Z0-9]+) = CIK (\d+)`", text):
        nodes = {x["id"]: x for x in g["nodes"]}
        sen = next((x for x in g["nodes"] if (x.get("identity") or {}).get("canonical_id") == bid
                    and not (x.get("provenance") or {}).get("merged_into")), None)
        sec = next((x for x in g["nodes"] if (x.get("identity") or {}).get("sec_cik") == cik
                    and not (x.get("provenance") or {}).get("merged_into")), None)
        if not sen or not sec or sen["id"] == sec["id"]:
            continue
        reason = (f"same person as {sec['label']} (SEC CIK {cik}): confirmed by the investigator in "
                  f"HOOT/Identity confirmations on {store.now_iso()[:10]} (name match + investigator confirmation)")
        consolidate(g, gov, nodes, sen["id"], sec["id"], reason=reason)
        try:
            node, ids = validator.validate_identity_addition(sec["id"], {"canonical_id": bid}, g, gov)
            node["identity"].update(ids)
            node.setdefault("provenance", {}).setdefault("identity_additions", []).append(
                {"added": ids, "at": store.now_iso(), "by": "investigator confirmation (Identity confirmations)"})
            audit.log_event("identity_added", node_id=sec["id"], identity=ids, basis=reason)
        except validator.ValidationError as ex:
            print(f"  {bid}: not added to {sec['id']} ({ex})")
        n += 1
    return n


if __name__ == "__main__":
    main(dry="--dry" in sys.argv)
