"""Same person, two records: fold name variants into their host record.

A person can arrive twice: once with an SEC filer ID (CIK), and again from a 13D/G cover page, a Form D or a
Form 990, where the source gives no ID and HOOT assigned a temporary 'manual:' one. Left alone, their
connections split across two records and overlap. This folds the temporary record into the official one,
only when two independent facts agree (gate 2, two-facts):

  fact 1  the names match: nicknames and middle names allowed (Margo = Margaret, Mary Margaret = Margaret),
          initials only when every initial and any Jr/Sr agree (J.W. Marriott, Jr. = J W Marriott Jr)
  fact 2  one of: both hold a role at the same organization; or the SEC lists the CIK as a filer of the very
          filing the temporary record came from

Never merged automatically (held, listed in the report for you):
  - two different official IDs (e.g. two SEC CIKs): the registry treats them as two filers (gate 3)
  - a name match with no second fact

The temporary ID moves to the host, so later runs of that source land on the host. Connections that become
the same connection combine into one record with all evidence kept. Every merge is logged.
Also corrected here: records filed as people whose names are plainly organizations (funds, GPs, SCSps)."""
import re
from collections import defaultdict

from . import yamlish
from .util import _DEGREES, log, looks_like_org, name_tokens, person_name_match, today

OFFICIAL = ("sec_cik", "uk_officer", "lei")
_GEN = {"jr", "sr", "ii", "iii", "iv"}
_HONOR = {"dr", "sir", "dame", "mr", "mrs", "ms", "md", "phd", "esq"}
_FILING = re.compile(r"/edgar/data/(\d+)/(\d{18})/")


def _parts(name):
    toks = [t for t in re.sub(r"\(.*?\)|[.,]", " ", _DEGREES.sub(" ", name or "")).lower().split() if t not in _HONOR]
    gen = {t for t in toks if t in _GEN}
    toks = [t for t in toks if t not in _GEN]
    return toks, gen


def name_fact(a, b):
    """'full' / 'first_last' / 'initials' when the names agree, else None."""
    (ta, ga), (tb, gb) = _parts(a), _parts(b)
    if ga and gb and ga != gb:                         # Jr vs Sr: father and son
        return None
    m = person_name_match(a, b)
    if m in ("full", "first_last"):
        return m
    if m == "initial" and len(ta) >= 3 and len(tb) >= 3 and ga == gb:
        if [t[0] for t in ta[:-1]] == [t[0] for t in tb[:-1]]:      # every initial agrees, in order
            return "initials"
    return None


def _surnames(name):
    toks, _ = _parts(name)
    return set(toks[-1].split("-")) if len(toks) >= 2 else set()


def name_changes(people):
    """Earlier names on record under the SAME official ID whose surname differs (marriage, divorce, legal
    change). The registry ID proves they are one person, so these are already one record; listed so you can
    see them. SEC writes names surname-first ('MATHEWS SYLVIA M'), so a name counts as a different surname
    only when the current surname appears nowhere in it."""
    out = []
    for p in people.values():
        if not _official(p["ids"]):
            continue
        cur = _surnames(p["name"])
        nt = name_tokens(p["name"])
        earlier = []
        for a in p["aliases"]:
            toks, _ = _parts(a)
            words = {x for t in toks for x in t.split("-")}
            if len(toks) < 2 or cur & words:
                continue
            firsts = set()
            for order in (toks, toks[1:] + toks[:1]):          # 'Sylvia M Mathews' or SEC's 'MATHEWS SYLVIA M'
                t = name_tokens(" ".join(order))
                if t:
                    firsts |= t[0]
            if nt and nt[0] & firsts:                          # same first name: a surname change, not a stray alias
                earlier.append(a)
        if earlier:
            seen, uniq = set(), []
            for a in sorted(earlier, key=lambda x: x.isupper()):  # 'Sylvia M Mathews' preferred over 'MATHEWS SYLVIA M'
                k = frozenset(_parts(a)[0])
                if k not in seen:
                    seen.add(k)
                    mid_now = {t[0] for t in _parts(p["name"])[0][1:-1]}
                    mid_then = {t[0] for t in _parts(a)[0][1:-1]} if not a.isupper() else {t[0] for t in _parts(a)[0][2:]}
                    note = " (middle initial differs too: worth a glance)" if mid_now and mid_then and not mid_now & mid_then else ""
                    uniq.append(a + note)
            out.append((p, uniq))
    return out


