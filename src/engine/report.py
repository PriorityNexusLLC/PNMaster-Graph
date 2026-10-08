"""Draft a public investigation report from the verified graph, in a fixed structure:

  1 Summary  2 Scope  3 Method  4 Findings  5 Limits  6 Right of reply  7 All sources  8 Integrity

Rules the drafter follows, so a report can be shared without overstating anything:
  * only verified, primary-source connections; each finding carries its filing link
  * findings are worded as what filings show, never as motive or wrongdoing
  * every signal names the published standard it is measured against, and its limit
  * the report is fingerprinted and the fingerprint is written to the hash-chained audit log
The draft is a starting point: the investigator reads it, completes the right-of-reply section, and
decides what to publish."""
import hashlib
import html
import os
import re
from collections import defaultdict
from datetime import date

from . import audit, balance, pathfinder, store
from .validator import is_superseded

REPORT_DIR = os.path.join(store.DATA, "reports")
ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")
PUBLISHER = "Priority Nexus LLC"           # the legal entity that publishes and stands behind each report
ROLE_WORDS = {"DIRECTOR_OF": "director", "OFFICER_OF": "officer", "TEN_PERCENT_OWNER_OF": "10%+ owner",
              "BENEFICIAL_OWNER_OF": "5%+ owner", "INSTITUTIONAL_HOLDING_IN": "institutional holder"}
GATES = [
    ("Identity", "Official ID", "every organization and person is tied to a registry identifier (SEC CIK, GLEIF LEI, IRS EIN)"),
    ("Identity", "Two facts", "a name alone never links a person; a second independent fact must agree"),
    ("Identity", "One ID, one entity", "duplicates are folded into one record with the reason logged; two different official IDs are never merged automatically"),
    ("Transparency", "Primary source", "connections come from filings themselves (SEC Forms 3/4/5, 13D/G, DEF 14A, Form D; IRS Form 990; statutes), not from news or aggregators"),
    ("Transparency", "Receipt", "each connection carries the filing reference and link"),
    ("Transparency", "Recency", "a role counts as current only if a filing reported it within about 15 months"),
    ("Integrity", "No silent change", "corrections are added as new records that point to the old one; nothing is overwritten"),
    ("Integrity", "Fingerprint", "every record and every save is fingerprinted (SHA-256) in a hash-chained log"),
    ("Integrity", "Boundary crossing", "links between sectors are measured and logged, not assumed to mean wrongdoing"),
]


# ------------------------------------------------------------------ small block model -> markdown + html
class Doc:
    def __init__(self):
        self.blocks = []

    def h(self, level, text):
        self.blocks.append(("h", level, text))

    def p(self, text):
        self.blocks.append(("p", text))

    def ul(self, items):
        if items:
            self.blocks.append(("ul", items))

    def table(self, head, rows):
        if rows:
            self.blocks.append(("table", head, rows))

    # inline: [text](url) links only
    @staticmethod
    def _md_cell(c):
        if isinstance(c, tuple):
            return f"[{c[0]}]({c[1]})" if c[1] else c[0]
        return str(c if c is not None else "")

    @staticmethod
    def _html_inline(s):
        out, pos = [], 0
        for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", s):
            out.append(html.escape(s[pos:m.start()]))
            out.append(f'<a href="{html.escape(m.group(2))}" rel="noopener noreferrer">{html.escape(m.group(1))}</a>')
            pos = m.end()
        out.append(html.escape(s[pos:]))
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", "".join(out))
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)

    def markdown(self):
        out = []
        for b in self.blocks:
            if b[0] == "h":
                out += ["#" * b[1] + " " + b[2], ""]
            elif b[0] == "p":
                out += [b[1], ""]
            elif b[0] == "ul":
                out += [f"- {x}" for x in b[1]] + [""]
            elif b[0] == "table":
                out.append("| " + " | ".join(b[1]) + " |")
                out.append("|" + "---|" * len(b[1]))
                out += ["| " + " | ".join(self._md_cell(c).replace("|", "/") for c in r) + " |" for r in b[2]]
                out.append("")
        return "\n".join(out)

    def html(self, title):
        body = []
        for b in self.blocks:
            if b[0] == "h":
                body.append(f"<h{b[1]}>{self._html_inline(b[2])}</h{b[1]}>")
            elif b[0] == "p":
                body.append(f"<p>{self._html_inline(b[1])}</p>")
            elif b[0] == "ul":
                body.append("<ul>" + "".join(f"<li>{self._html_inline(x)}</li>" for x in b[1]) + "</ul>")
            elif b[0] == "table":
                cell = lambda c: self._html_inline(self._md_cell(c))
                body.append("<table><thead><tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in b[1]) + "</tr></thead><tbody>"
                            + "".join("<tr>" + "".join(f"<td>{cell(c)}</td>" for c in r) + "</tr>" for r in b[2])
                            + "</tbody></table>")
        return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
                f"<title>{html.escape(title)}</title><link rel='stylesheet' href='/report.css'></head><body>"
                f"<div class='bar noprint'><button id='print'>Print / Save as PDF</button>"
                f"<a id='md' download>Download for Substack / Zenodo (.md)</a><button id='copy'>Copy text</button>"
                f"<span id='msg'></span></div><article>{''.join(body)}</article>"
                f"<script src='/js/report-page.js'></script></body></html>")


