"""SEC EDGAR: company profile, insiders (Forms 3/4/5 -> board seats & officer roles),
beneficial owners (Schedule 13D/13G XML -> % of class), and links to proxy statements."""
import re
from datetime import date, timedelta
import xml.etree.ElementTree as ET

from ..util import log, looks_like_org, norm_name, sec_person_name, smart_title

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
INSIDER_FORMS = {"3", "4", "5", "3/A", "4/A", "5/A"}
KEY_FORMS = ("DEF 14A", "10-K", "20-F", "13F-HR", "SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G", "8-K")


def pad(cik):
    return str(int(cik)).zfill(10)


def resolve(http, query):
    q = query.strip()
    if q.isdigit():
        return pad(q)
    data = http.json(TICKERS_URL, ttl=7 * 86400)
    rows = list(data.values())
    for r in rows:
        if r["ticker"].upper() == q.upper():
            return pad(r["cik_str"])
    ql = q.lower()
    hits = [r for r in rows if ql in r["title"].lower()]
    if len(hits) == 1:
        return pad(hits[0]["cik_str"])
    if hits:
        raise SystemExit("Ambiguous name, use ticker or CIK:\n" + "\n".join(
            f"  {h['ticker']:8} CIK {h['cik_str']:<10} {h['title']}" for h in hits[:25]))
    raise SystemExit(f"No SEC company matches {query!r} (try the CIK number)")


def submissions(http, cik):
    return http.json(f"https://data.sec.gov/submissions/CIK{pad(cik)}.json", ttl=86400)


def recent_rows(sub):
    r = sub.get("filings", {}).get("recent", {})
    n = len(r.get("form", []))
    keys = ["accessionNumber", "form", "filingDate", "primaryDocument", "reportDate"]
    return [{k: r.get(k, [None] * n)[i] for k in keys} for i in range(n)]


def doc_urls(cik, row):
    acc = row["accessionNumber"]
    nodash = acc.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}"
    prim = (row.get("primaryDocument") or "").split("/")[-1]      # strip xsl rendering folder
    return f"{base}/{prim}", f"{base}/{acc}-index.htm"


def _t(el, path):
    x = el.find(path)
    return x.text.strip() if x is not None and x.text else None


def _flag(el, path):
    v = _t(el, path)
    return v is not None and v.lower() in ("1", "true")


def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def company_entity(store, sub, cik):
    attrs = {
        "sic": sub.get("sic"), "industry": sub.get("sicDescription"),
        "state_of_incorporation": sub.get("stateOfIncorporation"),
        "tickers": sub.get("tickers"), "exchanges": sub.get("exchanges"),
        "sec_entity_type": sub.get("entityType"),
        "sec_profile_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={pad(cik)}",
    }
    former = [f.get("name") for f in sub.get("formerNames") or [] if f.get("name")]
    return store.upsert_entity("organization", smart_title(sub.get("name")) or f"CIK {cik}", {"sec_cik": pad(cik)},
                               attrs, aliases=former + [sub.get("name")])


def owner_entity(store, cik, raw_name):
    if looks_like_org(raw_name):
        return store.upsert_entity("organization", smart_title(raw_name), {"sec_cik": pad(cik)}, aliases=[raw_name])
    nice = sec_person_name(raw_name)
    return store.upsert_entity("person", nice, {"sec_cik": pad(cik)}, aliases=[raw_name])


def parse_ownership_doc(http, store, cik_in_path, row):
    """Form 3/4/5 -> edges reporting owner -> issuer. Returns list of (owner_cik) seen."""
    url, index = doc_urls(cik_in_path, row)
    try:
        b, sha = http.request(url, ttl=None)
        root = _strip_ns(ET.fromstring(b))
    except Exception as ex:                             # noqa: BLE001
        log(f"   ! could not parse {url}: {ex}")
        return []
    if root.tag != "ownershipDocument":
        return []
    issuer_cik = _t(root, "issuer/issuerCik")
    issuer_name = _t(root, "issuer/issuerName")
    if not issuer_cik:
        return []
    issuer = store.upsert_entity("organization", smart_title(issuer_name), {"sec_cik": pad(issuer_cik)},
                                 {"tickers": [_t(root, "issuer/issuerTradingSymbol")] if _t(root, "issuer/issuerTradingSymbol") else None})
    form = row["form"]
    seen = []
    for ro in root.findall("reportingOwner"):
        ocik = _t(ro, "reportingOwnerId/rptOwnerCik")
        oname = _t(ro, "reportingOwnerId/rptOwnerName")
        if not ocik:
            continue
        owner = owner_entity(store, ocik, oname)
        city, st = _t(ro, "reportingOwnerAddress/rptOwnerCity"), _t(ro, "reportingOwnerAddress/rptOwnerState")
        if city:
            store.set_attr(owner, "filing_city", f"{city.title()}, {st or ''}".strip(", "))
        seen.append(ocik)
        rel = ro.find("reportingOwnerRelationship")
        if rel is None:
            continue
        recent = row["filingDate"] >= (date.today() - timedelta(days=456)).isoformat()
        common = dict(source=f"SEC Form {form}", evidence_url=index, filing_date=row["filingDate"],
                      source_sha256=sha, is_current=1 if recent else 0,      # old filings alone don't prove a role is current
                      attrs={"period_of_report": _t(root, "periodOfReport"), "document_url": url})
        if _flag(rel, "isDirector"):
            store.add_edge(owner, issuer, "director_of", **common)
        if _flag(rel, "isOfficer"):
            title = _t(rel, "officerTitle")
            store.add_edge(owner, issuer, "officer_of", qualifier=title, **common)
        if _flag(rel, "isTenPercentOwner"):
            store.add_edge(owner, issuer, "ten_percent_owner_of", **common)
        if _flag(rel, "isOther"):
            store.add_edge(owner, issuer, "related_to", qualifier=_t(rel, "otherText"), **common)
    return seen


