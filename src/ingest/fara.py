"""Foreign agents connector: the U.S. Department of Justice's Foreign Agents Registration Act (FARA) registry.

Every active registrant (a firm or person working in the U.S. for a foreign government, party or company) and
the foreign principals each one represents, read from efile.fara.gov. Recorded on the map, linked to the
registration:
  registrant -> foreign principal    FOREIGN_AGENT_FOR    when the principal is an organization already on the map
  registrant -> organization          (the registrant itself is on the map, e.g. a listed PR or law firm)
Matching uses the principal's full name with legal endings removed; ambiguous short names are skipped.
Writes HOOT/Reports/Foreign agents (FARA) check.md with all matches and principals by country.

    python -I src/ingest/fara.py
"""
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _graphio import ROOT, Writer, org_key  # noqa: E402
from engine import paths  # noqa: E402

sys.path.insert(0, paths.HOOT_DIR)
from hootlib.util import Http, load_config  # noqa: E402
from engine import audit  # noqa: E402

REG_URL = "https://efile.fara.gov/api/v1/Registrants/json/Active"
FP_URL = "https://efile.fara.gov/api/v1/ForeignPrincipals/json/Active/{}"
REPORT = os.path.join(os.path.dirname(audit.ANCHOR_PATH), "Foreign agents (FARA) check.md")


def rows(raw):
    d = json.loads(raw.decode("latin-1"))
    for v in d.values():
        if isinstance(v, dict):
            r = v.get("ROW")
            return r if isinstance(r, list) else [r] if r else []
    return []


def pick(row, *words):
    for k, v in row.items():
        if all(w in k.lower() for w in words) and v not in (None, ""):
            return str(v).strip()
    return ""


def iso(d):
    try:
        return datetime.strptime(d, "%m/%d/%Y").date().isoformat()
    except (TypeError, ValueError):
        return date.today().isoformat()


def main():
    http = Http(load_config())
    http.min_interval["efile.fara.gov"] = 2.2            # FARA's API allows 5 requests per 10 seconds
    w = Writer("FARA connector (DOJ)", "automatic (fara v1)")
    keys = {}
    for n in w.g["nodes"]:
        if n.get("entity_type") != "PERSON" and not (n.get("provenance") or {}).get("merged_into"):
            k = org_key(n["label"])
            if len(k) >= 8 or len(k.split()) >= 2:
                keys.setdefault(k, n["id"])
    regs = rows(http.request(REG_URL, ttl=86400)[0])
    print(f"FARA: {len(regs)} active registrants")
    by_country, matches, n_fp = Counter(), [], 0
    for i, r in enumerate(regs, 1):
        num = str(r.get("Registration_Number") or "").strip()
        if not num:
            continue
        try:
            fps = rows(http.request(FP_URL.format(num), ttl=7 * 86400)[0])
        except Exception as ex:                          # noqa: BLE001
            print(f"  {num}: {str(ex)[:60]}")
            continue
        reg_name = r.get("Name") or f"FARA registrant {num}"
        for fp in fps:
            n_fp += 1
            pname = pick(fp, "fp", "name") or pick(fp, "principal") or pick(fp, "name")
            country = pick(fp, "country")
            by_country[country or "unknown"] += 1
            hit = keys.get(org_key(pname))
            if not hit:
                continue
            reg_node = w.node(reg_name, "FOREIGN_AGENT_REGISTRANT", f"FARA-REGISTRANT:{num}", ["DOM_C"],
                              {"domain_basis": "FARA registrant: foreign influence (DOM_C) by default",
                               "registration_date": r.get("Registration_Date")})
            t = {"verification_source": "FARA registration", "source_reference": num, "citation_url": FP_URL.format(num),
                 "document_date": iso(pick(fp, "reg", "date") or r.get("Registration_Date")), "active": True}
            w.edge(reg_node, hit, "FOREIGN_AGENT_FOR", t, 0.6, "FOREIGN_PRINCIPAL_REPRESENTATION",
                   supplement={"role_title": f"foreign principal as registered: {pname} ({country})"[:300]})
            matches.append((reg_name, num, pname, country, hit))
        if i % 100 == 0:
            print(f"  {i}/{len(regs)} registrants · {n_fp} principals · {len(matches)} on the map")
    w.save()
    labels = {n["id"]: n["label"] for n in w.g["nodes"]}
    L = ["---", "hoot: report", f"generated: {date.today().isoformat()}", "tags: [hoot/report]", "---",
         "# Foreign agents (FARA) check", "",
         f"{len(regs)} active registrants with {n_fp} foreign principals, read from the Justice Department's FARA "
         "registry (efile.fara.gov). Registration is a legal requirement for representing foreign interests in the U.S.; "
         "it is not itself wrongdoing.", "",
         f"## Foreign principals that are organizations on your map ({len(matches)})", ""]
    L += [f"- **{labels.get(h, h)}** ({c}) is represented by **{rn}** (FARA registration {num}), registered principal name: "
          f"\"{pn}\" [record]({FP_URL.format(num)})" for rn, num, pn, c, h in matches] or ["- none yet"]
    L += ["", "## Active foreign principals by country", "", "| Country | Principals |", "|---|---|"]
    L += [f"| {c} | {k} |" for c, k in by_country.most_common(40)]
    with open(REPORT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    audit.log_event("fara_run", registrants=len(regs), principals=n_fp, matched=len(matches), counts=w.counts)
    print(f"FARA: {n_fp} principals · {len(matches)} are organizations on the map · {w.counts} -> {REPORT}")


if __name__ == "__main__":
    main()
