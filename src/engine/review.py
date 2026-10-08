"""Second pass on every report: an independent review that runs AFTER the draft is written.

Version 1 is the draft. This review re-checks it from scratch and writes version 2 ("reviewed") plus a private
notes file for the investigator. It never trusts the draft: it re-opens every source and re-reads the records.

  1 Sources     every cited filing is re-opened; SEC filings must name BOTH parties of each claim (by CIK)
  2 Records     every claim's record still exists, unchanged (same fingerprint) and not superseded
  3 Wording     the text is scanned for accusations, legal conclusions, motive, overstated certainty and
                private details; each hit says why it is risky and what to write instead
  4 Structure   the protective sections are present: disclaimer, method, limits, right of reply, sources
  5 Reply       every named organization was contacted, and answered or had time to (REPLY_WAIT_DAYS)
  6 Integrity   the original draft is unaltered and the audit log is intact
  + Clarity     a plain-language glossary of every technical term the report uses

Verdicts: BLOCKED (do not publish) · NEEDS ATTENTION (fix the notes) · VERIFIED: NEXT STEP RIGHT OF REPLY ·
READY TO PUBLISH. This is a safety net built on journalism practice, not legal advice."""
import hashlib
import json
import os
import re
import sys
import threading
from datetime import date

from . import paths
from . import audit, report, store
from .validator import is_superseded

REPLY_WAIT_DAYS = 14          # common newsroom practice for a reasonable reply window; not a legal rule
HOOT_DIR = paths.HOOT_DIR
_lock = threading.Lock()

# (pattern, severity, why it is risky, what to write instead)
RISK = [
    (r"\bcorrupt(ion|ed|ly)?\b", "block", "a legal or moral conclusion only a court or regulator can reach",
     "state the filed fact and the standard it is measured against"),
    (r"\bfraud(ulent|ster)?s?\b", "block", "an accusation of a crime", "describe what the filing reports"),
    (r"\b(illegal(ly)?|unlawful(ly)?|crimes?|criminal(ly|s)?|felon(y|ies|s)?)\b", "block",
     "says a law was broken: only a court can find that", "write 'the standard restricts…' and cite it"),
    (r"\bviolat(e|es|ed|ing|ion|ions)\b", "block", "says a rule was broken: a legal conclusion",
     "write 'the standard restricts…' or 'is measured against…'"),
    (r"\bguilty\b", "block", "a court verdict word", "remove it"),
    (r"\b(false claims?|over.?bill(ed|ing|s)?|double.?bill(ed|ing)?|defraud(ed|ing|s)?( the)? (government|taxpayers?|medicare|medicaid)"
     r"|bilk(ed|ing)?|stole from (the )?(government|taxpayers?)|qui tam)\b", "block",
     "an allegation of fraud against the government (False Claims Act territory)",
     "remove it; if you believe you found this, see the False Claims Act hold in your review notes"),
    (r"\b(brib(e|es|ed|ery)|kickbacks?|launder(ing|ed|s)?|embezzl\w*|extort\w*|racketeer\w*|money.?launder\w*)\b", "block",
     "an accusation of a specific crime", "describe the documented roles and holdings only"),
    (r"\b(conspir\w*|collu(de|ded|sion|sive)|cabal|cartel|cover.?up|rigged|puppets?|shadowy|stooges?)\b", "warn",
     "a loaded word that implies secret wrongdoing", "describe the documented connection in neutral terms"),
    (r"\b(scheme|scam|crook\w*|cronies|cronyism|kleptocra\w*|corporate greed|evil)\b", "warn",
     "a loaded word that implies wrongdoing", "describe the documented connection in neutral terms"),
    (r"\b(secret(ly)?|hidden|conceal(ed|ing|s)?)\b", "warn",
     "these filings are public; 'secret' implies concealment you would have to prove", "write 'filed with the SEC on …'"),
    (r"\b(in order to|so that they|so as to|to enrich|to benefit (him|her|them)sel\w*|intended to|deliberately|knowingly|"
     r"purposely|on purpose|motive|wanted to|plotted)\b", "warn",
     "states a motive; filings show roles, not intentions", "remove the motive; keep the filed fact"),
    (r"\b(clearly|obviously|undoubtedly|without (a )?doubt|beyond doubt|proves?|proof that|certainly)\b", "warn",
     "overstates certainty", "let the filings speak: 'filings report…'"),
    (r"\bconflicts? of interest\b", "warn", "a conflict of interest is a finding, not a filed fact",
     "write 'a conflict signal under [named standard]'"),
    (r"\b(controls?|controlled|controlling)\b", "note", "'control' is a legal term",
     "use it only for filed control positions (10%+ owner, officer) or quote the filing"),
    (r"[\w.+-]+@[\w-]+\.[\w.]+", "warn", "an email address (private contact details don't belong in a public report)",
     "remove it (organizations' public contact pages are fine)"),
    (r"(?<![\d-])(\+?1[\s.-]?)?\(?\d{3}\)?[\s.]\d{3}[\s.-]\d{4}\b", "warn", "looks like a phone number", "remove it"),
    (r"\b\d{1,6}\s+\w+(\s\w+)?\s(Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Way|Place|Pl)\b\.?",
     "warn", "looks like a street address", "remove home addresses; a company's registered office is fine"),
    (r"\b(home address|wife|husband|spouse|children|daughter|son|divorce|medical|illness|religion|religious|sexual)\b", "warn",
     "personal life, not a public role", "keep to public roles and filed holdings"),
]
NEGATION = re.compile(r"\b(not|never|no|without|nor|cannot|isn't|aren't|wasn't|don't|doesn't)\b[^.]*$", re.I)