def parse_13dg(http, store, cik_in_path, row):
    """Structured Schedule 13D/13G (XML, filed since Dec 2024) -> beneficial_owner_of with % of class."""
    url, index = doc_urls(cik_in_path, row)
    if not url.endswith(".xml"):
        return
    try:
        b, sha = http.request(url, ttl=None)
        root = _strip_ns(ET.fromstring(b))
    except Exception as ex:                             # noqa: BLE001
        log(f"   ! could not parse {url}: {ex}")
        return
    issuer_cik = _t(root, ".//issuerInfo/issuerCik") or _t(root, ".//issuerCIK")
    issuer_name = _t(root, ".//issuerInfo/issuerName") or _t(root, ".//issuerName")
    if not issuer_cik:
        return
    issuer = store.upsert_entity("organization", smart_title(issuer_name), {"sec_cik": pad(issuer_cik)})
    filer_cik = _t(root, "headerData/filerInfo/filer/filerCredentials/cik")
    persons = root.findall(".//coverPageHeaderReportingPersonDetails") or root.findall(".//reportingPersonInfo")
    for i, p in enumerate(persons):
        name = _t(p, "reportingPersonName")
        if not name:
            continue
        pct = _t(p, "classPercent") or _t(p, "percentOfClass")
        shares = _t(p, "reportingPersonBeneficiallyOwnedAggregateNumberOfShares") or _t(p, "aggregateAmountOwned")
        try:
            pct = float(pct) if pct is not None else None
        except ValueError:
            pct = None
        # the filing's SEC ID belongs to whoever the SEC itself names for that ID, not to whoever is listed first
        filer_name = None
        if filer_cik:
            try:
                filer_name = submissions(http, filer_cik).get("name")
            except Exception:                                   # noqa: BLE001
                filer_name = None
        same = filer_name and (norm_name(filer_name) == norm_name(name) or
                               norm_name(sec_person_name(filer_name)) == norm_name(name))
        ids = {"sec_cik": pad(filer_cik)} if same else {"manual": "sec13:" + re.sub(r"\W+", "-", name.lower())}
        etype = "organization" if (looks_like_org(name) or _t(p, "typeOfReportingPerson") not in ("IN", None)) else "person"
        holder = store.upsert_entity(etype, smart_title(name), ids)
        store.add_edge(holder, issuer, "beneficial_owner_of", percent=pct,
                       is_current=0 if pct == 0 else 1,
                       source=f"SEC {row['form']}", evidence_url=index, filing_date=row["filingDate"],
                       source_sha256=sha,
                       attrs={"shares": shares, "class": _t(root, ".//securitiesClassTitle"),
                              "filer_type": _t(p, "typeOfReportingPerson"), "document_url": url})


