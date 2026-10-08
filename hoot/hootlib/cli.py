"""HOOT command line. Run `hoot help` for usage."""
import argparse
import json
import os
import re
import sys
import traceback

from . import yamlish
from .analysis import Reports
from .render import Renderer
from .sources import gleif, littlesis, nih, openalex, opensanctions, sec
from .store import RELATIONS, Store
from .util import CONFIG_PATH, DEFAULT_CONFIG, VAULT_DIR, Http, load_config, log, today

WATCH_RE = re.compile(r"^\s*[-*]\s+(sec|sec-person|13f|gleif|openalex|author|nih-pi|nih-org|nih-project|littlesis|sanctions|expand|careers|formd|nonprofit|uk|people)\s*:\s*(.+?)\s*$", re.I)


def ctx():
    cfg = load_config()
    s = Store()
    return cfg, Http(cfg), s, Renderer(s, cfg)


def finish(s, r):
    r.render()


def read_watchlist(cfg):
    p = os.path.join(VAULT_DIR, cfg["notes_folder"], "Watchlist.md")
    if not os.path.exists(p):
        return []
    items, fence = [], False
    for line in open(p, encoding="utf-8"):
        if line.strip().startswith("```"):
            fence = not fence
            continue
        m = WATCH_RE.match(line)
        if m and not fence:
            items.append((m.group(1).lower(), m.group(2)))
    return items


def do_expand(budget, cfg, http, s, r):
    from . import loops
    cands = loops.frontier(s, limit=budget)
    log(f"expand: pulling {len(cands)} frontier companies (most links into the mapped core first)")
    for core, people, _, cik, name in cands:
        log(f"  {name}: shares people with {core} mapped companies ({people} people)")
        try:
            sec.ingest_company(http, s, cfg, cik)
        except Exception as ex:                    # noqa: BLE001
            log(f"  ! {name}: {ex}")
        s.commit()


