"""Pattern engine: multi-hop paths, interlocks, shared funders/owners, bridges, integrity audit."""
import os
import random
from collections import defaultdict, deque
from itertools import combinations

from . import yamlish
from .store import verification_hash
from .util import VAULT_DIR, log, loose_name, today

INTERLOCK_RELS = {"director_of", "officer_of", "position_at", "trustee_of", "advisor_to"}
OWNER_RELS = {"beneficial_owner_of", "holds_shares_of", "ten_percent_owner_of", "owns"}
PARENT_RELS = {"subsidiary_of", "ultimately_owned_by"}


def _current(e):
    return not (e["is_current"] == 0 or (e["end_date"] and e["end_date"] < today()))


class Graph:
    def __init__(self, store, hard_only=False, current_only=False, min_weight=0.0):
        self.s = store
        self.adj = defaultdict(list)          # node -> [(other, edge)]
        self.edges = []
        for e in store.edges("1=1"):
            if hard_only and e["control"] != "hard":
                continue
            if current_only and not _current(e):
                continue
            if e["conflict_weight"] < min_weight:
                continue
            self.edges.append(e)
            self.adj[e["src"]].append((e["dst"], e))
            self.adj[e["dst"]].append((e["src"], e))
        self.names = {r[0]: r[1] for r in store.db.execute("SELECT id, name FROM entities")}
        self.types = {r[0]: r[1] for r in store.db.execute("SELECT id, type FROM entities")}

    def paths(self, a, b, max_hops=4, limit=25, avoid_hubs=None):
        """All shortest-ish simple paths a->b up to max_hops (BFS by layers, then DFS on the layered DAG)."""
        dist = {a: 0}
        q = deque([a])
        while q:
            u = q.popleft()
            if dist[u] >= max_hops:
                continue
            if avoid_hubs and u != a and len(self.adj[u]) > avoid_hubs:
                continue
            for v, _ in self.adj[u]:
                if v not in dist:
                    dist[v] = dist[u] + 1
                    q.append(v)
        if b not in dist:
            return []
        # backwards distance for pruning
        distb = {b: 0}
        q = deque([b])
        while q:
            u = q.popleft()
            if distb[u] >= max_hops:
                continue
            for v, _ in self.adj[u]:
                if v not in distb:
                    distb[v] = distb[u] + 1
                    q.append(v)
        out = []

        def dfs(u, path, eds):
            if len(out) >= limit:
                return
            if u == b:
                out.append((list(path), list(eds)))
                return
            if len(eds) >= max_hops:
                return
            if avoid_hubs and u != a and len(self.adj[u]) > avoid_hubs:
                return
            for v, e in sorted(self.adj[u], key=lambda x: -x[1]["conflict_weight"]):
                if v in path or distb.get(v, 99) > max_hops - len(eds) - 1:
                    continue
                path.append(v)
                eds.append(e)
                dfs(v, path, eds)
                path.pop()
                eds.pop()

        dfs(a, [a], [])
        out.sort(key=lambda pe: (len(pe[1]), -sum(e["conflict_weight"] for e in pe[1])))
        return out

    def betweenness(self, sample=300, seed=7):
        """Brandes betweenness (sampled for big graphs). Finds broker nodes that sit between clusters."""
        nodes = list(self.adj)
        if not nodes:
            return {}
        srcs = nodes if len(nodes) <= sample else random.Random(seed).sample(nodes, sample)
        bc = dict.fromkeys(nodes, 0.0)
        for s in srcs:
            stack, pred, sigma, dist = [], defaultdict(list), defaultdict(int), {s: 0}
            sigma[s] = 1
            q = deque([s])
            while q:
                v = q.popleft()
                stack.append(v)
                for w, _ in self.adj[v]:
                    if w not in dist:
                        dist[w] = dist[v] + 1
                        q.append(w)
                    if dist[w] == dist[v] + 1:
                        sigma[w] += sigma[v]
                        pred[w].append(v)
            delta = defaultdict(float)
            while stack:
                w = stack.pop()
                for v in pred[w]:
                    delta[v] += sigma[v] / sigma[w] * (1 + delta[w])
                if w != s:
                    bc[w] += delta[w]
        scale = len(nodes) / len(srcs)
        return {k: v * scale for k, v in bc.items()}