GLOSSARY = [
    ("Form 3/4/5", "Forms that company insiders (directors, officers, owners of over 10%) must file with the U.S. SEC "
                   "when they join, trade shares, or report yearly. They name both the person and the company."),
    ("Schedule 13D/13G", "SEC filings required from anyone who owns more than 5% of a company's voting shares."),
    ("DEF 14A", "A company's annual proxy statement: the board, executive pay and matters shareholders vote on."),
    ("Form D", "An SEC notice filed by private companies raising money; it lists their directors and executives."),
    ("Form 990", "The annual return U.S. nonprofits file with the IRS; it lists board members and officers."),
    ("SEC", "U.S. Securities and Exchange Commission, the federal regulator whose public filing system is EDGAR."),
    ("CIK", "Central Index Key: the permanent ID number the SEC gives every filer, person or company."),
    ("LEI", "Legal Entity Identifier: a global company ID from the GLEIF registry."),
    ("EIN", "Employer Identification Number: the IRS tax ID of an organization."),
    ("director", "A member of the board that oversees the organization."),
    ("officer", "A senior executive (for example CEO, CFO, General Counsel) as filed."),
    ("10%+ owner", "Owns more than 10% of a class of the company's shares, as filed."),
    ("5%+ owner", "Owns more than 5% of a class of the company's shares, as filed."),
    ("current", "A filing reported this within about the last 15 months."),
    ("historical", "Filings reported this in the past, with nothing newer on record."),
    ("crossing", "The two organizations are in different sectors (domains) of the map."),
    ("HHI", "Herfindahl-Hirschman Index, a standard measure of how concentrated something is (0 to 10,000)."),
    ("Clayton Act", "A U.S. antitrust law; section 8 restricts the same person sitting on the boards of competitors."),
    ("SHA-256", "A digital fingerprint: any change to a document, even one letter, changes it completely."),
]


def _sidecar(fid):
    with open(os.path.join(report.REPORT_DIR, f"{fid}.json"), encoding="utf-8") as f:
        return json.load(f)


def _save_sidecar(fid, sc):
    p = os.path.join(report.REPORT_DIR, f"{fid}.json")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(sc, f, indent=1, ensure_ascii=False)
    os.replace(p + ".tmp", p)


