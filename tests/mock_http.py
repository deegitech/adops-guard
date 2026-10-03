"""A tiny programmable HTTP server on 127.0.0.1, so tests exercise the real urllib code paths offline."""

from __future__ import annotations

import json
import threading
import traceback
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Reply = tuple[int, dict[str, str], Any]


@dataclass
class Recorded:
    method: str
    path: str
    raw_path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes = b""
    extra: dict[str, Any] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body or b"{}")

    def form(self) -> dict[str, str]:
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.body.decode(), keep_blank_values=True).items()}


class MockServer:
    def __init__(self, app: Callable[[Recorded], Reply]) -> None:
        self.app = app
        self.requests: list[Recorded] = []
        self.errors: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep test output quiet
                return

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urllib.parse.urlsplit(self.path)
                rec = Recorded(
                    self.command,
                    parts.path,
                    self.path,
                    urllib.parse.parse_qs(parts.query, keep_blank_values=True),
                    {k.lower(): v for k, v in self.headers.items()},
                    body,
                )
                owner.requests.append(rec)
                try:
                    status, headers, payload = owner.app(rec)
                except Exception:  # surfaced through MockServer.errors
                    owner.errors.append(traceback.format_exc())
                    status, headers, payload = 599, {}, {"error": {"message": "mock server crashed"}}
                if isinstance(payload, bytes):
                    data = payload
                elif isinstance(payload, str):
                    data = payload.encode()
                else:
                    data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def start(self) -> MockServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"
