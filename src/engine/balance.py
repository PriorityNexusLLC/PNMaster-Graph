"""Balance checks: is power in the mapped network checked by anything?

Four measures, each computed only from verified, active, primary-source connections already in the graph,
and each tied to a published standard (engine/gates.py BALANCE_STANDARDS; swap in your own values there):

  1. Independence      boards where most recorded directors have a filed tie to the company
                       (also an officer, a major owner, or holding a role at a major owner)
  2. Concentration     per domain, how concentrated the recorded ownership blocks (5%/10%+) are among
                       holders (Herfindahl-Hirschman index), plus how much of the domain the top 4 reach
  3. Single points     entities whose removal would cut part of the network off from the rest
  4. Tenure            board seats held longer than the standard, by the filings on record

These are signals for review, never findings. Every flag names its evidence and its standard. They describe
the data held: a director with no filed tie here may still have ties these filings don't show."""
import os
import re
from collections import defaultdict
from datetime import date

from . import gates
from .validator import is_superseded

OWN = ("TEN_PERCENT_OWNER_OF", "BENEFICIAL_OWNER_OF")
ROLE = ("DIRECTOR_OF", "OFFICER_OF")
_cache = {}


def _live(graph):
    nodes = {n["id"]: n for n in graph.get("nodes", []) if not (n.get("provenance") or {}).get("merged_into")}
    edges = [e for e in graph.get("edges", []) if not is_superseded(e)
             and (e.get("transparency") or {}).get("active") is not False
             and e["source"] in nodes and e["target"] in nodes]
    return nodes, edges


def _std(key):
    v, basis = gates.balance_standard(key)
    return v, basis


def independence(nodes, edges):
    min_share, basis = _std("independence_min_share")
    min_board, _ = _std("min_board_size")
    directors, officers, owners, roles = defaultdict(dict), defaultdict(set), defaultdict(set), defaultdict(set)
    for e in edges:
        rt, s, t = e["relationship_type"], e["source"], e["target"]
        if rt == "DIRECTOR_OF" and nodes[s].get("entity_type") == "PERSON":
            directors[t][s] = e
        if rt == "OFFICER_OF":
            officers[t].add(s)
        if rt in OWN:
            owners[t].add(s)
        if rt in ROLE + OWN:
            roles[s].add(t)
    out = []
    for org, ds in directors.items():
        if len(ds) < min_board:
            continue
        tied = {}
        for p in ds:
            why = []
            if p in officers[org]:
                why.append("also an officer here")
            if p in owners[org]:
                why.append("a major owner here")
            via = sorted(roles[p] & owners[org])
            if via:
                why.append("holds a role at " + ", ".join(nodes[v]["label"] for v in via[:2]) + ", a major owner here")
            if why:
                tied[p] = why
        ind_share = 1 - len(tied) / len(ds)
        if ind_share <= min_share:
            out.append({"org": org, "label": nodes[org]["label"], "directors": len(ds), "tied": len(tied),
                        "independent_share": round(ind_share, 2),
                        "ties": [{"person": p, "label": nodes[p]["label"], "why": w,
                                  "evidence": ds[p]["transparency"].get("citation_url")} for p, w in tied.items()]})
    out.sort(key=lambda x: (x["independent_share"], -x["directors"]))
    return {"standard": basis, "threshold": min_share, "flags": out,
            "note": "Companies more than 50% controlled by one holder (e.g. a parent's subsidiary or spin-off) are exempt "
                    "from this listing rule (NYSE 303A.00 / Nasdaq 5615(c)); the flag still shows whose interests "
                    "the board reflects.", "boards_scored":
            sum(1 for ds in directors.values() if len(ds) >= min_board)}


def concentration(nodes, edges, domains):
    high, basis = _std("concentration_hhi_high")
    min_pos, _ = _std("min_positions")
    out = []
    for d, name in domains.items():
        if not d.startswith("DOM_"):
            continue
        in_d = {n for n, v in nodes.items() if d in (v.get("domains") or []) and v.get("entity_type") != "PERSON"}
        blocks, reach = defaultdict(int), defaultdict(set)
        for e in edges:
            if e["target"] not in in_d:
                continue
            if e["relationship_type"] in OWN:
                blocks[e["source"]] += 1
            if e["relationship_type"] in OWN + ROLE:
                reach[e["source"]].add(e["target"])
        total = sum(blocks.values())
        controlled = set().union(*reach.values()) if reach else set()
        top = sorted(reach, key=lambda h: (-len(reach[h]), nodes[h]["label"]))[:4]
        cr4 = len(set().union(*(reach[h] for h in top))) / len(controlled) if top and controlled else 0
        hhi = round(sum((c / total * 100) ** 2 for c in blocks.values())) if total else None
        top_blocks = sorted(blocks, key=lambda h: -blocks[h])[:5]
        out.append({"domain": d, "name": name, "organizations": len(controlled), "ownership_blocks": total,
                    "holders": len(blocks), "hhi": hhi if total >= min_pos else None,
                    "flag": bool(total >= min_pos and hhi is not None and hhi > high),
                    "top_holders": [{"id": h, "label": nodes[h]["label"], "blocks": blocks[h],
                                     "share": round(blocks[h] / total, 3)} for h in top_blocks],
                    "top4_reach": [{"id": h, "label": nodes[h]["label"], "organizations": len(reach[h])} for h in top],
                    "top4_reach_share": round(cr4, 3)})
    return {"standard": basis, "threshold": high,
            "method": "HHI over the number of recorded 5%/10%+ ownership blocks each holder has in the domain "
                      "(not market share). Top-4 reach: share of the domain's mapped organizations the four "
                      "most-connected holders sit in or own blocks of.", "domains": out}