def run_source(kind, arg, cfg, http, s, r):
    if kind == "sec":
        sec.ingest_company(http, s, cfg, arg)
    elif kind == "sec-person":
        sec.ingest_person(http, s, cfg, sec.pad(arg))
    elif kind == "13f":
        sec.ingest_13f(http, s, cfg, arg)
    elif kind == "gleif":
        gleif.ingest(http, s, cfg, arg)
    elif kind == "openalex":
        openalex.ingest_work(http, s, cfg, arg)
    elif kind == "author":
        openalex.ingest_author(http, s, cfg, arg)
    elif kind == "nih-pi":
        nih.ingest(http, s, cfg, pi=arg)
    elif kind == "nih-org":
        nih.ingest(http, s, cfg, org=arg)
    elif kind == "nih-project":
        nih.ingest(http, s, cfg, project=arg)
    elif kind == "littlesis":
        littlesis.ingest(http, s, cfg, arg)
    elif kind == "people":
        from . import people
        people.run(http, s, cfg, limit=int(arg) if arg.strip().isdigit() else 150, renderer=r)
    elif kind == "uk":
        from .sources import ukch
        ukch.ingest(http, s, cfg, arg)
    elif kind == "formd":
        from .sources import private
        private.ingest_formd(http, s, cfg, arg)
    elif kind == "nonprofit":
        from .sources import private
        private.ingest_990(http, s, cfg, arg)
    elif kind == "careers":
        sec.careers_sweep(http, s, limit=None if arg.strip().lower() == "all" else int(arg))
    elif kind == "expand":
        do_expand(int(arg), cfg, http, s, r)
    elif kind == "sanctions":
        opensanctions.check(http, s, cfg, Reports(s, r, cfg).resolve(arg))


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="hoot", description="HOOT: evidence-first relationship graph for Obsidian")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("sec", help="SEC company: insiders (Forms 3/4/5), 13D/G owners, key filings")
    a.add_argument("company", help="ticker, CIK or name")
    a.add_argument("--no-expand", action="store_true", help="don't follow insiders to their other companies")
    a.add_argument("--max-filings", type=int)
    a = sub.add_parser("sec-person", help="SEC insider by CIK: every board/officer role they reported")
    a.add_argument("cik")
    a = sub.add_parser("13f", help="institutional manager's latest 13F holdings")
    a.add_argument("manager", help="ticker, CIK or name")
    a.add_argument("--top", type=int, default=50)
    a = sub.add_parser("gleif", help="GLEIF LEI + parent/child ownership tree")
    a.add_argument("query", help="LEI or legal name")
    a.add_argument("--no-children", action="store_true")
    a = sub.add_parser("openalex", help="study by DOI: authors, institutions, funders, award ids")
    a.add_argument("doi")
    a = sub.add_parser("author", help="OpenAlex author (ORCID or A-id): their recent works")
    a.add_argument("id")
    a.add_argument("--limit", type=int, default=25)
    a = sub.add_parser("nih", help="NIH RePORTER grants")
    a.add_argument("--pi")
    a.add_argument("--org")
    a.add_argument("--project")
    a.add_argument("--limit", type=int, default=100)
    a = sub.add_parser("littlesis", help="LittleSis entity (id or name) and its relationships")
    a.add_argument("query")
    a = sub.add_parser("sanctions", help="OpenSanctions name screening for an entity (needs API key)")
    a.add_argument("entity")

    a = sub.add_parser("link", help="record a manual, cited connection")
    a.add_argument("frm")
    a.add_argument("relation", help="e.g. " + ", ".join(list(RELATIONS)[:8]) + " …")
    a.add_argument("to")
    a.add_argument("--evidence", required=True, help="URL of the source document")
    a.add_argument("--source", default="manual research")
    a.add_argument("--date", help="filing/publication date of the evidence (YYYY-MM-DD)")
    a.add_argument("--start")
    a.add_argument("--end")
    a.add_argument("--percent", type=float)
    a.add_argument("--amount", type=float)
    a.add_argument("--qualifier")
    a.add_argument("--former", action="store_true")

    sub.add_parser("sync", help="read hand edits (domains, manual edge notes) back into the graph")
    a = sub.add_parser("render", help="re-render notes")
    a.add_argument("--all", action="store_true")
    a = sub.add_parser("paths", help="every documented path between two entities")
    a.add_argument("a")
    a.add_argument("b")
    a.add_argument("--hops", type=int, default=4)
    a.add_argument("--hard", action="store_true", help="hard structural control only")
    a.add_argument("--current", action="store_true", help="current ties only")
    a.add_argument("--limit", type=int, default=25)
    a.add_argument("--avoid-hubs", type=int, help="don't route through nodes with more than N ties")
    a = sub.add_parser("near", help="everything within N hops of an entity")
    a.add_argument("entity")
    a.add_argument("--hops", type=int, default=2)
    sub.add_parser("patterns", help="interlocks, common owners, shared funders, brokers, cross-domain bridges")
    sub.add_parser("audit", help="integrity audit: missing evidence, duplicates, edited notes")
    a = sub.add_parser("recheck", help="re-download SEC source documents and compare hashes")
    a.add_argument("--limit", type=int, default=200)
    a = sub.add_parser("merge", help="merge two entities that are the same (keep first)")
    a.add_argument("keep")
    a.add_argument("drop")
    a = sub.add_parser("loops", help="closed loops of organizations linked through people (SEC-filed roles)")
    a.add_argument("--max-len", type=int, default=4)
    a.add_argument("--include-former", action="store_true")
    a = sub.add_parser("dedupe", help="same person, two records: fold name variants into the host record (two facts required)")
    a.add_argument("--dry", action="store_true", help="show what would change; change nothing")
    sub.add_parser("conflicts", help="conflict signals: competitor interlocks, over-boarding, common ownership, hops, nonprofit bridges")
    a = sub.add_parser("people", help="people sweep: find an official record for every person named, then all their companies")
    a.add_argument("--limit", type=int, default=150)
    a.add_argument("--only", help="look up just these names now, separated by ';'")
    a = sub.add_parser("uk", help="UK company via Companies House: officers with registry IDs, and their other UK appointments")
    a.add_argument("company", help="name or 8-character company number")
    a.add_argument("--no-appointments", action="store_true")
    a = sub.add_parser("formd", help="private company via SEC Form D: its directors and executive officers")
    a.add_argument("company", help="name or CIK")
    a = sub.add_parser("nonprofit", help="nonprofit via IRS Form 990: board, officers, pay, related organizations")
    a.add_argument("org", help="name or EIN")
    a = sub.add_parser("careers", help="every company each person has ever filed for at the SEC (finds links outside the map)")
    a.add_argument("--limit", type=int)
    a.add_argument("--person", help="one person's SEC CIK")
    a = sub.add_parser("frontier", help="list companies most likely to close new loops if pulled")
    a.add_argument("--limit", type=int, default=25)
    a = sub.add_parser("expand", help="pull the top N frontier companies, then find loops")
    a.add_argument("budget", type=int, nargs="?", default=10)
    sub.add_parser("refresh", help="run every source in HOOT/Watchlist.md, then sync, patterns and audit")
    sub.add_parser("stats", help="counts")
    sub.add_parser("init", help="create config and starter notes")

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 0
    if args.cmd == "init":
        return init()

    cfg, http, s, r = ctx()
    rep = Reports(s, r, cfg)
    try:
        if args.cmd == "sec":
            sec.ingest_company(http, s, cfg, args.company, expand=False if args.no_expand else None,
                               max_filings=args.max_filings)
        elif args.cmd == "sec-person":
            sec.ingest_person(http, s, cfg, sec.pad(args.cik))
        elif args.cmd == "13f":
            sec.ingest_13f(http, s, cfg, args.manager, args.top)
        elif args.cmd == "gleif":
            gleif.ingest(http, s, cfg, args.query, children=not args.no_children)
        elif args.cmd == "openalex":
            openalex.ingest_work(http, s, cfg, args.doi)
        elif args.cmd == "author":
            openalex.ingest_author(http, s, cfg, args.id, args.limit)
        elif args.cmd == "nih":
            nih.ingest(http, s, cfg, pi=args.pi, org=args.org, project=args.project, limit=args.limit)
        elif args.cmd == "littlesis":
            littlesis.ingest(http, s, cfg, args.query)
        elif args.cmd == "sanctions":
            opensanctions.check(http, s, cfg, rep.resolve(args.entity))
        elif args.cmd == "link":
            def ent(q):
                try:
                    return rep.resolve(q)
                except SystemExit:
                    log(f"  creating new entity {q!r} (no identifier yet)")
                    return s.upsert_entity("unknown", q, {"manual": q.lower()})
            f = dict(source=args.source, evidence_url=args.evidence, filing_date=args.date, start_date=args.start,
                     end_date=args.end, percent=args.percent, amount=args.amount, qualifier=args.qualifier,
                     is_current=0 if args.former else (None if args.end else 1))
            if args.relation not in RELATIONS:
                log(f"  note: {args.relation!r} isn't a standard relation; it will count as soft influence (w=0.3)")
            s.add_edge(ent(args.frm), ent(args.to), args.relation, origin="manual", **f)
            s.commit()
        elif args.cmd == "sync":
            r.sync()
        elif args.cmd == "render":
            s.rehash()
            r.render(all_=True)
            return 0
        elif args.cmd == "paths":
            rep.paths(args.a, args.b, args.hops, args.hard, args.current, args.limit, args.avoid_hubs)
        elif args.cmd == "near":
            rep.neighborhood(args.entity, args.hops)
        elif args.cmd == "patterns":
            r.sync()
            rep.patterns()
        elif args.cmd == "audit":
            rep.audit()
        elif args.cmd == "recheck":
            rep.recheck_sources(args.limit)
        elif args.cmd == "merge":
            keep, drop = rep.resolve(args.keep), rep.resolve(args.drop)
            gone = s.merge(keep, drop)
            s.commit()
            log(f"merged {gone['name']} ({gone['canonical_id']}) into {s.entity(keep)['name']}")
        elif args.cmd == "loops":
            from . import loops
            loops.loops_report(s, r, cfg, args.max_len, not args.include_former)
        elif args.cmd == "dedupe":
            from . import dedupe
            out = dedupe.run(s, r, cfg, http, dry=args.dry)
            if args.dry:
                print("\n".join(out))
            else:
                r.render()
        elif args.cmd == "conflicts":
            from . import conflicts
            conflicts.report(s, r, cfg, http)
        elif args.cmd == "people":
            from . import people
            people.run(http, s, cfg, limit=args.limit, renderer=r,
                       only=[x.strip() for x in (args.only or "").split(";") if x.strip()] or None)
        elif args.cmd == "uk":
            from .sources import ukch
            ukch.ingest(http, s, cfg, args.company, appointments=not args.no_appointments)
        elif args.cmd == "formd":
            from .sources import private
            private.ingest_formd(http, s, cfg, args.company)
        elif args.cmd == "nonprofit":
            from .sources import private
            private.ingest_990(http, s, cfg, args.org)
        elif args.cmd == "careers":
            if args.person:
                n = sec.person_careers(http, s, args.person, log_each=True)
                s.commit()
                log(f"{n} new company links")
            else:
                sec.careers_sweep(http, s, limit=args.limit)
        elif args.cmd == "frontier":
            from . import loops
            for core, people, _, cik, name in loops.frontier(s, limit=args.limit):
                print(f"{core:3} mapped companies · {people:3} people · CIK {int(cik):<8} {name}")
            return 0
        elif args.cmd == "expand":
            do_expand(args.budget, cfg, http, s, r)
            r.render()
            from . import loops
            loops.loops_report(s, r, cfg)
        elif args.cmd == "refresh":
            items = read_watchlist(cfg)
            log(f"refresh: {len(items)} watchlist items")
            for kind, arg in items:
                try:
                    run_source(kind, arg, cfg, http, s, r)
                except SystemExit as ex:
                    log(f"  ! {kind}: {arg}: {ex}")
                except Exception as ex:             # noqa: BLE001
                    log(f"  ! {kind}: {arg}: {ex}")
                s.commit()
            r.render()
            r.sync()
            rep.patterns()
            rep.audit()
            from . import loops
            loops.loops_report(s, r, cfg)
            stamp = os.path.join(VAULT_DIR, cfg["notes_folder"], "Reports")
            os.makedirs(stamp, exist_ok=True)
        elif args.cmd == "stats":
            for t, n in s.db.execute("SELECT type, COUNT(*) FROM entities GROUP BY type"):
                print(f"{t:14} {n}")
            for c, n in s.db.execute("SELECT control, COUNT(*) FROM edges GROUP BY control"):
                print(f"edges/{c:8} {n}")
            return 0
        finish(s, r)
    except SystemExit as ex:
        s.commit()
        if ex.code not in (0, None):
            print(ex.code if isinstance(ex.code, str) else "", file=sys.stderr)
            return 1
    except Exception:                                # noqa: BLE001
        s.commit()
        traceback.print_exc()
        return 1
    return 0


def init():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        log(f"wrote {CONFIG_PATH}")
    log("config ok; starter notes live in HOOT/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