# ---------------------------------------------------------------- reports

class Reports:
    def __init__(self, store, renderer, cfg):
        self.s, self.r, self.cfg = store, renderer, cfg
        self.dir = f"{cfg.get('notes_folder', 'HOOT')}/Reports"

    def _save(self, name, fm, lines):
        rel = f"{self.dir}/{name}.md"
        fm = {"hoot": "report", "generated": today(), **fm, "tags": ["hoot/report"]}
        self.r._write(rel, yamlish.dump(fm) + "\n".join(lines) + "\n")
        self.s.commit()
        log(f"report -> {rel}")
        return rel

    def _hop(self, e, frm):
        L = self.r.link
        fwd = e["src"] == frm
        rel = e["relation"] + (f" ({e['qualifier']})" if e["qualifier"] else "")
        arrow = f"—{rel}→" if fwd else f"←{rel}—"
        extra = []
        if e["percent"] is not None:
            extra.append(f"{e['percent']:g}%")
        d = self.r._dates(e)
        if d:
            extra.append(d)
        if not _current(e):
            extra.append("former")
        ev = f"[{e['source'] or 'src'}]({e['evidence_url']})" if e["evidence_url"] else (e["source"] or "**no evidence**")
        return f"{arrow} {L(e['dst'] if fwd else e['src'])}  <small>({', '.join(extra + [e['control'], 'w=' + str(e['conflict_weight'])])}; {ev})</small>"

    def resolve(self, q):
        if q.startswith(("sec_cik:", "lei:", "doi:", "orcid:", "ror:", "openalex:", "littlesis:", "nih_project:", "cusip:")):
            s, v = q.split(":", 1)
            eid = self.s.find(s, v.zfill(10) if s == "sec_cik" else v)
            if eid:
                return eid
        hits = self.s.find_by_name(q)
        if not hits:
            low = q.lower()
            hits = [r[0] for r in self.s.db.execute("SELECT id, name FROM entities") if low in r[1].lower()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise SystemExit(f"no entity matches {q!r}")
        raise SystemExit(f"{q!r} is ambiguous:\n" + "\n".join(
            f"  {self.s.entity(h)['canonical_id']:<30} {self.s.entity(h)['name']}" for h in hits[:20]) +
            "\nUse the canonical id, e.g. sec_cik:0000936468")

    def paths(self, qa, qb, max_hops=4, hard_only=False, current_only=False, limit=25, avoid_hubs=None):
        a, b = self.resolve(qa), self.resolve(qb)
        g = Graph(self.s, hard_only=hard_only, current_only=current_only)
        res = g.paths(a, b, max_hops, limit, avoid_hubs)
        na, nb = g.names[a], g.names[b]
        lines = [f"# Paths: {na} ↔ {nb}", "",
                 f"Filters: max {max_hops} hops · {'hard control only' if hard_only else 'hard + soft'} · "
                 f"{'current ties only' if current_only else 'current + former'}"
                 + (f" · skipping hubs with >{avoid_hubs} ties" if avoid_hubs else ""), ""]
        if not res:
            lines.append("> [!note] No path found within these limits. Try more hops, include soft ties, or ingest more sources.")
        for i, (nodes, eds) in enumerate(res, 1):
            score = round(sum(e["conflict_weight"] for e in eds) / len(eds), 2)
            weakest = min(eds, key=lambda e: e["conflict_weight"])
            lines += [f"### Path {i}: {len(eds)} hops · mean weight {score} · weakest link {weakest['relation']} "
                      f"({weakest['conflict_weight']})", "", f"1. {self.r.link(nodes[0])}"]
            for frm, e in zip(nodes, eds):
                lines.append(f"   {self._hop(e, frm)}")
            lines.append("")
        lines += ["---", "*A path shows documented ties, not wrongdoing or coordination. Check each hop's source "
                  "before citing it; former ties and passive holdings carry less weight.*"]
        name = f"Paths - {na[:40]} to {nb[:40]}".replace("/", "-")
        return self._save(name, {"from": self.r.link(a), "to": self.r.link(b), "max_hops": max_hops,
                                 "paths_found": len(res)}, lines)

    def neighborhood(self, q, hops=2):
        a = self.resolve(q)
        g = Graph(self.s)
        dist = {a: 0}
        layer = [a]
        via = {}
        for h in range(1, hops + 1):
            nxt = []
            for u in layer:
                for v, e in g.adj[u]:
                    if v not in dist:
                        dist[v] = h
                        via[v] = (u, e)
                        nxt.append(v)
            layer = nxt
        lines = [f"# Neighborhood: {g.names[a]} ({hops} hops, {len(dist) - 1} entities)", ""]
        for h in range(1, hops + 1):
            ring = [v for v, d in dist.items() if d == h]
            lines += [f"## {h} hop{'s' if h > 1 else ''} ({len(ring)})", ""]
            for v in sorted(ring, key=lambda x: (g.types.get(x) or "", g.names[x])):
                u, e = via[v]
                lines.append(f"- {self.r.link(v)} *({g.types.get(v)})* via {self.r.link(u)} {self._hop(e, u)}")
            lines.append("")
        return self._save(f"Neighborhood - {g.names[a][:60]}".replace("/", "-"), {"center": self.r.link(a), "hops": hops}, lines)

    # -------------------------------------------------------------- pattern sweep
    def patterns(self, top=40):
        s, L = self.s, self.r.link
        g = Graph(self.s)
        lines = ["# Pattern sweep", "", f"{len(g.names)} entities · {len(g.edges)} edges", ""]

        # 1. board / leadership interlocks
        seats = defaultdict(list)
        for e in g.edges:
            if e["relation"] in INTERLOCK_RELS and _current(e):
                seats[e["src"]].append(e)
        inter = [(p, es) for p, es in seats.items() if len({x["dst"] for x in es}) >= 2]
        inter.sort(key=lambda pe: -len({x["dst"] for x in pe[1]}))
        lines += [f"## 1. Leadership interlocks: people with current seats at 2+ organizations ({len(inter)})", ""]
        for p, es in inter[:top]:
            orgs = sorted({x["dst"] for x in es}, key=lambda o: g.names[o])
            lines.append(f"- {L(p)} → " + ", ".join(
                f"{L(o)} ({'/'.join(sorted({x['relation'].replace('_of', '') for x in es if x['dst'] == o}))})" for o in orgs))
        lines.append("")
        pairs = defaultdict(set)
        for p, es in inter:
            for o1, o2 in combinations(sorted({x["dst"] for x in es}), 2):
                pairs[(o1, o2)].add(p)
        strong = sorted(pairs.items(), key=lambda kv: -len(kv[1]))
        lines += ["### Organization pairs sharing the most people", ""]
        for (o1, o2), ps in strong[:top]:
            lines.append(f"- {L(o1)} ⇄ {L(o2)}: **{len(ps)}** shared ({', '.join(L(p) for p in sorted(ps, key=lambda x: g.names[x]))})")
        lines.append("")

        # 2. common owners
        owners = defaultdict(list)
        for e in g.edges:
            if e["relation"] in OWNER_RELS and _current(e) and (e["percent"] is None or e["percent"] > 0):
                owners[e["src"]].append(e)
        multi = sorted(((o, es) for o, es in owners.items() if len({x["dst"] for x in es}) >= 2),
                       key=lambda oe: -len(oe[1]))
        lines += [f"## 2. Common owners: holders with stakes in 2+ tracked entities ({len(multi)})", ""]
        for o, es in multi[:top]:
            lines.append(f"- {L(o)} → " + ", ".join(
                f"{L(x['dst'])}" + (f" ({x['percent']:g}%)" if x["percent"] is not None else "") for x in
                sorted(es, key=lambda x: -(x["percent"] or 0))[:15]) + (" …" if len(es) > 15 else ""))
        lines.append("")

        # 3. shared funders
        funded = defaultdict(set)
        for e in g.edges:
            if e["relation"] in ("funded_by",):
                funded[e["dst"]].add(e["src"])
        lines += ["## 3. Funders backing multiple tracked works/grants", ""]
        for f, ws in sorted(funded.items(), key=lambda kv: -len(kv[1]))[:top]:
            if len(ws) >= 2:
                lines.append(f"- {L(f)} funds **{len(ws)}**: " + ", ".join(L(w) for w in sorted(ws, key=lambda x: g.names[x])[:10]))
        lines.append("")

        # 4. corporate families
        parent = {}
        for e in g.edges:
            if e["relation"] == "ultimately_owned_by" or (e["relation"] == "subsidiary_of" and e["src"] not in parent):
                parent[e["src"]] = e["dst"]
        fam = defaultdict(set)
        for c, p in parent.items():
            fam[p].add(c)
        lines += ["## 4. Corporate families (GLEIF parents with tracked subsidiaries)", ""]
        for p, cs in sorted(fam.items(), key=lambda kv: -len(kv[1]))[:top]:
            lines.append(f"- {L(p)}: {len(cs)} subsidiaries: " + ", ".join(L(c) for c in sorted(cs, key=lambda x: g.names[x])[:12])
                         + (" …" if len(cs) > 12 else ""))
        lines.append("")

        # 5. brokers / bridges
        bc = g.betweenness()
        dom = {r[0]: s.entity(r[0])["attrs"].get("domain") or [] for r in s.db.execute("SELECT id FROM entities")}
        lines += ["## 5. Broker nodes (highest betweenness: sit on the most shortest paths between others)", ""]
        for n, v in sorted(bc.items(), key=lambda kv: -kv[1])[:top]:
            if v <= 0:
                break
            nd = {d for m, _ in g.adj[n] for d in dom.get(m, [])}
            lines.append(f"- {L(n)} *({g.types.get(n)})*: betweenness {v:,.0f} · {len(g.adj[n])} ties"
                         + (f" · neighbours span domains {', '.join(sorted(nd))}" if nd else ""))
        lines.append("")

        # 6. cross-domain bridges (needs `domain` filled in on notes)
        cross = []
        for n in g.adj:
            ds = defaultdict(set)
            for m, _ in g.adj[n]:
                for d in dom.get(m, []):
                    ds[d].add(m)
            if len(ds) >= 2:
                cross.append((n, ds))
        lines += ["## 6. Cross-domain bridges (entities whose ties reach 2+ of your domains)", ""]
        if not cross:
            lines.append("*Fill in `domain:` on entity notes (e.g. A–G) and re-run `hoot patterns` to light this up.*")
        for n, ds in sorted(cross, key=lambda x: -len(x[1]))[:top]:
            lines.append(f"- {L(n)}: " + "; ".join(f"**{d}** via {', '.join(L(m) for m in list(ms)[:4])}" for d, ms in sorted(ds.items())))
        lines += ["", "---", "*These are structural patterns in documented ties. They are leads to investigate, not findings.*"]
        return self._save("Pattern sweep", {"entities": len(g.names), "edges": len(g.edges)}, lines)

    # -------------------------------------------------------------- integrity
    def audit(self, verify_notes=True):
        s, L = self.s, self.r.link
        lines = ["# Integrity audit", ""]
        es = s.edges("1=1")
        no_ev = [e for e in es if not e["evidence_url"]]
        no_date = [e for e in es if not (e["filing_date"] or e["start_date"] or e["first_seen"])]
        lines += [f"## Edges without an evidence URL ({len(no_ev)})", ""]
        lines += [f"- {L(e['src'])} **{e['relation']}** {L(e['dst'])} ([[{e['note_path'][:-3]}|edge]])" if e["note_path"] else
                  f"- {L(e['src'])} **{e['relation']}** {L(e['dst'])}" for e in no_ev[:200]]
        lines += ["", f"## Edges without any date ({len(no_date)})", ""]
        lines += [f"- {L(e['src'])} **{e['relation']}** {L(e['dst'])}" for e in no_date[:200]]

        # duplicate candidates (never auto-merged)
        by = defaultdict(list)
        for r in s.db.execute("SELECT id, type, name FROM entities"):
            by[(r[1], loose_name(r[2]))].append(r[0])
        for r in s.db.execute("SELECT entity_id, alias FROM aliases"):
            t = s.db.execute("SELECT type FROM entities WHERE id=?", (r[0],)).fetchone()
            if t:
                by[(t[0], loose_name(r[1]))].append(r[0])
        dup = [sorted(set(v)) for k, v in by.items() if k[1] and len(set(v)) > 1]
        dup = [list(x) for x in {tuple(d) for d in dup}]
        lines += ["", f"## Possible duplicate entities ({len(dup)})", "",
                  "Same type and near-identical name but different identifiers. Merge only after checking: "
                  "`hoot merge \"<keep canonical_id>\" \"<drop canonical_id>\"`", ""]
        for d in sorted(dup, key=lambda x: s.entity(x[0])["name"])[:300]:
            lines.append("- " + " ≟ ".join(f"{L(i)} `{s.entity(i)['canonical_id']}`" for i in d))

        # notes that drifted from the evidence store
        drift = []
        if verify_notes:
            for e in es:
                if not e["note_path"] or not os.path.exists(os.path.join(VAULT_DIR, e["note_path"])):
                    continue
                fm, _ = yamlish.split(open(os.path.join(VAULT_DIR, e["note_path"]), encoding="utf-8").read())
                if e["origin"] == "manual":
                    continue
                f = {k: fm.get(k) for k in ("relation", "qualifier", "start_date", "end_date", "percent", "amount",
                                            "source", "evidence_url", "filing_date", "source_sha256")}
                for k in ("start_date", "end_date", "filing_date"):
                    if f[k] is not None:
                        f[k] = str(f[k])
                h = verification_hash(f, fm.get("from_id"), fm.get("to_id"))
                if h != e["verification_hash"] or fm.get("verification_hash") != e["verification_hash"]:
                    drift.append(e)
        lines += ["", f"## Edge notes edited outside HOOT ({len(drift)})", "",
                  "Evidence fields in these notes no longer match the hash HOOT recorded. Either the note was edited by "
                  "hand (put corrections in a manual edge note instead) or the file was altered.", ""]
        lines += [f"- [[{e['note_path'][:-3]}|{s.entity(e['src'])['name']} → {e['relation']} → {s.entity(e['dst'])['name']}]]"
                  for e in drift]
        unknown = [r[0] for r in s.db.execute("SELECT id FROM entities WHERE type='unknown'")]
        lines += ["", f"## Entities with no type or identifier yet ({len(unknown)})", ""]
        lines += [f"- {L(i)}: created from a manual link; add an identifier by ingesting it from a source" for i in unknown]
        log(f"audit: {len(no_ev)} edges w/o evidence, {len(dup)} duplicate candidates, {len(drift)} drifted notes")
        return self._save("Integrity audit", {"edges_without_evidence": len(no_ev), "duplicate_candidates": len(dup),
                                              "drifted_notes": len(drift)}, lines)

    def recheck_sources(self, limit=200):
        """Re-download evidence for SEC edges and compare to the recorded sha256 (detects altered/removed documents)."""
        from .util import Http
        http = Http(self.cfg)
        bad, checked = [], 0
        for e in self.s.edges("source_sha256 IS NOT NULL ORDER BY date_verified LIMIT ?", (limit,)):
            url = (e["attrs"] or {}).get("document_url")
            if not url:
                continue
            try:
                _, sha = http.request(url, ttl=0)
            except Exception as ex:                     # noqa: BLE001
                bad.append((e, f"fetch failed: {ex}"))
                continue
            checked += 1
            if sha != e["source_sha256"]:
                bad.append((e, "document changed since it was recorded"))
            else:
                self.s.db.execute("UPDATE edges SET date_verified=? WHERE id=?", (today(), e["id"]))
        lines = ["# Source re-check", "", f"Re-downloaded {checked} source documents.", ""]
        lines += [f"- {self.r.link(e['src'])} **{e['relation']}** {self.r.link(e['dst'])}: {why} ({e['evidence_url']})" for e, why in bad] or ["All matched."]
        return self._save("Source recheck", {"checked": checked, "mismatches": len(bad)}, lines)