def from_markdown(md):
    """Read back the simple Markdown this module writes (headings, paragraphs, lists, tables) into a Doc, so an
    edited or reviewed report renders the same way as a fresh draft."""
    d, para, items, table = Doc(), [], [], []

    def flush():
        nonlocal para, items, table
        if para:
            d.p(" ".join(para))
        if items:
            d.ul(items)
        if table:
            rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in table]
            body = [r for r in rows[1:] if not all(set(c) <= {"-", ":", ""} for c in r)]
            d.table(rows[0], body)
        para, items, table = [], [], []
    for line in md.splitlines():
        if line.startswith("#"):
            flush()
            level = len(line) - len(line.lstrip("#"))
            d.h(level, line[level:].strip())
        elif line.startswith("- "):
            if para or table:
                flush()
            items.append(line[2:])
        elif line.startswith("    ") and items:
            items[-1] += " " + line.strip()
        elif line.startswith("|"):
            if para or items:
                flush()
            table.append(line)
        elif not line.strip():
            flush()
        else:
            if items or table:
                flush()
            para.append(line.strip())
    flush()
    return d


# ------------------------------------------------------------------ helpers
def _live(graph):
    nodes = {n["id"]: n for n in graph["nodes"] if not (n.get("provenance") or {}).get("merged_into")}
    edges = [e for e in graph["edges"] if not is_superseded(e) and e["source"] in nodes and e["target"] in nodes]
    return nodes, edges


def _role(e):
    t = (e.get("transparency") or {})
    w = ROLE_WORDS.get(e["relationship_type"], e["relationship_type"].replace("_", " ").lower())
    if t.get("role_title"):
        w += f" ({t['role_title']})"
    return w


def _status(e):
    return "current" if (e.get("transparency") or {}).get("active") is not False else "historical"


def _cite(e):
    t = e.get("transparency") or {}
    ref = t.get("source_reference") or t.get("verification_source") or "filing"
    return (f"{t.get('verification_source') or 'source'} {ref}".strip(), t.get("citation_url"))


def _doms(n):
    return {d for d in (n.get("domains") or []) if d.startswith("DOM_")}


def _dname(graph, d):
    return f"{d[-1]} ({(graph.get('schema_metadata') or {}).get('domains', {}).get(d, d)})"


def _slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:40] or "report"


def _has_src(e):
    return bool((e.get("transparency") or {}).get("citation_url"))


