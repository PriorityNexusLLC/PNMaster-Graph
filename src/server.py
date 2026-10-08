"""Priority Nexus local server: Python standard library only, bound to 127.0.0.1.

    python src/server.py [--port 8765]
"""
import argparse
import json
import mimetypes
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import audit, balance, pathfinder, patterns, report, review, staging, store, validator  # noqa: E402

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
MAX_BODY = 256 * 1024
_write_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "PriorityNexus/1.0"

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt, *args):
        sys.stderr.write("  %s %s\n" % (self.command, self.path))

    def _host_ok(self):
        """Block DNS-rebinding and cross-site requests: only our own origin may talk to the API."""
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc not in allowed:
            return False
        return True

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise validator.ValidationError([{"field": "request", "message": "expected application/json"}], 415)
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise validator.ValidationError([{"field": "request", "message": "body too large"}], 413)
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            raise validator.ValidationError([{"field": "request", "message": "invalid JSON"}], 400)

    # ------------------------------------------------------------ GET
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host/origin"})
        path = urlparse(self.path).path
        if path == "/api/graph":
            return self._send(200, {"graph": store.load_graph(), "governance": store.load_governance()})
        if path == "/api/version":
            return self._send(200, {"version": os.path.getmtime(store.GRAPH_PATH)})
        if path == "/api/audit":
            return self._send(200, {"issues": validator.audit_graph(store.load_graph(), store.load_governance())})
        if path == "/api/log":
            return self._send(200, {"events": audit.tail(200)})
        if path == "/api/chain":
            res = audit.verify()
            res.pop("_last_graph", None)
            return self._send(200, res)
        if path == "/api/stamps":
            p = os.path.join(store.DATA, "enforcement", "stamps.json")
            if not os.path.exists(p):
                return self._send(200, {"entities": {}})
            with open(p, encoding="utf-8") as f:
                return self._send(200, json.load(f))
        if path == "/api/patterns":
            return self._send(200, patterns.cached(store.GRAPH_PATH, store.load_graph))
        if path == "/api/balance":
            return self._send(200, balance.cached(store.GRAPH_PATH, store.load_graph))
        if path == "/api/staging":
            return self._send(200, {"batches": staging.list_batches()})
        m = re.fullmatch(r"/api/report/(PN-[A-Za-z0-9-]+)/status", path)
        if m:
            return self._send(200, review.status(m.group(1)))
        m = re.fullmatch(r"/reports/(PN-[A-Za-z0-9-]+)\.(md|html)", path)
        if m:
            f = os.path.join(report.REPORT_DIR, f"{m.group(1)}.{m.group(2)}")
            if not os.path.isfile(f):
                return self._send(404, {"error": "not found"})
            with open(f, "rb") as fh:
                data = fh.read()
            if m.group(2) == "html":
                return self._send(200, data, "text/html; charset=utf-8")
            return self._send(200, data, "text/markdown; charset=utf-8")
        if path.startswith("/api/staging/"):
            try:
                return self._send(200, staging.load_batch(path.rsplit("/", 1)[-1])[0])
            except validator.ValidationError as ex:
                return self._send(ex.status, {"error": "not found", "errors": ex.errors})
        return self._static(path)

    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(UI_DIR, rel))
        if not full.startswith(UI_DIR + os.sep) and full != os.path.join(UI_DIR, "index.html"):
            return self._send(404, {"error": "not found"})
        if not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            ctype = "text/javascript"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))

    # ------------------------------------------------------------ POST
    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host/origin"})
        path = urlparse(self.path).path
        try:
            body = self._json_body()
            if path == "/api/path":
                return self._path(body)
            if path == "/api/edges":
                return self._add_edge(body)
            if path == "/api/nodes":
                return self._add_node(body)
            if path == "/api/nodes/identity":
                return self._add_identity(body)
            if path == "/api/report":
                try:
                    with _write_lock:
                        res = report.draft(store.load_graph(), str(body.get("subject") or ""),
                                           path_to=body.get("path_to") or None)
                except ValueError as ex:
                    raise validator.ValidationError([{"field": "subject", "message": str(ex)}], 422)
                review.run_background(res["id"])           # second pass, every time, without waiting
                return self._send(201, res)
            m = re.fullmatch(r"/api/report/(PN-[A-Za-z0-9-]+)/review", path)
            if m:
                if review.status(m.group(1)).get("state") == "unknown":
                    return self._send(404, {"error": "no such report"})
                review.run_background(m.group(1))
                return self._send(202, {"state": "running"})
            m = re.fullmatch(r"/api/staging/([A-Z0-9T]+)/apply", path)
            if m:
                with _write_lock:
                    res = staging.apply(m.group(1), body)
                return self._send(200, res)
            return self._send(404, {"error": "not found"})
        except validator.ValidationError as ex:
            audit.log_event("rejected", endpoint=path, errors=ex.errors)
            return self._send(ex.status, {"error": "validation failed", "errors": ex.errors})

    def _path(self, b):
        g = store.load_graph()
        res = pathfinder.find_paths(
            g, from_ids=b.get("from_ids"), to_ids=b.get("to_ids"),
            from_domains=b.get("from_domains"), to_domains=b.get("to_domains"),
            mode="strongest" if b.get("mode") == "strongest" else "hops",
            include_inactive=bool(b.get("include_inactive")),
            restrict_domains=b.get("restrict_domains") or None, limit=int(b.get("limit") or 5))
        crossings = [c for p in res["paths"][:1] for c in p["crossings"]]
        audit.log_event("path_query", query={k: b.get(k) for k in ("from_ids", "to_ids", "from_domains", "to_domains",
                                                                    "mode", "include_inactive", "restrict_domains")},
                        paths_found=len(res["paths"]),
                        best_path=res["paths"][0]["nodes"] if res["paths"] else None,
                        boundary_crossings=crossings)
        return self._send(200, res)

    def _add_edge(self, b):
        with _write_lock:
            g, gov = store.load_graph(), store.load_governance()
            edge, sup_id, reason = validator.validate_new_edge(b, g, gov, store.next_edge_id(g))
            if sup_id:
                old = next(e for e in g["edges"] if e["edge_id"] == sup_id)
                old.setdefault("integrity", {}).update(superseded_by=edge["edge_id"],
                                                       superseded_at=edge["integrity"]["recorded_at"],
                                                       supersede_reason=reason)
            g.setdefault("edges", []).append(edge)
            store.save_graph(g)
        audit.log_event("edge_recorded", edge=edge, superseded=sup_id, reason=reason or None)
        return self._send(201, {"edge": edge})

    def _add_node(self, b):
        with _write_lock:
            g, gov = store.load_graph(), store.load_governance()
            node = validator.validate_new_node(b, g, gov)
            g.setdefault("nodes", []).append(node)
            store.save_graph(g)
        audit.log_event("node_recorded", node=node)
        return self._send(201, {"node": node})

    def _add_identity(self, b):
        with _write_lock:
            g, gov = store.load_graph(), store.load_governance()
            node, ids = validator.validate_identity_addition(b.get("node_id"), b.get("identity"), g, gov)
            node.setdefault("identity", {}).update(ids)
            node.setdefault("provenance", {}).setdefault("identity_additions", []).append(
                {"added": ids, "at": store.now_iso(), "evidence_url": (b.get("evidence_url") or "").strip() or None})
            store.save_graph(g)
        audit.log_event("identity_added", node_id=node["id"], identity=ids)
        return self._send(200, {"node": node})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"Priority Nexus running at {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