def maiden_pairs(people, orgs_of):
    """Two DIFFERENT records that may be one person under a maiden and a married name: same first name
    (nicknames allowed), one surname appears as the other's middle name ('Mary Smith Jones' / 'Mary Smith'),
    and they share an organization. Never merged automatically: a changed surname is where false matches
    hide. Held for you with the evidence."""
    by_first = defaultdict(list)
    for pid, p in people.items():
        t = name_tokens(p["name"])
        if t:
            for f in t[0]:
                by_first[f].append(pid)
    out, seen = [], set()
    for ids in by_first.values():
        for a in ids:
            for b in ids:
                if a >= b or (a, b) in seen:
                    continue
                seen.add((a, b))
                pa, pb = people[a], people[b]
                ta, tb = _parts(pa["name"])[0], _parts(pb["name"])[0]
                if len(ta) < 2 or len(tb) < 2 or _surnames(pa["name"]) & _surnames(pb["name"]):
                    continue
                if not (tb[-1] in ta[1:-1] or ta[-1] in tb[1:-1]):
                    continue
                shared = orgs_of[a] & orgs_of[b]
                if shared:
                    out.append((pa, pb, shared))
    return out


def _official(ids):
    return {k: v for k, v in ids.items() if k in OFFICIAL}


def _filing_lists_cik(http, url, cik):
    m = _FILING.search(url or "")
    if not m or http is None:
        return False
    acc = m.group(2)
    idx = f"https://www.sec.gov/Archives/edgar/data/{m.group(1)}/{acc}/{acc[:10]}-{acc[10:12]}-{acc[12:]}-index.htm"
    try:
        b, _ = http.request(idx, ttl=None)           # filings never change: cache forever
    except Exception:                                 # noqa: BLE001
        return False
    return cik.zfill(10) in b.decode("utf-8", "replace")


