"""Read the investigator's own domain classifications from a workbook (e.g. Connections.xlsx).

The result (data/staging/user_domains.json) is used only as a *labelled suggestion* in the Review
tab ("from your Connections.xlsx"). It is the investigator's prior judgment, not evidence, so it
never adds entities or edges by itself. Standard library only (xlsx is zipped XML).
"""
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src", "ingest"))
from claims import org_key  # noqa: E402

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
DOM = re.compile(r"Domain[s]?\s*([A-G](?:\s*[/,&]\s*[A-G])*)", re.I)


def col_index(ref):
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def read_sheets(path):
    z = zipfile.ZipFile(path)
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            ss.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels}
    sheets = {}
    for s in wb.find("m:sheets", NS):
        t = target[s.get(f"{{{NS['r']}}}id")].lstrip("/")
        t = t if t.startswith("xl/") else "xl/" + t
        grid = {}
        for row in ET.fromstring(z.read(t)).findall(".//m:row", NS):
            r = int(row.get("r"))
            for c in row.findall("m:c", NS):
                v = c.find("m:v", NS)
                if v is None:
                    tt = c.find(".//m:t", NS)
                    val = tt.text if tt is not None else ""
                else:
                    val = ss[int(v.text)] if c.get("t") == "s" else v.text
                if val:
                    grid[(r, col_index(c.get("r")))] = val.strip()
        sheets[s.get("name")] = grid
    return sheets


def domains_in(text):
    out = []
    for m in DOM.finditer(text or ""):
        out += ["DOM_" + x.upper() for x in re.findall(r"[A-G]", m.group(1), re.I)]
    return sorted(set(out))


def extract(path):
    found = {}

    def add(name, primary, bridges, where):
        name = re.sub(r"\(.*?\)", "", name or "").strip()
        if not name or name.lower() in ("entity name", "entity / corporation") or not (primary or bridges):
            return
        k = org_key(name)
        e = found.setdefault(k, {"name": name, "domains": set(), "bridges_to": set(), "where": set()})
        e["domains"].update(primary or bridges)        # classification column if present, else stated domains
        e["bridges_to"].update(set(bridges) - set(primary or bridges))
        e["where"].add(where)

    for sheet, g in read_sheets(path).items():
        for (r, c), v in sorted(g.items()):
            if v.lower() != "entity name":
                continue
            # this block runs from c to just before the next "Entity Name" header in the same row
            nxt = min([cc for (rr, cc), vv in g.items() if rr == r and cc > c and vv.lower() == "entity name"] or [c + 99])
            # block heading: nearest cell above, at or left of c, that names a domain
            head = None
            prev = max([cc for (r2, cc), vv in g.items() if r2 == r and cc < c and vv.lower() == "entity name"] or [0])
            is_title = lambda s: bool(re.match(r"^\s*Domain\s+[A-G]\b(?!\s*[/,&])", s)) and len(s) < 80
            for rr in range(r - 1, 0, -1):               # 1st choice: a domain title directly above, same column
                if is_title(g.get((rr, c), "")):
                    head = g[(rr, c)]
                    break
            for rr in range(r - 1, 0, -1) if head is None else ():
                cands = [(cc, vv) for (r2, cc), vv in g.items() if r2 == rr and prev < cc <= c and is_title(vv)]
                if cands:
                    head = max(cands)[1]
                    break
            cls_col = next((cc for (rr, cc), vv in g.items() if rr == r and c < cc < nxt and re.search(r"domain classification", vv, re.I)), None)
            bridge_col = next((cc for (rr, cc), vv in g.items() if rr == r and c < cc < nxt and re.search(r"domain", vv, re.I) and cc != cls_col), None)
            rr = r + 1
            while (rr, c) in g:
                primary = domains_in(g.get((rr, cls_col), "")) if cls_col else domains_in(head or "")
                bridges = domains_in(g.get((rr, bridge_col), "")) if bridge_col else []
                add(g[(rr, c)], primary, bridges, f"{sheet}!R{rr}")
                rr += 1
    for v in found.values():
        v["domains"] = sorted(v["domains"])
        v["bridges_to"] = sorted(v["bridges_to"])
        v["where"] = sorted(v["where"])[:5]
    return found


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser("~"), "Downloads", "Connections.xlsx")
    found = extract(path)
    out = os.path.join(ROOT, "data", "staging", "user_domains.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"source": os.path.basename(path), "entities": found}, f, indent=2, ensure_ascii=False)
    print(f"{len(found)} entities with domains from {os.path.basename(path)} -> {out}")
    for v in list(found.values())[:60]:
        print(f"  {', '.join(d[4:] for d in v['domains']):<6} bridges {', '.join(d[4:] for d in v['bridges_to']):<8} {v['name']}")


if __name__ == "__main__":
    main()
