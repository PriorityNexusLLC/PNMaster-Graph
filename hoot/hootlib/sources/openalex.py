"""OpenAlex: study -> authors -> institutions -> funders/awards."""
import urllib.error
import urllib.parse

from ..util import log

API = "https://api.openalex.org"


def _short(oa_id):
    return (oa_id or "").rsplit("/", 1)[-1]


def _url(path, cfg):
    sep = "&" if "?" in path else "?"
    return API + path + (f"{sep}mailto={urllib.parse.quote(cfg['openalex_mailto'])}" if cfg.get("openalex_mailto") else "")


def _work(http, cfg, ident):
    ident = ident.strip()
    if ident.lower().startswith("https://doi.org/"):
        ident = ident[16:]
    if ident.startswith("10."):
        path = f"/works/doi:{ident}"
    else:
        path = f"/works/{_short(ident)}"
    return http.json(_url(path, cfg), ttl=7 * 86400)


def institution(store, inst):
    ids = {"openalex": _short(inst.get("id"))}
    if inst.get("ror"):
        ids["ror"] = _short(inst["ror"])
    return store.upsert_entity("organization", inst.get("display_name") or ids["openalex"], ids,
                               {"country": inst.get("country_code"), "institution_type": inst.get("type")})


def ingest_work(http, store, cfg, ident):
    w = _work(http, cfg, ident)
    doi = (w.get("doi") or "").replace("https://doi.org/", "")
    ids = {"openalex": _short(w["id"])}
    if doi:
        ids["doi"] = doi.lower()
    evidence = w.get("doi") or w["id"]
    work = store.upsert_entity("work", w.get("display_name") or doi, ids, {
        "publication_date": w.get("publication_date"), "work_type": w.get("type"),
        "venue": ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
        "cited_by_count": w.get("cited_by_count"), "is_retracted": w.get("is_retracted"),
        "openalex_url": w["id"]})
    log(f"OpenAlex: {w.get('display_name')} ({w.get('publication_year')})")
    pub = w.get("publication_date")
    src = "OpenAlex work metadata"

    for a in w.get("authorships", []):
        au = a.get("author") or {}
        if not au.get("id"):
            continue
        aids = {"openalex": _short(au["id"])}
        if au.get("orcid"):
            aids["orcid"] = _short(au["orcid"])
        person = store.upsert_entity("person", au.get("display_name"), aids,
                                     aliases=[a.get("raw_author_name")] if a.get("raw_author_name") else ())
        store.add_edge(person, work, "author_of", qualifier=a.get("author_position"),
                       source=src, evidence_url=evidence, filing_date=pub,
                       attrs={"corresponding": a.get("is_corresponding")})
        for inst in a.get("institutions", []):
            org = institution(store, inst)
            store.add_edge(person, org, "affiliated_with", source=src, evidence_url=evidence, filing_date=pub,
                           start_date=pub, attrs={"as_listed_on": doi or w["id"]})

    awards = w.get("awards") or []
    funders = {f["id"]: f for f in (w.get("funders") or [])}
    for g in w.get("grants") or []:               # older API shape
        funders.setdefault(g.get("funder"), {"id": g.get("funder"), "display_name": g.get("funder_display_name")})
        awards.append({"funder_id": g.get("funder"), "funder_award_id": g.get("award_id")})
    fent = {}
    for fid, f in funders.items():
        if not fid:
            continue
        fids = {"openalex": _short(fid)}
        if f.get("ror"):
            fids["ror"] = _short(f["ror"])
        fent[fid] = store.upsert_entity("organization", f.get("display_name") or _short(fid), fids, {"is_funder": True})
    awarded = set()
    for aw in awards:
        fid = aw.get("funder_id")
        if fid in fent:
            store.add_edge(work, fent[fid], "funded_by", qualifier=aw.get("funder_award_id"),
                           source=src, evidence_url=evidence, filing_date=pub,
                           attrs={"award_title": aw.get("display_name")})
            awarded.add(fid)
    for fid, e in fent.items():
        if fid not in awarded:
            store.add_edge(work, e, "funded_by", source=src, evidence_url=evidence, filing_date=pub)
    log(f"  {len(w.get('authorships', []))} authors, {len(fent)} funders, {len(awards)} award ids")
    store.commit()
    return work


def ingest_author(http, store, cfg, ident, limit=25):
    """Recent works of an author (ORCID or OpenAlex A-id)."""
    ident = ident.strip()
    key = f"orcid:{_short(ident)}" if "-" in ident else _short(ident)
    try:
        a = http.json(_url(f"/authors/{key}", cfg), ttl=7 * 86400)
    except urllib.error.HTTPError:
        raise SystemExit(f"OpenAlex author {ident} not found")
    log(f"OpenAlex author: {a.get('display_name')} ({a.get('works_count')} works); ingesting latest {limit}")
    d = http.json(_url(f"/works?filter=author.id:{_short(a['id'])}&sort=publication_date:desc&per-page={limit}", cfg),
                  ttl=86400)
    for w in d.get("results", []):
        ingest_work(http, store, cfg, w["id"])
