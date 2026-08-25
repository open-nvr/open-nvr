# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Camera discovery via OpenNVR's existing internal endpoint.

Reuses opennvr-core's ``GET /api/v1/internal/camera-agent/cameras`` — the same
internal, ``X-Internal-Api-Key``-authenticated endpoint the camera-agent uses. It
already resolves each active camera to a pullable ``frame_url`` (the MediaMTX tap
with a signed JWT — i.e. OpenNVR keeps ownership of the single camera connection —
or the stored RTSP URL as a fallback). So the Tier-0 service needs **no new
server endpoint**: it consumes the exact frame source OpenNVR already exposes.

Stdlib-only (urllib); the opener is injectable for tests. Discovery failure
returns ``None`` — distinct from ``[]`` (genuinely no cameras) — so the
manager keeps its current workers and retries next tick.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

from .service import CameraSpec

log = logging.getLogger("detect_pipeline.providers")

DEFAULT_PATH = "/api/v1/internal/camera-agent/cameras"


class HttpCameraProvider:
    """Reads active cameras (as frame sources) from opennvr-core."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        path: str = DEFAULT_PATH,
        opener=None,
        timeout: float = 10.0,
        hwaccel: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.path = path
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        # Our RESOLVED decode backend, sent with every discovery request. Core
        # picks the tap stream from it: full-resolution main only when the
        # reader can genuinely hardware-decode. Must be the effective value
        # (post resolve_hwaccel), never the configured one — reporting an
        # intent we cannot honour is the whole bug this closes.
        self.hwaccel = hwaccel
        # Rows already reported as unusable — warn once, not every tick.
        self._warned_bad_rows: set = set()

    def list_cameras(self) -> list[CameraSpec] | None:
        req = urllib.request.Request(f"{self.base_url}{self.path}")
        if self.api_key:
            req.add_header("X-Internal-Api-Key", self.api_key)
        if self.hwaccel:
            req.add_header("X-Detect-Hwaccel", self.hwaccel)
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            log.warning("camera discovery failed at %s%s", self.base_url, self.path, exc_info=True)
            return None
        # Parse per ROW. A single malformed camera — frame_url absent because
        # MediaMTX has not published that path yet, a null fps, a row caught
        # mid-provisioning — used to raise inside the comprehension and make
        # the whole call return None. reconcile reads None as "discovery
        # failed, keep the current workers and come back later", so it also
        # stopped refreshing _latest_url: every tap JWT then aged out and, an
        # hour later, the ENTIRE fleet was dark with nothing in the log but a
        # repeating warning. One bad row must cost one camera, not all of them.
        specs: list[CameraSpec] = []
        for c in data.get("cameras", []) or []:
            try:
                specs.append(_to_spec(c))
            except Exception:
                cid = c.get("camera_id") if isinstance(c, dict) else None
                if cid not in self._warned_bad_rows:
                    self._warned_bad_rows.add(cid)
                    log.warning(
                        "skipping unusable camera row %r from discovery; the rest "
                        "of the fleet is unaffected", cid, exc_info=True,
                    )
        return specs


def _default_fps() -> int:
    """Per-camera analysis rate: DETECT_FPS env, else 2.

    Detection currently runs on EVERY analyzed frame (the gate skips
    alarms, not inference), so this is the single biggest CPU dial the
    pipeline has. The default is the CPU-friendly 2: it behaves well on
    laptops and ANY macOS/Windows Docker install (the VM has no GPU),
    and pipeline CPU scales ~linearly if a server with headroom — or a
    DETECT_HWACCEL host — raises it to 5-10 for finer motion/track
    granularity. Clamped to [1, 30]; a camera dict carrying an explicit
    per-camera ``fps`` still wins.
    """
    try:
        fps = int(os.environ.get("DETECT_FPS", "2"))
    except ValueError:
        log.warning("DETECT_FPS=%r is not an integer; using 2",
                    os.environ.get("DETECT_FPS"))
        return 2
    return max(1, min(30, fps))


# Skills whose consumers ride the Tier-0 detection stream. Used ONLY by the
# opt-in DETECT_SKIP_UNASSIGNED mode: a camera whose assignments contain
# none of these (e.g. LPR-only) can skip Tier-0 analysis entirely — a CPU
# saving. Kept deliberately broad, and the mode ships OFF, because Tier-0
# feeds many subscribers (footage-search indexes everything, the agent's
# events skill reads every camera): skipping must be an explicit operator
# choice, never an inference.
DETECTION_SHAPED_SKILLS: frozenset[str] = frozenset({
    "object_detection", "occupancy_counting", "people_counting",
    "line_crossing", "intrusion_detection", "loitering_detection",
    "abandoned_object", "package_delivery", "smart_doorbell",
    "footage_search", "motion_detection",
})


def _skip_unassigned() -> bool:
    """DETECT_SKIP_UNASSIGNED=true opts in to skipping cameras whose
    assignments are detection-free. Default OFF (see above)."""
    return os.environ.get("DETECT_SKIP_UNASSIGNED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _assignment_view(c: dict) -> tuple[frozenset[str] | None, bool]:
    """(per-camera labels, analyze) from a camera dict's ``assignments``.

    Labels come from an ``object_detection`` assignment carrying labels —
    "camera 4 wants person + truck" — and REPLACE the global DETECT_LABELS
    for that camera only (unset → global applies, as ever). ``analyze``
    stays True unless the opt-in skip mode is on AND the camera carries
    assignments, none of them detection-shaped. The back-compat rule
    everywhere: NO assignments = no restriction declared = analyze with
    global labels, exactly as before assignments existed.
    """
    assignments = c.get("assignments")
    if not isinstance(assignments, list) or not assignments:
        return None, True
    labels: frozenset[str] | None = None
    skills: set[str] = set()
    for a in assignments:
        if not isinstance(a, dict):
            continue
        skill = str(a.get("skill", "")).strip().lower()
        if not skill:
            continue
        skills.add(skill)
        if skill == "object_detection" and a.get("labels"):
            labels = frozenset(
                str(s).strip().lower() for s in a["labels"] if str(s).strip()
            ) or None
    analyze = True
    if _skip_unassigned() and skills and not (skills & DETECTION_SHAPED_SKILLS):
        analyze = False
        log.info(
            "tier0: skipping %s — assignments (%s) need no Tier-0 detection "
            "and DETECT_SKIP_UNASSIGNED is on",
            c.get("camera_id"), ", ".join(sorted(skills)),
        )
    return labels, analyze


# Cameras already warned about a missing/garbled open_nvr_camera_id — the
# provider re-fetches every reconcile tick, so warn once per handle, not
# once per 30 seconds.
_warned_no_nvr_id: set[str] = set()


def _to_spec(c: dict) -> CameraSpec:
    # The endpoint returns active cameras with a resolved ``frame_url``. All
    # active cameras are analyzed by default (on-by-default); an ``analyze`` flag
    # is honoured if the endpoint ever adds per-camera opt-out.
    labels, assignment_analyze = _assignment_view(c)
    # Core's numeric Camera.id, sent alongside the "cam{id}" handle — the
    # events store keys on the number (see CameraSpec.nvr_camera_id). The
    # str() round-trip rejects bools/floats (int(True) == 1 would file
    # events under camera 1) instead of silently accepting them.
    handle = str(c.get("camera_id"))
    try:
        nvr_id = int(str(c["open_nvr_camera_id"]))
        _warned_no_nvr_id.discard(handle)   # healed — re-warn if it breaks again
    except (KeyError, TypeError, ValueError):
        nvr_id = None
        if handle not in _warned_no_nvr_id:
            _warned_no_nvr_id.add(handle)
            log.warning(
                "camera %s: no usable open_nvr_camera_id (%r) — visit posts "
                "will fall back to parsing the handle",
                handle, c.get("open_nvr_camera_id"),
            )
    return CameraSpec(
        camera_id=str(c["camera_id"]),
        nvr_camera_id=nvr_id,
        name=c.get("name", str(c["camera_id"])),
        substream_url=c["frame_url"],
        analyze=bool(c.get("analyze", True)) and assignment_analyze,
        width=c.get("width"),
        height=c.get("height"),
        fps=int(c.get("fps", _default_fps())),
        hwaccel=(c.get("hwaccel") or None),   # None = not declared → global applies
        labels=labels,
    )


DETECT_CONFIG_PATH = "/api/v1/internal/camera-agent/detect-config"


def fetch_detect_config(
    base_url: str,
    api_key: str | None = None,
    *,
    opener=None,
    timeout: float = 5.0,
) -> dict | None:
    """Fetch the managed Tier-0 config override from core (guided promotion).

    Returns e.g. ``{"gate_mode": "enforce"}`` — ``gate_mode: None`` means "no
    override, follow env". Any failure returns None (caller keeps current
    settings; this must never disturb the pipeline)."""
    _opener = opener or urllib.request.urlopen
    req = urllib.request.Request(f"{base_url.rstrip('/')}{DETECT_CONFIG_PATH}")
    if api_key:
        req.add_header("X-Internal-Api-Key", api_key)
    try:
        with _opener(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        log.debug("detect-config fetch failed", exc_info=True)
        return None
