"""Append-only, hash-chained event log (data/audit_log.jsonl).

Writes, rejections, corrections and the boundary crossings found by path queries are all recorded here.
Nothing is ever rewritten. Each entry carries the fingerprint of the entry before it (`prev`) and its own
(`hash`), so changing or deleting any past line breaks every fingerprint after it, and `verify()` names the
exact line. Entries written before chaining began are sealed as one block by the first `chain_start`
entry: its fingerprint covers every byte that came before it.

A local file can still be rewritten wholesale by anyone with access, so `anchor()` copies the latest
fingerprint OUTSIDE this folder (the Obsidian vault, whose sync keeps version history). A rewritten log
can't reproduce fingerprints that were already anchored elsewhere."""
import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager

from . import paths
from .store import DATA, GRAPH_PATH, now_iso

LOG_PATH = os.path.join(DATA, "audit_log.jsonl")
LOCK_PATH = os.path.join(DATA, "audit_log.lock")
_VAULT_REPORTS = paths.REPORTS
ANCHOR_PATH = os.environ.get("PN_ANCHOR_PATH") or (
    os.path.join(_VAULT_REPORTS, "Audit anchors.md") if os.path.isdir(_VAULT_REPORTS) else os.path.join(DATA, "audit_anchors.md"))
ZERO = "0" * 64
_lock = threading.Lock()
_tip_cache = {"size": None, "tip": None}