def single_points(nodes, edges):
    min_cut, basis = _std("single_point_min_cut")
    adj = defaultdict(set)
    for e in edges:
        if e["source"] != e["target"]:
            adj[e["source"]].add(e["target"])
            adj[e["target"]].add(e["source"])
    seen, comps = set(), []
    for s in adj:                                    # connected components; score the largest
        if s in seen:
            continue
        comp, stack = [], [s]
        seen.add(s)
        while stack:
            u = stack.pop()
            comp.append(u)
            for w in adj[u]:
                if w not in seen:
                    seen.add(w)
                    stack.append(w)
        comps.append(comp)
    if not comps:
        return {"standard": basis, "flags": [], "network_size": 0}
    main = max(comps, key=len)
    root = main[0]
    disc, low, size, pieces = {root: 0}, {root: 0}, {root: 1}, defaultdict(list)
    t, stack = 1, [(root, None, iter(adj[root]))]
    while stack:                                     # iterative Tarjan with subtree sizes
        u, parent, it = stack[-1]
        for w in it:
            if w == parent:
                continue
            if w not in disc:
                disc[w] = low[w] = t
                size[w] = 1
                t += 1
                stack.append((w, u, iter(adj[w])))
                break
            low[u] = min(low[u], disc[w])
        else:
            stack.pop()
            if parent is not None:
                low[parent] = min(low[parent], low[u])
                size[parent] += size[u]
                if low[u] >= disc[parent]:
                    pieces[parent].append(size[u])
    total = len(main)
    out = []
    for v, ps in pieces.items():
        if v == root and len(ps) < 2:
            continue
        parts = ps if v == root else ps + [total - 1 - sum(ps)]
        cut = sum(parts) - max(parts)
        if cut >= min_cut:
            out.append({"id": v, "label": nodes[v]["label"], "entity_type": nodes[v].get("entity_type"),
                        "cut_off": cut, "groups": len(parts) - 1, "connections": len(adj[v])})
    out.sort(key=lambda x: -x["cut_off"])
    return {"standard": basis, "network_size": total, "flags": out[:60], "count": len(out),
            "meaning": "if this entity's recorded ties ended, this many entities would no longer connect to the "
                       "main network: everything between them runs through this one point"}


def tenure(nodes, edges):
    years, basis = _std("tenure_years")
    out = []
    for e in edges:
        if e["relationship_type"] != "DIRECTOR_OF":
            continue
        t = e.get("transparency") or {}
        w = t.get("evidence_window") or {}
        first = t.get("tenure_start") or w.get("first_filing")
        last = w.get("last_filing") or t.get("document_date")
        try:
            span = (date.fromisoformat(last[:10]) - date.fromisoformat(first[:10])).days / 365.25
        except (TypeError, ValueError):
            continue
        if span > years:
            out.append({"person": e["source"], "label": nodes[e["source"]]["label"], "org": e["target"],
                        "org_label": nodes[e["target"]]["label"], "years_at_least": round(span, 1),
                        "first_filing": first, "latest_filing": last, "edge_id": e["edge_id"]})
    out.sort(key=lambda x: -x["years_at_least"])
    return {"standard": basis, "threshold": years, "flags": out,
            "method": "time between the earliest and latest filings on record that report the seat; real tenure "
                      "is at least this long"}


def overboarding(nodes, edges):
    """Current public-company board seats per person (SEC Forms 3/4/5 are filed only for public companies)."""
    limit, basis = _std("overboard_seats")
    ceo_limit, ceo_basis = _std("overboard_ceo_outside")
    seats, ceo_at = defaultdict(set), defaultdict(set)
    for e in edges:
        t = e.get("transparency") or {}
        if t.get("verification_source") != "Form 3/4/5":
            continue
        if e["relationship_type"] == "DIRECTOR_OF":
            seats[e["source"]].add(e["target"])
        if e["relationship_type"] == "OFFICER_OF" and re.search(r"\b(CEO|chief executive)", t.get("role_title") or "", re.I):
            ceo_at[e["source"]].add(e["target"])
    out = []
    for p, orgs in seats.items():
        if nodes[p].get("entity_type") != "PERSON":
            continue
        outside = orgs - ceo_at[p]
        why = None
        if len(orgs) > limit:
            why = f"{len(orgs)} current public-company board seats (standard: no more than {limit})"
        elif ceo_at[p] and len(outside) > ceo_limit:
            why = f"a sitting CEO with {len(outside)} outside public-company board seats (standard: no more than {ceo_limit})"
        if why:
            out.append({"person": p, "label": nodes[p]["label"], "seats": len(orgs), "why": why,
                        "orgs": [{"id": o, "label": nodes[o]["label"]} for o in sorted(orgs, key=lambda o: nodes[o]["label"])]})
    out.sort(key=lambda x: -x["seats"])
    return {"standard": basis, "ceo_standard": ceo_basis, "threshold": limit, "flags": out,
            "note": "counts current seats on record (filed within about 15 months); fund and trust boards of one family "
                    "may be counted separately here though proxy advisers often count them once"}


