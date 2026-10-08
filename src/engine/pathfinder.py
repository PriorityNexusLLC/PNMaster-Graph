"""Shortest-path interlock finder.

Edges are directed in the data (source -> target) but influence runs both ways along an interlock,
so traversal is undirected; every hop reports the edge's true direction. Superseded edges are never
traversed. Inactive (historical) edges are skipped unless asked for."""
import heapq
from collections import defaultdict, deque

from . import gates
from .validator import is_superseded


def build_adjacency(graph, include_inactive=False):
    adj = defaultdict(list)
    for e in graph.get("edges", []):
        if is_superseded(e):
            continue
        if not include_inactive and (e.get("transparency") or {}).get("active") is False:
            continue
        adj[e["source"]].append((e["target"], e))
        adj[e["target"]].append((e["source"], e))
    return adj


def components(graph, adj):
    seen, comps = {}, []
    for n in graph.get("nodes", []):
        nid = n["id"]
        if nid in seen:
            continue
        comp, q = [], deque([nid])
        seen[nid] = len(comps)
        while q:
            u = q.popleft()
            comp.append(u)
            for v, _ in adj[u]:
                if v not in seen:
                    seen[v] = len(comps)
                    q.append(v)
        comps.append(comp)
    return seen, comps


def _search(sources, targets, adj, mode, allowed):
    """Multi-source search. Returns {node: (cost, hops, prev_node, prev_edge)} for every reached node."""
    best = {}
    if mode == "hops":
        q = deque()
        for s in sources:
            best[s] = (0, 0, None, None)
            q.append(s)
        while q:
            u = q.popleft()
            if u in targets and u not in sources:
                continue                                  # stop expanding through a target
            for v, e in adj[u]:
                if v in best or (allowed is not None and v not in allowed and v not in targets):
                    continue
                c = best[u][0] + 1
                best[v] = (c, c, u, e)
                q.append(v)
        return best
    pq = []
    for s in sources:
        best[s] = (0.0, 0, None, None)
        heapq.heappush(pq, (0.0, 0, s))
    done = set()
    while pq:
        c, h, u = heapq.heappop(pq)
        if u in done:
            continue
        done.add(u)
        if u in targets and u not in sources:
            continue
        for v, e in adj[u]:
            if allowed is not None and v not in allowed and v not in targets:
                continue
            nc = c + gates.edge_cost(e)
            if v not in best or nc < best[v][0] - 1e-12:
                best[v] = (nc, h + 1, u, e)
                heapq.heappush(pq, (nc, h + 1, v))
    return best


def _reconstruct(best, end):
    nodes, edges = [end], []
    while best[nodes[-1]][2] is not None:
        _, _, p, e = best[nodes[-1]]
        edges.append(e)
        nodes.append(p)
    return nodes[::-1], edges[::-1]


def describe_path(graph, nodes, edges):
    """Crossings are measured organization to organization. A person between two organizations is the
    bridge itself, not a place: Company(B) -> person -> Company(E) is ONE crossing, B -> E, via that person."""
    by_id = {n["id"]: n for n in graph.get("nodes", [])}
    is_person = lambda nid: by_id[nid].get("entity_type") == "PERSON"
    doms = lambda nid: {d for d in (by_id[nid].get("domains") or []) if d.startswith("DOM_")}
    hops, crossings = [], []
    last_org, via, seg = (nodes[0] if not is_person(nodes[0]) else None), [], []
    for i, e in enumerate(edges):
        a, b = nodes[i], nodes[i + 1]
        hop = {"from": a, "to": b, "edge_id": e["edge_id"], "relationship_type": e.get("relationship_type"),
               "direction": "forward" if e["source"] == a else "reverse",
               "human_bridge": e.get("human_bridge"),
               "verification_source": (e.get("transparency") or {}).get("verification_source"),
               "citation_url": (e.get("transparency") or {}).get("citation_url"),
               "active": (e.get("transparency") or {}).get("active"),
               "conflict_weight": (e.get("integrity") or {}).get("conflict_weight"),
               "control_type": (e.get("integrity") or {}).get("control_type")}
        seg.append(e)
        if is_person(b):
            via.append(b)
        else:
            if last_org is not None and doms(last_org) != doms(b):
                da, db = doms(last_org), doms(b)
                cross = {"hop": i + 1, "edge_id": e["edge_id"], "from": last_org, "to": b, "via": via[:],
                         "leaving": sorted(da - db), "entering": sorted(db - da), "full_boundary": not (da & db),
                         "measured_weight": min(gates.crossing_weight(x, sorted(da), sorted(db)) for x in seg)}
                hop["crossing"] = cross
                crossings.append(cross)
            last_org, via, seg = b, [], []
        hops.append(hop)
    ws = [h["conflict_weight"] for h in hops if isinstance(h["conflict_weight"], (int, float))]
    return {"nodes": nodes, "hops": hops, "crossings": crossings, "length": len(edges),
            "min_weight": min(ws) if ws else None, "mean_weight": round(sum(ws) / len(ws), 3) if ws else None}


def find_paths(graph, *, from_ids=None, to_ids=None, from_domains=None, to_domains=None,
               mode="hops", include_inactive=False, restrict_domains=None, limit=5):
    """Entity -> entity (from_ids/to_ids) or domain -> domain (from_domains/to_domains)."""
    nodes = graph.get("nodes", [])
    by_dom = lambda ds: {n["id"] for n in nodes if set(n.get("domains") or []) & set(ds)}
    sources = set(from_ids or []) | (by_dom(from_domains) if from_domains else set())
    targets = set(to_ids or []) | (by_dom(to_domains) if to_domains else set())
    if not sources or not targets:
        return {"paths": [], "message": "nothing selected on one side"}
    spanning = sorted(sources & targets)
    adj = build_adjacency(graph, include_inactive)
    allowed = by_dom(restrict_domains) if restrict_domains else None
    best = _search(sources, targets, adj, mode, allowed)
    reached = sorted((t for t in targets if t in best and t not in sources), key=lambda t: best[t][:2])
    paths = [describe_path(graph, *_reconstruct(best, t)) for t in reached[:limit]]
    out = {"paths": paths, "spanning_nodes": spanning, "mode": mode, "include_inactive": include_inactive}
    if not paths:
        seen, comps = components(graph, adj)
        out["message"] = "No documented path connects these with the current filters."
        out["source_components"] = sorted({seen[s] for s in sources})
        out["target_components"] = sorted({seen[t] for t in targets})
        out["components"] = [c for c in comps if len(c) > 1]
    return out
