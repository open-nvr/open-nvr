# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``OpenNVR`` — the one object an app uses to talk to the platform.

Everything a vision app needs from core and from the AI layer, behind
one client that already knows the app's credential (``credentials.py``)
and therefore the app's camera roster::

    from opennvr_app_sdk import OpenNVR

    nvr = OpenNVR()                        # OPENNVR_URL + the app key
    for cam in nvr.cameras():              # the cameras assigned to this app
        jpeg = nvr.snapshot(cam)           # current frame
        caps = nvr.ai.capabilities()       # which adapters/tasks exist
        det = nvr.ai.infer("yolov8", jpeg, task="object_detection")
    nvr.state.set("last_plate", "KA01AB1234")   # survives restarts
    for seg in nvr.recordings(cam).list(): ...
    stats = nvr.timeline.plate_stats(days=7)
    mine = nvr.alerts.inbox(unacked=True)  # what the operator hasn't acked

Synchronous on purpose: most detectors are a loop, and the one async
archetype (``Detector``) can call these from ``asyncio.to_thread``.
Every method is a thin, typed wrapper over a core route on the app
platform door (``/api/v1/internal/app/*``, ``/internal/camera-agent/*``)
or KAI-C (``/api/v1/infer``, ``/api/v1/ai/capabilities``); nothing here
touches NATS, the database or files. Errors: reads return ``None`` /
``[]`` and log (the platform being briefly unreachable must not take an
app down); writes raise :class:`PlatformError` so a lost state write is
never silent.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import urlencode

import httpx

from .credentials import AppCredentials

logger = logging.getLogger("opennvr.app.client")

DEFAULT_TIMEOUT = 5.0


class PlatformError(RuntimeError):
    """A write (state, actions) the platform refused or could not take."""


def _camera_id(value) -> int:
    """``Camera`` / ``"cam3"`` / ``"cam-3"`` / ``3`` → ``3``."""
    if isinstance(value, Camera):
        return int(value.id)
    if isinstance(value, bool):
        raise ValueError("camera must be an id, a handle or a Camera")
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if text.startswith("cam-"):
        text = text[4:]
    elif text.startswith("cam"):
        text = text[3:]
    if not text.isdigit():
        raise ValueError(f"not a camera id or handle: {value!r}")
    return int(text)


@dataclass(frozen=True)
class Camera:
    """One camera in the app's roster (what core assigned to this app)."""

    id: int
    handle: str            # "cam3" — the bus / app-state key
    name: str
    role: str              # name; location; description — for prompts and UIs
    frame_url: str         # where the platform serves this camera's frames
    assignments: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    def has_skill(self, skill: str) -> bool:
        return any(isinstance(a, dict) and a.get("skill") == skill for a in self.assignments)


@dataclass(frozen=True)
class Recording:
    start: str
    duration: float
    raw: dict = field(default_factory=dict, compare=False, repr=False)


class _Http:
    """The client's one HTTP session: base URL + the app's headers,
    re-read per request so a key issued after start-up is honoured."""

    def __init__(self, base: str, creds: AppCredentials, timeout: float) -> None:
        self.base = base.rstrip("/")
        self.creds = creds
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout, trust_env=False)

    def headers(self) -> dict[str, str]:
        return self.creds.headers()

    def get(self, path: str, **params) -> httpx.Response:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{self.base}{path}" + (f"?{urlencode(clean)}" if clean else "")
        return self._client.get(url, headers=self.headers())

    def get_json(self, path: str, **params) -> Any | None:
        try:
            r = self.get(path, **params)
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

    def get_bytes(self, path: str, **params) -> bytes | None:
        try:
            r = self.get(path, **params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenNVR GET %s failed: %s", path, exc)
            return None
        return r.content if r.status_code == 200 else None

    def put_json(self, path: str, body: Any, **params) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{self.base}{path}" + (f"?{urlencode(clean)}" if clean else "")
        try:
            r = self._client.put(url, json=body, headers=self.headers())
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"PUT {path} failed: {exc}") from exc
        if r.status_code >= 400:
            raise PlatformError(f"PUT {path} → HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def delete(self, path: str, **params) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{self.base}{path}" + (f"?{urlencode(clean)}" if clean else "")
        try:
            r = self._client.delete(url, headers=self.headers())
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"DELETE {path} failed: {exc}") from exc
        if r.status_code >= 400:
            raise PlatformError(f"DELETE {path} → HTTP {r.status_code}")
        return r.json()

    def close(self) -> None:
        self._client.close()


# ── Sub-APIs ────────────────────────────────────────────────────────


class RecordingsAPI:
    def __init__(self, http: _Http, camera_id: int) -> None:
        self._http = http
        self._cam = camera_id

    def list(self, start: datetime | str | None = None,
             end: datetime | str | None = None) -> list[Recording]:
        body = self._http.get_json(
            f"/api/v1/internal/app/recordings/{self._cam}",
            start=_iso(start), end=_iso(end))
        rows = (body or {}).get("recordings") or []
        return [Recording(start=str(r.get("start", "")),
                          duration=float(r.get("duration", 0) or 0), raw=r)
                for r in rows if isinstance(r, dict)]

    def url(self, start: datetime | str, duration: float) -> str | None:
        body = self._http.get_json(
            f"/api/v1/internal/app/recordings/{self._cam}/url",
            start=_iso(start), duration=duration)
        return (body or {}).get("url")

    def frame_at(self, at: datetime | str) -> bytes | None:
        """One JPEG out of the recording at a past instant."""
        return self._http.get_bytes(
            "/api/v1/internal/camera-agent/recordings/frame",
            camera_id=self._cam, at=_iso(at))


class TimelineAPI:
    """The events store: visits, plates, evidence photos."""

    def __init__(self, http: _Http) -> None:
        self._http = http

    def search(self, *, camera=None, label: str | None = None,
               plate: str | None = None, start=None, end=None,
               limit: int = 50) -> list[dict] | None:
        """Visits overlapping [start, end), newest first. ``None`` when the
        store could not be reached (distinct from an empty window)."""
        body = self._http.get_json(
            "/api/v1/internal/camera-agent/events",
            camera_id=None if camera is None else _camera_id(camera),
            label=label, plate=plate, limit=limit,
            **{"from": _iso(start), "to": _iso(end)})
        return None if body is None else list(body.get("events") or [])

    def evidence(self, event_id: int, *, scene: bool = False) -> bytes | None:
        suffix = "scene-evidence" if scene else "evidence"
        return self._http.get_bytes(
            f"/api/v1/internal/camera-agent/events/{int(event_id)}/{suffix}")

    def plate_stats(self, days: int = 7) -> dict | None:
        return self._http.get_json("/api/v1/internal/app/plates/stats", days=days)

    def plate_summary(self, plate: str) -> dict | None:
        return self._http.get_json("/api/v1/internal/app/plates/summary", plate=plate)

    def plate_sessions(self, plate: str, *, in_cameras: Iterable = (),
                       out_cameras: Iterable = (), limit: int = 50) -> dict | None:
        return self._http.get_json(
            "/api/v1/internal/app/plates/sessions", plate=plate, limit=limit,
            in_cameras=",".join(str(_camera_id(c)) for c in in_cameras),
            out_cameras=",".join(str(_camera_id(c)) for c in out_cameras))


class AlertsAPI:
    def __init__(self, http: _Http) -> None:
        self._http = http

    def inbox(self, *, unacked: bool = False, limit: int = 50,
              after_id: int | None = None) -> list[dict]:
        """The operator-inbox rows THIS app raised, newest first, with
        their acknowledgement state."""
        body = self._http.get_json("/api/v1/internal/app/alerts",
                                   unacked="true" if unacked else None,
                                   limit=limit, after_id=after_id)
        return list((body or {}).get("alerts") or [])


class StateAPI:
    """Durable per-app key/value in core — survives restarts and
    redeploys, unlike ``KeyedState``."""

    def __init__(self, http: _Http) -> None:
        self._http = http

    def get(self, key: str, default: Any = None) -> Any:
        body = self._http.get_json(f"/api/v1/internal/app/state/{key}")
        return default if body is None else body.get("value", default)

    def set(self, key: str, value: Any) -> None:
        self._http.put_json(f"/api/v1/internal/app/state/{key}", value)

    def delete(self, key: str) -> bool:
        return bool(self._http.delete(f"/api/v1/internal/app/state/{key}").get("deleted"))

    def items(self, prefix: str = "") -> dict[str, Any]:
        body = self._http.get_json("/api/v1/internal/app/state", prefix=prefix or None)
        return {r["key"]: r["value"] for r in (body or {}).get("items") or []}


class AIAPI:
    """KAI-C: what adapters exist and running one on a frame."""

    def __init__(self, kaic_url: str | None, api_key: str | None,
                 timeout: float, client_id: str) -> None:
        self._base = (kaic_url or "").rstrip("/")
        self._key = api_key
        self._timeout = timeout
        self._client_id = client_id
        self._kaic: dict[str, Any] = {}

    def _client_for(self, adapter: str):
        if not self._base:
            raise PlatformError("KAI-C is not configured (KAIC_URL)")
        client = self._kaic.get(adapter)
        if client is None:
            from .frame_app import KaiCClient

            client = KaiCClient(self._base, adapter, api_key=self._key,
                                timeout_seconds=self._timeout)
            self._kaic[adapter] = client
        return client

    def capabilities(self) -> dict | None:
        """KAI-C's ``/api/v1/ai/capabilities``: adapters, their tasks and
        health — so an app can check ``requires_tasks`` are really there."""
        if not self._base:
            return None
        headers = {"X-Internal-Api-Key": self._key} if self._key else {}
        try:
            r = httpx.get(f"{self._base}/api/v1/ai/capabilities", headers=headers,
                          timeout=self._timeout, trust_env=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("KAI-C capabilities failed: %s", exc)
            return None
        return r.json() if r.status_code == 200 else None

    def infer(self, adapter: str, jpeg: bytes, *, task: str,
              camera_id: str | None = None, params: dict | None = None,
              correlation_id: str | None = None) -> dict:
        """One HTTP inference call (§5.1 ``InferResponse``); raises
        ``KaiCError`` on transport failure or a non-200. Pass
        ``camera_id`` (a ``camN`` handle) whenever the frame belongs to
        a camera — it drives KAI-C's audit, budgets and bus subject."""
        return self._client_for(adapter).infer(
            jpeg, task=task,
            camera_id=None if camera_id is None else str(camera_id),
            params=params, correlation_id=correlation_id)

    def stream(self, adapter: str, *, camera_id: str, timeout: float | None = None):
        """A persistent WebSocket session for a camera (§6.1 handshake,
        per-frame ``send``): ``with nvr.ai.stream("yolov8", camera_id="cam1") as s:
        result = s.infer(jpeg)``. Needs the ``websockets`` package."""
        from .infer_stream import InferStream

        return InferStream(self._base, self._key, adapter=adapter,
                           camera_id=camera_id, client_id=self._client_id,
                           timeout=timeout or self._timeout)


# ── The client ──────────────────────────────────────────────────────


class OpenNVR:
    """See the module docstring. All arguments fall back to the
    environment the app overlays already set: ``OPENNVR_URL``,
    ``KAIC_URL`` / ``OPENNVR_KAIC_URL``, ``OPENNVR_INTERNAL_API_KEY``
    (bootstrap) and the app key from ``credentials.py``."""

    def __init__(self, url: str | None = None, *, token: str | None = None,
                 kaic_url: str | None = None, kaic_api_key: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT, client_id: str = "opennvr-app") -> None:
        base = url or os.environ.get("OPENNVR_URL") or ""
        if not base:
            raise ValueError("OpenNVR(url=...) or OPENNVR_URL is required")
        self.credentials = AppCredentials(token)
        self._http = _Http(base, self.credentials, timeout)
        kaic = kaic_url or os.environ.get("KAIC_URL") or os.environ.get("OPENNVR_KAIC_URL")
        self.ai = AIAPI(kaic, kaic_api_key or os.environ.get("KAIC_API_KEY")
                        or os.environ.get("OPENNVR_INTERNAL_API_KEY"), timeout, client_id)
        self.timeline = TimelineAPI(self._http)
        self.alerts = AlertsAPI(self._http)
        self.state = StateAPI(self._http)

    @property
    def url(self) -> str:
        return self._http.base

    # ── cameras ────────────────────────────────────────────────────

    def cameras(self) -> list[Camera]:
        """The roster core assigned to this app (every active camera
        when the operator assigned none). ``[]`` when core can't be
        reached — log it; never guess."""
        body = self._http.get_json("/api/v1/internal/camera-agent/cameras")
        out: list[Camera] = []
        for row in (body or {}).get("cameras") or []:
            if not isinstance(row, dict):
                continue
            try:
                cid = int(row.get("open_nvr_camera_id") or _camera_id(row.get("camera_id")))
            except (TypeError, ValueError):
                continue
            out.append(Camera(
                id=cid, handle=str(row.get("camera_id") or f"cam{cid}"),
                name=str(row.get("name") or f"Camera {cid}"),
                role=str(row.get("role") or ""),
                frame_url=str(row.get("frame_url") or ""),
                assignments=list(row.get("assignments") or []), raw=row))
        return out

    def camera(self, camera) -> Camera | None:
        want = _camera_id(camera)
        return next((c for c in self.cameras() if c.id == want), None)

    def snapshot(self, camera) -> bytes | None:
        """The camera's current frame as JPEG, or ``None``."""
        return self._http.get_bytes(
            f"/api/v1/internal/app/cameras/{_camera_id(camera)}/snapshot")

    def recordings(self, camera) -> RecordingsAPI:
        return RecordingsAPI(self._http, _camera_id(camera))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OpenNVR":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
