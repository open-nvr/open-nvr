# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``AsyncOpenNVR`` — the platform client for apps that live on an
event loop.

The sync :class:`~opennvr_app_sdk.OpenNVR` is right for a detector
loop. An app with a ``/ui`` page, a FastAPI/Starlette service, or a
voice/agent loop that must never block would have to push every
platform call through ``asyncio.to_thread`` — the OpenNVR Agent ended
up writing its own async clients instead. This module is the same
surface, ``await``-ed::

    from opennvr_app_sdk.aio import AsyncOpenNVR

    async with AsyncOpenNVR() as nvr:
        for cam in await nvr.cameras():
            jpeg = await nvr.snapshot(cam)
            det = await nvr.ai.infer("yolov8", jpeg, task="object_detection")
        await nvr.state.set("last_seen", {"cam1": 12.5})
        recent = await nvr.timeline.search(camera="cam1", limit=5)

Method names, arguments, return types and error behaviour are those
of the sync client — reads degrade to ``None`` / ``[]`` and log,
writes raise :class:`PlatformError` — and both share the same route
paths, request builders and response parsers (``client.py``,
``frame_app.build_infer_request``), so they cannot drift. The one
difference: ``ai.stream()`` (a blocking WebSocket session) has no
async form yet; use ``ai.infer()`` per frame from async code.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Iterable

import httpx

from .client import (
    DEFAULT_TIMEOUT, Camera, PlatformError, Recording, _camera_id, _iso,
    parse_cameras, parse_recordings, parse_state_items, query_string,
)
from .credentials import AppCredentials
from .frame_app import KaiCError, build_infer_request

logger = logging.getLogger("opennvr.app.client.aio")

__all__ = ["AsyncOpenNVR", "Camera", "Recording", "PlatformError"]


