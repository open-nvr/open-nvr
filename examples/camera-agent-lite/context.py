# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Camera roster + frame cache — the single seam between the agent and cameras.

Two ways to get cameras (same as the camera-agent example):

* ``opennvr_cameras_url`` + ``opennvr_api_key`` — auto-discover from a running
  OpenNVR: ``GET /api/v1/internal/camera-agent/cameras`` with the
  ``X-Internal-Api-Key`` header. OpenNVR owns the camera connection and returns
  per-camera MediaMTX tap URLs with a signed ``?jwt=`` token embedded, so the
  agent needs **no user login, no password, no refresh token** — just the same
  INTERNAL_API_KEY the rest of the stack shares.
* a static ``cameras:`` list in the config, each with a ``frame_url``
  (file:// for tests, http(s):// snapshot, rtsp://).

Frames are fetched on demand through :mod:`frame_sources` and cached briefly
(``frame_cache_ttl_seconds``) because one LLM turn often looks at the same
camera more than once.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx

from frame_sources import FrameSourceError, build_frame_source

import logging

logger = logging.getLogger(__name__)

_NUM_RE = re.compile(r"(\d+)")


class CameraState(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class CameraInfo:
    camera_id: str                 # friendly id used across the agent, e.g. "camera_2"
    name: str
    state: CameraState = CameraState.UNKNOWN
    recording: bool = True         # OpenNVR records 24/7 (mandatory)
    role: str = ""
    frame_url: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class Frame:
    """A frame held as JPEG bytes — the source already encoded it, so the
    agent never touches raw video."""

    camera_id: str
    jpeg: bytes
    timestamp: float               # monotonic seconds when fetched


class CameraContextError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


def _friendly_id(nvr_id: str | int) -> str:
    return f"camera_{nvr_id}"


def _squash(s: str) -> str:
    """Lower-case and drop every non-alphanumeric ('CP Plus' -> 'cpplus')."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class CameraContext:
    """Owns the camera roster and produces frames for the vision tool."""

    def __init__(
        self,
        *,
        cameras: Optional[list[dict]] = None,
        opennvr_cameras_url: str = "",
        opennvr_api_key: str = "",
        frame_cache_ttl_seconds: float = 2.0,
        roster_ttl_seconds: float = 900.0,
        request_timeout_seconds: float = 8.0,
    ) -> None:
        self._static_cameras = cameras or []
        self._roster_url = opennvr_cameras_url
        self._api_key = opennvr_api_key
        self._frame_ttl = frame_cache_ttl_seconds
        self._roster_ttl = roster_ttl_seconds
        self._timeout = request_timeout_seconds

        self._cams: dict[str, CameraInfo] = {}
        self._sources: dict[str, object] = {}       # camera_id -> FrameSource
        self._frame_cache: dict[str, Frame] = {}
        self._roster_at: float = 0.0                # monotonic time of last refresh
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout), trust_env=False
        )

    # ---- roster ---------------------------------------------------------- #
    def _load_static(self) -> None:
        cams: dict[str, CameraInfo] = {}
        for row in self._static_cameras:
            cid = str(row.get("camera_id") or "").strip()
            url = str(row.get("frame_url") or "").strip()
            if not cid:
                continue
            cams[cid] = CameraInfo(
                camera_id=cid,
                name=str(row.get("name") or cid),
                state=CameraState.ONLINE,
                role=str(row.get("role") or ""),
                frame_url=url,
            )
        self._cams = cams
        self._rebuild_sources()

    async def _load_roster(self) -> None:
        resp = await self._client.get(
            self._roster_url, headers={"X-Internal-Api-Key": self._api_key}
        )
        if resp.status_code == 401:
            raise CameraContextError(
                "OpenNVR rejected the internal API key (check opennvr_api_key "
                "matches INTERNAL_API_KEY in OpenNVR's .env)",
                transient=False,
            )
        if resp.status_code != 200:
            raise CameraContextError(f"camera roster HTTP {resp.status_code}")
        rows = resp.json().get("cameras", [])
        cams: dict[str, CameraInfo] = {}
        for row in rows:
            nvr_id = str(row.get("open_nvr_camera_id") or "").strip()
            if not nvr_id:
                m = _NUM_RE.search(str(row.get("camera_id") or ""))
                nvr_id = m.group(1) if m else ""
            if not nvr_id:
                continue
            cid = _friendly_id(nvr_id)
            cams[cid] = CameraInfo(
                camera_id=cid,
                name=str(row.get("name") or cid),
                state=CameraState.ONLINE,   # the endpoint only lists active cameras
                role=str(row.get("role") or ""),
                frame_url=str(row.get("frame_url") or ""),
            )
        self._cams = cams
        self._rebuild_sources()

    def _rebuild_sources(self) -> None:
        self._sources = {}
        for cid, cam in self._cams.items():
            if not cam.frame_url:
                continue
            try:
                self._sources[cid] = build_frame_source(
                    camera_id=cid, url=cam.frame_url
                )
            except FrameSourceError as exc:
                logger.warning("camera %s: unusable frame_url (%s)", cid, exc)

    async def refresh(self, *, force: bool = False) -> None:
        """(Re)load the roster. MediaMTX tap URLs carry a ~60-minute JWT, so a
        periodic refresh (or a forced one after a fetch failure) keeps them
        valid without any credential handling on our side."""
        async with self._lock:
            if not force and self._cams and (
                time.monotonic() - self._roster_at < self._roster_ttl
            ):
                return
            if self._static_cameras:
                self._load_static()
            elif self._roster_url:
                await self._load_roster()
            else:
                raise CameraContextError(
                    "no cameras configured: set opennvr_cameras_url (+ "
                    "opennvr_api_key) or a static cameras: list",
                    transient=False,
                )
            self._roster_at = time.monotonic()

    # ---- queries --------------------------------------------------------- #
    async def list_cameras(self) -> list[CameraInfo]:
        await self.refresh()
        return list(self._cams.values())

    def resolve_id(self, ref: Optional[str]) -> Optional[str]:
        """Resolve any reasonable camera reference to a canonical id.

        Small LLMs routinely pass the camera NAME ('cpplus') or a variant id
        ('cam2', '2') instead of the canonical 'camera_2', so be liberal:
        exact id > 'camN'/'camera N'/bare number > exact name >
        unique substring of a name (all case-insensitive)."""
        if not ref:
            return None
        ref_s = str(ref).strip()
        if ref_s in self._cams:
            return ref_s
        low = ref_s.lower()
        if re.fullmatch(r"(?:cam(?:era)?[\s_-]*)?(\d+)", low):
            cid = _friendly_id(int(_NUM_RE.search(low).group(1)))
            if cid in self._cams:
                return cid
        # STT and LLMs mangle separators ('CP Plus', 'cp-plus', 'CPPlus' for a
        # camera named 'cpplus'), so compare with spaces/punctuation stripped.
        nref = _squash(low)
        exact = [
            c.camera_id for c in self._cams.values()
            if _squash(c.name) == nref or _squash(c.camera_id) == nref
        ]
        if len(exact) == 1:
            return exact[0]
        subs = [
            c.camera_id for c in self._cams.values()
            if len(nref) >= 3 and (nref in _squash(c.name) or _squash(c.name) in nref)
        ]
        if len(subs) == 1:
            return subs[0]
        return None

    def _unknown(self, ref: str) -> CameraContextError:
        # List what IS available so a tool-calling LLM can self-correct.
        known = ", ".join(
            f"{c.camera_id} ({c.name})" for c in self._cams.values()
        ) or "none"
        return CameraContextError(
            f"unknown camera '{ref}'; available cameras: {known}", transient=False
        )

    async def get_status(self, camera_id: str) -> CameraInfo:
        await self.refresh()
        cid = self.resolve_id(camera_id)
        if cid is None:
            raise self._unknown(camera_id)
        return self._cams[cid]

    def known_ids(self) -> list[str]:
        return list(self._cams)

    def known_names(self) -> dict[str, str]:
        """Lower-cased camera name -> canonical id (for the router)."""
        return {c.name.lower(): c.camera_id for c in self._cams.values() if c.name}

    def default_camera(self) -> Optional[str]:
        online = [c.camera_id for c in self._cams.values() if c.state == CameraState.ONLINE]
        return online[0] if len(online) == 1 else None

    # ---- frames ---------------------------------------------------------- #
    async def get_frame(self, camera_id: str) -> Frame:
        await self.refresh()
        resolved = self.resolve_id(camera_id)
        if resolved is None:
            raise self._unknown(camera_id)
        camera_id = resolved
        cached = self._frame_cache.get(camera_id)
        if cached and (time.monotonic() - cached.timestamp) < self._frame_ttl:
            return cached
        source = self._sources.get(camera_id)
        if source is None:
            raise CameraContextError(f"camera '{camera_id}' has no frame source")
        try:
            jpeg = await asyncio.to_thread(source.fetch)
        except FrameSourceError:
            # The tap JWT may have expired — refresh the roster once and retry.
            await self.refresh(force=True)
            source = self._sources.get(camera_id)
            if source is None:
                raise CameraContextError(f"camera '{camera_id}' has no frame source")
            try:
                jpeg = await asyncio.to_thread(source.fetch)
            except FrameSourceError as exc:
                raise CameraContextError(str(exc)) from exc
        frame = Frame(camera_id=camera_id, jpeg=jpeg, timestamp=time.monotonic())
        self._frame_cache[camera_id] = frame
        return frame

    async def get_frames(
        self, camera_id: str, count: int, interval_ms: int
    ) -> list[Frame]:
        """Sample the live view ``count`` times ~``interval_ms`` apart —
        best-effort coverage for temporal questions (there is no
        historical-snapshot API)."""
        await self.refresh()
        camera_id = self.resolve_id(camera_id) or camera_id
        frames: list[Frame] = []
        for i in range(max(1, count)):
            self._frame_cache.pop(camera_id, None)  # force a fresh fetch each sample
            try:
                frames.append(await self.get_frame(camera_id))
            except CameraContextError:
                break
            if i < count - 1:
                await asyncio.sleep(interval_ms / 1000.0)
        return frames

    async def aclose(self) -> None:
        await self._client.aclose()
