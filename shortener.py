"""A small persistent URL shortener with no third-party runtime dependencies."""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def validate_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid URL")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL must be an http(s) URL without credentials")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("invalid port")
    return value


class Store:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS links (code TEXT PRIMARY KEY, url TEXT NOT NULL, clicks INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL)")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, url: str, code: str | None = None) -> str:
        validate_url(url)
        if code is not None and not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", code):
            raise ValueError("code must be 3-32 URL-safe characters")
        with self.lock:
            with self._db() as db:
                for _ in range(5):
                    candidate = code or secrets.token_urlsafe(6)
                    try:
                        db.execute("INSERT INTO links (code,url,created) VALUES (?,?,?)", (candidate, url, datetime.now(timezone.utc).isoformat()))
                        return candidate
                    except sqlite3.IntegrityError:
                        if code:
                            raise ValueError("code already exists")
        raise RuntimeError("could not allocate unique code")

    def resolve(self, code: str) -> str | None:
        with self.lock:
            with self._db() as db:
                row = db.execute("SELECT url FROM links WHERE code=?", (code,)).fetchone()
                if row:
                    db.execute("UPDATE links SET clicks=clicks+1 WHERE code=?", (code,))
                    return row["url"]
        return None

    def stats(self, code: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT code,url,clicks,created FROM links WHERE code=?", (code,)).fetchone()
            return dict(row) if row else None


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/api/links":
                return self.respond(404, {"error": "not found"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > 4096:
                    return self.respond(413, {"error": "body must be 1-4096 bytes"})
                data = json.loads(self.rfile.read(size))
                code = store.create(data["url"], data.get("code"))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                return self.respond(400, {"error": str(exc)})
            except RuntimeError:
                return self.respond(503, {"error": "try again"})
            host = self.headers.get("Host", "localhost:8000")
            if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
                host = "localhost:8000"
            self.respond(201, {"code": code, "short_url": f"http://{host}/{code}"})

        def do_GET(self):
            if self.path == "/health":
                return self.respond(200, {"status": "ok"})
            if self.path.startswith("/api/links/") and self.path.endswith("/stats"):
                code = self.path[len("/api/links/"):-len("/stats")]
                row = store.stats(code)
                return self.respond(200, row) if row else self.respond(404, {"error": "not found"})
            code = self.path.lstrip("/")
            if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", code):
                return self.respond(404, {"error": "not found"})
            url = store.resolve(code)
            if not url:
                return self.respond(404, {"error": "not found"})
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

    return Handler


def main():
    host = os.environ.get("SHORTENER_HOST", "127.0.0.1")
    port = int(os.environ.get("SHORTENER_PORT", "8000"))
    store = Store(os.environ.get("SHORTENER_DB", "links.sqlite3"))
    print(f"Listening on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), make_handler(store)).serve_forever()


if __name__ == "__main__":
    main()
