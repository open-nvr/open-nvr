# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Async REST client for OpenNVR (contract 1.x).

The caller owns the ``aiohttp.ClientSession`` (Home Assistant passes its
shared one), so this never creates or closes sessions. Every request carries
the API token, and optionally an ``X-Correlation-Id`` that OpenNVR records on
the audit rows it writes, so "which automation moved the camera?" has an
answer.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .exceptions import (
    OpenNVRAuthError,
    OpenNVRConnectionError,
    OpenNVRContractError,
    OpenNVRNotFoundError,
    OpenNVRRequestError,
    OpenNVRSSLError,
)
from .models import (
    Camera,
    EntityCatalog,
    SignedMedia,
    SiteMode,
    StreamInfo,
    SystemInfo,
    Zone,
)

API = "/api/v1"
#: Names why a request was refused, when the client must act differently.
ERROR_HEADER = "X-OpenNVR-Error"
#: Contract major versions this library speaks (design §6.11).
SUPPORTED_CONTRACT_MAJOR = 1

def check_contract(info: SystemInfo) -> None:
    """Raise OpenNVRContractError unless the server speaks contract 1.x."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", info.contract_version or "")
    if not m or int(m.group(1)) != SUPPORTED_CONTRACT_MAJOR:
        raise OpenNVRContractError(
            f"server contract {info.contract_version!r} is not supported "
            f"(this client speaks {SUPPORTED_CONTRACT_MAJOR}.x)", info.contract_version)


class OpenNVRClient:
    """One OpenNVR site."""

    def __init__(
        self,
        base_url: str,
        token: str,
        session: aiohttp.ClientSession,
        *,
        verify_ssl: bool = True,
        request_timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._session = session
        self._ssl: bool | None = None if verify_ssl else False
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)

    # ── plumbing ─────────────────────────────────────────────────────────

    @property
    def ssl(self) -> bool | None:
        """The ``ssl=`` value for aiohttp calls to this site."""
        return self._ssl

    def _headers(self, correlation_id: str | None = None,
                 extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if correlation_id:
            h["X-Correlation-Id"] = correlation_id
        if extra:
            h.update(extra)
        return h

    def site_url(self, url: str) -> str:
        """Re-root a server-built absolute URL onto this site's base URL.

        OpenNVR builds some URLs from its own view of its public address
        (``https://localhost/webrtc/...`` on a default install). A client on
        another machine must use the address IT reached the API on.
        """
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        return f"{self.base_url}{path}" if parts.scheme else f"{self.base_url}{url}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        headers: dict[str, str] | None = None,
        raw: bool = False,
    ) -> Any:
        """One API call; ``path`` is relative to /api/v1. Returns decoded
        JSON (or bytes with ``raw=True``); raises the pyopennvr errors."""
        url = f"{self.base_url}{API}{path}"
        try:
            async with self._session.request(
                method, url, json=json, params=_clean(params),
                headers=self._headers(correlation_id, headers),
                timeout=self._timeout, ssl=self._ssl,
            ) as resp:
                if resp.status in (401, 403):
                    raise OpenNVRAuthError(await _detail(resp), resp.status,
                                           resp.headers.get(ERROR_HEADER))
                if resp.status == 404:
                    raise OpenNVRNotFoundError(await _detail(resp))
                if resp.status >= 500:
                    raise OpenNVRConnectionError(f"{method} {path}: {resp.status}")
                if resp.status >= 400:
                    detail = await _detail(resp)
                    raise OpenNVRRequestError(f"{method} {path}: {detail}", resp.status, detail)
                if resp.status == 304:
                    return None
                if raw:
                    return await resp.read()
                if resp.content_type == "application/json":
                    return await resp.json()
                return await resp.text()
        except aiohttp.ClientSSLError as exc:
            raise OpenNVRSSLError(f"{method} {path}: {exc}") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise OpenNVRConnectionError(f"{method} {path}: {exc}") from exc

    # ── site ─────────────────────────────────────────────────────────────

    async def get_system_info(self) -> SystemInfo:
        return SystemInfo.from_dict(await self.request("GET", "/system/info"))

    async def get_system_resources(self) -> dict:
        return await self.request("GET", "/system/resources")

    async def get_site_mode(self) -> SiteMode:
        return SiteMode.from_dict(await self.request("GET", "/site-mode"))

    async def set_site_mode(self, mode: str, *, reason: str | None = None,
                            correlation_id: str | None = None) -> SiteMode:
        return SiteMode.from_dict(await self.request(
            "PUT", "/site-mode", json={"mode": mode, "reason": reason},
            correlation_id=correlation_id))

    # ── cameras ──────────────────────────────────────────────────────────

    async def get_cameras(self, *, page_size: int = 200) -> list[Camera]:
        """Every camera the token can see, turned off ones included (a camera
        that is off still exists; its entities must not vanish), all pages."""
        out: list[Camera] = []
        while True:
            data = await self.request("GET", "/cameras/", params={
                "active_only": False, "skip": len(out), "limit": page_size})
            rows = data.get("cameras", []) if isinstance(data, dict) else data
            out += [Camera.from_dict(c) for c in rows]
            total = data.get("total") if isinstance(data, dict) else None
            if len(rows) < page_size or (isinstance(total, int) and len(out) >= total):
                return out

    async def get_camera(self, camera_id: int) -> Camera:
        return Camera.from_dict(await self.request("GET", f"/cameras/{int(camera_id)}"))

    async def get_camera_stats(self, camera_id: int) -> dict:
        return await self.request("GET", f"/cameras/{int(camera_id)}/stats")

    async def get_snapshot(self, camera_id: int) -> bytes:
        return await self.request("GET", f"/cameras/{int(camera_id)}/snapshot", raw=True)

    async def set_detection(self, camera_id: int, enabled: bool, *, reason: str | None = None,
                            correlation_id: str | None = None) -> Camera:
        return Camera.from_dict(await self.request(
            "PUT", f"/cameras/{int(camera_id)}",
            json={"detection_enabled": bool(enabled), "reason": reason},
            correlation_id=correlation_id))

    async def set_camera_active(self, camera_id: int, active: bool, *,
                                correlation_id: str | None = None) -> Camera:
        """Turn a camera on/off. Turning it off also stops recording, so the
        server allows it only where the site allows pausing recording."""
        return Camera.from_dict(await self.request(
            "PUT", f"/cameras/{int(camera_id)}", json={"is_active": bool(active)},
            correlation_id=correlation_id))

    async def set_recording(self, camera_id: int, enabled: bool, *,
                            resume_after_s: int | None = None, reason: str | None = None,
                            correlation_id: str | None = None) -> dict:
        body: dict[str, Any] = {"enabled": bool(enabled), "reason": reason}
        if resume_after_s is not None:
            body["resume_after_s"] = int(resume_after_s)
        return await self.request("POST", f"/cameras/{int(camera_id)}/recording",
                                  json=body, correlation_id=correlation_id)

    async def get_stream_info(self, camera_id: int) -> StreamInfo:
        info = StreamInfo.from_dict(await self.request("GET", f"/streams/{int(camera_id)}/info"))
        if info.webrtc_url:
            object.__setattr__(info, "webrtc_url", self.site_url(info.webrtc_url))
        return info

    async def get_zones(self, camera_id: int) -> list[Zone]:
        data = await self.request("GET", f"/cameras/{int(camera_id)}/zones")
        return [Zone.from_dict(z) for z in data.get("zones", [])]

    # ── PTZ ──────────────────────────────────────────────────────────────

    async def ptz_move(self, camera_id: int, x: float = 0.0, y: float = 0.0, z: float = 0.0,
                       *, correlation_id: str | None = None) -> dict:
        return await self.request("POST", f"/cameras/{int(camera_id)}/ptz/move",
                                  params={"x": x, "y": y, "z": z},
                                  correlation_id=correlation_id)

    async def ptz_stop(self, camera_id: int, *, correlation_id: str | None = None) -> dict:
        return await self.request("POST", f"/cameras/{int(camera_id)}/ptz/stop",
                                  correlation_id=correlation_id)

    async def ptz_presets(self, camera_id: int) -> list[dict]:
        data = await self.request("GET", f"/cameras/{int(camera_id)}/ptz/presets")
        return list(data.get("presets", []))

    async def ptz_goto_preset(self, camera_id: int, preset_token: str, *,
                              correlation_id: str | None = None) -> dict:
        return await self.request(
            "POST", f"/cameras/{int(camera_id)}/ptz/presets/{preset_token}/goto",
            correlation_id=correlation_id)

    # ── events, alerts, media, search ───────────────────────────────────

    async def get_live_state(self, camera_id: int | None = None) -> dict:
        return await self.request("GET", "/live-state", params={"camera_id": camera_id})

    async def create_event(self, camera_id: int, *, label: str = "manual",
                           note: str | None = None, duration_s: float | None = None,
                           correlation_id: str | None = None) -> dict:
        return await self.request("POST", "/events", json=_body({
            "camera_id": int(camera_id), "label": label, "note": note,
            "duration_s": duration_s}), correlation_id=correlation_id)

    async def end_event(self, event_id: int, *, correlation_id: str | None = None) -> dict:
        return await self.request("PUT", f"/events/{int(event_id)}/end",
                                  correlation_id=correlation_id)

    async def protect_event(self, event_id: int, *, pre_s: int = 10, post_s: int = 10,
                            correlation_id: str | None = None) -> dict:
        return await self.request("POST", f"/events/{int(event_id)}/protect",
                                  json={"pre_s": pre_s, "post_s": post_s},
                                  correlation_id=correlation_id)

    async def ack_alerts(self, *, ids: list[int] | None = None, source: str | None = None,
                         severity: str | None = None,
                         correlation_id: str | None = None) -> dict:
        """Acknowledge these alert ids, or every unacknowledged alert matching
        ``source``/``severity``, or (neither) every unacknowledged alert the
        token can see. Ids and filters are exclusive (the server refuses both)."""
        body = ({"ids": ids} if ids else
                _body({"source_name": source, "severity": severity}))
        return await self.request("POST", "/alerts-inbox/ack", json=body,
                                  correlation_id=correlation_id)

    async def sign_media(self, kind: str, *, id: int | None = None, name: str | None = None,
                         camera_id: int | None = None, start: str | None = None,
                         duration_s: float | None = None,
                         ttl_s: int | None = None) -> SignedMedia:
        data = await self.request("POST", "/media/sign", json=_body({
            "kind": kind, "id": id, "name": name, "camera_id": camera_id, "start": start,
            "duration_s": duration_s, "ttl_s": ttl_s}))
        return SignedMedia(url=self.site_url(data["url"]), expires_at=data["expires_at"])

    async def get_events(self, *, camera_id: int | None = None, label: str | None = None,
                         from_: str | None = None, to: str | None = None,
                         skip: int = 0, limit: int = 50) -> dict:
        """``GET /events``: ``{"events": [...], "total": n}``, newest first."""
        return await self.request("GET", "/events", params={
            "camera_id": camera_id, "label": label, "from": from_, "to": to,
            "skip": skip, "limit": limit})

    async def get_alerts(self, *, severity: str | None = None, source: str | None = None,
                         camera_id: str | None = None, unacked: bool = False,
                         skip: int = 0, limit: int = 50) -> dict:
        """``GET /alerts-inbox``: ``{"alerts": [...], "total": n}``, newest
        first; each alert lists its image ``names``."""
        return await self.request("GET", "/alerts-inbox", params={
            "severity": severity, "source_name": source, "camera_id": camera_id,
            "unacked": unacked, "skip": skip, "limit": limit})

    async def search(self, **filters: Any) -> dict:
        """``GET /search``; ``from_`` is sent as ``from``."""
        if "from_" in filters:
            filters["from"] = filters.pop("from_")
        return await self.request("GET", "/search", params=filters)

    # ── entities ─────────────────────────────────────────────────────────

    async def get_entities(self, etag: str | None = None) -> EntityCatalog | None:
        """The descriptors this token may see; None when ``etag`` is current."""
        headers = {"If-None-Match": f'"{etag}"'} if etag else None
        data = await self.request("GET", "/entities", headers=headers)
        return None if data is None else EntityCatalog.from_dict(data)

    async def get_entity_states(self) -> dict[str, dict]:
        return (await self.request("GET", "/entities/states")).get("states", {})

    async def command_entity(self, key: str, value: Any = None, *,
                             args: dict[str, Any] | None = None,
                             correlation_id: str | None = None) -> dict:
        return await self.request("POST", f"/entities/{key}/command",
                                  json={"value": value, "args": args or {}},
                                  correlation_id=correlation_id)

    async def open_session(self, *, camera_ids: list[int] | None = None,
                           ttl_s: int = 600) -> dict:
        """A dashboard card's credential (contract 1.1, ``card_session``):
        this token's reading scopes, its cameras or fewer, ≤ 10 minutes,
        revoked with this token. ``{"token", "expires_at", "scopes",
        "camera_ids"}``."""
        return await self.request("POST", "/api-tokens/session", json=_body({
            "camera_ids": camera_ids, "ttl_s": int(ttl_s)}))

    # ── events websocket ─────────────────────────────────────────────────

    async def ws_ticket(self) -> str:
        return (await self.request("POST", "/events/ws-ticket"))["ticket"]

    def ws_url(self, ticket: str, *, since: int | None = None, epoch: str | None = None,
               types: list[str] | None = None) -> str:
        base = self.base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        q = [f"ticket={ticket}", "v=2"]
        if since is not None and epoch:
            q += [f"since={int(since)}", f"epoch={epoch}"]
        q += [f"types={t}" for t in types or []]
        return f"{base}{API}/events/ws?" + "&".join(q)


def _clean(d: dict[str, Any] | None) -> dict[str, Any] | None:
    """Query params: drop None, and spell booleans the way FastAPI parses."""
    if d is None:
        return None
    return {k: (str(v).lower() if isinstance(v, bool) else v)
            for k, v in d.items() if v is not None}


def _body(d: dict[str, Any]) -> dict[str, Any]:
    """JSON bodies: drop None, keep every other value as-is."""
    return {k: v for k, v in d.items() if v is not None}


async def _detail(resp: aiohttp.ClientResponse) -> str:
    try:
        body = await resp.json(content_type=None)
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
        return str(body)
    except Exception:  # noqa: BLE001
        return f"HTTP {resp.status}"
