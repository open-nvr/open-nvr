# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The attendance app's decisions, with no platform in sight.

Everything here is a pure function of what core said (the active
sessions), what the face adapter said (one face_identify result) and the
clock, so it is tested with a synthetic clock and dicts — no camera, no
model, no core (tests/test_core.py).

The app's whole job: for every camera of a running session, look at it
every ``attempt_interval_s``, and report what it saw. Work is keyed by
CAMERA, not by session: two groups in one room are one frame, one face
pass, one attempt. Core decides which sessions that attempt serves and
which matches count; the app only avoids sending what cannot count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class ActiveSession:
    session_id: int
    camera_ids: tuple[int, ...]
    person_ids: frozenset[str]
    start_at: datetime
    end_at: datetime
    attempt_interval_s: int
    bucket_minutes: int
    match_threshold: float


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_active(body: Any) -> list[ActiveSession] | None:
    """Core's ``/internal/app/attendance/active`` answer. ``None`` when
    the body is not an answer at all (core unreachable or off-contract):
    keep doing what you were doing. ``[]`` is an answer — nothing runs."""
    if not isinstance(body, dict) or not isinstance(body.get("sessions"), list):
        return None
    out: list[ActiveSession] = []
    for s in body["sessions"]:
        if not isinstance(s, dict):
            continue
        start, end = _when(s.get("start_at")), _when(s.get("end_at"))
        cams = tuple(int(c) for c in s.get("camera_ids") or [] if isinstance(c, int))
        if start is None or end is None or not cams:
            continue
        try:
            out.append(ActiveSession(
                session_id=int(s["session_id"]), camera_ids=cams,
                person_ids=frozenset(str(p) for p in s.get("person_ids") or []),
                start_at=start, end_at=end,
                attempt_interval_s=max(10, int(s.get("attempt_interval_s") or 150)),
                bucket_minutes=max(1, int(s.get("bucket_minutes") or 5)),
                match_threshold=float(s.get("match_threshold") or 0.5)))
        except (KeyError, TypeError, ValueError):
            continue
    return out


@dataclass(frozen=True)
class CameraWork:
    """One camera to look at now, and the sessions that look is for."""
    camera_id: int
    sessions: tuple[ActiveSession, ...]

    @property
    def interval_s(self) -> int:
        return min(s.attempt_interval_s for s in self.sessions)

    @property
    def threshold(self) -> float:
        # The most lenient of the groups' thresholds: core applies each
        # group's own; sending less than that would lose a real sighting.
        return min(s.match_threshold for s in self.sessions)

    @property
    def members(self) -> frozenset[str]:
        out: set[str] = set()
        for s in self.sessions:
            out |= s.person_ids
        return frozenset(out)


@dataclass
class Planner:
    """When each camera was last looked at, and when its evidence was
    last attached. Lives as long as the app process: a restart costs at
    most one early look per camera, never a missed one."""

    last_attempt: dict[int, datetime] = field(default_factory=dict)
    last_evidence_bucket: dict[int, tuple[int, int]] = field(default_factory=dict)

    def work(self, sessions: list[ActiveSession], now: datetime) -> list[CameraWork]:
        """Cameras of running sessions, grouped: one entry per camera."""
        by_camera: dict[int, list[ActiveSession]] = {}
        for s in sessions:
            if not (s.start_at <= now < s.end_at):
                continue
            for cam in s.camera_ids:
                by_camera.setdefault(cam, []).append(s)
        return [CameraWork(cam, tuple(sorted(ss, key=lambda s: s.session_id)))
                for cam, ss in sorted(by_camera.items())]

    def due(self, sessions: list[ActiveSession], now: datetime) -> list[CameraWork]:
        """The cameras whose interval has passed (or that were never looked
        at). Forgets cameras no session wants any more."""
        work = self.work(sessions, now)
        wanted = {w.camera_id for w in work}
        for cam in list(self.last_attempt):
            if cam not in wanted:
                del self.last_attempt[cam]
        out = []
        for w in work:
            last = self.last_attempt.get(w.camera_id)
            if last is None or (now - last).total_seconds() >= w.interval_s:
                out.append(w)
        return out

    def looked(self, camera_id: int, at: datetime) -> None:
        self.last_attempt[camera_id] = at

    def evidence_due(self, w: CameraWork, at: datetime) -> bool:
        """At most one evidence frame per camera per bucket: frames of a
        classroom are full of faces and are kept for weeks, so keep only
        what a person checking a mark needs. Keyed on the earliest session
        of the look, whose bucket grid the frame is judged on."""
        first = min(w.sessions, key=lambda s: s.start_at)
        bucket = int((at - first.start_at).total_seconds() // (first.bucket_minutes * 60))
        key = (first.session_id, bucket)
        if self.last_evidence_bucket.get(w.camera_id) == key:
            return False
        self.last_evidence_bucket[w.camera_id] = key
        return True


def matches_from(result: dict[str, Any], w: CameraWork) -> list[dict[str, Any]]:
    """The recognised faces worth reporting for this look: members of one
    of its sessions' groups, at or above the most lenient threshold,
    best score per person. Strangers and non-members are not sent — core
    would drop them, and they are faces of people who did not consent to
    attendance here."""
    members, threshold = w.members, w.threshold
    best: dict[str, dict[str, Any]] = {}
    for face in result.get("faces") or []:
        if not isinstance(face, dict) or not face.get("recognized"):
            continue
        pid, sim = face.get("person_id"), face.get("similarity")
        if pid not in members or not isinstance(sim, (int, float)) or sim < threshold:
            continue
        if pid not in best or sim > best[pid]["similarity"]:
            best[pid] = {"person_id": pid, "similarity": round(float(sim), 4),
                         "bbox": face.get("bbox")}
    return sorted(best.values(), key=lambda m: m["person_id"])


def attempt_payload(w: CameraWork, at: datetime, *, ok: bool,
                    result: dict[str, Any] | None = None,
                    evidence_path: str | None = None) -> dict[str, Any]:
    """The ``attendance.attempt.v1`` payload (docs/EVENT_CONTRACTS.md)."""
    result = result or {}
    payload: dict[str, Any] = {
        "at": at.astimezone(UTC).isoformat(),
        "ok": ok,
        "faces_detected": int(result.get("detected_count") or result.get("face_count") or 0)
        if ok else 0,
        "matches": matches_from(result, w) if ok else [],
        "session_ids": [s.session_id for s in w.sessions],
    }
    if evidence_path:
        payload["evidence_path"] = evidence_path
    return payload
