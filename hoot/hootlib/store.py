"""SQLite graph store. The database is the machine truth; Obsidian notes are rendered from it
(plus whatever you write by hand, which `hoot sync` reads back in)."""
import json
import os
import sqlite3

from .util import HOOT_DIR, norm_name, now_iso, sha256_json, today

# relation -> (control class, base conflict_weight)
RELATIONS = {
    # hard structural control
    "director_of": ("hard", 1.0),
    "officer_of": ("hard", 0.9),
    "ten_percent_owner_of": ("hard", 1.0),
    "beneficial_owner_of": ("hard", 0.9),      # Schedule 13D/13G (>5% of a class)
    "subsidiary_of": ("hard", 1.0),            # GLEIF direct accounting consolidation
    "ultimately_owned_by": ("hard", 0.9),      # GLEIF ultimate parent
    "owns": ("hard", 0.9),
    "veto_right_over": ("hard", 1.0),
    "trustee_of": ("hard", 0.9),
    "position_at": ("hard", 0.7),
    "hierarchy": ("hard", 0.7),
    # soft influence
    "holds_shares_of": ("soft", 0.5),          # 13F passive holding
    "funded_by": ("soft", 0.6),
    "grant_awarded_to": ("soft", 0.6),
    "principal_investigator_of": ("soft", 0.6),
    "author_of": ("soft", 0.4),
    "affiliated_with": ("soft", 0.5),
    "advisor_to": ("soft", 0.5),
    "member_of": ("soft", 0.4),
    "donated_to": ("soft", 0.6),
    "lobbying_tie": ("soft", 0.6),
    "family_of": ("soft", 0.5),
    "educated_at": ("soft", 0.2),
    "transaction_with": ("soft", 0.4),
    "professional_tie": ("soft", 0.4),
    "social_tie": ("soft", 0.2),
    "related_to": ("soft", 0.2),
}