def run(store, renderer, cfg, http=None, dry=False):
    L = renderer.link
    people = {r[0]: store.entity(r[0]) for r in store.db.execute("SELECT id FROM entities WHERE type='person'")}

    # 1. records filed as people whose names are organizations
    retyped = []
    for pid, p in list(people.items()):
        if looks_like_org(p["name"]):
            retyped.append(p)
            people.pop(pid)
            if not dry:
                store.db.execute("UPDATE entities SET type='organization' WHERE id=?", (pid,))
                store.logline("identity_correction", {"entity": p["canonical_id"], "name": p["name"], "was": "person",
                                                       "now": "organization", "basis": "name is an organization (fund/GP/partnership form)"})
                store.touched.add(pid)

    # 2. pairs of person records that may be one person
    orgs_of = defaultdict(set)
    for r in store.db.execute("SELECT src, dst FROM edges"):
        orgs_of[r[0]].add(r[1])
        orgs_of[r[1]].add(r[0])
    by_last = defaultdict(list)
    for pid, p in people.items():
        toks, _ = _parts(p["name"])
        if len(toks) >= 2:
            for part in toks[-1].split("-"):
                by_last[part].append(pid)

    merges, held, seen = [], [], set()
    for ids in by_last.values():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = sorted((ids[i], ids[j]))
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                pa, pb = people[a], people[b]
                nf = name_fact(pa["name"], pb["name"])
                if not nf:
                    continue
                oa, ob = _official(pa["ids"]), _official(pb["ids"])
                if oa and ob:
                    if oa != ob:
                        shared = orgs_of[a] & orgs_of[b]
                        if shared:                     # only worth your attention when they also share an organization
                            held.append((pa, pb, "two different official IDs: the registry lists two filers", shared))
                    continue
                shared = orgs_of[a] & orgs_of[b]
                second = None
                if shared:
                    second = "both tied to " + ", ".join(sorted(store.entity(x)["name"] for x in shared)[:3])
                elif oa or ob:
                    host, temp = (pa, pb) if oa else (pb, pa)
                    cik = host["ids"].get("sec_cik")
                    if cik and any(k.startswith("manual") for k in temp["ids"]):
                        for e in store.edges_of(temp["id"]):
                            if (e["source"] or "").upper().startswith("SEC") and _filing_lists_cik(http, e["evidence_url"], cik):
                                second = f"SEC lists CIK {int(cik)} as a filer of {e['evidence_url']}"
                                break
                if nf == "initials" and not shared:
                    second = None                      # initials alone need a shared organization
                if not second:
                    held.append((pa, pb, "names match, no second fact yet", set()))
                    continue
                host, temp = (pa, pb) if oa else (pb, pa) if ob else \
                    sorted((pa, pb), key=lambda p: (-len(p["name"]), p["id"]))
                merges.append((host, temp, nf, second))

    done = set()
    for host, temp, nf, second in merges:
        if host["id"] in done or temp["id"] in done:
            continue                                   # chained pairs settle on the next run
        done.update([host["id"], temp["id"]])
        if not dry:
            store.logline("identity_merge_basis", {"keep": host["canonical_id"], "drop": temp["canonical_id"],
                                                    "name_match": nf, "second_fact": second})
            store.merge(host["id"], temp["id"])

    out = ["# Same person check", "",
           "One person, one record. A person sometimes arrives twice: once with an official ID (SEC CIK) and again from a "
           "13D/G, Form D or Form 990, which carry no ID. HOOT folds the second record into the first **only when two facts "
           "agree**: the names match (nicknames and middle names allowed) **and** something independent ties them "
           "together. Their connections then combine; nothing overlaps and no evidence is lost.", ""]
    out += [f"## Folded in ({len(done) // 2})", ""]
    out += [f"- **{temp['name']}** (`{temp['canonical_id']}`) → {L(host['id']) if not dry else host['name']} · names: {nf} · {second}"
            for host, temp, nf, second in merges if host["id"] in done] or ["- none this run"]
    out += ["", f"## Held: possibly the same person ({len(held)})", "",
            "Not merged. Two official IDs are never merged automatically, and a name alone is not enough. "
            "Each line says what is missing. If you know two are one person, run `hoot merge \"<keep>\" \"<drop>\"`.", ""]
    out += [f"- {a['name']} (`{a['canonical_id']}`) / {b['name']} (`{b['canonical_id']}`): {why}"
            + (f" · both tied to {', '.join(sorted(store.entity(x)['name'] for x in sh)[:2])}" if sh else "")
            for a, b, why, sh in held] or ["- none"]
    # surname changes (marriage, divorce, legal change)
    after = {r[0]: store.entity(r[0]) for r in store.db.execute("SELECT id FROM entities WHERE type='person'")}
    changes = name_changes(after)
    maiden = maiden_pairs(after, orgs_of)
    out += ["", f"## Name changes on record ({len(changes)})", "",
            "Same official ID, different surname over time. The registry keeps one filer number when a person's name "
            "changes, so these are already **one record**. Searching either name finds them.", ""]
    out += [f"- {L(p['id']) if not dry else p['name']}: also filed as {', '.join(names)}" for p, names in changes] or ["- none"]
    out += ["", f"## Held: possible maiden / married or reordered names ({len(maiden)})", "",
            "Different records, same first name, one person's surname appears as the other's middle name (a maiden name "
            "kept as a middle name, or two surnames in a different order), and both are tied to the same organization. **Not merged**: a changed surname is exactly where false matches hide. "
            "Confirm from a filing or biography, then `hoot merge \"<keep>\" \"<drop>\"`.", ""]
    out += [f"- {a['name']} (`{a['canonical_id']}`) / {b['name']} (`{b['canonical_id']}`) · both tied to "
            + ", ".join(sorted(store.entity(x)["name"] for x in sh)[:2]) for a, b, sh in maiden] or ["- none"]
    out += ["", f"## Re-filed as organizations ({len(retyped)})", ""]
    out += [f"- {p['name']} (`{p['canonical_id']}`)" for p in retyped] or ["- none"]

    rel = f"{cfg.get('notes_folder', 'HOOT')}/Reports/Same person check.md"
    if not dry:
        fm = {"hoot": "report", "generated": today(), "merged": len(done) // 2, "held": len(held),
              "refiled_as_organization": len(retyped), "name_changes": len(changes), "maiden_held": len(maiden),
              "tags": ["hoot/report"]}
        renderer._write(rel, yamlish.dump(fm) + "\n".join(out) + "\n")
        store.commit()
    log(f"same-person check: {len(done) // 2} folded in · {len(held)} held · {len(retyped)} re-filed as organizations"
        + ("" if dry else f" -> {rel}"))
    return out
