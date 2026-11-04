# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Attendance — who was in the room, from the cameras.

The app looks; core decides. Every tick it asks core which attendance
sessions are running on its cameras (``/internal/app/attendance/active``),
and for each camera that is due it takes a snapshot, runs ``face_identify``
on the attendance InsightFace through KAI-C, and publishes one
``attendance.attempt.v1`` with what it saw. Core links the attempt to its
sessions and, at the end of each, decides PRESENT / LATE / ABSENT
(server/services/attendance_scheduler.py). Nothing here keeps a roster or
a verdict, so the app can restart, or be reinstalled, without losing one.

A look that could not happen — no frame, the adapter down — is published
too, with ``ok`` false: core then leaves that slice of the session out of
everyone's ratio instead of reading an empty room.

Run: ``python attendance_app.py --config config.yml``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from attendance.core import CameraWork, Planner, attempt_payload, parse_active
from opennvr_app_sdk import AppManifest, FrameApp, Param, StateView
from opennvr_app_sdk.alerts import AlertDispatcher, StdoutChannel
from opennvr_app_sdk.client import OpenNVR
from opennvr_app_sdk.domain_events import DomainEventPublisher

logger = logging.getLogger("attendance")

APP_ID = "attendance"
SCHEMA = "attendance.attempt.v1"
#: How often the app asks core what is running. Each camera is still only
#: looked at every attempt_interval_s; this is how quickly a bell is heard.
TICK_SECONDS = 15.0

MANIFEST = AppManifest(
    id=APP_ID,
    name="Attendance",
    version="1.0.0",
    category="people",
    summary=(
        "Takes attendance from the cameras during scheduled classes or shifts: "
        "recognises enrolled people on the room's cameras and lets OpenNVR mark "
        "them present, late or absent."),
    requires_tasks=["face_identify"],
    # Lights the first-class Attendance page and lets the app publish its
    # attempts on opennvr.events.attendance.*.
    provides=["attendance"],
    subscribes=None,  # looks at frames itself, through KAI-C
    params=[
        Param("adapter", str, default="insightface_attendance",
              label="Face adapter",
              description=("The KAI-C adapter that runs face_identify for attendance. "
                           "Its own instance, so a classroom never waits on the doorbell.")),
    ],
    state_schema=[
        StateView(name="sessions_running", label="Sessions running", kind="metric",
                  path="sessions_running"),
        StateView(name="cameras_watched", label="Cameras watched", kind="metric",
                  path="cameras_watched"),
        StateView(name="attempts_published", label="Looks reported", kind="metric",
                  path="attempts_published"),
        StateView(name="looks_failed", label="Looks that got no frame", kind="metric",
                  path="looks_failed"),
        StateView(name="recent", label="Recent looks", kind="log", path="recent"),
    ],
)


@dataclass
class AppConfig:
    opennvr_url: str = ""
    kaic_url: str = ""
    kaic_api_key: str = ""
    adapter: str = "insightface_attendance"
    nats_url: str = ""
    nats_token: str | None = None
    contract_port: int | None = None
    contract_bind_host: str = "0.0.0.0"
    contract_host: str | None = None
    request_timeout_seconds: float = 30.0
    poll_interval_seconds: float = TICK_SECONDS
    #: Never read by this app (it watches what core says is running), but
    #: FrameApp's camera list defaults from it.
    cameras: tuple = ()


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    known = set(AppConfig.__dataclass_fields__)
    unknown = sorted(set(raw) - known)
    if unknown:
        logger.warning("config: ignoring unknown keys %s", unknown)
    return AppConfig(**{k: v for k, v in raw.items() if k in known})


