# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HTTP to a remote site's cameras through its agent (issue #925, C3).

Core cannot reach a camera on a customer's LAN, but the site's agent can. So
for a site camera every HTTP request core's camera code makes (ONVIF SOAP,
PTZ, vendor CGI/ISAPI, the capability probe, event PullPoint) is carried by
the agent: core queues it, the agent picks it up on its long poll, sends it
on the site's LAN and posts the response back. Core keeps all the protocol
logic (digest / WS-Security auth, parsing); the agent only forwards bytes,
and only to that site's own cameras.

How a request finds its way: a site camera's control host is a synthetic,
never-resolvable name, ``c<camera>.s<site>.opennvr-site.invalid``
(``control_host``). ``camera_http_client()`` is httpx with a transport that
sends requests for those names through the agent and everything else
directly, so the camera code needs no other change. Code that still builds
its own client fails to resolve the name: it never reaches the wrong device.

Not carried: long-lived streaming responses (some vendors' event streams).
A request is one response, at most RESPONSE_MAX bytes, within REQUEST_MAX_S.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import re
import time
from typing import Any

import httpx

SITE_HOST_SUFFIX = ".opennvr-site.invalid"
_HOST_RE = re.compile(r"^c(\d+)\.s(\d+)" + re.escape(SITE_HOST_SUFFIX) + r"$")

REQUEST_BODY_MAX = 1 << 20  # 1 MiB
RESPONSE_MAX = 4 << 20  # 4 MiB
REQUEST_MAX_S = 20.0
#: An RPC the agent has not picked up within this is dropped (site offline).
PICKUP_MAX_S = 30.0
#: How long a camera's LAN host is trusted before it is read again.
ROUTE_CACHE_S = 60.0


# ── names ────────────────────────────────────────────────────────────────


def control_host(camera: Any) -> str:
    """The host core's camera code should use for this camera: the synthetic
    site name for a site camera, its own address otherwise."""
    if getattr(camera, "site_id", None) and getattr(camera, "site_lan_host", None):
        return f"c{camera.id}.s{camera.site_id}{SITE_HOST_SUFFIX}"
    return camera.ip_address


def is_site_host(host: str | None) -> bool:
    return bool(host) and host.lower().endswith(SITE_HOST_SUFFIX)


def parse_host(host: str | None) -> tuple[int, int] | None:
    """``(camera_id, site_id)`` for a synthetic site host, else None."""
    m = _HOST_RE.match((host or "").lower())
    return (int(m.group(1)), int(m.group(2))) if m else None


# ── routes: synthetic host -> the camera's address on the site LAN ──────

_routes: dict[int, tuple[float, int, str]] = {}  # camera_id -> (at, site_id, lan_host)


def prime(camera: Any) -> None:
    """Remember a site camera's LAN host (the driver registry calls this)."""
    if getattr(camera, "site_id", None) and getattr(camera, "site_lan_host", None):
        _routes[camera.id] = (time.monotonic(), camera.site_id, camera.site_lan_host)


def _lan_host(camera_id: int, site_id: int) -> str | None:
    hit = _routes.get(camera_id)
    if hit and time.monotonic() - hit[0] < ROUTE_CACHE_S and hit[1] == site_id:
        return hit[2]
    from core.database import SessionLocal
    from models import Camera

    with SessionLocal() as db:
        cam = db.get(Camera, camera_id)
        if cam is None or cam.site_id != site_id or not cam.site_lan_host or cam.deleted_at is not None:
            _routes.pop(camera_id, None)
            return None
        prime(cam)
        return cam.site_lan_host


# ── the queue between core and the agents ───────────────────────────────

_ids = itertools.count(1)
_pending: dict[int, dict[str, dict[str, Any]]] = {}  # site -> rpc id -> request
_inflight: dict[str, tuple[int, asyncio.Future]] = {}  # rpc id -> (site, future)


def has_pending(site_id: int) -> bool:
    return bool(_pending.get(site_id))


def take_pending(site_id: int) -> list[dict[str, Any]]:
    """Hand the site's queued requests to its agent (on its long poll)."""
    reqs = _pending.pop(site_id, {})
    return list(reqs.values())


def complete(site_id: int, rpc_id: str, result: dict[str, Any]) -> bool:
    """The agent's answer for one request. False for an unknown id or one
    that belongs to another site (the caller answers 404)."""
    entry = _inflight.get(rpc_id)
    if entry is None or entry[0] != site_id:
        return False
    _inflight.pop(rpc_id, None)
    fut = entry[1]
    if not fut.done():
        fut.get_loop().call_soon_threadsafe(_resolve, fut, result)
    return True


def _resolve(fut: asyncio.Future, result: dict[str, Any]) -> None:
    if not fut.done():
        fut.set_result(result)


async def call(site_id: int, request: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Queue one request for the site's agent and wait for its answer.
    Raises asyncio.TimeoutError when the agent does not answer in time."""
    from services import sites

    rpc_id = f"r{next(_ids)}"
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _inflight[rpc_id] = (site_id, fut)
    _pending.setdefault(site_id, {})[rpc_id] = {"id": rpc_id, **request}
    sites.notify(site_id)
    try:
        return await asyncio.wait_for(fut, timeout + PICKUP_MAX_S)
    finally:
        _inflight.pop(rpc_id, None)
        site_q = _pending.get(site_id)
        if site_q is not None:
            site_q.pop(rpc_id, None)
            if not site_q:
                _pending.pop(site_id, None)


# ── the httpx side ──────────────────────────────────────────────────────


def _timeout_of(request: httpx.Request) -> float:
    t = (request.extensions or {}).get("timeout") or {}
    vals = [v for v in (t.get("read"), t.get("connect")) if isinstance(v, (int, float))]
    return min(max(vals) if vals else REQUEST_MAX_S, REQUEST_MAX_S)


class SiteAwareTransport(httpx.AsyncBaseTransport):
    """Requests for a synthetic site host go through that site's agent;
    every other request goes out directly, as httpx would send it."""

    def __init__(self, verify: Any = True) -> None:
        self._direct = httpx.AsyncHTTPTransport(verify=verify)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        route = parse_host(request.url.host)
        if route is None:
            return await self._direct.handle_async_request(request)
        camera_id, site_id = route
        lan_host = _lan_host(camera_id, site_id)
        if lan_host is None:
            raise httpx.ConnectError("not a camera of an enrolled site", request=request)
        body = await request.aread()
        if len(body) > REQUEST_BODY_MAX:
            raise httpx.RequestError("request too large for the site relay", request=request)
        target = request.url.copy_with(host=lan_host)
        headers: dict[str, list[str]] = {}
        for k, v in request.headers.multi_items():
            if k.lower() in ("host", "content-length", "connection", "transfer-encoding"):
                continue
            headers.setdefault(k, []).append(v)
        timeout = _timeout_of(request)
        try:
            res = await call(site_id, {
                "method": request.method,
                "url": str(target),
                "headers": headers,
                "body_b64": base64.b64encode(body).decode() if body else "",
                "timeout_s": timeout,
            }, timeout)
        except (TimeoutError, asyncio.TimeoutError) as e:
            raise httpx.ReadTimeout("the site's agent did not answer in time", request=request) from e
        if res.get("error"):
            raise httpx.ConnectError(f"at the site: {res['error']}", request=request)
        content = base64.b64decode(res.get("body_b64") or "")
        if len(content) > RESPONSE_MAX:
            raise httpx.RequestError("response too large for the site relay", request=request)
        out_headers = [
            (k, v) for k, vs in (res.get("headers") or {}).items()
            if k.lower() not in ("content-length", "transfer-encoding", "content-encoding", "connection")
            for v in (vs if isinstance(vs, list) else [vs])
        ]
        return httpx.Response(int(res.get("status") or 502), headers=out_headers, content=content,
                              request=request)

    async def aclose(self) -> None:
        await self._direct.aclose()


def camera_http_client(**kwargs: Any) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` for talking to a camera: same arguments, plus
    site cameras reached through their site's agent."""
    verify = kwargs.pop("verify", True)
    return httpx.AsyncClient(transport=SiteAwareTransport(verify=verify), **kwargs)
