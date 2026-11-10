# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The host agent: OpenNVR's one long-running process on the host machine.

Docker Desktop cannot see USB webcams, and a container cannot measure the
machine it runs on (on Windows/macOS it sees Docker's VM). The host agent
(``host-agent/``, Go, installed by start.ps1 / start.sh) does both from the
host, and talks to core in one direction only: every few seconds it POSTs
``/api/v1/host-agent/sync`` with what it sees (webcams, ffmpeg, metrics, the
state of each webcam it streams) and gets back what it should do — which
webcam feeds which camera. Core never sends it a command or a string to run;
the agent acts only on devices it enumerated itself.

Credential: one key per site. ``start`` writes the key where only the host
can read it (``data/host-agent-key/``) and its sha256 to
``data/host-agent/agent.key.sha256``, which is mounted read-only here. The
routes also refuse anything that came through the reverse proxy: the agent
calls core directly on 127.0.0.1, so the key is useless from the LAN even if
it leaks.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from core.camera_source import PUBLISH_TOKEN_MINUTES, SOURCE_PUBLISHER
from core.config import settings
from models import Camera

KEY_HASH_FILE_ENV = "OPENNVR_HOST_AGENT_KEY_HASH_FILE"
DEFAULT_KEY_HASH_FILE = "/app/host-agent/agent.key.sha256"
REPORT_SETTING_KEY = "host_agent_report"

#: How often the agent should call; the response says so, so it can change
#: without an agent release.
POLL_AFTER_S = 5
#: Missed polls before the agent counts as gone.
CONNECTED_WITHIN_S = 30
#: Metrics older than this are not shown as the host's.
METRICS_FRESH_S = 45

#: Default capture for a webcam the agent streams. 25 fps, like a typical IP
#: camera: at 15 a webcam looked visibly choppier than the cameras next to it
#: in Live View. 720p keeps the encode (ultrafast) cheap on the host.
DEFAULT_WIDTH, DEFAULT_HEIGHT, DEFAULT_FPS = 1280, 720, 25


# ── Key ────────────────────────────────────────────────────────────────


class _KeyHash:
    """The stored key hash, re-read when the file changes (a rotation by
    ``start`` takes effect without restarting core)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._hash: str | None = None

    def get(self) -> str | None:
        path = os.environ.get(KEY_HASH_FILE_ENV) or DEFAULT_KEY_HASH_FILE
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            with self._lock:
                self._mtime, self._hash = None, None
            return None
        with self._lock:
            if mtime != self._mtime:
                try:
                    with open(path, encoding="utf-8") as fh:
                        # `sha256sum` format ("<hex>  file") is fine too.
                        text = (fh.read().split() or [""])[0]
                except OSError:
                    text = ""
                self._hash = text.lower() if len(text) == 64 else None
                self._mtime = mtime
            return self._hash


_key_hash = _KeyHash()


def verify_key(presented: str | None) -> bool:
    """Constant-time check of a presented agent key. False when no key has
    been set up on this site."""
    stored = _key_hash.get()
    if not presented or not stored:
        return False
    digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, stored)


def came_through_proxy(headers: Any) -> bool:
    """True when the request was relayed by nginx (or any proxy that says
    so). nginx sets these on every request it forwards to core
    (nginx/opennvr.conf), and a client cannot remove them; the host agent
    calls core directly and sends none."""
    return any(headers.get(h) for h in ("x-forwarded-for", "x-real-ip", "forwarded"))


# ── Reported state ─────────────────────────────────────────────────────


class _AgentState:
    """The agent's last report, in memory. One agent per site."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.report: dict[str, Any] | None = None
        self.last_seen: float | None = None  # time.time()
        self._persisted: dict[str, Any] | None = None

    def update(self, report: dict[str, Any]) -> bool:
        """Store a report; True when the agent was not connected before."""
        with self._lock:
            was = self._connected_locked()
            self.report = report
            self.last_seen = time.time()
            return not was

    def _connected_locked(self) -> bool:
        return self.last_seen is not None and time.time() - self.last_seen < CONNECTED_WITHIN_S

    def connected(self) -> bool:
        with self._lock:
            return self._connected_locked()

    def snapshot(self) -> tuple[dict[str, Any] | None, float | None]:
        with self._lock:
            return self.report, self.last_seen

    def reset(self) -> None:  # tests
        with self._lock:
            self.report, self.last_seen, self._persisted = None, None, None


state = _AgentState()


#: Cap on remembered webcam names (see _persistable).
_MAX_KNOWN_NAMES = 200


def _persistable(report: dict[str, Any], known: dict[str, str]) -> dict[str, Any]:
    """What survives a core restart: who the agent is and which webcams it
    saw, so the Add Camera dialog has a list before the next poll. Never the
    metrics or stream states, which change every poll.

    ``known_names`` remembers the name of every webcam id ever seen, because
    the id of a webcam moved to another USB port changes and only the name
    lets the agent find it again (desired_state's ``device_name``)."""
    for w in report.get("webcams") or []:
        if isinstance(w, dict) and w.get("id"):
            known[w["id"]] = w.get("name") or ""
    while len(known) > _MAX_KNOWN_NAMES:
        known.pop(next(iter(known)))
    return {
        "version": report.get("version"),
        "os": report.get("os"),
        "arch": report.get("arch"),
        "hostname": report.get("hostname"),
        "ffmpeg": report.get("ffmpeg"),
        "webcams": report.get("webcams") or [],
        "known_names": dict(known),
    }


def record_report(db: Session, report: dict[str, Any]) -> bool:
    """Store a sync report. Persists the slow-changing part only when it
    changed (a 5-second poll must not be a DB write every 5 seconds).
    Returns True when the agent (re)connected with this report."""
    reconnected = state.update(report)
    if state._persisted is None:
        stored = _stored_report(db) or {}
        state._persisted = stored
    known = dict((state._persisted or {}).get("known_names") or {})
    slow = _persistable(report, known)
    if slow != state._persisted:
        from services.site_settings import set_json

        set_json(db, REPORT_SETTING_KEY, slow)
        state._persisted = slow
    return reconnected


def _stored_report(db: Session) -> dict[str, Any] | None:
    from services.site_settings import get_json

    value = get_json(db, REPORT_SETTING_KEY)
    return value if isinstance(value, dict) else None


# ── Desired state ──────────────────────────────────────────────────────


def _host_webcam_query(db: Session):
    return (
        db.query(Camera)
        .filter(Camera.source_type == SOURCE_PUBLISHER)
        .filter(Camera.source_device.isnot(None))
        .filter(Camera.source_device != "")
        .filter(Camera.is_active == True)  # noqa: E712 - SQLAlchemy expression
        .filter(Camera.deleted_at.is_(None))
    )


def _webcam_names(db: Session) -> dict[str, str]:
    stored = state._persisted if state._persisted is not None else (_stored_report(db) or {})
    names = dict(stored.get("known_names") or {})
    report, _ = state.snapshot()
    for w in (report or {}).get("webcams") or []:
        if isinstance(w, dict) and w.get("id"):
            names[w["id"]] = w.get("name") or ""
    return names


def desired_state(db: Session) -> list[dict[str, Any]]:
    """Which webcam feeds which camera — everything the agent may do.

    ``device_name`` lets the agent find the same webcam after it moved to
    another USB port (its id includes the port); it only uses it when
    exactly one present webcam has that name.
    """
    names = _webcam_names(db)
    return [
        {
            "camera_id": cam.id,
            "device_id": cam.source_device,
            "device_name": names.get(cam.source_device, ""),
            "width": DEFAULT_WIDTH,
            "height": DEFAULT_HEIGHT,
            "fps": DEFAULT_FPS,
        }
        for cam in _host_webcam_query(db).order_by(Camera.id.asc()).all()
    ]


def camera_is_desired(db: Session, camera_id: int) -> Camera | None:
    return _host_webcam_query(db).filter(Camera.id == camera_id).first()


def publish_token(camera: Camera) -> tuple[str, str]:
    """A short publish-only token for one webcam camera's path: ``(path,
    token)``. Shared by the browser publisher (WHIP) and the host agent."""
    from services.mediamtx_jwt_service import MediaMtxJwtService
    from services.stream_service import _build_stream_name

    stream_name = _build_stream_name(
        settings.mediamtx_stream_prefix, camera.id, camera.ip_address
    )
    token = MediaMtxJwtService.create_publish_token(
        camera_id=camera.id,
        camera_path=stream_name,
        expiry_minutes=PUBLISH_TOKEN_MINUTES,
    )
    return stream_name, token


def core_build() -> str:
    """What the agent compares its own build with: version plus commit. An
    agent built from another core exits and is restarted on the binary
    ``start`` installed next to it."""
    import sys

    # Read from the already-running app, never by importing ``main``: that
    # import builds the whole application (services.version does it), which
    # in a test process re-wires shared modules under every later test.
    # supervisord runs ``uvicorn server.main:app``; tests and dev run ``main``.
    app = sys.modules.get("server.main") or sys.modules.get("main")
    version = str(getattr(app, "__version__", "") or "unknown")
    commit = (os.environ.get("OPENNVR_BUILD_COMMIT") or "").strip()
    return f"{version}+{commit}" if commit else version


# ── For the web app and the monitor ────────────────────────────────────


def public_view(db: Session) -> dict[str, Any]:
    """The agent as the web app shows it: connected, which webcams it sees
    and which camera each one feeds, how each stream is doing."""
    report, last_seen = state.snapshot()
    connected = state.connected()
    base = report or _stored_report(db) or {}
    assigned = {
        cam.source_device: {"camera_id": cam.id, "camera_name": cam.name}
        for cam in (
            db.query(Camera)
            .filter(Camera.source_type == SOURCE_PUBLISHER)
            .filter(Camera.source_device.isnot(None))
            .filter(Camera.deleted_at.is_(None))
            .all()
        )
    }
    streams = {
        int(p.get("camera_id")): p
        for p in ((report or {}).get("publishers") or [])
        if isinstance(p, dict) and p.get("camera_id") is not None
    } if connected else {}
    webcams = []
    for w in base.get("webcams") or []:
        if not isinstance(w, dict):
            continue
        a = assigned.get(w.get("id"))
        webcams.append({
            "id": w.get("id"),
            "name": w.get("name"),
            "assigned_camera_id": a["camera_id"] if a else None,
            "assigned_camera_name": a["camera_name"] if a else None,
        })
    return {
        "installed": _key_hash.get() is not None,
        "connected": connected,
        "last_seen": last_seen,
        "version": base.get("version"),
        "os": base.get("os"),
        "arch": base.get("arch"),
        "hostname": base.get("hostname"),
        "ffmpeg": base.get("ffmpeg") or {"found": False},
        "webcams": webcams,
        "streams": list(streams.values()),
    }


def fresh_metrics() -> dict[str, Any] | None:
    """The host's own metrics, if the agent reported them recently."""
    report, last_seen = state.snapshot()
    if not report or last_seen is None or time.time() - last_seen > METRICS_FRESH_S:
        return None
    metrics = report.get("metrics")
    return metrics if isinstance(metrics, dict) else None
