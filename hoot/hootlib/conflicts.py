"""Conflict signals computable from the evidence HOOT holds. Each is a *signal for review*, tied to a
legal or governance standard, never a finding. Every line links back to its filings.

 1. Competitor interlocks        Clayton Act §8 (15 U.S.C. §19): same person as director/officer of two competitors
 2. Over-boarding                proxy-adviser policies (ISS / Glass Lewis): attention & loyalty spread thin
 3. Common ownership             the same >5% holder in two competitors (horizontal shareholding)
 4. Competitor hops              a person leaves one company's leadership for a competitor's
 5. Corporate–nonprofit bridges  public-company insiders governing nonprofits (IRS 990): philanthropy, capture
 6. Self-disclosed (IRS 990)     Part VI line 2 family/business relationships; missing conflict-of-interest policy
"""
from collections import defaultdict
from datetime import date, timedelta

from . import yamlish
from .util import log, today

ACTIVE_DAYS = 456


def _active(e):
    if e["is_current"] == 0:
        return False
    last = e["last_seen"] or e["filing_date"]
    return bool(last) and last >= (date.today() - timedelta(days=ACTIVE_DAYS)).isoformat()


def _ok_identity(e):
    return ((e["attrs"].get("identity_match") or {}).get("confidence")) in (None, "high", "registry")


def _sic(store, http, oid, cache):
    if oid in cache:
        return cache[oid]
    e = store.entity(oid)
    sic = e["attrs"].get("sic")
    if not sic and e["ids"].get("sec_cik") and http is not None:
        try:
            d = http.json(f"https://data.sec.gov/submissions/CIK{e['ids']['sec_cik']}.json", ttl=30 * 86400)
            sic = d.get("sic")
            if sic:
                store.set_attr(oid, "sic", sic)
                store.set_attr(oid, "industry", d.get("sicDescription"))
        except Exception:                                    # noqa: BLE001
            sic = None
    cache[oid] = str(sic) if sic else None
    return cache[oid]