class Sources:
    """Every filing behind every finding, numbered once. A finding cites ALL the filings on record for it
    (its evidence trail), not just the latest; the appendix lists them all."""

    def __init__(self):
        self.by_url, self.rows, self.claims = {}, [], {}

    def add(self, url, date_, kind, supports):
        if not url:
            return None
        acc = ACCESSION.search(url)
        key = acc.group(0) if acc else url        # one filing, even when the SEC shows it under each filer's folder
        if key not in self.by_url:
            self.by_url[key] = len(self.rows) + 1
            self.rows.append({"n": self.by_url[key], "url": url, "ref": acc.group(0) if acc else "", "date": date_ or "",
                              "kind": (kind or "").replace(acc.group(0), "").strip() if acc else (kind or ""),
                              "supports": set()})
        self.rows[self.by_url[key] - 1]["supports"].add(supports)
        return self.by_url[key]

    def for_edge(self, e, supports):
        t = e.get("transparency") or {}
        kind = f"{t.get('verification_source') or 'filing'}"
        nums = []
        for x in (t.get("evidence_trail") or []):
            if isinstance(x, dict) and x.get("url"):
                nums.append(self.add(x["url"], x.get("date"), kind, supports))
        nums.append(self.add(t.get("citation_url"), t.get("document_date"), f"{kind} {t.get('source_reference') or ''}".strip(), supports))
        nums = sorted({n for n in nums if n}, key=lambda n: self.rows[n - 1]["date"], reverse=True)
        # machine-readable record of the claim, so the independent review can re-check it later
        c = self.claims.setdefault(e["edge_id"], {"edge_id": e["edge_id"], "source": e["source"], "target": e["target"],
                                                  "relationship_type": e["relationship_type"],
                                                  "verification_hash": (e.get("integrity") or {}).get("verification_hash"),
                                                  "statements": [], "sources": []})
        if supports not in c["statements"]:
            c["statements"].append(supports)
        c["sources"] = sorted({*c["sources"], *(f"S{n}" for n in nums)}, key=lambda s: int(s[1:]))
        return ", ".join(f"[S{n}]({self.rows[n - 1]['url']})" for n in nums)


