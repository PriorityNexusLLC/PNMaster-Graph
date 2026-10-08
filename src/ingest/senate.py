"""U.S. Senate connector: official rosters into the master graph.

  senate.gov          every sitting senator (with their Bioguide ID) and every committee's members and positions
  Congress.gov (LoC)  former senators' official terms, for the people your documents name

Identity: senators are keyed by their Bioguide ID (the Congressional Biographical Directory's permanent ID).
A senator is never assumed to be the same person as an SEC filer with the same name: the two records share no
ID, so each possible pair goes on a tick-box list in Obsidian (HOOT/Identity confirmations). Ticking a box is
the second fact; identity_sync then joins the two records, with your confirmation logged.

Seats that disappear from the roster are not deleted: their record is superseded by a historical one.

    python -I src/ingest/senate.py
"""
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
from engine import paths  # noqa: E402
HOOT_DIR = paths.HOOT_DIR
sys.path.insert(0, HOOT_DIR)
from engine import audit, store, validator  # noqa: E402
from hootlib.util import Http, load_config, person_name_match  # noqa: E402

SENATORS_URL = "https://www.senate.gov/general/contact_information/senators_cfm.xml"
COMMITTEE_URL = "https://www.senate.gov/general/committee_membership/committee_memberships_{}.xml"
COMMITTEES = ["SSAF", "SSAP", "SSAS", "SSBK", "SSBU", "SSCM", "SSEG", "SSEV", "SSFI", "SSFR", "SSHR", "SSGA",
              "SSJU", "SSRA", "SSSB", "SSVA", "SLIA", "SLIN", "SPAG", "SLET"]
WATCH = os.path.join(ROOT, "data", "staging", "congress_watch.json")   # former members to look up: {bioguide: expected name}
CONFIRM = os.path.join(os.path.dirname(audit.ANCHOR_PATH), "..", "Identity confirmations.md")
SENATE_ID, SENATE_CID = "ENT_US_SENATE", "USSENATE"
WEIGHT_BASIS = "automatic default for a legislative seat (not a Priority Nexus threshold); not reviewed"


def fetch(http, url, ttl=86400):
    b, sha = http.request(url, ttl=ttl)
    return b, sha


def node(g, gov, nid, label, etype, cid, provenance):
    idx = {(n.get("identity") or {}).get("canonical_id"): n["id"] for n in g["nodes"]}
    if cid in idx:
        return idx[cid], False
    n = validator.validate_new_node({"id": nid, "label": label, "entity_type": etype, "domains": ["DOM_C"],
                                     "identity": {"canonical_id": cid},
                                     "provenance": {"created_via": "senate connector", "review": "automatic (senate v1)",
                                                    **provenance}}, g, gov)
    g["nodes"].append(n)
    audit.log_event("node_recorded", node=n)
    return n["id"], True


def edge(g, gov, src, tgt, rtype, t, weight, ctype, role=None, supersede=None):
    payload = {"source": src, "target": tgt, "relationship_type": rtype, "human_bridge": "",
               "transparency": t, "integrity": {"control_type": ctype, "control_class": "hard", "conflict_weight": weight},
               "supplement": {"role_title": role, "weight_basis": WEIGHT_BASIS,
                              "provenance": {"via": "senate connector", "review": "automatic (senate v1)"}}}
    if supersede:
        payload["supersedes"] = supersede
    try:
        e, sup, reason = validator.validate_new_edge(payload, g, gov, store.next_edge_id(g))
    except validator.ValidationError as ex:
        if any(x["field"] == "supersedes" for x in ex.errors):
            return None                                   # already recorded
        raise
    if sup:
        old = next(x for x in g["edges"] if x["edge_id"] == sup)
        old["integrity"].update(superseded_by=e["edge_id"], superseded_at=e["integrity"]["recorded_at"], supersede_reason=reason)
    g["edges"].append(e)
    audit.log_event("edge_recorded", edge=e, superseded=sup, reason=reason or None)
    return e