class _AsyncHttp:
    def __init__(self, base: str, creds: AppCredentials, timeout: float,
                 client: httpx.AsyncClient | None = None) -> None:
        self.base = base.rstrip("/")
        self.creds = creds
        self.timeout = timeout
        self._owns = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, trust_env=False)

    def headers(self) -> dict[str, str]:
        return self.creds.headers()

    async def get(self, path: str, **params) -> httpx.Response:
        return await self._client.get(f"{self.base}{path}{query_string(params)}",
                                      headers=self.headers())

    async def get_json(self, path: str, **params) -> Any | None:
        try:
            r = await self.get(path, **params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenNVR GET %s failed: %s", path, exc)
            return None
        if r.status_code >= 400:
            logger.warning("OpenNVR GET %s → HTTP %d", path, r.status_code)
            return None
        try:
            return r.json()
        except ValueError:
            return None

    async def get_bytes(self, path: str, **params) -> bytes | None:
        try:
            r = await self.get(path, **params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenNVR GET %s failed: %s", path, exc)
            return None
        return r.content if r.status_code == 200 else None

    async def put_json(self, path: str, body: Any, **params) -> Any:
        url = f"{self.base}{path}{query_string(params)}"
        try:
            r = await self._client.put(url, json=body, headers=self.headers())
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"PUT {path} failed: {exc}") from exc
        if r.status_code >= 400:
            raise PlatformError(f"PUT {path} → HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    async def delete(self, path: str, **params) -> Any:
        url = f"{self.base}{path}{query_string(params)}"
        try:
            r = await self._client.delete(url, headers=self.headers())
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"DELETE {path} failed: {exc}") from exc
        if r.status_code >= 400:
            raise PlatformError(f"DELETE {path} → HTTP {r.status_code}")
        return r.json()

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()


# ── Sub-APIs ────────────────────────────────────────────────────────


class AsyncRecordingsAPI:
    def __init__(self, http: _AsyncHttp, camera_id: int) -> None:
        self._http = http
        self._cam = camera_id

    async def list(self, start: datetime | str | None = None,
                   end: datetime | str | None = None) -> list[Recording]:
        return parse_recordings(await self._http.get_json(
            f"/api/v1/internal/app/recordings/{self._cam}",
            start=_iso(start), end=_iso(end)))

    async def url(self, start: datetime | str, duration: float) -> str | None:
        body = await self._http.get_json(
            f"/api/v1/internal/app/recordings/{self._cam}/url",
            start=_iso(start), duration=duration)
        return (body or {}).get("url")

    async def frame_at(self, at: datetime | str) -> bytes | None:
        return await self._http.get_bytes(
            "/api/v1/internal/camera-agent/recordings/frame",
            camera_id=self._cam, at=_iso(at))


class AsyncTimelineAPI:
    def __init__(self, http: _AsyncHttp) -> None:
        self._http = http

    async def search(self, *, camera=None, label: str | None = None,
                     plate: str | None = None, start=None, end=None,
                     limit: int = 50) -> list[dict] | None:
        body = await self._http.get_json(
            "/api/v1/internal/camera-agent/events",
            camera_id=None if camera is None else _camera_id(camera),
            label=label, plate=plate, limit=limit,
            **{"from": _iso(start), "to": _iso(end)})
        return None if body is None else list(body.get("events") or [])

    async def evidence(self, event_id: int, *, scene: bool = False) -> bytes | None:
        suffix = "scene-evidence" if scene else "evidence"
        return await self._http.get_bytes(
            f"/api/v1/internal/camera-agent/events/{int(event_id)}/{suffix}")

    async def plate_stats(self, days: int = 7) -> dict | None:
        return await self._http.get_json("/api/v1/internal/app/plates/stats", days=days)

    async def plate_summary(self, plate: str) -> dict | None:
        return await self._http.get_json("/api/v1/internal/app/plates/summary", plate=plate)

    async def plate_sessions(self, plate: str, *, in_cameras: Iterable = (),
                             out_cameras: Iterable = (), limit: int = 50) -> dict | None:
        return await self._http.get_json(
            "/api/v1/internal/app/plates/sessions", plate=plate, limit=limit,
            in_cameras=",".join(str(_camera_id(c)) for c in in_cameras),
            out_cameras=",".join(str(_camera_id(c)) for c in out_cameras))


class AsyncAlertsAPI:
    def __init__(self, http: _AsyncHttp) -> None:
        self._http = http

    async def inbox(self, *, unacked: bool = False, limit: int = 50,
                    after_id: int | None = None) -> list[dict]:
        body = await self._http.get_json("/api/v1/internal/app/alerts",
                                         unacked="true" if unacked else None,
                                         limit=limit, after_id=after_id)
        return list((body or {}).get("alerts") or [])


class AsyncStateAPI:
    def __init__(self, http: _AsyncHttp) -> None:
        self._http = http

    async def get(self, key: str, default: Any = None) -> Any:
        body = await self._http.get_json(f"/api/v1/internal/app/state/{key}")
        return default if body is None else body.get("value", default)

    async def set(self, key: str, value: Any) -> None:
        await self._http.put_json(f"/api/v1/internal/app/state/{key}", value)

    async def delete(self, key: str) -> bool:
        body = await self._http.delete(f"/api/v1/internal/app/state/{key}")
        return bool(body.get("deleted"))

    async def items(self, prefix: str = "") -> dict[str, Any]:
        return parse_state_items(await self._http.get_json(
            "/api/v1/internal/app/state", prefix=prefix or None))


class AsyncAIAPI:
    """KAI-C: adapters that exist, and running one on a frame."""

    def __init__(self, kaic_url: str | None, api_key: str | None,
                 timeout: float, client: httpx.AsyncClient | None = None) -> None:
        self._base = (kaic_url or "").rstrip("/")
        self._key = api_key
        self._timeout = timeout
        self._owns = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, trust_env=False)

    async def capabilities(self) -> dict | None:
        if not self._base:
            return None
        headers = {"X-Internal-Api-Key": self._key} if self._key else {}
        try:
            r = await self._client.get(f"{self._base}/api/v1/ai/capabilities",
                                       headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.warning("KAI-C capabilities failed: %s", exc)
            return None
        return r.json() if r.status_code == 200 else None

    async def infer(self, adapter: str, jpeg: bytes, *, task: str,
                    camera_id: str | None = None, params: dict | None = None,
                    correlation_id: str | None = None) -> dict:
        """One inference call; raises :class:`KaiCError` on transport
        failure or a non-200, exactly like the sync client."""
        if not self._base:
            raise PlatformError("KAI-C is not configured (KAIC_URL)")
        url, headers, body = build_infer_request(
            self._base, adapter, jpeg, task=task, api_key=self._key,
            camera_id=None if camera_id is None else str(camera_id),
            params=params, correlation_id=correlation_id)
        try:
            r = await self._client.post(url, json=body, headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise KaiCError(f"KAI-C unreachable at {url}: {exc}") from exc
        if r.status_code != 200:
            raise KaiCError(f"KAI-C returned HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()


# ── The client ──────────────────────────────────────────────────────


class AsyncOpenNVR:
    """The async twin of :class:`opennvr_app_sdk.OpenNVR`; same
    arguments and environment fallbacks. Pass ``http_client`` to share
    one ``httpx.AsyncClient`` (a FastAPI app's lifespan pool, a test
    transport); the client then does not close it."""

    def __init__(self, url: str | None = None, *, token: str | None = None,
                 kaic_url: str | None = None, kaic_api_key: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 http_client: httpx.AsyncClient | None = None) -> None:
        base = url or os.environ.get("OPENNVR_URL") or ""
        if not base:
            raise ValueError("AsyncOpenNVR(url=...) or OPENNVR_URL is required")
        self.credentials = AppCredentials(token)
        self._http = _AsyncHttp(base, self.credentials, timeout, http_client)
        kaic = kaic_url or os.environ.get("KAIC_URL") or os.environ.get("OPENNVR_KAIC_URL")
        self.ai = AsyncAIAPI(kaic, kaic_api_key or os.environ.get("KAIC_API_KEY")
                             or os.environ.get("OPENNVR_INTERNAL_API_KEY"),
                             timeout, http_client)
        self.timeline = AsyncTimelineAPI(self._http)
        self.alerts = AsyncAlertsAPI(self._http)
        self.state = AsyncStateAPI(self._http)

    @property
    def url(self) -> str:
        return self._http.base

    async def cameras(self) -> list[Camera]:
        return parse_cameras(await self._http.get_json(
            "/api/v1/internal/camera-agent/cameras"))

    async def camera(self, camera) -> Camera | None:
        want = _camera_id(camera)
        return next((c for c in await self.cameras() if c.id == want), None)

    async def snapshot(self, camera) -> bytes | None:
        return await self._http.get_bytes(
            f"/api/v1/internal/app/cameras/{_camera_id(camera)}/snapshot")

    def recordings(self, camera) -> AsyncRecordingsAPI:
        return AsyncRecordingsAPI(self._http, _camera_id(camera))

    async def aclose(self) -> None:
        await self._http.aclose()
        await self.ai.aclose()

    async def __aenter__(self) -> "AsyncOpenNVR":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()
