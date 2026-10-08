"""OpenSanctions (needs a free non-commercial API key): sanctions / politically-exposed-person flags."""
import urllib.parse

from ..util import log

API = "https://api.opensanctions.org"


def check(http, store, cfg, eid):
    if not cfg.get("opensanctions_api_key"):
        raise SystemExit("Set opensanctions_api_key in .hoot/config.json (free for non-commercial use: "
                         "https://www.opensanctions.org/api/)")
    e = store.entity(eid)
    schema = "Person" if e["type"] == "person" else "Organization"
    d = http.json(f"{API}/search/default?q={urllib.parse.quote(e['name'])}&schema={schema}&limit=5", ttl=86400)
    hits = []
    for r in d.get("results", []):
        hits.append({"id": r.get("id"), "caption": r.get("caption"), "topics": r.get("properties", {}).get("topics", []),
                     "datasets": r.get("datasets", [])[:6], "score": r.get("score"),
                     "url": f"https://www.opensanctions.org/entities/{r.get('id')}/"})
    store.set_attr(eid, "opensanctions_candidates", hits)
    log(f"OpenSanctions: {len(hits)} candidate matches for {e['name']} (name matches only; verify before relying on them)")
    store.commit()
    return hits