def main():
    cfg = load_config()
    http = Http(cfg)
    g, gov = store.load_graph(), store.load_governance()
    today = date.today().isoformat()
    added = {"senators": 0, "committees": 0, "seats": 0, "ended": 0, "former": 0}

    node(g, gov, SENATE_ID, "United States Senate", "LEGISLATIVE_CHAMBER", SENATE_CID,
         {"basis": "U.S. Constitution, Article I; confirms nominees under Article II, §2"})

    # ---- sitting senators
    xml, _ = fetch(http, SENATORS_URL)
    senators = {}
    for m in ET.fromstring(xml).iter("member"):
        f = lambda k: (m.findtext(k) or "").strip()
        bid = f("bioguide_id")
        if not bid:
            continue
        first = re.sub(r"\s+[A-Z]\.$", "", f("first_name"))
        label = f"{first} {f('last_name')}"
        nid, new = node(g, gov, "ENT_SEN_" + re.sub(r"[^A-Z0-9]+", "_", label.upper()).strip("_")[:44], label, "PERSON",
                        f"BIOGUIDE:{bid}", {"office": f"U.S. Senator ({f('party')}-{f('state')})", "bioguide": bid})
        added["senators"] += new
        senators[(f("last_name").lower(), f("state"))] = (nid, label, bid)
        edge(g, gov, nid, SENATE_ID, "SENATOR_IN",
             {"verification_source": "U.S. Senate roster", "source_reference": bid, "citation_url": SENATORS_URL,
              "document_date": today, "active": True}, 0.5, "LEGISLATIVE_SEAT", role=f"Senator for {f('state')} ({f('party')}), {f('class')}")

    # ---- committees and seats
    current = set()
    for code in COMMITTEES:
        url = COMMITTEE_URL.format(code)
        try:
            xml, _ = fetch(http, url)
            root = ET.fromstring(xml)
        except Exception as ex:                          # noqa: BLE001
            print(f"  {code}: not available ({str(ex)[:60]})")
            continue
        c = root.find(".//committees")
        cname, ccode = (c.findtext("committee_name") or code).strip(), (c.findtext("committee_code") or f"{code}00").strip()
        cid, new = node(g, gov, f"ENT_SENATE_{code}", f"Senate {cname}", "LEGISLATIVE_COMMITTEE", f"USSENATE:{ccode}",
                        {"committee_code": ccode})
        added["committees"] += new
        edge(g, gov, cid, SENATE_ID, "COMMITTEE_OF",
             {"verification_source": "U.S. Senate roster", "source_reference": ccode, "citation_url": url,
              "document_date": today, "active": True}, 0.3, "LEGISLATIVE_STRUCTURE")
        for m in c.iter("member"):
            last, state = (m.findtext("name/last") or "").strip(), (m.findtext("state") or "").strip()
            hit = senators.get((last.lower(), state))
            if not hit:
                continue                                  # two facts: surname AND state must match the Senate roster
            pos = (m.findtext("position") or "Member").strip()
            e = edge(g, gov, hit[0], cid, "COMMITTEE_MEMBER_OF",
                     {"verification_source": "U.S. Senate roster", "source_reference": ccode, "citation_url": url,
                      "document_date": today, "active": True}, 0.5, "LEGISLATIVE_OVERSIGHT",
                     role={"Ranking": "Ranking Member"}.get(pos, pos))
            added["seats"] += bool(e)
            current.add((hit[0], cid))

    # seats no longer on the roster: superseded by a historical record, never deleted
    for e in list(g["edges"]):
        if (e["relationship_type"] == "COMMITTEE_MEMBER_OF" and not (e.get("integrity") or {}).get("superseded_by")
                and (e.get("transparency") or {}).get("verification_source") == "U.S. Senate roster"
                and e["transparency"].get("active") and (e["source"], e["target"]) not in current):
            t = dict(e["transparency"], active=False, document_date=today)
            if edge(g, gov, e["source"], e["target"], "COMMITTEE_MEMBER_OF", {k: t.get(k) for k in (
                    "verification_source", "source_reference", "citation_url", "document_date", "active")},
                    e["integrity"]["conflict_weight"], "LEGISLATIVE_OVERSIGHT", role=t.get("role_title"),
                    supersede={"edge_id": e["edge_id"], "reason": f"no longer on the senate.gov committee roster as of {today}"}):
                added["ended"] += 1

    # ---- former senators named in your documents (Congress.gov, Library of Congress)
    watch = json.load(open(WATCH, encoding="utf-8")) if os.path.exists(WATCH) else {}
    key = cfg.get("api_data_gov_key") or cfg.get("congress_api_key") or "DEMO_KEY"
    for bid, expected in watch.items():
        try:
            d = json.loads(fetch(http, f"https://api.congress.gov/v3/member/{bid}?api_key={key}&format=json", ttl=30 * 86400)[0])["member"]
        except Exception as ex:                          # noqa: BLE001
            print(f"  {bid}: Congress.gov lookup failed ({str(ex)[:60]}); add a free key as congress_api_key in .hoot/config.json")
            continue
        name = d.get("directOrderName") or ""
        if not person_name_match(expected, name):
            print(f"  {bid}: Congress.gov says {name!r}, not {expected!r}; skipped")
            continue
        terms = [t for t in d.get("terms") or [] if t.get("chamber") == "Senate"]
        if not terms:
            continue
        start, end = min(t["startYear"] for t in terms), max(t.get("endYear") or 0 for t in terms)
        label = re.sub(r"\s+(III|II|Jr\.?)$", "", name)
        nid, new = node(g, gov, "ENT_SEN_" + re.sub(r"[^A-Z0-9]+", "_", label.upper()).strip("_")[:44], label, "PERSON",
                        f"BIOGUIDE:{bid}", {"office": f"former U.S. Senator ({d.get('state')})", "bioguide": bid,
                                            "previous_names": [p.get("fullName") for p in d.get("previousNames") or []]})
        added["former"] += new
        current_member = bool(d.get("currentMember"))
        edge(g, gov, nid, SENATE_ID, "SENATOR_IN",
             {"verification_source": "Congress.gov member record", "source_reference": bid,
              "citation_url": f"https://bioguide.congress.gov/search/bio/{bid}", "document_date": today,
              "active": current_member}, 0.5, "LEGISLATIVE_SEAT",
             role=f"U.S. Senator, {d.get('state')}, {start}–{end if end else 'present'}")

    store.save_graph(g)
    write_confirmations(g)
    print("senate: " + " · ".join(f"{v} {k}" for k, v in added.items()))


