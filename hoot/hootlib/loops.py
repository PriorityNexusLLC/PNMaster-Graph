"""Closed loops through people, and frontier expansion to find more of them.

A *closed loop* is a cycle of organizations A → B → C → … → A where every step is a person with a
filed role (SEC Form 3/4/5: director or officer) at both organizations. Loops held together by a
single person sitting on every board are excluded as trivial. Only primary-source edges count.
"""
from collections import defaultdict
from datetime import date, timedelta

from . import yamlish
from .util import log, today

ROLE_RELS = {"director_of", "officer_of"}
ACTIVE_DAYS = 456


def _is_primary(e, include_index=False):
    s = (e["source"] or "").upper()
    conf = ((e.get("attrs") or {}).get("identity_match") or {}).get("confidence")
    if conf not in (None, "high", "registry"):                # name-only matches never form loops
        return False
    return s.startswith("SEC FORM") or s.startswith("IRS FORM 990") or s.startswith("UK COMPANIES HOUSE") or (include_index and s.startswith("SEC OWNERSHIP INDEX"))


def _active(e):
    if e["is_current"] == 0:
        return False
    last = e["last_seen"] or e["filing_date"]
    return bool(last) and date.fromisoformat(last) >= date.today() - timedelta(days=ACTIVE_DAYS)


def projection(store, active_only=True, include_index=False):
    """org -> {org: {person: (edge_to_a, edge_to_b)}} built from people's filed roles."""
    roles = defaultdict(dict)                      # person -> {org: edge}
    for e in store.edges("relation IN ('director_of','officer_of')"):
        if not _is_primary(e, include_index) or (active_only and not _active(e)):
            continue
        prev = roles[e["src"]].get(e["dst"])
        if not prev or (e["relation"] == "director_of" and prev["relation"] != "director_of"):
            roles[e["src"]][e["dst"]] = e
    proj = defaultdict(lambda: defaultdict(dict))
    for p, orgs in roles.items():
        os_ = sorted(orgs)
        for i, a in enumerate(os_):
            for b in os_[i + 1:]:
                proj[a][b][p] = (orgs[a], orgs[b])
                proj[b][a][p] = (orgs[b], orgs[a])
    return proj, roles


def find_loops(proj, max_len=4, limit=400):
    """Simple cycles of 3..max_len organizations, each reported once (rotation/direction canonical)."""
    nodes = sorted(proj)
    found, seen = [], set()
    for s in nodes:
        stack = [(s, [s])]
        while stack and len(found) < limit:
            u, path = stack.pop()
            for v in proj[u]:
                if v == s and len(path) >= 3:
                    key = tuple(sorted(path))
                    if key in seen:
                        continue
                    hops = [proj[path[i]][path[(i + 1) % len(path)]] for i in range(len(path))]
                    people = set().union(*[set(h) for h in hops])
                    if len(people) < 2:
                        continue                                  # one person on every board: trivial
                    seen.add(key)
                    found.append((list(path), hops))
                elif v > s and v not in path and len(path) < max_len:
                    stack.append((v, path + [v]))
    return found


def loops_report(store, renderer, cfg, max_len=4, active_only=True):
    proj, _ = projection(store, active_only)
    loops = find_loops(proj, max_len)
    name = lambda i: store.entity(i)["name"]
    L = renderer.link

    def hop_people(h):
        # prefer a different person per hop where possible
        return sorted(h, key=lambda p: name(p))

    scored = []
    for path, hops in loops:
        n_people = len(set().union(*[set(h) for h in hops]))
        scored.append((len(path), -n_people, path, hops))
    scored.sort(key=lambda x: (x[0], x[1]))

    lines = ["# Closed loops through people", "",
             f"{len(loops)} loops of 3–{max_len} organizations where every step is a person with a filed "
             f"{'current ' if active_only else ''}director/officer role at both ends (SEC Forms 3/4/5). "
             "Loops held together by a single person are excluded.", "",
             "*A loop is a documented structure, not evidence of coordination. Each hop links to its filings.*", ""]
    for i, (_, _, path, hops) in enumerate(scored[:200], 1):
        lines.append(f"### Loop {i}: " + " → ".join(name(o) for o in path) + f" → {name(path[0])}")
        for k, h in enumerate(hops):
            a, b = path[k], path[(k + 1) % len(path)]
            bits = []
            for p in hop_people(h)[:4]:
                ea, eb = h[p]
                bits.append(f"{L(p)} ([{ea['relation'].replace('_of', '')} {ea['last_seen'] or ''}]({ea['evidence_url']}) · "
                            f"[{eb['relation'].replace('_of', '')} {eb['last_seen'] or ''}]({eb['evidence_url']}))")
            lines.append(f"- {L(a)} ⇄ {L(b)} via " + "; ".join(bits) + (" …" if len(h) > 4 else ""))
        lines.append("")
    rel = f"{cfg.get('notes_folder', 'HOOT')}/Reports/Closed loops.md"
    fm = {"hoot": "report", "generated": today(), "loops": len(loops), "max_len": max_len, "tags": ["hoot/report"]}
    renderer._write(rel, yamlish.dump(fm) + "\n".join(lines) + "\n")
    log(f"loops: {len(loops)} closed loops -> {rel}")
    return loops


def frontier(store, active_only=True, limit=25):
    """Organizations seen only through people's filings (not yet pulled themselves), ranked by how many
    already-pulled organizations they share people with: the likeliest places to close new loops."""
    proj, _ = projection(store, active_only, include_index=True)     # index-level career links may point the way
    pulled = {r[0] for r in store.db.execute("SELECT id FROM entities WHERE attrs LIKE '%key_filings%'")}
    cands = []
    for o, nbrs in proj.items():
        if o in pulled:
            continue
        e = store.entity(o)
        cik = e["ids"].get("sec_cik")
        if not cik:
            continue
        core = [n for n in nbrs if n in pulled]
        if len(core) >= 1:
            people = set().union(*[set(nbrs[n]) for n in core])
            cands.append((len(core), len(people), o, cik, e["name"]))
    cands.sort(key=lambda c: (-c[0], -c[1], c[4]))
    return cands[:limit]