def ingest_company(http, store, cfg, query, expand=None, max_filings=None):
    cik = resolve(http, query)
    sub = submissions(http, cik)
    comp = company_entity(store, sub, cik)
    log(f"SEC: {sub.get('name')} (CIK {int(cik)})")
    rows = recent_rows(sub)

    key = []
    for form in KEY_FORMS:
        hits = [r for r in rows if r["form"] == form or r["form"].startswith(form + "/")]
        for r in hits[:3]:
            _, index = doc_urls(cik, r)
            key.append({"form": r["form"], "date": r["filingDate"], "url": index})
    store.set_attr(comp, "key_filings", key)

    # every insider filing in the last ~15 months (directors often file only once a year), capped;
    # falls back to the most recent N when the window is quiet
    max_filings = max_filings or cfg["sec_max_insider_filings"]
    cutoff = (date.today() - timedelta(days=cfg.get("sec_insider_window_days", 456))).isoformat()
    allins = [r for r in rows if r["form"] in INSIDER_FORMS]
    ins = [r for r in allins if r["filingDate"] >= cutoff][:cfg.get("sec_max_window_filings", 600)]
    if len(ins) < max_filings:
        ins = allins[:max_filings]
    store.set_attr(comp, "insider_window", {"from": ins[-1]["filingDate"] if ins else None,
                                            "to": ins[0]["filingDate"] if ins else None, "filings": len(ins)})
    log(f"  parsing {len(ins)} insider filings (Forms 3/4/5) from {ins[-1]['filingDate'] if ins else '-'}...")
    owners = set()
    for n, r in enumerate(reversed(ins), 1):      # oldest first so newest facts win
        owners.update(parse_ownership_doc(http, store, cik, r))
        if n % 25 == 0:
            store.commit()                        # release the write lock regularly for other HOOT commands

    dg = [r for r in rows if r["form"].startswith("SCHEDULE 13")][:60]
    log(f"  parsing {len(dg)} structured Schedule 13D/13G filings...")
    for r in reversed(dg):
        parse_13dg(http, store, cik, r)
    legacy = [r for r in rows if r["form"].startswith("SC 13")]
    if legacy:
        log(f"  ({len(legacy)} older SC 13D/G filings are HTML/text; linked in key filings, not parsed)")

    expand = cfg["sec_expand_insiders"] if expand is None else expand
    if expand and owners:
        per = cfg["sec_expand_filings_per_person"]
        log(f"  expanding {len(owners)} insiders' other filings (board interlocks), {per} filings each...")
        for ocik in sorted(owners):
            expand_owner(http, store, ocik, per)
    store.commit()
    return comp


def expand_owner(http, store, owner_cik, per):
    try:
        sub = submissions(http, owner_cik)
    except Exception as ex:                             # noqa: BLE001
        log(f"   ! owner {owner_cik}: {ex}")
        return
    rows = [r for r in recent_rows(sub) if r["form"] in INSIDER_FORMS][:per]
    for r in reversed(rows):
        parse_ownership_doc(http, store, owner_cik, r)
    store.commit()


def ingest_person(http, store, cfg, cik, per=60):
    """All boards/officer roles an insider has reported (by their own CIK)."""
    sub = submissions(http, cik)
    log(f"SEC insider: {sub.get('name')} (CIK {int(cik)})")
    expand_owner(http, store, cik, per)
    store.commit()


def ingest_13f(http, store, cfg, query, top=50):
    """Latest 13F-HR of an institutional manager -> holds_shares_of edges for its top positions."""
    cik = resolve(http, query)
    sub = submissions(http, cik)
    mgr = company_entity(store, sub, cik)
    rows = [r for r in recent_rows(sub) if r["form"] in ("13F-HR", "13F-HR/A")]
    if not rows:
        raise SystemExit(f"{sub.get('name')} has no 13F-HR filings")
    r = rows[0]
    nodash = r["accessionNumber"].replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}"
    idx = http.json(base + "/index.json", ttl=None)
    info = [i["name"] for i in idx["directory"]["item"]
            if i["name"].lower().endswith(".xml") and "primary_doc" not in i["name"].lower()]
    if not info:
        raise SystemExit("could not find the 13F information table")
    b, sha = http.request(base + "/" + info[0], ttl=None)
    root = _strip_ns(ET.fromstring(b))
    agg = {}
    for it in root.findall("infoTable"):
        cusip = _t(it, "cusip")
        d = agg.setdefault(cusip, {"name": _t(it, "nameOfIssuer"), "value": 0, "shares": 0, "class": _t(it, "titleOfClass")})
        d["value"] += float(_t(it, "value") or 0)
        d["shares"] += float(_t(it, "shrsOrPrnAmt/sshPrnamt") or 0)
    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["value"])[:top]
    total = sum(v["value"] for v in agg.values()) or 1
    log(f"13F: {sub.get('name')} period {r.get('reportDate')}: {len(agg)} positions, recording top {len(ranked)}")
    for cusip, d in ranked:
        hits = store.find_by_name(d["name"], "organization")
        issuer = hits[0] if len(hits) == 1 else store.upsert_entity("organization", smart_title(d["name"]), {"cusip": cusip})
        store.add_edge(mgr, issuer, "holds_shares_of", qualifier=d["class"], amount=d["value"], currency="USD",
                       source="SEC 13F-HR", evidence_url=f"{base}/{r['accessionNumber']}-index.htm",
                       filing_date=r["filingDate"], source_sha256=sha, is_current=1,
                       attrs={"shares": d["shares"], "portfolio_share_pct": round(100 * d["value"] / total, 3),
                              "period": r.get("reportDate"), "cusip": cusip})
    store.commit()
    return mgr