def compute(graph):
    nodes, edges = _live(graph)
    domains = (graph.get("schema_metadata") or {}).get("domains") or {}
    return {"generated": date.today().isoformat(),
            "basis": "verified, active, primary-source connections only; signals for review, not findings",
            "independence": independence(nodes, edges), "concentration": concentration(nodes, edges, domains),
            "single_points": single_points(nodes, edges), "tenure": tenure(nodes, edges),
            "overboarding": overboarding(nodes, edges)}


def cached(graph_path, load):
    """compute() once per version of the graph file."""
    m = os.path.getmtime(graph_path)
    if _cache.get("m") != m:
        _cache.update(m=m, res=compute(load()))
    return _cache["res"]


def report_markdown(r):
    ind, con, sp, ten = r["independence"], r["concentration"], r["single_points"], r["tenure"]
    L = ["---", "hoot: report", f"generated: {r['generated']}", "tags: [hoot/report]", "---", "# Balance check", "",
         "Is power in the mapped network checked by anything? Four measures, each from verified, active, "
         "primary-source connections only, each tied to a published standard. **Signals for review, not findings.** "
         "They describe the filings held: a tie these filings don't show is not counted.", "",
         f"## 1. Board independence ({len(ind['flags'])} of {ind['boards_scored']} boards flagged)", "",
         f"Standard: {ind['standard']}. Flagged when {int(ind['threshold'] * 100)}% or fewer of the recorded "
         f"directors are free of a filed tie to the company. {ind['note']}", ""]
    for f in ind["flags"][:40]:
        L.append(f"- **{f['label']}**: {f['tied']} of {f['directors']} recorded directors tied "
                 f"({int(f['independent_share'] * 100)}% untied)")
        L += [f"    - {t['label']}: {'; '.join(t['why'])}" for t in f["ties"][:6]]
    if not ind["flags"]:
        L.append("- none: every scored board has a majority with no filed tie")
    L += ["", "## 2. Concentration by domain", "", f"Standard: {con['standard']}.", f"Method: {con['method']}", "",
          "| domain | organizations | ownership blocks | holders | HHI | top-4 reach | flag |", "|---|---|---|---|---|---|---|"]
    for d in con["domains"]:
        L.append(f"| {d['domain'][-1]} {d['name']} | {d['organizations']} | {d['ownership_blocks']} | {d['holders']} | "
                 f"{d['hhi'] if d['hhi'] is not None else 'too few blocks'} | {int(d['top4_reach_share'] * 100)}% | "
                 f"{'**highly concentrated**' if d['flag'] else ''} |")
    for d in con["domains"]:
        if d["top_holders"]:
            L.append(f"- {d['domain'][-1]}: largest block holders: " + ", ".join(
                f"{h['label']} ({h['blocks']})" for h in d["top_holders"][:3]))
    L += ["", f"## 3. Single points of failure ({sp.get('count', 0)})", "",
          f"In a network of {sp['network_size']} connected entities: {sp.get('meaning', '')}.", ""]
    L += [f"- **{f['label']}** ({(f['entity_type'] or '').lower()}): {f['cut_off']} entities in {f['groups']} "
          f"group{'s' if f['groups'] != 1 else ''} depend on it" for f in sp["flags"][:30]] or ["- none"]
    L += ["", f"## 4. Long tenure ({len(ten['flags'])})", "", f"Standard: {ten['standard']}.", f"Method: {ten['method']}.", ""]
    L += [f"- {f['label']} at {f['org_label']}: at least {f['years_at_least']} years ({f['first_filing']} → {f['latest_filing']})"
          for f in ten["flags"][:60]] or ["- none on record (the filings held may not reach back far enough)"]
    ob = r.get("overboarding") or {"flags": []}
    if ob["flags"] or "standard" in ob:
        L += ["", f"## 5. Over-boarding ({len(ob['flags'])})", "", f"Standard: {ob['standard']}. Also: {ob['ceo_standard']}.",
              f"Note: {ob['note']}.", ""]
        L += [f"- {f['label']}: {f['why']}: " + ", ".join(o["label"] for o in f["orgs"]) for f in ob["flags"]] or ["- none"]
    return "\n".join(L) + "\n"