def write_confirmations(g):
    """Tick-box list: each senator next to the SEC filer(s) with a matching name. Your tick is the second fact."""
    people = [n for n in g["nodes"] if n.get("entity_type") == "PERSON" and not (n.get("provenance") or {}).get("merged_into")]
    sen = [n for n in people if str((n.get("identity") or {}).get("canonical_id", "")).startswith("BIOGUIDE:")]
    sec = [n for n in people if (n.get("identity") or {}).get("sec_cik")]
    orgs_of = {}
    labels = {n["id"]: n["label"] for n in g["nodes"]}
    for e in g["edges"]:
        if not (e.get("integrity") or {}).get("superseded_by"):
            orgs_of.setdefault(e["source"], set()).add(labels.get(e["target"], e["target"]))
    old = open(CONFIRM, encoding="utf-8").read() if os.path.exists(CONFIRM) else ""
    ticked = set(re.findall(r"- \[[xX]\] .*?`(BIOGUIDE:[A-Z0-9]+) = CIK (\d+)`", old))
    rows = []
    for s in sen:
        for p in sec:
            if person_name_match(s["label"], p["label"]) or any(person_name_match(a, s["label"]) for a in
                                                                 (p.get("provenance") or {}).get("aliases_seen_in_sources") or []):
                k = (s["identity"]["canonical_id"], p["identity"]["sec_cik"])
                rows.append((k in ticked, s, p, k))
    L = ["# Identity confirmations", "",
         "Official records that may be the same person but share no ID: a senator (Bioguide ID) and an SEC filer with "
         "a matching name. **Tick the box only if you are sure they are one person** (for example, the senator's "
         "official biography or financial disclosure names the same company). Your tick is recorded as the second fact; "
         "the two records are then joined on the next update. Leave unsure ones unticked: nothing is lost.", ""]
    for done, s, p, k in sorted(rows, key=lambda r: r[1]["label"]):
        L.append(f"- [{'x' if done else ' '}] **{s['label']}** ({s['provenance'].get('office', 'senator')}) = "
                 f"**{p['label']}**, SEC filer for: {', '.join(sorted(orgs_of.get(p['id'], []))[:6]) or 'n/a'} "
                 f"`{k[0]} = CIK {k[1]}`")
    if not rows:
        L.append("- nothing to confirm right now")
    os.makedirs(os.path.dirname(os.path.abspath(CONFIRM)), exist_ok=True)
    with open(CONFIRM, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