def _http():
    sys.path.insert(0, HOOT_DIR)
    from hootlib.util import Http, load_config
    return Http(load_config())


# ------------------------------------------------------------------ checks
def check_sources(sc, graph, http, progress=None):
    """Re-open every cited filing; for SEC filings, both parties of each claim must appear by CIK."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    cik = lambda i: ((nodes.get(i) or {}).get("identity") or {}).get("sec_cik")
    parties = {}                                         # source number -> CIKs that must appear in it
    for c in sc["claims"]:
        for s in c["sources"]:
            parties.setdefault(s, set()).update(x for x in (cik(c["source"]), cik(c["target"])) if x)
    out = []
    for i, r in enumerate(sc["sources"]):
        sid = f"S{r['n']}"
        res = {"source": sid, "url": r["url"], "ref": r.get("ref")}
        try:
            b, sha = http.request(r["url"], ttl=None)    # filings never change: cached after the first check
            text = b.decode("utf-8", "replace")
            res["sha256"] = sha
            if r.get("ref") and r["ref"] not in text:
                res.update(status="block", detail="page opened but does not show this filing's reference number")
            elif "sec.gov" in r["url"]:
                missing = [c for c in parties.get(sid, ()) if c.zfill(10) not in text]
                res.update(status="ok" if not missing else "warn",
                           detail="opened; names both parties" if not missing
                           else f"opened, but party ID(s) {', '.join(missing)} not listed on the filing index")
            else:
                res.update(status="ok", detail="opened")
        except Exception as ex:                          # noqa: BLE001
            res.update(status="block", detail=f"could not be opened ({str(ex)[:80]})")
        out.append(res)
        if progress and i % 25 == 0:
            progress(i, len(sc["sources"]))
    return out


def check_records(sc, graph):
    edges = {e["edge_id"]: e for e in graph["edges"]}
    out = []
    for c in sc["claims"]:
        e = edges.get(c["edge_id"])
        if not e:
            out.append({"claim": c["statements"][0], "status": "block", "detail": "the record no longer exists"})
        elif is_superseded(e):
            out.append({"claim": c["statements"][0], "status": "warn",
                        "detail": f"replaced since the draft by {e['integrity']['superseded_by']}: "
                                  f"{e['integrity'].get('supersede_reason', '')[:120]}; draft again"})
        elif c.get("verification_hash") and (e.get("integrity") or {}).get("verification_hash") != c["verification_hash"]:
            out.append({"claim": c["statements"][0], "status": "warn", "detail": "record changed since the draft; draft again"})
    return out


def _prose_lines(md):
    """Lines a reader reads as prose, with link targets and code removed (sources tables excluded)."""
    skip = False
    for i, line in enumerate(md.splitlines(), 1):
        if line.startswith("## ") or line.startswith("### "):
            # the sources list, and agency records quoted as published (attributed, not our claims), are not linted
            skip = line.startswith("## 7.") or "All sources" in line or line.startswith("### 4.5 Public enforcement records")
        if skip:
            continue
        clean = re.sub(r"\]\([^)]*\)", "]", line)
        clean = re.sub(r"`[^`]*`", "", clean)
        yield i, clean


PRIVATE = ("email address", "phone number", "street address")
QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”")


def check_wording(md, names=()):
    """Quoted words (an organization's own reply) are theirs, not yours, and are skipped. Official names from the
    filings ('Motive Capital Corp II') are masked first. Tables come from filings, so they are checked only for legal
    conclusions and private details."""
    names = sorted({n for n in names if n and len(n) >= 4}, key=len, reverse=True)
    out = []
    for i, line in _prose_lines(md):
        table = line.startswith("|")
        text = QUOTED.sub(lambda m: " " * len(m.group(0)), line)
        for n in names:
            if n in text:
                text = text.replace(n, " " * len(n))
        for pat, sev, why, instead in RISK:
            private = any(p in why for p in PRIVATE)
            if table and not (sev == "block" or private):
                continue
            for m in re.finditer(pat, text, re.I):
                before = re.split(r"[.!?;:]\s", text[:m.start()])[-1]
                if not private and NEGATION.search(before):
                    continue                             # "not findings of wrongdoing", "no evidence of…"
                out.append({"line": i, "word": m.group(0), "status": sev, "why": why, "instead": instead,
                            "text": line.strip()[:160]})
    long = [(i, len(s.split())) for i, line in _prose_lines(md) if not line.startswith("|")
            for s in re.split(r"(?<=[.!?])\s+", line) if len(s.split()) > 45]
    for i, n in long:
        out.append({"line": i, "word": f"{n} words", "status": "note", "why": "a very long sentence is easy to misread",
                    "instead": "split it in two", "text": ""})
    return out


REQUIRED = [("Signals for review, not findings of wrongdoing", "the disclaimer line"),
            ("## 2. Scope", "Scope"), ("## 3. Method", "Method"), ("## 5. Limits", "Limits"),
            ("## 6. Right of reply", "Right of reply"), ("All sources", "the full sources list"),
            (report.PUBLISHER, "the publisher's legal name")]


# ------------------------------------------------------------------ False Claims Act hold
# Signs that a report has moved from governance connections into claims for government money. The generated
# report never contains these; they appear when the investigator adds findings about government payments.
FCA_SIGNS = re.compile(
    r"\b(federal (contracts?|grants?|funds?|awards?|payments?)|government (contracts?|grants?|funds?|payments?|billing)"
    r"|medicare|medicaid|tricare|usaspending|sam\.gov|invoic(e|es|ed|ing)|bill(ed|ing) (the )?(government|agency|va|dod|cms)"
    r"|reimburse(d|ment|ments)|cost.?plus|grant funds?|false claims?|qui tam|over.?bill\w*|kickbacks?|anti.?kickback)\b", re.I)
CLEARED = re.compile(r"legal consult:\s*(\d{4}-\d{2}-\d{2})[^\n]*\bcleared\b", re.I)


def check_fca(md):
    """A possible False Claims Act matter is never published from here. New fraud against the government belongs in
    a sealed whistleblower (qui tam) filing with a lawyer; publishing first can bar that claim (31 U.S.C. §3730(e)(4))
    and is high risk for you. The hold lifts only when your working copy records a legal consult that cleared it."""
    hits = []
    for i, line in _prose_lines(md):
        clean = QUOTED.sub(lambda m: " " * len(m.group(0)), line)
        for m in FCA_SIGNS.finditer(clean):
            hits.append({"line": i, "word": m.group(0), "text": line.strip()[:160]})
    cleared = CLEARED.search(md)
    return {"hold": bool(hits) and not cleared, "signs": hits[:20], "sign_count": len(hits),
            "cleared": {"date": cleared.group(1), "text": cleared.group(0)[:200]} if cleared else None}


def check_structure(md):
    out = [{"status": "block", "detail": f"missing: {what}"} for needle, what in REQUIRED if needle not in md]
    rows = [l for l in md.split("## 4. Findings", 1)[-1].split("## 5.", 1)[0].splitlines()
            if l.startswith("| ") and not l.startswith("| What") and not l.startswith("| Linked") and not l.startswith("| From")]
    bad = [l for l in rows if "](http" not in l]
    if bad:
        out.append({"status": "block", "detail": f"{len(bad)} finding row(s) without a source link (public record only)"})
    return out


def check_reply(md, named):
    sec = md.split("## 6. Right of reply", 1)[-1].split("\n## ", 1)[0]
    rows = {}
    for l in sec.splitlines():
        cells = [c.strip() for c in l.strip().strip("|").split("|")]
        if l.startswith("|") and len(cells) >= 3 and cells[0] not in ("Organization", "---") and not set(cells[0]) <= {"-"}:
            rows[cells[0]] = cells[1:3]
    today = date.today()
    waiting, missing, done = [], [], []
    for org in named:
        contacted, response = rows.get(org, ["", ""])
        if response:
            done.append(org)
        elif contacted:
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", contacted)
            if m and (today - date(*map(int, m.groups()))).days >= REPLY_WAIT_DAYS:
                done.append(org)                         # contacted, no answer within the window: say so in the report
            else:
                waiting.append(org)
        else:
            missing.append(org)
    return {"done": done, "waiting": waiting, "not_contacted": missing, "wait_days": REPLY_WAIT_DAYS}


def glossary(md):
    low = md.lower()
    return [(t, d) for t, d in GLOSSARY if re.search(r"(?<![\w])" + re.escape(t.lower()) + r"(?![\w])", low)]


# ------------------------------------------------------------------ the review
def run(fid, progress=None):
    with _lock:
        sc = _sidecar(fid)
        sc["review"] = {"state": "running", "started_at": store.now_iso()}
        _save_sidecar(fid, sc)
    try:
        return _run(fid, sc, progress)
    except Exception as ex:                              # noqa: BLE001
        sc["review"] = {"state": "error", "error": str(ex)[:300]}
        _save_sidecar(fid, sc)
        raise


def _run(fid, sc, progress):
    graph = store.load_graph()
    working = paths.relocate(sc.get("working_copy"))   # still found after the vault moves
    src_path = working if working and os.path.exists(working) else os.path.join(report.REPORT_DIR, f"{fid}.md")
    with open(src_path, encoding="utf-8") as f:
        md = f.read()
    with open(os.path.join(report.REPORT_DIR, f"{fid}.md"), encoding="utf-8") as f:
        original = f.read()
    orig_body = original.rsplit("\nReport fingerprint (SHA-256):", 1)[0]
    original_ok = hashlib.sha256(orig_body.encode("utf-8")).hexdigest() == sc["sha256"]
    edited = sum(1 for a, b in zip(md.splitlines(), original.splitlines()) if a != b) + abs(len(md.splitlines()) - len(original.splitlines()))

    sources = check_sources(sc, graph, _http(), progress)
    records = check_records(sc, graph)
    wording = check_wording(md, [n["label"] for n in graph["nodes"]]
                            + [a for n in graph["nodes"] for a in (n.get("provenance") or {}).get("aliases_seen_in_sources") or []])
    structure = check_structure(md)
    reply = check_reply(md, sc.get("named_organizations") or [])
    chain = audit.verify()

    integrity = []
    if not original_ok:
        integrity.append({"status": "block", "detail": "the original draft file was altered after it was written"})
    if not chain["ok"]:
        integrity.append({"status": "block", "detail": f"the audit log shows {chain['break_count']} problem(s)"})

    fca = check_fca(md)
    allx = sources + records + wording + structure + integrity
    n = lambda s: sum(1 for x in allx if x.get("status") == s)
    if fca["hold"]:
        verdict = "ON HOLD: possible False Claims Act matter, consult a lawyer before anything is published"
    elif n("block"):
        verdict = "BLOCKED: do not publish"
    elif n("warn"):
        verdict = "NEEDS ATTENTION: fix the review notes, then re-check"
    elif reply["not_contacted"] or reply["waiting"]:
        verdict = "VERIFIED: next step is the right of reply"
    else:
        verdict = "READY TO PUBLISH"
    src_ok = sum(1 for s in sources if s["status"] == "ok")

    # ---- version 2: the reviewed report
    v2 = md.rsplit("\nReport fingerprint (SHA-256):", 1)[0].rstrip() + "\n"
    v2 = re.sub(r"\*\*Status: DRAFT[^\n]*\*\*", f"**Status: REVIEWED (version 2), {date.today().isoformat()} · {verdict}**", v2)
    v2 = v2.replace("· version 1 · drafted", "· version 2 · reviewed · drafted")
    v2 = v2.replace("## 8. Integrity", "## 10. Integrity").replace(", version 1. Corrections", ", version 2. Corrections")
    v2 = v2.replace('under "report_drafted"', 'under "report_reviewed"').replace(
        "This report's own fingerprint", "This reviewed version's fingerprint")
    glos = glossary(v2)
    ins = ["## 8. Plain-language glossary", ""] + [f"- **{t}**: {d}" for t, d in glos] + [""]
    ins += ["## 9. Independent review", "",
            f"After drafting, this report was re-checked from scratch on {date.today().isoformat()}:", "",
            f"- **Sources:** {src_ok} of {len(sources)} cited filings re-opened"
            + (" and confirmed to name both parties." if src_ok == len(sources) else "; see the review notes for the rest."),
            f"- **Records:** {len(sc['claims']) - len(records)} of {len(sc['claims'])} records unchanged since drafting.",
            f"- **Wording:** checked for accusations, legal conclusions, motive, overstated certainty and private details: "
            f"{n('block') and 'problems found' or ('clear' if not [w for w in wording if w['status'] == 'warn'] else 'items to fix')}.",
            f"- **Right of reply:** {len(reply['done'])} of {len(sc.get('named_organizations') or [])} organizations answered or "
            f"had {REPLY_WAIT_DAYS} days to.",
            f"- **Integrity:** original draft {'unaltered' if original_ok else 'ALTERED'}; audit log "
            f"{'intact' if chain['ok'] else 'shows problems'}.", ""]
    v2 = v2.replace("## 10. Integrity", "\n".join(ins) + "\n## 10. Integrity", 1)
    body_sha = hashlib.sha256(v2.encode("utf-8")).hexdigest()
    v2 += f"\nReport fingerprint (SHA-256): `{body_sha}`\n"

    rid2 = f"{fid}-reviewed"
    with open(os.path.join(report.REPORT_DIR, f"{rid2}.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(v2)
    with open(os.path.join(report.REPORT_DIR, f"{rid2}.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(report.from_markdown(v2).html(f"{sc['subject_label']}: {report.PUBLISHER} report (reviewed)"))

    # ---- private notes for the investigator (not for publication)
    notes = [f"# Review notes: {fid}", "", f"**Verdict: {verdict}**", "",
             "These notes are for you, not for publication. Fix anything marked BLOCK or WARN in your working copy, "
             "then re-check (button on the report page, or it re-checks overnight).", "",
             f"Working copy reviewed: `{src_path}` ({edited} line(s) differ from the original draft).", ""]
    if fca["signs"]:
        notes += ["## 0. False Claims Act hold" + (" (ON HOLD)" if fca["hold"] else " (cleared)"), "",
                  "This report now talks about government money (contracts, grants, Medicare/Medicaid, billing). If it "
                  "suggests the government was billed falsely, that is a possible **False Claims Act** matter, and:", "",
                  "- **Don't publish it, and don't share the draft.** New fraud against the government is reported through "
                  "a sealed whistleblower (*qui tam*) case filed by a lawyer. Publishing first can bar that case "
                  "(31 U.S.C. §3730(e)(4)) and is high risk for you.",
                  "- **Talk to a whistleblower lawyer first.** Many take False Claims Act cases on contingency (paid only "
                  "from a recovery) or pro bono. If none will, this report waits until you can consult one.",
                  "- **Keep everything**: this report, its sources and your notes. Don't contact the organizations about it.", "",
                  "When a lawyer has advised you, add one line to your working copy and re-check:", "",
                  "`Legal consult: YYYY-MM-DD, [lawyer or organization], cleared for publication`", "",
                  "Lines that triggered the hold:"]
        notes += [f"- line {s['line']}: \"{s['word']}\"" for s in fca["signs"][:12]]
        if fca["cleared"]:
            notes += ["", f"Recorded consult: {fca['cleared']['text']}"]
        notes.append("")

    def sec(title, items, fmt):
        notes.extend([f"## {title}", ""] + ([fmt(x) for x in items] or ["- all clear"]) + [""])
    sec("1. Sources", [s for s in sources if s["status"] != "ok"],
        lambda s: f"- **{s['status'].upper()}** {s['source']} ({s.get('ref') or s['url']}): {s['detail']}")
    sec("2. Records", records, lambda r: f"- **{r['status'].upper()}** {r['claim']}: {r['detail']}")
    sec("3. Wording", wording, lambda w: f"- **{w['status'].upper()}** line {w['line']}: \"{w['word']}\": {w['why']}. "
                                         f"Instead: {w['instead']}." + (f"\n    > {w['text']}" if w["text"] else ""))
    sec("4. Structure", structure, lambda s: f"- **{s['status'].upper()}** {s['detail']}")
    notes += ["## 5. Right of reply", "",
              f"- answered, or no answer after {REPLY_WAIT_DAYS} days: {len(reply['done'])}",
              f"- contacted, still within the {REPLY_WAIT_DAYS}-day window: {', '.join(reply['waiting']) or 'none'}",
              f"- not contacted yet: {', '.join(reply['not_contacted']) or 'none'}",
              "", "Fill the table in section 6 of your working copy: date and how you contacted them "
              "(e.g. `2026-10-09 email to press office`), and their response. If they don't answer, the report should say "
              "\"did not respond by [date]\".", ""]
    sec("6. Integrity", integrity, lambda s: f"- **{s['status'].upper()}** {s['detail']}")
    notes_md = "\n".join(notes) + "\n"
    with open(os.path.join(report.REPORT_DIR, f"{fid}-review-notes.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(notes_md)
    if working:
        base = os.path.splitext(working)[0]
        with open(base + " (reviewed).md", "w", encoding="utf-8", newline="\n") as f:
            f.write(v2)
        with open(base + " (review notes).md", "w", encoding="utf-8", newline="\n") as f:
            f.write(notes_md)

    result = {"state": "done", "verdict": verdict, "finished_at": store.now_iso(),
              "counts": {"block": n("block"), "warn": n("warn"), "note": n("note")},
              "fca": {"hold": fca["hold"], "signs": fca["sign_count"], "cleared": fca["cleared"]},
              "sources_ok": src_ok, "sources": len(sources), "reply": {k: len(v) if isinstance(v, list) else v for k, v in reply.items()},
              "reviewed_url": f"/reports/{rid2}.html", "notes_url": f"/reports/{fid}-review-notes.md", "sha256": body_sha,
              "reviewed_mtime": os.path.getmtime(src_path)}
    sc["review"] = result
    _save_sidecar(fid, sc)
    audit.log_event("report_reviewed", report_id=fid, verdict=verdict, sha256=body_sha, counts=result["counts"],
                    sources_ok=src_ok, sources=len(sources), fca=result["fca"])
    return result


def run_background(fid):
    t = threading.Thread(target=lambda: _safe(fid), daemon=True)
    t.start()
    return t


def _safe(fid):
    try:
        run(fid)
    except Exception:                                    # noqa: BLE001  (state recorded in the sidecar)
        pass


def status(fid):
    try:
        return _sidecar(fid).get("review") or {"state": "pending"}
    except FileNotFoundError:
        return {"state": "unknown"}


def stale_reviews():
    """Reports whose working copy changed after their last review (you edited them), or never reviewed."""
    out = []
    for fn in os.listdir(report.REPORT_DIR) if os.path.isdir(report.REPORT_DIR) else []:
        if not fn.endswith(".json") or fn.startswith("auto_state"):
            continue
        fid = fn[:-5]
        try:
            sc = _sidecar(fid)
        except (ValueError, OSError):
            continue
        w = paths.relocate(sc.get("working_copy"))
        rv = sc.get("review") or {}
        if rv.get("state") != "done" or (w and os.path.exists(w) and os.path.getmtime(w) > (rv.get("reviewed_mtime") or 0) + 1):
            out.append(fid)
    return out