# canonical_id preference: statutory/cryptographic ids beat aggregator ids beat names
ID_PRIORITY = ["sec_cik", "lei", "ein", "doi", "orcid", "ror", "nih_project", "openalex",
               "littlesis", "opensanctions", "cusip", "manual"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities(
  id INTEGER PRIMARY KEY, type TEXT, name TEXT, canonical_id TEXT UNIQUE,
  note_path TEXT, attrs TEXT DEFAULT '{}', created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS ids(
  scheme TEXT, value TEXT, entity_id INTEGER, PRIMARY KEY(scheme, value));
CREATE TABLE IF NOT EXISTS aliases(entity_id INTEGER, alias TEXT, UNIQUE(entity_id, alias));
CREATE TABLE IF NOT EXISTS edges(
  id TEXT PRIMARY KEY, dedupe_key TEXT UNIQUE, src INTEGER, dst INTEGER, relation TEXT,
  control TEXT, conflict_weight REAL, qualifier TEXT,
  start_date TEXT, end_date TEXT, is_current INTEGER,
  amount REAL, currency TEXT, percent REAL,
  source TEXT, evidence_url TEXT, filing_date TEXT, first_seen TEXT, last_seen TEXT,
  date_verified TEXT, source_sha256 TEXT, verification_hash TEXT,
  origin TEXT, note_path TEXT, attrs TEXT DEFAULT '{}');
CREATE INDEX IF NOT EXISTS edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS log(ts TEXT, action TEXT, detail TEXT);
"""

EDGE_FIELDS = ["start_date", "end_date", "is_current", "amount", "currency", "percent",
               "source", "evidence_url", "filing_date", "source_sha256", "qualifier"]


def verification_hash(e, src_cid, dst_cid):
    """Fingerprint of the evidentiary content of an edge. If any of these fields is edited
    in a note without going through HOOT, `hoot audit` will flag the mismatch."""
    num = lambda v: None if v is None else float(v)
    s = lambda v: None if v in (None, "") else str(v)
    return sha256_json({
        "from": src_cid, "to": dst_cid, "relation": e["relation"], "qualifier": s(e.get("qualifier")),
        "start_date": s(e.get("start_date")), "end_date": s(e.get("end_date")),
        "percent": num(e.get("percent")), "amount": num(e.get("amount")),
        "source": s(e.get("source")), "evidence_url": s(e.get("evidence_url")),
        "filing_date": s(e.get("filing_date")), "source_sha256": s(e.get("source_sha256")),
    })


class Store:
    def __init__(self, path=None):
        self.path = path or os.path.join(HOOT_DIR, "graph.db")
        self.db = sqlite3.connect(self.path, timeout=120)     # wait for another HOOT run instead of failing
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.touched = set()          # entity ids whose notes need re-rendering

    def commit(self):
        self.db.commit()

    def logline(self, action, detail):
        self.db.execute("INSERT INTO log VALUES(?,?,?)", (now_iso(), action, json.dumps(detail, ensure_ascii=False)))

    # ------------------------------------------------------------ entities
    def find(self, scheme, value):
        if value is None:
            return None
        r = self.db.execute("SELECT entity_id FROM ids WHERE scheme=? AND value=?", (scheme, str(value))).fetchone()
        return r[0] if r else None

    def find_by_name(self, name, etype=None):
        key = norm_name(name)
        rows = self.db.execute("SELECT id, type, name FROM entities").fetchall()
        hits = [r["id"] for r in rows if norm_name(r["name"]) == key and (etype is None or r["type"] == etype)]
        if not hits:
            al = self.db.execute("SELECT entity_id, alias FROM aliases").fetchall()
            hits = sorted({a[0] for a in al if norm_name(a[1]) == key})
        return hits

    def entity(self, eid):
        r = self.db.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["attrs"] = json.loads(d["attrs"] or "{}")
        d["ids"] = {s: v for s, v in self.db.execute("SELECT scheme, value FROM ids WHERE entity_id=?", (eid,))}
        d["aliases"] = [a[0] for a in self.db.execute("SELECT alias FROM aliases WHERE entity_id=? ORDER BY alias", (eid,))]
        return d

    def _canonical(self, ids):
        for s in ID_PRIORITY:
            if ids.get(s):
                return f"{s}:{ids[s]}"
        s = sorted(ids)[0]
        return f"{s}:{ids[s]}"

    def upsert_entity(self, etype, name, ids, attrs=None, aliases=()):
        ids = {k: str(v) for k, v in ids.items() if v}
        if not ids:
            raise ValueError(f"entity {name!r} has no identifier")
        eid = None
        for s, v in ids.items():
            eid = self.find(s, v)
            if eid:
                break
        ts = now_iso()
        if eid is None:
            cur = self.db.execute(
                "INSERT INTO entities(type,name,canonical_id,attrs,created,updated) VALUES(?,?,?,?,?,?)",
                (etype, name, None, json.dumps(attrs or {}, ensure_ascii=False), ts, ts))
            eid = cur.lastrowid
        else:
            row = self.entity(eid)
            merged = row["attrs"]
            for k, v in (attrs or {}).items():
                if v not in (None, "", [], {}):
                    merged[k] = v
            self.db.execute("UPDATE entities SET attrs=?, updated=? WHERE id=?",
                            (json.dumps(merged, ensure_ascii=False), ts, eid))
            if row["type"] in (None, "unknown") and etype:
                self.db.execute("UPDATE entities SET type=? WHERE id=?", (etype, eid))
            if row["name"] != name and name:
                aliases = list(aliases) + [name]
        for s, v in ids.items():
            self.db.execute("INSERT OR IGNORE INTO ids VALUES(?,?,?)", (s, v, eid))
        allids = {s: v for s, v in self.db.execute("SELECT scheme, value FROM ids WHERE entity_id=?", (eid,))}
        old_cid = self.db.execute("SELECT canonical_id FROM entities WHERE id=?", (eid,)).fetchone()[0]
        new_cid = self._canonical(allids)
        if old_cid != new_cid:
            self.db.execute("UPDATE entities SET canonical_id=? WHERE id=?", (new_cid, eid))
            if old_cid is not None:
                self.rehash(eid)
        nm =self.db.execute("SELECT name FROM entities WHERE id=?", (eid,)).fetchone()[0]
        for a in aliases:
            if a and norm_name(a) != norm_name(nm):
                self.db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (eid, a))
        self.touched.add(eid)
        return eid

    def set_attr(self, eid, key, value):
        e = self.entity(eid)
        e["attrs"][key] = value
        self.db.execute("UPDATE entities SET attrs=? WHERE id=?", (json.dumps(e["attrs"], ensure_ascii=False), eid))
        self.touched.add(eid)

    # ------------------------------------------------------------ edges
    def add_edge(self, src, dst, relation, *, origin="auto", **f):
        if src == dst:
            return None
        control, w = RELATIONS.get(relation, ("soft", 0.3))
        f.setdefault("date_verified", today())
        dkey = sha256_json([src, dst, relation, f.get("qualifier") or "", (f.get("source") or "").split(" ")[0]])
        existing = self.db.execute("SELECT * FROM edges WHERE dedupe_key=?", (dkey,)).fetchone()
        fd = f.get("filing_date") or f.get("start_date") or ""
        evidence = {"url": f.get("evidence_url"), "date": f.get("filing_date"), "sha256": f.get("source_sha256")}
        if existing:
            ex = dict(existing)
            attrs = json.loads(ex["attrs"] or "{}")
            ev = attrs.get("evidence", [])
            if evidence["url"] and evidence["url"] not in [x.get("url") for x in ev]:
                ev.append(evidence)
                ev.sort(key=lambda x: x.get("date") or "")
                attrs["evidence"] = ev[-25:]
            for k, v in (f.get("attrs") or {}).items():
                attrs[k] = v
            upd = {"first_seen": min(filter(None, [ex["first_seen"], fd])) if (ex["first_seen"] or fd) else None,
                   "last_seen": max(filter(None, [ex["last_seen"], fd])) if (ex["last_seen"] or fd) else None,
                   "date_verified": f["date_verified"], "attrs": json.dumps(attrs, ensure_ascii=False)}
            if fd >= (ex["last_seen"] or ""):        # newest filing wins for mutable facts
                for k in EDGE_FIELDS:
                    if k in f and f[k] is not None:
                        upd[k] = f[k]
            ex.update(upd)
            ex["conflict_weight"] = self._weight(relation, ex)
            ex["verification_hash"] = self._vhash(ex)
            sets = ", ".join(f"{k}=?" for k in list(upd) + ["conflict_weight", "verification_hash"])
            self.db.execute(f"UPDATE edges SET {sets} WHERE id=?",
                            [ex[k] for k in list(upd) + ["conflict_weight", "verification_hash"]] + [ex["id"]])
            eid = ex["id"]
        else:
            row = {k: f.get(k) for k in EDGE_FIELDS}
            row.update(src=src, dst=dst, relation=relation, control=control, origin=origin,
                       dedupe_key=dkey, first_seen=fd or None, last_seen=fd or None,
                       date_verified=f["date_verified"],
                       attrs=json.dumps({**(f.get("attrs") or {}), "evidence": [evidence] if evidence["url"] else []},
                                        ensure_ascii=False))
            row["id"] = "E" + dkey[:12]
            row["conflict_weight"] = self._weight(relation, row)
            row["verification_hash"] = self._vhash(row)
            cols = ", ".join(row)
            self.db.execute(f"INSERT INTO edges({cols}) VALUES({', '.join('?' * len(row))})", list(row.values()))
            eid = row["id"]
        self.touched.update([src, dst])
        return eid

    def _weight(self, relation, e):
        control, w = RELATIONS.get(relation, ("soft", 0.3))
        if e.get("is_current") == 0 or (e.get("end_date") and e["end_date"] < today()):
            w *= 0.5
        if e.get("percent") is not None and relation in ("beneficial_owner_of", "holds_shares_of", "owns"):
            p = float(e["percent"])
            w = 0.0 if p == 0 else min(1.0, w * (0.6 + p / 25))
        return round(w, 3)

    def _vhash(self, e):
        cid = lambda i: self.db.execute("SELECT canonical_id FROM entities WHERE id=?", (i,)).fetchone()[0]
        return verification_hash(e, cid(e["src"]), cid(e["dst"]))

    def rehash(self, eid=None):
        es = self.edges_of(eid) if eid is not None else self.edges("1=1")
        for e in es:
            self.db.execute("UPDATE edges SET verification_hash=? WHERE id=?", (self._vhash(e), e["id"]))
            self.touched.update([e["src"], e["dst"]])

    def edges(self, where="1=1", params=()):
        out = []
        for r in self.db.execute(f"SELECT * FROM edges WHERE {where}", params):
            d = dict(r)
            d["attrs"] = json.loads(d["attrs"] or "{}")
            out.append(d)
        return out

    def edges_of(self, eid):
        return self.edges("src=? OR dst=?", (eid, eid))

    def all_entities(self):
        return [self.entity(r[0]) for r in self.db.execute("SELECT id FROM entities ORDER BY name")]

    # ------------------------------------------------------------ merge
    def merge(self, keep, drop):
        """Fold entity `drop` into `keep` (ids, aliases, edges). Logged for audit."""
        a, b = self.entity(keep), self.entity(drop)
        self.logline("merge", {"keep": a["canonical_id"], "drop": b["canonical_id"], "drop_name": b["name"]})
        self.db.execute("UPDATE ids SET entity_id=? WHERE entity_id=?", (keep, drop))
        for al in b["aliases"] + [b["name"]]:
            self.db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (keep, al))
        merged = {**b["attrs"], **a["attrs"]}
        merged["merged_from"] = sorted(set(a["attrs"].get("merged_from", []) + [b["canonical_id"]]))
        self.db.execute("UPDATE entities SET attrs=? WHERE id=?", (json.dumps(merged, ensure_ascii=False), keep))
        for e in self.edges("src=? OR dst=?", (drop, drop)):
            src = keep if e["src"] == drop else e["src"]
            dst = keep if e["dst"] == drop else e["dst"]
            self.db.execute("DELETE FROM edges WHERE id=?", (e["id"],))
            if src == dst:
                continue
            f = {k: e[k] for k in EDGE_FIELDS}
            f.update(date_verified=e["date_verified"], attrs=e["attrs"])
            nid = self.add_edge(src, dst, e["relation"], origin=e["origin"], **f)
            fs = [x for x in (e["first_seen"], self.edges("id=?", (nid,))[0]["first_seen"]) if x]
            if fs:
                self.db.execute("UPDATE edges SET first_seen=? WHERE id=?", (min(fs), nid))
            self.touched.update([src, dst])
        self.db.execute("DELETE FROM aliases WHERE entity_id=?", (drop,))
        self.db.execute("DELETE FROM entities WHERE id=?", (drop,))
        allids = {s: v for s, v in self.db.execute("SELECT scheme, value FROM ids WHERE entity_id=?", (keep,))}
        self.db.execute("UPDATE entities SET canonical_id=? WHERE id=?", (self._canonical(allids), keep))
        self.rehash(keep)                            # canonical id may have changed
        self.touched.add(keep)
        self.touched.discard(drop)
        return b