def report(store, renderer, cfg, http=None):
    L = renderer.link
    name = lambda i: store.entity(i)["name"]
    roles = defaultdict(list)                     # person -> [edge] (director/officer, primary, identity ok)
    for e in store.edges("relation IN ('director_of','officer_of')"):
        s = (e["source"] or "").upper()
        if (s.startswith("SEC FORM") or s.startswith("IRS FORM 990") or s.startswith("UK COMPANIES HOUSE")) and _ok_identity(e):
            roles[e["src"]].append(e)
    sics, out = {}, ["# Conflict signals", "",
                     "Each section applies a recognized legal or governance standard to the filings HOOT holds. "
                     "These are **signals for review, not findings**: each needs the context in the linked filings.", ""]

    # affiliates are not competitors: same name stem, a recorded parent/subsidiary tie, or mostly the same board
    import re as _re
    boards_of = defaultdict(set)
    for p, es in roles.items():
        for e in es:
            if e["relation"] == "director_of" and _active(e):
                boards_of[e["dst"]].add(p)
    parent = {(e["src"], e["dst"]) for e in store.edges("relation IN ('subsidiary_of','ultimately_owned_by')")}
    stem = lambda i: (_re.sub(r"[^a-z0-9 ]", " ", name(i).lower()).split() or [""])[0]

    def affiliated(a, b):
        if stem(a) == stem(b) or (a, b) in parent or (b, a) in parent:
            return True
        A, B = boards_of.get(a, set()), boards_of.get(b, set())
        shared = len(A & B)
        return min(len(A), len(B)) >= 4 and shared >= 3 and shared / min(len(A), len(B)) >= 0.5
    excluded = []

    # 1 + 4: competitor interlocks and competitor hops (same 4-digit SIC)
    inter, hops = [], []
    for p, es in roles.items():
        pub = [e for e in es if (e["source"] or "").upper().startswith("SEC FORM 3") or (e["source"] or "").upper().startswith("SEC FORM 4")
               or (e["source"] or "").upper().startswith("SEC FORM 5")]
        by_org = {}
        for e in pub:
            by_org.setdefault(e["dst"], []).append(e)
        orgs = list(by_org)
        for i, a in enumerate(orgs):
            for b in orgs[i + 1:]:
                sa, sb = _sic(store, http, a, sics), _sic(store, http, b, sics)
                if not sa or sa != sb:
                    continue
                if affiliated(a, b):
                    excluded.append((a, b))
                    continue
                act_a = any(_active(x) for x in by_org[a]); act_b = any(_active(x) for x in by_org[b])
                if act_a and act_b:
                    inter.append((p, a, b, sa, by_org[a][0], by_org[b][0]))
                elif act_a != act_b:
                    old, new = (a, b) if act_b else (b, a)
                    hops.append((p, old, new, sa, by_org[old][0], by_org[new][0]))
    out += [f"## 1. Competitor interlocks: Clayton Act §8 ({len(inter)})", "",
            "Same person currently a director or officer of two companies in the same 4-digit SEC industry code. "
            "§8 bars this between *competitors* above size thresholds; same industry code ≠ competitors, so check overlap of products/markets.", ""]
    for p, a, b, sic, ea, eb in sorted(inter, key=lambda x: name(x[0])):
        out.append(f"- {L(p)}: {L(a)} ([filing]({ea['evidence_url']})) & {L(b)} ([filing]({eb['evidence_url']})) · SIC {sic} {store.entity(a)['attrs'].get('industry') or ''}")
    ex = sorted({tuple(sorted((name(a), name(b)))) for a, b in excluded})
    if ex:
        out += ["", f"*Excluded as affiliates (parent/subsidiary or shared board), not competitors: {len(ex)} pairs, e.g. "
                + "; ".join(f"{a} & {b}" for a, b in ex[:6]) + "*"]
    out += ["", f"## 4. Competitor hops ({len(hops)})", "",
            "Person who held a role at one company and now holds one at another in the same industry code: "
            "knowledge transfer / non-compete / revolving-door signal.", ""]
    for p, old, new, sic, eo, en in sorted(hops, key=lambda x: name(x[0]))[:150]:
        out.append(f"- {L(p)}: formerly {L(old)} (last filing {eo['last_seen']}) → now {L(new)} · SIC {sic}")

    # 2: over-boarding
    over = []
    for p, es in roles.items():
        boards = {e["dst"] for e in es if e["relation"] == "director_of" and _active(e) and (e["source"] or "").upper().startswith("SEC FORM")}
        ceo = [e for e in es if e["relation"] == "officer_of" and _active(e) and
               any(t in (e["qualifier"] or "").upper() for t in ("CHIEF EXECUTIVE", "CEO", "PRESIDENT & CEO"))]
        families = {stem(b) for b in boards if store.entity(b)["attrs"].get("sic") != "6770"}   # fund/SPAC families count once
        outside = {stem(b) for b in boards} - {stem(c["dst"]) for c in ceo}
        if len(families) >= 5 or (ceo and len(outside) >= 2):
            over.append((p, boards, ceo))
    out += ["", f"## 2. Over-boarding ({len(over)})", "",
            "5+ current public boards, or a sitting CEO with 2+ outside public boards (common proxy-adviser thresholds). "
            "Boards in one fund or SPAC family count once.", ""]
    for p, boards, ceo in sorted(over, key=lambda x: -len(x[1])):
        out.append(f"- {L(p)}: {len(boards)} boards" + (f" while CEO of {L(ceo[0]['dst'])}" if ceo else "") + ": " +
                   ", ".join(L(b) for b in sorted(boards, key=name)))

    # 3: common ownership
    holders = defaultdict(list)
    for e in store.edges("relation IN ('beneficial_owner_of','ten_percent_owner_of')"):
        if _active(e) and (e["percent"] is None or e["percent"] >= 5):
            holders[e["src"]].append(e)
    common = []
    for h, es in holders.items():
        by_sic = defaultdict(list)
        for e in es:
            s = _sic(store, http, e["dst"], sics)
            if s:
                by_sic[s].append(e)
        for s, group in by_sic.items():
            if len({x["dst"] for x in group}) >= 2:
                common.append((h, s, group))
    out += ["", f"## 3. Common ownership of competitors ({len(common)})", "",
            "The same >5% holder in two or more companies of one industry: horizontal-shareholding concern (softened competition).", ""]
    for h, s, group in common:
        out.append(f"- {L(h)} in SIC {s}: " + ", ".join(f"{L(x['dst'])}" + (f" {x['percent']:g}%" if x["percent"] is not None else "") for x in group))

    # 5: corporate–nonprofit bridges
    bridges = []
    for p, es in roles.items():
        corp = [e for e in es if (e["source"] or "").upper().startswith("SEC FORM") and _active(e)]
        npo = [e for e in es if (e["source"] or "").upper().startswith("IRS FORM 990") and _active(e)]
        if corp and npo:
            bridges.append((p, corp, npo))
    out += ["", f"## 5. Corporate–nonprofit bridges ({len(bridges)})", "",
            "Current public-company insiders who also govern a nonprofit (IRS 990). Watch for corporate giving to that "
            "nonprofit, policy advocacy, or the nonprofit's Schedule L transactions with insiders.", ""]
    for p, corp, npo in bridges:
        out.append(f"- {L(p)}: " + ", ".join(L(e["dst"]) for e in corp) + " ⇄ " +
                   ", ".join(f"{L(e['dst'])} ({e['attrs'].get('title') or e['relation']})" for e in npo))

    # 6: self-disclosed in 990 Part VI
    flagged = []
    for r in store.db.execute("SELECT id FROM entities WHERE attrs LIKE '%part_vi%'"):
        a = store.entity(r[0])["attrs"].get("part_vi") or {}
        notes = []
        if a.get("family_or_business_relationships_among_officers"):
            notes.append("reports family/business relationships among officers/directors (Part VI line 2)")
        if a.get("conflict_of_interest_policy") is False:
            notes.append("no written conflict-of-interest policy (Part VI line 12a)")
        if notes:
            flagged.append((r[0], notes))
    out += ["", f"## 6. Self-disclosed by nonprofits (IRS 990 Part VI) ({len(flagged)})", ""]
    out += [f"- {L(o)}: " + "; ".join(n) for o, n in flagged] or ["- none among the nonprofits pulled so far"]

    rel = f"{cfg.get('notes_folder', 'HOOT')}/Reports/Conflict signals.md"
    fm = {"hoot": "report", "generated": today(), "competitor_interlocks": len(inter), "overboarded": len(over),
          "common_ownership": len(common), "competitor_hops": len(hops), "nonprofit_bridges": len(bridges), "tags": ["hoot/report"]}
    renderer._write(rel, yamlish.dump(fm) + "\n".join(out) + "\n")
    store.commit()
    log(f"conflicts: {len(inter)} competitor interlocks · {len(over)} over-boarded · {len(common)} common-ownership · "
        f"{len(hops)} competitor hops · {len(bridges)} nonprofit bridges -> {rel}")
