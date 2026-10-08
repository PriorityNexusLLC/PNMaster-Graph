"""Load and save the master graph. Writes are atomic, backed up first, and never delete history."""
import json
import os
import shutil
import threading
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, "data")
GRAPH_PATH = os.path.join(DATA, "priority_nexus_graph.json")
GOVERNANCE_PATH = os.path.join(DATA, "governance.json")
BACKUP_DIR = os.path.join(DATA, "backups")

_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_loaded = {}          # id(graph) -> file modification time when it was loaded


class GraphChangedError(RuntimeError):
    """Another program saved the graph after this copy was loaded; saving would silently drop its changes."""


def load_graph(path=GRAPH_PATH):
    mt = os.stat(path).st_mtime_ns if os.path.exists(path) else None
    g = load_json(path)
    _loaded[id(g)] = (path, mt)
    return g


def load_governance(path=GOVERNANCE_PATH):
    return load_json(path)


def save_graph(graph, path=GRAPH_PATH):
    """Back up the current file, then replace it atomically (write temp file, rename over)."""
    with _lock:
        seen = _loaded.get(id(graph))
        if seen and seen[0] == path and os.path.exists(path) and os.stat(path).st_mtime_ns != seen[1]:
            raise GraphChangedError(f"{os.path.basename(path)} was changed by another program after this copy was loaded; "
                                    "nothing was saved. Run this step again (it starts from the newer file).")
        os.makedirs(BACKUP_DIR, exist_ok=True)
        if os.path.exists(path):
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            shutil.copy2(path, os.path.join(BACKUP_DIR, f"priority_nexus_graph.{stamp}.json"))
        graph.setdefault("schema_metadata", {})["last_updated"] = now_iso()[:10]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(graph, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _loaded[id(graph)] = (path, os.stat(path).st_mtime_ns)
    if path == GRAPH_PATH:                 # fingerprint every save in the chained log, so edits made outside the app show
        import hashlib
        from . import audit
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        audit.log_event("graph_saved", sha256=h.hexdigest(), nodes=len(graph.get("nodes", [])),
                        edges=len(graph.get("edges", [])))


def next_edge_id(graph):
    nums = []
    for e in graph.get("edges", []):
        tail = str(e.get("edge_id", "")).rsplit("_", 1)[-1]
        if tail.isdigit():
            nums.append(int(tail))
    return f"EDGE_{(max(nums) + 1 if nums else 1):03d}"