class AttendanceApp(FrameApp):
    """A FrameApp whose camera list is not the picker's but core's: the
    cameras of sessions running now. The picker still matters — core
    answers only for the cameras picked for this app."""

    manifest = MANIFEST

    def __init__(self, config: AppConfig, *, nvr: OpenNVR | None = None,
                 events: DomainEventPublisher | None = None,
                 clock=lambda: datetime.now(UTC)) -> None:
        self._nvr = nvr
        self._events = events
        self._clock = clock
        self.planner = Planner()
        self._lock = threading.Lock()
        self._sessions: list = []
        self._recent: deque = deque(maxlen=50)
        self._published = 0
        self._failed = 0
        self._last_error: str | None = None
        super().__init__(config, AlertDispatcher([StdoutChannel()]),
                         frame_source=self, cameras=[],
                         poll_interval_seconds=float(config.poll_interval_seconds))

    # ── wiring (built on first use, so tests inject fakes) ─────────

    @property
    def nvr(self) -> OpenNVR:
        if self._nvr is None:
            self._nvr = OpenNVR(url=self.cfg.opennvr_url or None,
                                kaic_url=self.cfg.kaic_url or None,
                                kaic_api_key=self.cfg.kaic_api_key or None,
                                timeout=self.cfg.request_timeout_seconds)
        return self._nvr

    @property
    def events(self) -> DomainEventPublisher:
        if self._events is None:
            self._events = DomainEventPublisher(self.cfg.nats_url or "",
                                                token=self.cfg.nats_token,
                                                producer=f"app:{APP_ID}")
        return self._events

    def get_frame(self, camera_id: str) -> bytes | None:  # FrameSource; unused by the tick
        return self.nvr.snapshot(camera_id)

    def on_frame(self, camera_id, frame_bytes):  # pragma: no cover - the tick is overridden
        return None

    def on_cameras_update(self, camera_ids) -> None:
        # The picker bounds what core will answer for; the cameras looked
        # at come from the active sessions, not from here.
        return

    def on_config_update(self, config: dict[str, Any]) -> None:
        adapter = str((config or {}).get("adapter") or "").strip()
        if adapter:
            self.cfg.adapter = adapter

    # ── the tick ───────────────────────────────────────────────────

    def handle_tick(self) -> list:
        if self.app_enabled is False:
            return []
        # Core's own route for this app; the SDK client has no wrapper for
        # it, so it goes through the client's authenticated GET.
        body = self.nvr._http.get_json("/api/v1/internal/app/attendance/active")
        sessions = parse_active(body)
        if sessions is None:
            self._last_error = "OpenNVR did not answer which sessions are running"
            return []        # keep the planner's memory; try again next tick
        with self._lock:
            self._sessions = sessions
        now = self._clock()
        for work in self.planner.due(sessions, now):
            self.look(work, now)
        return []

    def look(self, work: CameraWork, now: datetime) -> None:
        """One camera, once: frame → face_identify → one attempt event."""
        handle = f"cam{work.camera_id}"
        self.planner.looked(work.camera_id, now)
        frame = None
        try:
            frame = self.nvr.snapshot(work.camera_id)
        except Exception as exc:
            logger.warning("snapshot %s failed: %s", handle, exc)
        # When the frame was taken, not when the tick began: looks run one
        # after another, a crowded face pass is ~10 s, and on a busy tick
        # the last camera is minutes later. Buckets and LATE use this time.
        taken = self._clock()
        if not frame:
            return self._publish(work, taken, ok=False, note="no frame")
        self._contract_note_event()
        try:
            resp = self.nvr.ai.infer(self.cfg.adapter, frame, task="face_identify",
                                     camera_id=handle,
                                     params={"threshold": work.threshold})
        except Exception as exc:
            logger.warning("face_identify on %s failed: %s", handle, exc)
            self._last_error = f"face adapter: {exc}"[:200]
            return self._publish(work, taken, ok=False, note="adapter failed")
        result = (resp or {}).get("result") or {}
        if not isinstance(result.get("faces"), list):
            self._last_error = ("the face adapter does not answer face_identify; "
                                "update it to a release that has it")
            return self._publish(work, taken, ok=False, note="adapter too old")
        evidence = None
        payload = attempt_payload(work, taken, ok=True, result=result)
        if payload["matches"] and self.planner.evidence_due(work, taken):
            try:
                evidence = self.nvr.save_evidence(frame)
            except Exception as exc:
                logger.warning("evidence upload for %s failed: %s", handle, exc)
        self._last_error = None
        self._publish(work, taken, ok=True, result=result, evidence=evidence,
                      note=f"{payload['faces_detected']} faces, {len(payload['matches'])} recognised")

    def _publish(self, work: CameraWork, now: datetime, *, ok: bool,
                 result: dict | None = None, evidence: str | None = None,
                 note: str = "") -> None:
        payload = attempt_payload(work, now, ok=ok, result=result, evidence_path=evidence)
        sent = self.events.publish(SCHEMA, camera_id=f"cam{work.camera_id}", payload=payload)
        with self._lock:
            self._published += 1 if sent else 0
            self._failed += 0 if ok else 1
            self._recent.appendleft(
                f"{now.astimezone(UTC):%H:%M:%S} cam{work.camera_id}: {note}"
                + ("" if sent else " (bus unreachable)"))

    # ── contract surface ───────────────────────────────────────────

    def state_snapshot(self) -> dict[str, Any]:
        with self._lock:
            sessions = list(self._sessions)
            return {
                "sessions_running": len(sessions),
                "cameras_watched": len({c for s in sessions for c in s.camera_ids}),
                "attempts_published": self._published,
                "looks_failed": self._failed,
                "recent": list(self._recent),
            }

    def not_ready_reason(self) -> str | None:
        return self._last_error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenNVR attendance app")
    parser.add_argument("--config", required=True)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = AttendanceApp(load_config(args.config))
    loop = asyncio.new_event_loop()

    def _stop(signum, _frame):
        logger.info("signal %s; stopping", signum)
        loop.call_soon_threadsafe(app.stop)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        loop.run_until_complete(app.run())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
