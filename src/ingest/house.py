"""U.S. House connector: the Clerk of the House's official member list (clerk.house.gov/xml/lists/MemberData.xml).

Every sitting Representative (keyed by Bioguide ID, the same ID senators use) and their committee seats.
Seats that disappear from the list are superseded by a historical record, never deleted.

    python -I src/ingest/house.py
"""
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _graphio import Writer  # noqa: E402
from engine import paths  # noqa: E402

sys.path.insert(0, paths.HOOT_DIR)
from hootlib.util import Http, load_config  # noqa: E402
from engine import audit, store, validator  # noqa: E402

URL = "https://clerk.house.gov/xml/lists/MemberData.xml"
HOUSE_ID, HOUSE_CID = "ENT_US_HOUSE", "USHOUSE"


def main():
    http = Http(load_config())
    xml, sha = http.request(URL, ttl=86400)
    root = ET.fromstring(xml)
    today = date.today().isoformat()
    w = Writer("house connector (Clerk of the House)", "automatic (house v1)")
    if HOUSE_CID not in w.by_cid:
        n = validator.validate_new_node({"id": HOUSE_ID, "label": "United States House of Representatives",
                                         "entity_type": "LEGISLATIVE_CHAMBER", "domains": ["DOM_C"],
                                         "identity": {"canonical_id": HOUSE_CID},
                                         "provenance": {"created_via": w.via, "review": w.review,
                                                        "basis": "U.S. Constitution, Article I"}}, w.g, w.gov)
        w.g["nodes"].append(n)
        w.by_cid[HOUSE_CID] = HOUSE_ID
        audit.log_event("node_recorded", node=n)
    t = lambda ref: {"verification_source": "U.S. House roster", "source_reference": ref, "citation_url": URL,
                     "document_date": today, "active": True}
    committees = {}
    for c in root.iter("committee"):
        code, name = c.get("comcode"), (c.findtext("committee-fullname") or "").strip()
        if code and name:
            committees[code] = w.node(f"House {name}", "LEGISLATIVE_COMMITTEE", f"USHOUSE:{code}", ["DOM_C"],
                                      {"committee_code": code})
            w.edge(committees[code], HOUSE_ID, "COMMITTEE_OF", t(code), 0.3, "LEGISLATIVE_STRUCTURE", "hard")
    current, members = set(), 0
    for m in root.iter("member"):
        info = m.find("member-info")
        if info is None or not (info.findtext("bioguideID") or "").strip():
            continue
        bid = info.findtext("bioguideID").strip()
        first, last = (info.findtext("firstname") or "").strip(), (info.findtext("lastname") or "").strip()
        sd, party = (m.findtext("statedistrict") or "").strip(), (info.findtext("party") or "").strip()
        official = (info.findtext("official-name") or "").strip()
        aka = [x for x in {official, f"{first} {(info.findtext('middlename') or '').strip()} {last}".replace("  ", " ")}
               if x and x != f"{first} {last}"]
        rep = w.node(f"{first} {last}", "PERSON", f"BIOGUIDE:{bid}", ["DOM_C"],
                     {"office": f"U.S. Representative ({party}-{sd})", "bioguide": bid,
                      "official_name": official, "aliases_seen_in_sources": aka})
        node = next(n for n in w.g["nodes"] if n["id"] == rep)       # existing records gain the names too (additive, logged)
        have = node.setdefault("provenance", {}).setdefault("aliases_seen_in_sources", [])
        new = [a for a in aka if a not in have]
        if new:
            have += new
            audit.log_event("aliases_added", node_id=rep, aliases=new, source="House Clerk member list")
            w.counts["updated"] += 1
        members += 1
        w.edge(rep, HOUSE_ID, "REPRESENTATIVE_IN", t(bid), 0.5, "LEGISLATIVE_SEAT", "hard",
               supplement={"role_title": f"Representative for {sd} ({party})"})
        for ca in m.iter("committee"):
            code = ca.get("comcode")
            if code in committees:
                role = ca.get("leadership") or ("Member, rank " + ca.get("rank", "?"))
                w.edge(rep, committees[code], "COMMITTEE_MEMBER_OF", t(code), 0.5, "LEGISLATIVE_OVERSIGHT", "hard",
                       supplement={"role_title": role})
                current.add((rep, committees[code]))
    for e in list(w.g["edges"]):                    # seats no longer listed: superseded, never deleted
        tt = e.get("transparency") or {}
        if (e["relationship_type"] == "COMMITTEE_MEMBER_OF" and tt.get("verification_source") == "U.S. House roster"
                and tt.get("active") and not (e.get("integrity") or {}).get("superseded_by")
                and (e["source"], e["target"]) not in current):
            payload = {"source": e["source"], "target": e["target"], "relationship_type": "COMMITTEE_MEMBER_OF",
                       "human_bridge": "", "transparency": {**t(tt.get("source_reference")), "active": False},
                       "integrity": {k: e["integrity"][k] for k in ("control_type", "control_class", "conflict_weight")},
                       "supersedes": {"edge_id": e["edge_id"], "reason": f"no longer on the House Clerk's list as of {today}"}}
            ne, sup, reason = validator.validate_new_edge(payload, w.g, w.gov, store.next_edge_id(w.g))
            e["integrity"].update(superseded_by=ne["edge_id"], superseded_at=ne["integrity"]["recorded_at"], supersede_reason=reason)
            w.g["edges"].append(ne)
            audit.log_event("edge_recorded", edge=ne, superseded=sup, reason=reason)
    w.save()
    print(f"house: {members} representatives · {len(committees)} committees · {w.counts}")


if __name__ == "__main__":
    main()