def _canonical(rec):
    return json.dumps(rec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def entry_hash(rec):
    """Fingerprint of one entry: sha256 over the entry (including `prev`, excluding `hash`)."""
    return hashlib.sha256(_canonical({k: v for k, v in rec.items() if k != "hash"}).encode("utf-8")).hexdigest()


@contextmanager
def _file_lock():
    """Cross-process lock, so the server and command-line scripts never interleave the chain."""
    os.makedirs(DATA, exist_ok=True)
    f = open(LOCK_PATH, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            for _ in range(1200):
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                raise TimeoutError("audit log is locked by another process")
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        f.close()


def _last_line(path):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        buf, pos = b"", end
        while pos > 0:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            stripped = buf.rstrip(b"\n")
            if b"\n" in stripped:
                return stripped.rsplit(b"\n", 1)[1]
        return buf.rstrip(b"\n")


def _file_sha256(path, upto=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        left = upto
        while True:
            chunk = f.read(1 << 20 if left is None else min(1 << 20, left))
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
                if left <= 0:
                    break
    return h.hexdigest()


def _tip():
    """Fingerprint the next entry must point to. Starts (or restarts) the chain if the last line is unchained."""
    if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0:
        return ZERO, None
    size = os.path.getsize(LOG_PATH)
    if _tip_cache["size"] == size:
        return _tip_cache["tip"], None
    try:
        last = json.loads(_last_line(LOG_PATH).decode("utf-8"))
    except ValueError:
        last = {}
    if last.get("hash"):
        return last["hash"], None
    # earlier entries without fingerprints: seal all of them under one fingerprint
    with open(LOG_PATH, "rb") as f:
        lines = sum(1 for _ in f)
    digest = _file_sha256(LOG_PATH)
    start = {"ts": now_iso(), "event": "chain_start", "sealed_lines": lines, "sealed_bytes": size,
             "sealed_sha256": digest, "note": "every earlier line is covered by sealed_sha256; from here on each "
                                             "entry carries the fingerprint of the one before", "prev": digest}
    return digest, start


def _append(rec):
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    with open(LOG_PATH, "ab") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    _tip_cache.update(size=os.path.getsize(LOG_PATH), tip=rec["hash"])


def log_event(kind, **detail):
    with _lock, _file_lock():
        prev, start = _tip()
        if start:
            start["hash"] = entry_hash(start)
            _append(start)
            prev = start["hash"]
        rec = {"ts": now_iso(), "event": kind, **detail, "prev": prev}
        rec["hash"] = entry_hash(rec)
        _append(rec)
    return rec


def tail(n=100):
    if not os.path.exists(LOG_PATH):
        return []
    out, size = [], os.path.getsize(LOG_PATH)
    with open(LOG_PATH, "rb") as f:          # read only the end of the file; it grows large
        f.seek(max(0, size - 4_000_000))
        lines = f.read().split(b"\n")
    for x in lines[1 if size > 4_000_000 else 0:][-(n + 1):]:
        if x.strip():
            try:
                out.append(json.loads(x.decode("utf-8")))
            except ValueError:
                pass
    return out[-n:]


# ------------------------------------------------------------------ verification
_verify_cache = {}


def verify(check_anchors=True):
    """Walk the whole log and re-derive every fingerprint. Returns a plain report; changes nothing."""
    if not os.path.exists(LOG_PATH):
        return {"ok": True, "entries": 0, "chained": 0, "breaks": [], "tip": None, "message": "no log yet"}
    st = os.stat(LOG_PATH)
    key = (st.st_size, st.st_mtime_ns, os.path.exists(ANCHOR_PATH) and os.stat(ANCHOR_PATH).st_mtime_ns)
    if _verify_cache.get("key") == key:
        res = dict(_verify_cache["res"])
        res["graph"] = _graph_check(res.get("_last_graph"))
        return res

    running = hashlib.sha256()
    breaks, unchained_after = [], 0
    tip, chained, n, started_at, sealed = None, 0, 0, None, 0
    last_graph, hashes = None, {}
    with open(LOG_PATH, "rb") as f:
        for raw in f:
            n += 1
            line = raw.rstrip(b"\n")
            try:
                rec = json.loads(line.decode("utf-8")) if line.strip() else None
            except ValueError:
                rec = None
                if tip is not None:
                    breaks.append({"line": n, "problem": "unreadable line inside the chain"})
            if rec is not None and rec.get("event") == "chain_start":
                if rec.get("sealed_sha256") != running.hexdigest() or rec.get("prev") != rec.get("sealed_sha256"):
                    breaks.append({"line": n, "problem": f"the {rec.get('sealed_lines')} lines before this point were "
                                                         "changed after they were sealed"})
                if rec.get("hash") != entry_hash(rec):
                    breaks.append({"line": n, "problem": "seal entry itself was altered"})
                if started_at is None:
                    started_at, sealed = n, rec.get("sealed_lines", 0)
                tip = rec.get("hash")
                chained += 1
                hashes[tip] = n
            elif rec is not None and "hash" in rec:
                if tip is None:
                    breaks.append({"line": n, "problem": "fingerprinted entry with no chain start before it"})
                elif rec.get("prev") != tip:
                    breaks.append({"line": n, "problem": "does not point to the entry before it (a line was removed, "
                                                         "inserted or reordered here)"})
                if rec.get("hash") != entry_hash(rec):
                    breaks.append({"line": n, "problem": "content changed after it was written"})
                tip = rec.get("hash")
                chained += 1
                hashes[tip] = n
                if rec.get("event") == "graph_saved":
                    last_graph = rec
            elif rec is not None and tip is not None:
                unchained_after += 1
                breaks.append({"line": n, "problem": "entry written without a fingerprint inside the chain"})
            running.update(raw)

    anchors = _anchor_check(hashes) if check_anchors else {"checked": 0, "missing": []}
    for a in anchors["missing"]:
        breaks.append({"line": None, "problem": f"anchor from {a['ts']} ({a['tip'][:12]}…) is no longer in the log: "
                                                "history before it was rewritten"})
    res = {"ok": not breaks, "entries": n, "chained": chained, "sealed_legacy_lines": sealed,
           "chain_started_line": started_at, "tip": tip, "tip_line": hashes.get(tip), "breaks": breaks[:50], "break_count": len(breaks),
           "anchors": anchors, "anchor_file": ANCHOR_PATH, "_last_graph": last_graph}
    _verify_cache.update(key=key, res=res)
    out = dict(res)
    out["graph"] = _graph_check(last_graph)
    return out


def _graph_check(last_graph):
    """Was the master graph file changed outside the app since its last recorded save?"""
    if not last_graph or not os.path.exists(GRAPH_PATH):
        return {"status": "unknown", "message": "no recorded save yet"}
    cur = _file_sha256(GRAPH_PATH)
    if cur == last_graph.get("sha256"):
        return {"status": "matches", "message": f"graph file matches its last recorded save ({last_graph['ts']})"}
    return {"status": "changed_outside", "message": "the graph file differs from its last recorded save "
                                                    f"({last_graph['ts']}): it was edited outside the app",
            "recorded_sha256": last_graph.get("sha256"), "current_sha256": cur}


# ------------------------------------------------------------------ anchors (copies kept outside this folder)
_ANCHOR_ROW = re.compile(r"^\|\s*(\S+)\s*\|\s*(\d+)\s*\|\s*`?([0-9a-f]{64})`?\s*\|")


def read_anchors():
    if not os.path.exists(ANCHOR_PATH):
        return []
    out = []
    with open(ANCHOR_PATH, encoding="utf-8") as f:
        for line in f:
            m = _ANCHOR_ROW.match(line)
            if m:
                out.append({"ts": m.group(1), "line": int(m.group(2)), "tip": m.group(3)})
    return out


def _anchor_check(hashes):
    anchors = read_anchors()
    missing = [a for a in anchors if hashes.get(a["tip"]) != a["line"]]
    return {"checked": len(anchors), "missing": missing, "latest": anchors[-1] if anchors else None}


def anchor():
    """Record the current fingerprint outside the data folder. Run daily (update_all does)."""
    res = verify(check_anchors=False)
    if not res["tip"]:
        log_event("chain_check", note="starting the chain")
        res = verify(check_anchors=False)
    hashes_line = res["tip_line"]
    graph_sha = _file_sha256(GRAPH_PATH) if os.path.exists(GRAPH_PATH) else ""
    os.makedirs(os.path.dirname(ANCHOR_PATH), exist_ok=True)
    new = not os.path.exists(ANCHOR_PATH)
    with open(ANCHOR_PATH, "a", encoding="utf-8", newline="\n") as f:
        if new:
            f.write("---\nhoot: report\ntags: [hoot/report]\n---\n# Audit anchors\n\n"
                    "Each row is a fingerprint of Priority Nexus's audit log on that day. The log chains every entry to "
                    "the one before it, so these few characters stand for its entire history. Keep this note where it "
                    "is (your vault's sync keeps old versions); for extra safety, copy the newest row somewhere "
                    "else now and then: an email to yourself, a printout, a USB stick.\n\n"
                    "If anyone rewrites the log, its fingerprints change and the app's Integrity tab reports which "
                    "anchor no longer matches. Do not edit the rows below.\n\n"
                    "| recorded (UTC) | log line | log fingerprint | graph file fingerprint |\n|---|---|---|---|\n")
        f.write(f"| {now_iso()} | {hashes_line} | `{res['tip']}` | `{graph_sha}` |\n")
    return {"ok": res["ok"], "tip": res["tip"], "line": hashes_line, "anchor_file": ANCHOR_PATH,
            "break_count": res["break_count"]}


if __name__ == "__main__":
    import sys
    if "--anchor" in sys.argv:
        a = anchor()
        print(f"audit chain {'intact' if a['ok'] else 'BROKEN (' + str(a['break_count']) + ' problems)'} · "
              f"anchored line {a['line']} fingerprint {a['tip'][:16]}… -> {a['anchor_file']}")
    else:
        r = verify()
        print(f"audit chain {'intact' if r['ok'] else 'BROKEN'} · {r['entries']} entries "
              f"({r['sealed_legacy_lines']} sealed earlier lines, {r['chained']} chained) · "
              f"anchors checked {r['anchors']['checked']} · graph: {r['graph']['message']}")
        for b in r["breaks"][:20]:
            print(f"  line {b['line']}: {b['problem']}")
