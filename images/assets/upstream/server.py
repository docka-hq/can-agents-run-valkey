#!/usr/bin/env python3
"""Inventory service for the storefront: the slow upstream that the cache job puts a cache in front of.

GET  /products/<id>   -> the product as JSON, after ~300 ms (logged)
PUT  /products/<id>   -> body {"price": <number>} changes the price (logged)
GET  /health          -> "ok" (not logged)
"""
import json
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = pathlib.Path("/opt/upstream/products.json")
LOG = pathlib.Path("/var/log/upstream/requests.log")
LOCK = threading.Lock()
PRODUCTS: dict = {}


def load() -> None:
    global PRODUCTS
    PRODUCTS = {p["id"]: dict(p) for p in json.loads(DATA.read_text())}


def log(line: str) -> None:
    with LOCK, LOG.open("a") as f:
        f.write(f"{line} {time.time():.3f}\n")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _pid(self):
        parts = self.path.rstrip("/").split("/")
        if len(parts) == 3 and parts[1] == "products" and parts[2].isdigit():
            return int(parts[2])
        return None

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            return self._send(200, b"ok", "text/plain")
        pid = self._pid()
        if pid is None:
            return self._send(404, b'{"error": "not found"}')
        quiet = self.headers.get("X-Inventory-Audit") == "1"  # internal audit reads skip the delay and the log
        if not quiet:
            log(f"GET {pid}")
            time.sleep(0.3)
        with LOCK:
            p = PRODUCTS.get(pid)
        if p is None:
            return self._send(404, b'{"error": "no such product"}')
        return self._send(200, json.dumps(p).encode())

    def do_PUT(self):  # noqa: N802
        pid = self._pid()
        if pid is None:
            return self._send(404, b'{"error": "not found"}')
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            price = float(body["price"])
        except Exception:  # noqa: BLE001
            return self._send(400, b'{"error": "body must be {\\"price\\": <number>}"}')
        with LOCK:
            if pid not in PRODUCTS:
                return self._send(404, b'{"error": "no such product"}')
            PRODUCTS[pid]["price"] = price
            p = dict(PRODUCTS[pid])
        log(f"PUT {pid}")
        return self._send(200, json.dumps(p).encode())

    def do_POST(self):  # noqa: N802
        if self.path == "/__reset":  # used between test runs: original data, empty request log
            load()
            with LOCK:
                LOG.write_text("")
            return self._send(200, b"reset", "text/plain")
        return self._send(404, b'{"error": "not found"}')

    def log_message(self, *args):  # keep stderr quiet
        pass


if __name__ == "__main__":
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.touch()
    load()
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
