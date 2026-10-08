"""GLEIF: Legal Entity Identifiers and Level 2 'who owns whom' parent/child relationships."""
import re
import urllib.error
import urllib.parse

from ..util import log, smart_title

API = "https://api.gleif.org/api/v1"
LEI_RE = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")


def record_url(lei):
    return f"https://search.gleif.org/#/record/{lei}"


def _get(http, path, ttl=7 * 86400):
    try:
        return http.json(API + path, ttl=ttl)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def search(http, name, n=10):
    q = urllib.parse.quote(name)
    exact = (_get(http, f"/lei-records?filter[entity.legalName]={q}&page[size]={n}", ttl=86400) or {}).get("data", [])
    active = [r for r in exact if r["attributes"]["entity"].get("status") == "ACTIVE"]
    if active:
        return active
    d = _get(http, f"/lei-records?filter[fulltext]={q}&page[size]={n}", ttl=86400) or {}
    return d.get("data", [])


def entity_from_record(store, rec):
    a = rec["attributes"]
    ent = a["entity"]
    lei = a["lei"]
    attrs = {
        "jurisdiction": ent.get("jurisdiction"),
        "legal_form": (ent.get("legalForm") or {}).get("id"),
        "country": (ent.get("legalAddress") or {}).get("country"),
        "city": (ent.get("legalAddress") or {}).get("city"),
        "registered_as": ent.get("registeredAs"),
        "entity_status": ent.get("status"),
        "lei_registration_status": (a.get("registration") or {}).get("status"),
        "lei_record_url": record_url(lei),
    }
    others = [o.get("name") for o in ent.get("otherNames") or [] if o.get("name")]
    return store.upsert_entity("organization", smart_title(ent["legalName"]["name"]), {"lei": lei}, attrs,
                               aliases=others + [ent["legalName"]["name"]]), lei


def _exception(http, lei, which):
    d = _get(http, f"/lei-records/{lei}/{which}-parent-reporting-exception")
    if d and d.get("data"):
        return (d["data"]["attributes"] or {}).get("reason")
    return None


def ingest(http, store, cfg, query, children=True):
    q = query.strip().upper()
    if LEI_RE.match(q):
        d = _get(http, f"/lei-records/{q}")
        if not d:
            raise SystemExit(f"LEI {q} not found")
        rec = d["data"]
    else:
        hits = search(http, query)
        if not hits:
            raise SystemExit(f"GLEIF: nothing found for {query!r}")
        exact = [h for h in hits if h["attributes"]["entity"]["legalName"]["name"].lower() == query.lower()]
        if len(exact) == 1 or len(hits) == 1:
            rec = (exact or hits)[0]
        else:
            raise SystemExit("GLEIF: several matches, re-run with the LEI:\n" + "\n".join(
                f"  {h['attributes']['lei']}  {h['attributes']['entity']['legalName']['name']}"
                f"  [{h['attributes']['entity'].get('jurisdiction')}]" for h in hits))
    eid, lei = entity_from_record(store, rec)
    log(f"GLEIF: {rec['attributes']['entity']['legalName']['name']} ({lei})")

    for which, rel in (("direct", "subsidiary_of"), ("ultimate", "ultimately_owned_by")):
        p = _get(http, f"/lei-records/{lei}/{which}-parent")
        if p and p.get("data"):
            pid, plei = entity_from_record(store, p["data"])
            store.add_edge(eid, pid, rel, source=f"GLEIF Level 2 ({which} parent)",
                           evidence_url=record_url(lei), filing_date=None, is_current=1,
                           attrs={"parent_lei": plei})
            log(f"  {which} parent: {p['data']['attributes']['entity']['legalName']['name']}")
        else:
            reason = _exception(http, lei, which)
            if reason:
                store.set_attr(eid, f"{which}_parent_exception", reason)
                log(f"  {which} parent not reported: {reason}")

    if children:
        n = cfg["gleif_max_children"]
        d = _get(http, f"/lei-records/{lei}/direct-children?page[size]={min(n, 100)}") or {}
        kids = d.get("data", [])[:n]
        total = ((d.get("meta") or {}).get("pagination") or {}).get("total", len(kids))
        for k in kids:
            cid, clei = entity_from_record(store, k)
            store.add_edge(cid, eid, "subsidiary_of", source="GLEIF Level 2 (direct parent)",
                           evidence_url=record_url(clei), is_current=1, attrs={"parent_lei": lei})
        log(f"  direct children recorded: {len(kids)} of {total}")
        if total > len(kids):
            store.set_attr(eid, "gleif_children_total", total)
    store.commit()
    return eid
