# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The attendance app's own web UI: attendly's screens on OpenNVR's data.

The page (``web/``, built into ``web/dist``) is served here, on a port of
its own, and every call it makes goes through here to core:

* ``POST /auth/login`` and ``/auth/refresh`` pass through to core's
  ``/api/v1/auth/login-json`` and ``/refresh``. The user signs in with
  their OpenNVR account; MFA, lockout and setup-required stay core's.
* ``/api/<path>`` passes through to core's ``/api/v1/<path>`` with the
  user's own bearer token, for the attendance routes and the few reads
  the page needs (cameras and their stills, the user's permissions). Anything else is 404:
  this is not a general way into core.

The app keeps no session and decides no permission. Core checks every
request as the user who made it, so its RBAC, audit and API tokens hold
exactly as on core's own page, and a user who may not see attendance
there cannot see it here.

One thing core cannot tell through a proxy is which browser is asking.
Its device firewall lets an internal caller with no device token through
(a sibling service), and every request from here comes from an internal
address. So a request without one is sent with a token that is never
valid: core then answers it as it would the browser itself — allowed
while the firewall is off, refused while it is on and the browser has
not been approved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

logger = logging.getLogger("attendance.webui")

CORE_API = "/api/v1"
#: Sent as the device token when the browser sent none (see the module doc).
NO_DEVICE = "no-device-token"
#: Core takes up to 10 photos of up to 8 MB each per enrolment.
MAX_BODY_BYTES = 10 * 8 * 1024 * 1024 + 1024 * 1024
MAX_AUTH_BODY_BYTES = 16 * 1024
#: An enrolment of ten photos is ~5 s of adapter time; a report is quick.
TIMEOUT_SECONDS = 120.0

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
#: x-forwarded-for: the attendance nginx appends the browser's address,
#: and core reads it from the right, trusting only its own network, so
#: the audit log and the device firewall see the browser, not this
#: container (a forged entry stays to the left and is never used).
_REQUEST_HEADERS = ("authorization", "content-type", "accept", "user-agent", "x-device-token",
                    "x-forwarded-for")
_RESPONSE_HEADERS = ("content-type", "content-disposition", "retry-after",
                     "x-setup-required", "www-authenticate")

#: The page loads only its own files and talks only to this origin. The
#: camera is for enrolment from a webcam.
SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(self), microphone=(), geolocation=()",
    "x-frame-options": "DENY",
}


def core_path(method: str, path: str) -> str | None:
    """Core's path for ``/api/<path>``, or None when the page may not go
    there. The attendance routes, any verb; the reads the page needs."""
    if method not in _METHODS:
        return None
    parts = path.strip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if parts[0] == "attendance":
        # Core's app-only routes live under /internal; none is under
        # /attendance today, and none ever may be reached from a browser.
        if len(parts) > 1 and parts[1] == "internal":
            return None
        return f"{CORE_API}/{'/'.join(parts)}"
    if method == "GET":
        if parts == ["cameras"]:
            # Every camera the user may see, inactive ones too (a slot can
            # be set up before its camera is switched on): core's default
            # is the first 100 active ones. The page's own query is not
            # passed on for this one.
            return CAMERA_LIST
        # One still of a camera, so the timetable's camera picker shows
        # what each camera sees. Core checks the user may see it.
        # isascii: "²".isdigit() is True and int("²") raises.
        if (len(parts) == 3 and parts[0] == "cameras" and parts[1].isascii()
                and parts[1].isdigit() and parts[2] == "snapshot"):
            return f"{CORE_API}/cameras/{int(parts[1])}/snapshot"
        if parts in (["users", "me", "permissions"], ["auth", "me"]):
            return f"{CORE_API}/{'/'.join(parts)}"
    return None


#: Core's camera list as the page reads it. Its answer carries stream
#: URLs with credentials, so it is matched by this exact value and cut
#: down to names (``_cameras_only_names``).
CAMERA_LIST = f"{CORE_API}/cameras/?limit=1000&status=all"

#: What the page needs of a camera: which one, and what it is called.
_CAMERA_FIELDS = ("id", "name", "location")


def _cameras_only_names(body: bytes) -> bytes:
    """Core's camera list carries each camera's stream URLs and their
    credentials; the timetable needs a name to pick. Anything that is not
    the expected list becomes an empty one rather than passing through."""
    try:
        data = json.loads(body)
        rows = data.get("cameras") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            rows = []
    except ValueError:
        rows = []
    cameras = [{k: c.get(k) for k in _CAMERA_FIELDS} for c in rows if isinstance(c, dict)]
    return json.dumps({"cameras": cameras}).encode()


class _TooLarge(Exception):
    pass


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _TooLarge
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