# ------------------------------------------------------------------ the report
def draft(graph, subject_id, path_to=None, include_inactive=True, folder=None):
    """Write version 1 (the draft). `folder`: a subfolder for auto-reports. Returns ids and urls."""
    nodes, edges_all = _live(graph)
    if subject_id not in nodes:
        raise ValueError(f"{subject_id} is not a live entity in the graph")
    edges = [e for e in edges_all if _has_src(e)]           # public record only: no source link, no finding
    unsourced = sum(1 for e in edges_all if not _has_src(e) and subject_id in (e["source"], e["target"]))
    edge_by_id = {e["edge_id"]: e for e in edges_all}
    src = Sources()
    S = nodes[subject_id]
    is_person = lambda i: nodes[i].get("entity_type") == "PERSON"
    label = lambda i: nodes[i]["label"]
    by_node = defaultdict(list)
    for e in edges:
        by_node[e["source"]].append(e)
        by_node[e["target"]].append(e)
    direct = sorted(by_node[subject_id], key=lambda e: (_status(e) != "current", label(e["target"] if e["source"] == subject_id else e["source"])))

    # organizations linked through a shared person
    links = []                            # (other_org, person, edge_here, edge_there)
    if is_person(subject_id):
        orgs = [(e["target"] if e["source"] == subject_id else e["source"], e) for e in direct]
        orgs = [(o, e) for o, e in orgs if not is_person(o)]
        for i in range(len(orgs)):
            for j in range(i + 1, len(orgs)):
                if orgs[i][0] != orgs[j][0]:
                    links.append((orgs[i][0], orgs[j][0], subject_id, orgs[i][1], orgs[j][1]))
    else:
        for e in direct:
            p = e["source"] if e["target"] == subject_id else e["target"]
            if not is_person(p):
                continue
            for f in by_node[p]:
                o = f["target"] if f["source"] == p else f["source"]
                if o != subject_id and not is_person(o):
                    links.append((subject_id, o, p, e, f))
    seen, uniq = set(), []
    for a, b, p, e, f in links:
        k = (min(a, b), max(a, b), p)
        if k not in seen:
            seen.add(k)
            uniq.append((a, b, p, e, f))
    links = sorted(uniq, key=lambda x: (_status(x[3]) != "current" or _status(x[4]) != "current", label(x[1])))

    bal = balance.cached(store.GRAPH_PATH, store.load_graph)
    ind = [f for f in bal["independence"]["flags"] if f["org"] == subject_id or any(t["person"] == subject_id for t in f["ties"])]
    ten = [f for f in bal["tenure"]["flags"] if subject_id in (f["person"], f["org"])]
    sp = [f for f in bal["single_points"]["flags"] if f["id"] == subject_id]
    obf = [f for f in (bal.get("overboarding") or {}).get("flags", [])
          if f["person"] == subject_id or any(o["id"] == subject_id for o in f["orgs"])]

    path = None
    if path_to and path_to in nodes:
        sourced = {"nodes": graph["nodes"], "edges": [e for e in graph["edges"] if is_superseded(e) or _has_src(e)]}
        r = pathfinder.find_paths(sourced, from_ids=[subject_id], to_ids=[path_to], include_inactive=include_inactive, limit=1)
        path = r["paths"][0] if r["paths"] else {"none": True}

    chain = audit.verify()
    today = date.today().isoformat()
    base = f"PN-{today.replace('-', '')}-{_slug(S['label'])}"
    os.makedirs(REPORT_DIR, exist_ok=True)
    n = 1                                           # the number printed in the report always matches its file
    while os.path.exists(os.path.join(REPORT_DIR, f"{base}{'' if n == 1 else f'-{n}'}.md")):
        n += 1
    rid = base if n == 1 else f"{base}-{n}"
    n_cur = sum(_status(e) == "current" for e in direct)
    cross = [x for x in links if not (_doms(nodes[x[0]]) & _doms(nodes[x[1]]))]
    same = [x for x in links if _doms(nodes[x[0]]) & _doms(nodes[x[1]])]
    people_n = len({x[2] for x in links})
    org_n = len({x[1] for x in links} | {x[0] for x in links} - {subject_id})

    d = Doc()
    d.h(1, f"{S['label']}: connections on the public record")
    d.p(f"Published by **{PUBLISHER}** · Systems Assurance Forensic Investigation & Design · Report {rid} · version 1 · drafted {today}")
    d.p("**Status: DRAFT (version 1). Not yet reviewed. Do not publish this version.**")
    d.p("**Signals for review, not findings of wrongdoing.** Everything below is what public filings report. Each item links "
        "to its filing so any reader can check it.")

    d.h(2, "1. Summary")
    summ = [f"Public filings on record connect **{S['label']}** to {len(direct)} "
            f"{'organizations and holders' if not is_person(subject_id) else 'organizations'} directly "
            f"({n_cur} current, {len(direct) - n_cur} historical)."]
    if links and is_person(subject_id):
        summ.append(f"Because filings report this person at each of them, those organizations are linked to one another: "
                    f"{len(links)} pair(s), {len(cross)} crossing between sectors and {len(same)} within a sector.")
    elif links:
        summ.append(f"Through {people_n} {'person' if people_n == 1 else 'people'}, it is linked to {org_n} other "
                    f"organization{'s' if org_n != 1 else ''}; {len(cross)} of those links cross between sectors "
                    f"and {len(same)} stay within a sector.")
    if ind or ten or sp or obf:
        summ.append(f"Balance measures flag {len(ind) + len(ten) + len(sp) + len(obf)} item(s) for review (section 4.3), each against a "
                    "published governance standard.")
    if path and not path.get("none"):
        summ.append(f"A documented path of {path['length']} step(s) connects it to {label(path_to)} (section 4.4).")
    d.p(" ".join(summ))

    d.h(2, "2. Scope")
    dates = sorted(filter(None, [(e.get("transparency") or {}).get("document_date") for e in direct]))
    srcs = sorted({(e.get("transparency") or {}).get("verification_source") or "?" for e in direct})
    d.ul([f"Subject: {S['label']} ({', '.join(f'{k} {v}' for k, v in (S.get('identity') or {}).items())})",
          f"Sources: {', '.join(srcs) or 'none'}",
          f"Filing dates covered: {dates[0] + ' to ' + dates[-1] if dates else 'n/a'}",
          "Not examined: private contracts, lobbying and campaign-finance records, foreign registries not yet connected, "
          "news reporting, and any relationship that is not in a filing."])

    d.h(2, "3. Method")
    d.p("Nine checks, three for each core principle, applied to every record before it enters the map:")
    d.ul([f"**{core}: {name}**: {text}" for core, name, text in GATES])

    d.h(2, "4. Findings")
    d.h(3, "4.1 Direct connections")
    d.p("Each row cites every filing on record for it (S-numbers, newest first); all are listed in section 7.")
    rows = []
    for e in direct:
        other = e["target"] if e["source"] == subject_id else e["source"]
        holder, org = (subject_id, other) if e["source"] == subject_id else (other, subject_id)
        who = f"Filings report {label(holder)} as {_role(e)} of {label(org)}"
        rows.append([who, _status(e), (e.get("transparency") or {}).get("document_date") or "",
                     src.for_edge(e, f"{label(holder)} is {_role(e)} of {label(org)}")])
    d.table(["What the filings report", "Status", "Latest filing", "Sources"], rows)
    if not direct:
        d.p("No verified connections on record.")

    d.h(3, "4.2 Organizations linked through shared people")
    if links:
        d.p("A person who holds roles at two organizations links them. **Crossing** marks organizations in different "
            "sectors (domains). Clayton Act §8 (15 U.S.C. §19) restricts the same person serving as director or officer "
            "of two *competing* companies; whether any pair below competes was **not assessed**.")
        rows = []
        for a, b, p, e, f in links:
            oa = e["target"] if e["source"] == p else e["source"]
            ob = f["target"] if f["source"] == p else f["source"]
            rows.append([label(b if a == subject_id else a) if not is_person(subject_id) else f"{label(a)} / {label(b)}",
                         label(p), f"{_role(e)} at {label(oa)}; {_role(f)} at {label(ob)}",
                         "crossing" if not (_doms(nodes[a]) & _doms(nodes[b])) else "same sector",
                         f"{_status(e)} / {_status(f)}",
                         src.for_edge(e, f"{label(p)} is {_role(e)} of {label(oa)}"),
                         src.for_edge(f, f"{label(p)} is {_role(f)} of {label(ob)}")])
        d.table(["Linked organization", "Through", "Roles as filed", "Sectors", "Status", "Sources (first role)", "Sources (second role)"], rows)
    else:
        d.p("None on record.")

    d.h(3, "4.3 Balance measures")
    items = []
    def tie_edges(person, org):
        """The filings behind a director's tie: their roles at the company, their role at a major owner, and
        that owner's ownership filing."""
        owners = {y["source"] for y in by_node[org] if y["target"] == org and y["relationship_type"] in balance.OWN}
        out = [x for x in by_node[person] if x["source"] == person and x["target"] == org]
        for x in by_node[person]:
            if x["source"] == person and x["target"] in owners:
                out.append(x)
                out += [y for y in by_node[org] if y["source"] == x["target"] and y["target"] == org]
        return out

    for f in ind:
        ties = "; ".join(f"{t['label']}: {', '.join(t['why'])} ("
                         + ", ".join(src.for_edge(x, f"{t['label']}: tie to {f['label']}") for x in tie_edges(t["person"], f["org"]))
                         + ")" for t in f["ties"])
        items.append(f"Board independence: at {f['label']}, {f['tied']} of {f['directors']} recorded directors have a filed "
                     f"tie to the company: {ties}. Standard: {bal['independence']['standard']}. Limit: {bal['independence']['note']}")
    for f in ten:
        e = edge_by_id.get(f["edge_id"])
        cite = f" Sources: {src.for_edge(e, f['label'] + ' is director of ' + f['org_label'])}." if e and _has_src(e) else ""
        items.append(f"Tenure: {f['label']} has served on the board of {f['org_label']} for at least {f['years_at_least']} years "
                     f"({f['first_filing']} to {f['latest_filing']}). Standard: {bal['tenure']['standard']}.{cite}")
    for f in obf:
        seats = [e for e in by_node[f["person"]] if e["source"] == f["person"] and e["relationship_type"] == "DIRECTOR_OF"
                 and any(o["id"] == e["target"] for o in f["orgs"])]
        items.append(f"Over-boarding: filings report {f['label']} with {f['why']}: "
                     + ", ".join(o["label"] for o in f["orgs"]) + f". Standard: {bal['overboarding']['standard']}. "
                     f"Limit: {bal['overboarding']['note']}. Sources: "
                     + ", ".join(src.for_edge(e, f"{f['label']} is director of {label(e['target'])}") for e in seats) + ".")
    for f in sp:
        items.append(f"Single point: {f['cut_off']} entities on the map connect to the rest only through {f['label']}. "
                     "This describes the shape of the mapped network, not conduct; it is computed from the sourced "
                     "connections in section 4.1.")
    d.ul(items) if items else d.p("No balance measure flags this subject.")

    if path:
        d.h(3, f"4.4 Documented path to {label(path_to)}")
        if path.get("none"):
            d.p("No documented path connects them in the verified data.")
        else:
            rows = []
            for h in path["hops"]:
                c = h.get("crossing")
                rows.append([label(h["from"]), (h["relationship_type"] or "").replace("_", " ").lower(), label(h["to"]),
                             "current" if h.get("active") is not False else "historical",
                             (f"crosses {', '.join(_dname(graph, x) for x in c['leaving'])} → {', '.join(_dname(graph, x) for x in c['entering'])}" if c else ""),
                             src.for_edge(edge_by_id[h["edge_id"]], f"path step: {label(h['from'])} to {label(h['to'])}")])
            d.table(["From", "Connection", "To", "Status", "Sector crossing", "Sources"], rows)

    d.h(3, "4.5 Public enforcement records (as published by the agency)")
    sp_path = os.path.join(store.DATA, "enforcement", "stamps.json")
    stamp = None
    if os.path.exists(sp_path):
        import json as _json
        stamp = (_json.load(open(sp_path, encoding="utf-8")).get("entities") or {}).get(subject_id)
    if not stamp:
        d.p("Not yet checked against public enforcement records.")
    else:
        d.p(f"Checked {stamp['reviewed']} against: {'; '.join(stamp['sources'])}.")
        items = []
        for f in stamp.get("confirmed") or []:
            n_ = src.add(f["url"], f.get("date"), f["source"], f"{S['label']}: {f['source']} record")
            if f["source"].startswith("DOJ"):
                items.append(f"U.S. Department of Justice press release, {f['date']}: \"{f['title']}\" [S{n_}]({f['url']}). "
                             f"Described by the Department as: {f['kind']}."
                             + (" The release states that the claims resolved are allegations only and that there has "
                                "been no determination of liability." if f.get("no_liability") else ""))
            else:
                items.append(f"HHS-OIG List of Excluded Individuals/Entities: \"{f['title']}\", dated {f['date']} "
                             f"({f['kind']}) [S{n_}]({f['url']}).")
        if items:
            d.ul(items)
        else:
            d.p("No confirmed record." + (f" {stamp['possible']} possible name match(es) were not confirmed as being "
                                          "about this entity and are not reported." if stamp.get("possible") else
                                          " No record found."))

    d.h(3, "4.6 Pattern signals (official records combined)")
    from . import patterns as _patterns
    sigs = _patterns.for_entity(_patterns.cached(store.GRAPH_PATH, store.load_graph), subject_id)
    if sigs:
        d.p("Connections no single document states, found by combining the official records above. Each is a signal "
            "for review: it describes what the records show, not why.")
        items = []
        for s in sigs:
            refs = []
            for ev in s["evidence"]:
                if ev.get("url"):
                    k = src.add(ev["url"], ev.get("date"), ev.get("source") or "record", f"pattern: {s['type']}")
                    refs.append(f"[S{k}]({ev['url']})")
            items.append(f"**{s['type']}**: {s['text']} " + ", ".join(refs))
        d.ul(items)
    else:
        d.p("No pattern signal involves this subject in the records on the map.")

    d.h(2, "5. Limits")
    d.ul(["Filings show roles and holdings, not intent, influence actually exercised, or wrongdoing.",
          "A role counts as current only if filed within about 15 months; real start and end dates may differ from filing dates.",
          "People are matched only by official ID or two agreeing facts; uncertain matches are held out, so some real "
          "connections may be missing.",
          "Ownership blocks come from filings over the 5% and 10% thresholds; smaller holdings are not shown.",
          "Sector (domain) labels are classifications made for this investigation, based on official industry codes where available.",
          "Public record only: every finding links to its filings. "
          + (f"{unsourced} connection(s) on the map with no source link were left out of this report." if unsourced
             else "No connection without a source link was needed or used.")])

    d.h(2, "6. Right of reply")
    everyone = ({label(x[1]) for x in links if not is_person(x[1])} | {label(x[0]) for x in links if not is_person(x[0])}
                | ({label(subject_id)} if not is_person(subject_id) else set())
                | {label(e["target"] if e["source"] == subject_id else e["source"]) for e in direct
                   if not is_person(e["target"] if e["source"] == subject_id else e["source"])})
    # required: whoever a flag or the report's subject concerns; a person is reached through their current organizations
    req = set()
    if not is_person(subject_id):
        req.add(label(subject_id))
    else:
        req |= {label(e["target"]) for e in direct if e["source"] == subject_id and _status(e) == "current"
                and not is_person(e["target"])}
    req |= {f["label"] for f in ind} | {f["org_label"] for f in ten}
    req |= {o["label"] for f in obf for o in f["orgs"]} if obf else set()
    if path and not path.get("none"):
        req |= {label(x) for x in path["nodes"] if not is_person(x)}
    named = sorted(req)
    optional = sorted(everyone - req)
    d.p("Before publishing, each organization a finding concerns was invited to comment. **Required** before release:")
    d.table(["Organization", "Contacted (date, how)", "Response"], [[n, "", ""] for n in named])
    if optional:
        d.p(f"**Optional**: the {len(optional)} organization(s) below appear only as linked organizations; no finding is about "
            "them. Contacting them is good practice for any you single out when you publish.")
        d.ul(optional)
    d.p(f"Suggested message: \"{PUBLISHER} is preparing a report on publicly filed governance connections involving your "
        "organization. A draft of the relevant section is attached, with links to each filing. We welcome any correction or "
        "comment by [date]; responses will be published in full.\"")

    d.h(2, "7. All sources")
    d.p(f"Every filing cited above ({len(src.rows)}), newest first. Each is an official public record. Before publishing, "
        "save each link with the Internet Archive's Save Page Now (web.archive.org) so the evidence stays reachable.")
    d.table(["#", "Filing date", "Document", "Supports", "Filing (opens the official record)"],
            [[f"S{r['n']}", r["date"], r["kind"], (lambda s: s if len(s) <= 300 else s[:297] + "...")("; ".join(sorted(r["supports"]))), (r["ref"] or "open", r["url"])]
             for r in sorted(src.rows, key=lambda r: r["date"], reverse=True)])

    d.h(2, "8. Integrity")
    d.ul([f"Report {rid}, version 1. Corrections will be added as dated notes and new versions; earlier versions stay available.",
          f"Audit log at drafting: {'intact' if chain['ok'] else 'ALTERED: do not publish until resolved'}, "
          f"{chain['entries']:,} entries, latest fingerprint `{(chain.get('tip') or '')[:32]}…`",
          f"Map data file: {chain['graph']['message']}",
          "This report's own fingerprint (SHA-256 of its full text, excluding the final fingerprint line) is recorded in the audit log under "
          f"\"report_drafted\" for {rid}.",
          "Corrections log: none yet.",
          f"© {today[:4]} {PUBLISHER}. Corrections and responses: contact {PUBLISHER}."])

    md = d.markdown()
    body_sha = hashlib.sha256(md.encode("utf-8")).hexdigest()
    md += f"\nReport fingerprint (SHA-256): `{body_sha}`\n"
    d.p(f"Report fingerprint (SHA-256): `{body_sha}`")

    fid = rid
    with open(os.path.join(REPORT_DIR, f"{fid}.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(md)
    with open(os.path.join(REPORT_DIR, f"{fid}.html"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(d.html(f"{S['label']}: {PUBLISHER} report"))
    vault = os.path.dirname(audit.ANCHOR_PATH)
    working = None
    if os.path.isdir(vault):                      # your working copy: edit this one (fill in the right of reply)
        wdir = os.path.join(vault, "Auto reports" if folder == "auto" else "Drafts for publishing")
        os.makedirs(wdir, exist_ok=True)
        working = os.path.join(wdir, f"{fid}.md")
        with open(working, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(md)
    import json
    with open(os.path.join(REPORT_DIR, f"{fid}.json"), "w", encoding="utf-8") as fh:
        json.dump({"report_id": fid, "subject": subject_id, "subject_label": S["label"], "path_to": path_to,
                   "drafted_at": store.now_iso(), "sha256": body_sha, "working_copy": working, "folder": folder,
                   "named_organizations": named, "claims": list(src.claims.values()),
                   "sources": [{**r, "supports": sorted(r["supports"])} for r in src.rows]}, fh, indent=1, ensure_ascii=False)
    audit.log_event("report_drafted", report_id=fid, subject=subject_id, path_to=path_to, sha256=body_sha,
                    findings={"direct": len(direct), "links": len(links), "balance": len(ind) + len(ten) + len(sp) + len(obf)})
    return {"id": fid, "html_url": f"/reports/{fid}.html", "md_url": f"/reports/{fid}.md", "sha256": body_sha,
            "review_url": f"/reports/{fid}-reviewed.html"}