# ---------------------------------------------------------------- career sweep (all companies a person has filed for)
OWN_ROW = re.compile(r"<tr><td valign='top'>(.*?)</td>\s*<td><a href=\"/cgi-bin/browse-edgar\?action=getcompany&amp;CIK=(\d+)\">\d+</a></td>\s*"
                     r"<td>([\d-]*)</td>\s*<td>(.*?)</td>\s*</tr>", re.S)


def _owner_roles(type_of_owner):
    t = (type_of_owner or "").strip()
    out = []
    if re.search(r"\bdirector\b", t, re.I):
        out.append(("director_of", None))
    m = re.search(r"officer:\s*(.+)", t, re.I)
    if m:
        out.append(("officer_of", m.group(1).strip()))
    if re.search(r"10\s*percent owner", t, re.I):
        out.append(("ten_percent_owner_of", None))
    m = re.search(r"other:\s*(.+)", t, re.I)
    if m and not out:
        out.append(("related_to", m.group(1).strip()))
    return out


def _accessions(sub):
    return {r["accessionNumber"]: r for r in recent_rows(sub) if r["form"] in INSIDER_FORMS}


def person_careers(http, store, person_cik, log_each=False):
    """Every issuer a reporting person has filed for (SEC ownership index), confirmed from a real
    Form 3/4/5 where one can be matched; otherwise kept as an index-level lead. Returns new issuer count."""
    from datetime import date as _d
    url = f"https://www.sec.gov/cgi-bin/own-disp?action=getowner&CIK={pad(person_cik)}"
    try:
        html = http.request(url, ttl=30 * 86400)[0].decode("utf-8", "replace")
    except Exception as ex:                                     # noqa: BLE001
        log(f"   ! careers {person_cik}: {ex}")
        return 0
    pid = store.find("sec_cik", pad(person_cik))
    if pid is None:
        return 0
    have = {(e["dst"], e["relation"]) for e in store.edges("src=?", (pid,)) if (e["source"] or "").upper().startswith("SEC FORM")}
    psub = None
    new = 0
    for name_html, icik, tdate, towner in OWN_ROW.findall(html):
        name = re.sub(r"<[^>]+>", " ", name_html)
        name = name.split("Current Name:")[-1].strip()
        roles = _owner_roles(re.sub(r"<[^>]+>", " ", towner))
        if not roles:
            continue
        issuer = store.upsert_entity("organization", smart_title(re.sub(r"\s+", " ", name)), {"sec_cik": pad(icik)})
        if all((issuer, rel) in have for rel, _ in roles):
            continue
        new += 1
        if psub is None:
            try:
                psub = _accessions(submissions(http, person_cik))
            except Exception:                                   # noqa: BLE001
                psub = {}
        confirmed = False
        try:
            isub = _accessions(submissions(http, icik))
            shared = sorted(set(psub) & set(isub), key=lambda a: psub[a]["filingDate"], reverse=True)
            if shared:
                parse_ownership_doc(http, store, icik, psub[shared[0]])
                confirmed = True
        except Exception:                                       # noqa: BLE001
            pass
        if not confirmed:                                       # index-level lead, clearly labelled
            stale = tdate and tdate < (_d.today() - timedelta(days=456)).isoformat()
            for rel, title in roles:
                store.add_edge(pid, issuer, rel, qualifier=title, source="SEC ownership index (no filing matched)",
                               evidence_url=url, filing_date=tdate or None, is_current=0 if stale else None,
                               attrs={"type_of_owner": re.sub(r"<[^>]+>", " ", towner).strip(),
                                      "note": "listed in SEC's ownership index; individual filing not matched (older than EDGAR's recent list)"})
        if log_each:
            log(f"    + {name} ({'filing' if confirmed else 'index only'})")
    store.set_attr(pid, "careers_checked", _d.today().isoformat())
    return new


def careers_sweep(http, store, limit=None, stale_days=30):
    from datetime import date as _d
    cutoff = (_d.today() - timedelta(days=stale_days)).isoformat()
    people = []
    for e in store.all_entities():
        if e["type"] == "person" and e["ids"].get("sec_cik") and (e["attrs"].get("careers_checked") or "") < cutoff:
            people.append(e)
    people = people[:limit] if limit else people
    log(f"careers: checking {len(people)} people's full SEC filing history")
    total = 0
    for i, e in enumerate(people, 1):
        total += person_careers(http, store, e["ids"]["sec_cik"])
        if i % 10 == 0:
            store.commit()
            log(f"  {i}/{len(people)} people · {total} new company links so far")
    store.commit()
    log(f"careers: {total} new person→company links across {len(people)} people")
    return total