class _CoreClient:
    """One pooled client per event loop: uvicorn runs this app on a loop
    of its own, tests on theirs."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None) -> None:
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: httpx.AsyncClient | None = None

    def get(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            self._client = httpx.AsyncClient(base_url=self._base, transport=self._transport,
                                             timeout=TIMEOUT_SECONDS, follow_redirects=False,
                                             trust_env=False)
            self._loop = loop
        return self._client


def build_web_app(core_url: str, dist_dir: str | Path, *,
                  transport: httpx.AsyncBaseTransport | None = None) -> Any:
    """The UI's ASGI app (a Starlette app with SECURITY_HEADERS on every
    response). ``transport`` is for tests."""
    dist = Path(dist_dir).resolve()
    core = _CoreClient(core_url, transport)

    async def forward(request: Request, method: str, path: str, *, limit: int,
                      stand_in_device: bool) -> Response:
        try:
            body = await _read_body(request, limit)
        except _TooLarge:
            return JSONResponse({"detail": "The request is too large."}, status_code=413)
        headers = {k: v for k in _REQUEST_HEADERS if (v := request.headers.get(k))}
        if stand_in_device and not headers.get("x-device-token"):
            headers["x-device-token"] = NO_DEVICE
        url = path if "?" in path else path + (f"?{request.url.query}" if request.url.query else "")
        try:
            resp = await core.get().request(method, url, content=body or None, headers=headers)
        except httpx.TimeoutException:
            return JSONResponse({"detail": "OpenNVR took too long to answer."}, status_code=504)
        except httpx.HTTPError as exc:
            logger.warning("core unreachable for %s %s: %s", method, path, exc)
            # 503 says "ask again"; a 401 here would sign the user out of
            # a session that is still good.
            return JSONResponse({"error": "core_unreachable",
                                 "detail": "OpenNVR is not answering; try again."},
                                status_code=503, headers={"retry-after": "2"})
        out = {k: v for k in _RESPONSE_HEADERS if (v := resp.headers.get(k))}
        out["cache-control"] = "no-store"
        content = resp.content
        if path == CAMERA_LIST and resp.status_code == 200:
            content = _cameras_only_names(content)
            out.pop("content-length", None)
        return Response(content, status_code=resp.status_code, headers=out)

    async def api(request: Request) -> Response:
        target = core_path(request.method, request.path_params["path"])
        if target is None:
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return await forward(request, request.method, target, limit=MAX_BODY_BYTES,
                             stand_in_device=True)

    async def login(request: Request) -> Response:
        # The device token the browser holds is forwarded as it is: login
        # is where core enrols a browser, and it answers with a new token
        # for one that has none.
        return await forward(request, "POST", f"{CORE_API}/auth/login-json",
                             limit=MAX_AUTH_BODY_BYTES, stand_in_device=False)

    async def refresh(request: Request) -> Response:
        return await forward(request, "POST", f"{CORE_API}/auth/refresh",
                             limit=MAX_AUTH_BODY_BYTES, stand_in_device=False)

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok", "ui_built": (dist / "index.html").is_file()})

    async def page(request: Request) -> Response:
        rel = request.path_params.get("path", "")
        if rel:
            candidate = (dist / rel).resolve()
            if candidate.is_relative_to(dist) and candidate.is_file():
                return FileResponse(candidate)
        index = dist / "index.html"
        if not index.is_file():
            return PlainTextResponse("The attendance web UI is not built into this image.",
                                     status_code=503)
        # Every route of the single-page app is the same page.
        return FileResponse(index, headers={"cache-control": "no-cache"})

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/auth/login", login, methods=["POST"]),
        Route("/auth/refresh", refresh, methods=["POST"]),
        Route("/api/{path:path}", api, methods=list(_METHODS)),
        # Hashed file names: a cached copy is never stale.
        Mount("/assets", app=_Immutable(StaticFiles(directory=dist / "assets", check_dir=False))),
        Route("/{path:path}", page, methods=["GET", "HEAD"]),
    ]
    return _with_headers(Starlette(routes=routes))


class _Immutable:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        async def _send(message):
            if message["type"] == "http.response.start" and message.get("status") == 200:
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [
                    (b"cache-control", b"public, max-age=31536000, immutable")]
            await send(message)
        await self.app(scope, receive, _send)


def _with_headers(app: Any) -> Any:
    """Every response carries SECURITY_HEADERS."""
    extra = [(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]

    async def wrapped(scope, receive, send) -> None:
        if scope["type"] != "http":
            return await app(scope, receive, send)

        async def _send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers") or []) + extra
            await send(message)
        await app(scope, receive, _send)

    return wrapped


def serve_in_thread(app: Any, *, host: str, port: int) -> Any:
    """Run the UI on its own thread and loop, beside the app's tick.
    Returns the uvicorn server (set ``should_exit`` to stop it)."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, lifespan="off",
                                           log_level="warning", server_header=False,
                                           proxy_headers=False))
    threading.Thread(target=server.run, name="attendance-web", daemon=True).start()
    logger.info("web UI on %s:%d", host, port)
    return server
