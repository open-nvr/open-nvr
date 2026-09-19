# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""What each camera sees right now (HA-110).

Fed every Tier-0 frame by ``tier0_track_consumer`` (before the overlay's
filters, and whether or not the overlay is enabled), this keeps, per camera:

* the tracks present now, with the same "present" rule the overlay uses
  (``track_is_drawable``) and the same score floor;
* counts per label, as ``total`` and ``active`` (active = not stationary: a
  parked car is present but not active), for the whole picture and per zone
  (a track is in a zone when the bottom-centre of its box is inside);
* motion: on as soon as an active track is present, off only after
  ``motion_off_after_s`` with none, so a person pausing mid-frame does not
  flap the sensor;
* only while the track consumer runs: ``DETECTION_OVERLAY_ENABLED=false``
  stops it (no track data reaches any consumer), and with it live state;
* staleness: Tier-0 publishes only frames that HAVE tracks, so an emptied
  scene simply goes quiet. A camera with no frame for ``stale_s`` reads as
  zero (``stale: true``), never as a frozen last count, and :meth:`sweep`
  ends its tracks. ``stale`` therefore means "nothing seen lately", not
  "pipeline down" (that is ``/cameras/{id}/stats``' ``detect_up``).

Pure logic, no I/O: zones are handed in by the caller, and the clock is a
parameter, so every rule is unit-testable with synthetic frames.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from services.zones import point_in_polygon

DEFAULT_STALE_S = float(os.environ.get("LIVE_STATE_STALE_S", "5") or 5)
DEFAULT_MOTION_OFF_S = float(os.environ.get("LIVE_STATE_MOTION_OFF_S", "10") or 10)
MIN_SCORE = 0.25


@dataclass
class _Track:
    label: str
    stationary: bool
    zones: frozenset[int]
    first_seen: float
    last_seen: float


@dataclass
class _Camera:
    last_frame: float = 0.0
    tracks: dict[str, _Track] = field(default_factory=dict)
    last_active: float = 0.0
    #: (zone ids, labels) per zone, as given by the caller.
    zones: dict[int, tuple[str, list[list[float]], frozenset[str] | None]] = field(
        default_factory=dict)


def _foot(box: Any, fw: Any, fh: Any) -> tuple[float, float] | None:
    try:
        x1, y1, x2, y2 = (float(v) for v in box)
        w, h = float(fw), float(fh)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (min(1.0, max(0.0, (x1 + x2) / 2 / w)), min(1.0, max(0.0, y2 / h)))


class LiveState:
    def __init__(self, *, stale_s: float = DEFAULT_STALE_S,
                 motion_off_after_s: float = DEFAULT_MOTION_OFF_S) -> None:
        self.stale_s = stale_s
        self.motion_off_after_s = motion_off_after_s
        self._cams: dict[int, _Camera] = {}
        self._lock = threading.Lock()

    # ── inputs ──────────────────────────────────────────────────────────

    def set_zones(self, camera_id: int, zones: list[Any]) -> None:
        """Zones for a camera: objects with id, name, polygon, labels."""
        with self._lock:
            cam = self._cams.setdefault(camera_id, _Camera())
            cam.zones = {
                int(z.id): (z.name, list(z.polygon or []),
                            frozenset(s.lower() for s in z.labels) if z.labels else None)
                for z in zones
            }

    def update(self, camera_id: int, raw: dict[str, Any], now: float | None = None,
               ) -> dict[str, Any]:
        """Apply one Tier-0 frame. Returns what changed:
        ``{"started": [...], "ended": [...], "changed": bool}``."""
        from services.tier0_track_consumer import track_is_drawable

        now = time.time() if now is None else now
        frame = raw.get("frame") or {}
        fw, fh = frame.get("w"), frame.get("h")
        with self._lock:
            cam = self._cams.setdefault(camera_id, _Camera())
            before = self._counts(cam, now)
            was_motion = self._motion(cam, now)
            if now - cam.last_frame > self.stale_s:
                # Frames resumed after a gap: whatever was there then is
                # not evidence of anything now.
                cam.tracks.clear()
            cam.last_frame = now
            seen: dict[str, _Track] = {}
            for t in raw.get("tracks") or []:
                if not isinstance(t, dict) or t.get("id") is None:
                    continue
                try:
                    score = float(t.get("score", 0.0))
                except (TypeError, ValueError):
                    continue
                if score < MIN_SCORE or not track_is_drawable(t):
                    continue
                tid = str(t["id"])
                label = str(t.get("label") or "object").lower()
                foot = _foot(t.get("box"), fw, fh)
                zones = frozenset(
                    zid for zid, (_n, poly, labels) in cam.zones.items()
                    if foot is not None and (labels is None or label in labels)
                    and point_in_polygon(foot[0], foot[1], poly)
                )
                prev = cam.tracks.get(tid)
                seen[tid] = _Track(label, bool(t.get("stationary", False)), zones,
                                   prev.first_seen if prev else now, now)
            started = [{"track_id": k, "label": v.label, "zones": sorted(v.zones)}
                       for k, v in seen.items() if k not in cam.tracks]
            ended = [{"track_id": k, "label": v.label, "zones": sorted(v.zones),
                      "duration_s": round(v.last_seen - v.first_seen, 1)}
                     for k, v in cam.tracks.items() if k not in seen]
            cam.tracks = seen
            if any(not tr.stationary for tr in seen.values()):
                cam.last_active = now
            after = self._counts(cam, now)
            changed = after != before or self._motion(cam, now) != was_motion
            return {"started": started, "ended": ended, "changed": changed}

    # ── outputs ─────────────────────────────────────────────────────────

    def _stale(self, cam: _Camera, now: float) -> bool:
        return now - cam.last_frame > self.stale_s

    def _motion(self, cam: _Camera, now: float) -> bool:
        """On while an active track was seen within the off-window."""
        return cam.last_active > 0 and now - cam.last_active <= self.motion_off_after_s

    def _counts(self, cam: _Camera, now: float) -> dict[str, Any]:
        objects: dict[str, dict[str, int]] = {}
        zones: dict[int, dict[str, dict[str, int]]] = {zid: {} for zid in cam.zones}
        if not self._stale(cam, now):
            for tr in cam.tracks.values():
                for bucket in [objects] + [zones[z] for z in tr.zones if z in zones]:
                    c = bucket.setdefault(tr.label, {"total": 0, "active": 0})
                    c["total"] += 1
                    c["active"] += 0 if tr.stationary else 1
        return {"objects": objects, "zones": zones}

    def camera(self, camera_id: int, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            cam = self._cams.get(camera_id) or _Camera()
            counts = self._counts(cam, now)
            return {
                "camera_id": camera_id,
                "stale": self._stale(cam, now),
                "last_frame_at": cam.last_frame or None,
                "motion": self._motion(cam, now),
                "objects": counts["objects"],
                "zones": [
                    {"zone_id": zid, "name": cam.zones[zid][0], "objects": objs}
                    for zid, objs in sorted(counts["zones"].items())
                ],
            }

    def sweep(self, now: float | None = None) -> list[tuple[int, list[dict[str, Any]]]]:
        """End the tracks of cameras that went quiet. Returns
        ``[(camera_id, ended)]`` for each camera that changed, so the caller
        can publish the drop to zero."""
        now = time.time() if now is None else now
        out = []
        with self._lock:
            for cid, cam in self._cams.items():
                if cam.tracks and self._stale(cam, now):
                    ended = [{"track_id": k, "label": v.label, "zones": sorted(v.zones),
                              "duration_s": round(v.last_seen - v.first_seen, 1)}
                             for k, v in cam.tracks.items()]
                    cam.tracks = {}
                    out.append((cid, ended))
        return out

    def camera_ids(self) -> list[int]:
        with self._lock:
            return sorted(self._cams)

    def reset(self) -> None:
        with self._lock:
            self._cams.clear()


_instance: LiveState | None = None
_instance_lock = threading.Lock()


def get_live_state() -> LiveState:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = LiveState()
    return _instance


# ── zones cache (the consumer runs on every frame; zones change rarely) ────

_ZONES_TTL_S = 10.0
_zones_loaded: dict[int, float] = {}


def refresh_zones_if_due(camera_id: int, now: float | None = None) -> None:
    """Load this camera's zones into the live state at most every 10 s."""
    now = time.monotonic() if now is None else now
    if now - _zones_loaded.get(camera_id, -1e9) < _ZONES_TTL_S:
        return
    _zones_loaded[camera_id] = now
    try:
        from core.database import SessionLocal
        from models import CameraZone

        with SessionLocal() as db:
            zones = db.query(CameraZone).filter(CameraZone.camera_id == camera_id).all()
            get_live_state().set_zones(camera_id, zones)
    except Exception:  # noqa: BLE001 - counts without zones beat no counts
        pass


def invalidate_zones(camera_id: int | None = None) -> None:
    """Zone CRUD calls this so a new zone counts within a frame, not 10 s."""
    if camera_id is None:
        _zones_loaded.clear()
    else:
        _zones_loaded.pop(camera_id, None)
