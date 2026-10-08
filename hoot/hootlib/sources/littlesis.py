"""LittleSis (Public Accountability Initiative, CC BY-SA 4.0): curated, cited power-network ties."""
import re
import urllib.parse

from ..util import log

API = "https://littlesis.org/api"

CATEGORY = {1: "position", 2: "educated_at", 3: "member_of", 4: "family_of", 5: "donated_to",
            6: "transaction_with", 7: "lobbying_tie", 8: "social_tie", 9: "professional_tie",
            10: "owns", 11: "hierarchy", 12: "related_to"}


def _date(s):
    if not s:
        return None
    return re.sub(r"(-00)+$", "", s)


def _from_link(link):
    """'https://littlesis.org/person/275569-Louis_Cappelli,_Jr.' -> (275569, 'Louis Cappelli, Jr.', 'person')"""
    m = re.search(r"littlesis\.org/(person|org)/(\d+)-(.+)$", link or "")
    if not m:
        return None
    return int(m.group(2)), urllib.parse.unquote(m.group(3)).replace("_", " "), \
        "person" if m.group(1) == "person" else "organization"


def _entity(http, store, lsid):
    d = http.json(f"{API}/entities/{lsid}", ttl=7 * 86400)["data"]["attributes"]
    etype = "person" if d.get("primary_ext") == "Person" else "organization"
    return store.upsert_entity(etype, d["name"], {"littlesis": lsid}, {
        "blurb": d.get("blurb"), "website": d.get("website"), "littlesis_types": d.get("types"),
        "littlesis_url": f"https://littlesis.org/entities/{lsid}"}, aliases=d.get("aliases") or ()), d["name"]


def ingest(http, store, cfg, query):
    q = query.strip()
    if not q.isdigit():
        res = http.json(f"{API}/entities/search?q={urllib.parse.quote(q)}", ttl=86400).get("data", [])
        if not res:
            raise SystemExit(f"LittleSis: nothing found for {q!r}")
        exact = [r for r in res if r["attributes"]["name"].lower() == q.lower()]
        if len(exact) == 1 or len(res) == 1:
            q = str((exact or res)[0]["id"])
        else:
            raise SystemExit("LittleSis: several matches, re-run with the numeric id:\n" + "\n".join(
                f"  {r['id']:>8}  {r['attributes']['name']}  - {r['attributes'].get('blurb') or ''}" for r in res[:20]))
    lsid = int(q)
    me, name = _entity(http, store, lsid)
    log(f"LittleSis: {name} (#{lsid})")
    n = 0
    for page in range(1, cfg["littlesis_max_pages"] + 1):
        d = http.json(f"{API}/entities/{lsid}/relationships?page={page}", ttl=86400)
        for rel in d.get("data", []):
            a = rel["attributes"]
            other = _from_link(rel.get("related"))
            if not other:
                continue
            oid, oname, otype = other
            other_e = store.upsert_entity(otype, oname, {"littlesis": oid},
                                          {"littlesis_url": f"https://littlesis.org/entities/{oid}"})
            src_e, dst_e = (me, other_e) if a["entity1_id"] == lsid else (other_e, me)
            cat = CATEGORY.get(a.get("category_id"), "related_to")
            ca = a.get("category_attributes") or {}
            if cat == "position":
                cat = "director_of" if ca.get("is_board") else "officer_of" if ca.get("is_executive") else "position_at"
            store.add_edge(src_e, dst_e, cat,
                           qualifier=a.get("description1") or None,
                           start_date=_date(a.get("start_date")), end_date=_date(a.get("end_date")),
                           is_current=None if a.get("is_current") is None else int(bool(a.get("is_current"))),
                           amount=a.get("amount"), currency=a.get("currency"),
                           source="LittleSis (CC BY-SA 4.0)", evidence_url=rel.get("self"),
                           filing_date=(a.get("updated_at") or "")[:10] or None,
                           attrs={"description": a.get("description"), "goods": a.get("goods")})
            n += 1
        meta = d.get("meta") or {}
        if page >= (meta.get("pageCount") or 1):
            break
    log(f"  {n} relationships recorded")
    store.commit()
    return me
