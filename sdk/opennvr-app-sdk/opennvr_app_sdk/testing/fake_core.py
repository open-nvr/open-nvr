# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""A tiny in-process OpenNVR core for app tests.

Serves the internal routes the platform client (``OpenNVR`` /
``AsyncOpenNVR``) and the SDK's registration use, on a loopback port,
from plain dicts you set up: cameras with assignments, snapshots,
per-app state (a dict), an alerts inbox, and ``POST /apps/register``
(issues a key on first registration like core does). Every request is
recorded in ``requests`` so a test can assert what the app asked for.

    with FakeCore(cameras=[{"camera_id": "cam1", "name": "Gate"}]) as core:
        nvr = OpenNVR(core.url, token="oak_test")
        assert [c.name for c in nvr.cameras()] == ["Gate"]
        nvr.state.set("seen", 3)
        assert core.state["seen"] == 3
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


class FakeCore:
    def __init__(self, *, cameras: list[dict[str, Any]] | None = None,
                 snapshots: dict[str, bytes] | None = None,
                 alerts: list[dict[str, Any]] | None = None,
                 events: list[dict[str, Any]] | None = None,
                 app_key: str = "oak_test-app_" + "0" * 32) -> None:
        self.cameras = [self._camera(c, i) for i, c in enumerate(cameras or [], start=1)]
        self.snapshots = dict(snapshots or {})
        self.alerts = list(alerts or [])
        self.events = list(events or [])
        self.state: dict[str, Any] = {}
        self.registrations: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.app_key = app_key
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    @staticmethod
    def _camera(c: dict[str, Any], i: int) -> dict[str, Any]:
        out = {"camera_id": f"cam{i}", "open_nvr_camera_id": str(i), "name": f"Camera {i}",
               "role": "", "frame_url": f"rtsp://tap/cam-{i}", "assignments": []}
        out.update(c)
        return out

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self) -> "FakeCore":
        core = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # quiet
                pass

            def _reply(self, status: int, body: Any, ctype: str = "application/json") -> None:
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> Any:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"null") if n else None

            def _route(self, method: str) -> None:
                u = urlparse(self.path)
                body = self._body() if method in ("POST", "PUT") else None
                core.requests.append({"method": method, "path": u.path,
                                      "query": {k: v[0] for k, v in parse_qs(u.query).items()},
                                      "key": self.headers.get("X-Internal-Api-Key"), "body": body})
                status, out, ctype = core.handle(method, u.path, dict(parse_qs(u.query)), body)
                self._reply(status, out, ctype)

            def do_GET(self):  # noqa: N802
                self._route("GET")

            def do_POST(self):  # noqa: N802
                self._route("POST")

            def do_PUT(self):  # noqa: N802
                self._route("PUT")

            def do_DELETE(self):  # noqa: N802
                self._route("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> "FakeCore":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ── routes ─────────────────────────────────────────────────────

    def handle(self, method: str, path: str, query: dict[str, list[str]], body: Any):
        """(status, body, content-type) for one request. Override or
        extend in a subclass for routes your app needs beyond these."""
        p = path
        if method == "POST" and p == "/api/v1/apps/register":
            self.registrations.append(body or {})
            app_id = ((body or {}).get("manifest") or {}).get("id", "app")
            out = {"id": app_id, "enabled": True, "status": "registered",
                   "config": (body or {}).get("config") or {},
                   "has_api_key": True, "entitlement": {"mode": "none", "status": "none"},
                   "registry": {"server_version": "test", "api_version": "1.4",
                                "min_sdk_version": "0.2.0"}}
            if len(self.registrations) == 1 or (body or {}).get("wants_key"):
                out["api_key"] = self.app_key
            return 200, out, "application/json"
        if method == "GET" and p == "/api/v1/internal/camera-agent/cameras":
            return 200, {"cameras": self.cameras}, "application/json"
        if method == "GET" and p.startswith("/api/v1/internal/app/cameras/") and p.endswith("/snapshot"):
            cam = p.split("/")[-2]
            jpeg = self.snapshots.get(cam) or self.snapshots.get(self._cam_id(cam))
            if jpeg is None:
                return 503, {"detail": "no snapshot"}, "application/json"
            return 200, jpeg, "image/jpeg"
        if method == "GET" and p == "/api/v1/internal/camera-agent/events":
            return 200, {"events": self.events}, "application/json"
        if method == "GET" and p == "/api/v1/internal/app/alerts":
            return 200, {"alerts": self.alerts}, "application/json"
        if p == "/api/v1/internal/app/state" and method == "GET":
            prefix = (query.get("prefix") or [""])[0]
            return 200, {"items": [{"key": k, "value": v} for k, v in self.state.items()
                                   if k.startswith(prefix)]}, "application/json"
        if p.startswith("/api/v1/internal/app/state/"):
            key = p.rsplit("/", 1)[1]
            if method == "GET":
                if key in self.state:
                    return 200, {"key": key, "value": self.state[key]}, "application/json"
                return 404, {"detail": "No such key"}, "application/json"
            if method == "PUT":
                self.state[key] = body                      # the value IS the body (core's shape)
                return 200, {"key": key, "value": body}, "application/json"
            if method == "DELETE":
                existed = key in self.state
                self.state.pop(key, None)
                return 200, {"deleted": existed}, "application/json"
        return 404, {"detail": f"FakeCore has no route {method} {p}"}, "application/json"

    def _cam_id(self, numeric: str) -> str:
        for c in self.cameras:
            if c.get("open_nvr_camera_id") == numeric:
                return c["camera_id"]
        return numeric
