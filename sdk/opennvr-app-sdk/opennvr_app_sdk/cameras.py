# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""
Camera discovery — ask OpenNVR which cameras exist instead of making the
operator retype them.

Every zone/tripwire app needs the same thing before it can do anything:
the set of camera ids currently configured in OpenNVR. Hand-copying them
into each app's YAML is the single most common way an app ends up doing
nothing at all — OpenNVR names cameras ``cam<id>`` (``cam1``, ``cam2``),
while a hand-written config almost always says ``cam-1``, and the two
look identical until you notice the app has counted zero objects all day.

The list comes from the same internal endpoint the camera-agent uses,
authenticated with the stack's ``INTERNAL_API_KEY``:

    GET {opennvr_url}/api/v1/internal/camera-agent/cameras
    -> {"cameras": [{"camera_id": "cam1", "name": ..., "role": ...}, ...]}

Note what it does NOT return: frame dimensions. That is fine, and the
reason is worth stating because it looks like a gap. Tier-0 publishes
PIXEL boxes plus the frame size, the SDK bridge normalises them to 0-1,
and an app scales them back by whatever ``frame_width``/``frame_height``
its own zone was drawn in. Point and polygon therefore live in the SAME
space whatever numbers are used, so an auto-derived full-frame zone in a
unit space (see ``UNIT_FRAME``) is exactly correct for every camera
regardless of its real resolution.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("opennvr_app_sdk.cameras")

# The virtual coordinate space auto-derived zones are expressed in. Any
# consistent number works (see the module docstring); 1000 keeps the
# polygon readable in logs and in the catalog UI's zone editor.
UNIT_FRAME: int = 1000

CAMERAS_PATH = "/api/v1/internal/camera-agent/cameras"


def internal_api_key(explicit: str | None = None) -> str | None:
    """The credential to present: the app's OWN key when it has one
    (issued at registration — see ``credentials.py``), else an explicit
    value, else the ``OPENNVR_INTERNAL_API_KEY`` env var the app
    overlays set. With an app key core returns only the cameras the
    operator assigned to this app."""
    from .credentials import AppCredentials

    return AppCredentials(explicit).token()


def discover_cameras(
    opennvr_url: str,
    *,
    api_key: str | None = None,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Return OpenNVR's configured cameras, or ``[]`` if they can't be read.

    Never raises: discovery runs at app startup, and an app that refuses
    to boot because core was still starting is worse than one that boots
    with no cameras and says so. Callers should log the empty result.

    ``[]`` deliberately conflates "core said zero cameras" with "core
    could not be reached" — for a boot-time listing they are the same
    non-answer. Callers that must tell them apart (assignment, where
    the two mean opposite things) use :func:`_fetch_cameras`.
    """
    return _fetch_cameras(opennvr_url, api_key=api_key, timeout=timeout) or []


def _fetch_cameras(
    opennvr_url: str,
    *,
    api_key: str | None = None,
    timeout: float = 5.0,
) -> list[dict[str, Any]] | None:
    """As :func:`discover_cameras`, but ``None`` when core could not be
    asked at all — as opposed to ``[]``, core answering "no cameras"."""
    base = (opennvr_url or "").rstrip("/")
    if not base:
        return None
    key = internal_api_key(api_key)
    headers = {"X-Internal-Api-Key": key} if key else {}
    try:
        import httpx

        response = httpx.get(f"{base}{CAMERAS_PATH}", headers=headers, timeout=timeout)
        if response.status_code != 200:
            logger.warning(
                "camera discovery: %s returned HTTP %s%s", CAMERAS_PATH,
                response.status_code,
                " (is OPENNVR_INTERNAL_API_KEY set?)" if response.status_code in (401, 403) else "",
            )
            return None
        payload = response.json()
    except Exception as exc:                      # network, JSON, import
        logger.warning("camera discovery failed against %s: %s", base, exc)
        return None
    cameras = payload.get("cameras") if isinstance(payload, dict) else None
    if not isinstance(cameras, list):
        return None
    return [c for c in cameras if isinstance(c, dict) and c.get("camera_id")]


def full_frame_polygon(size: int = UNIT_FRAME) -> list[list[int]]:
    """The whole-frame zone, in the unit space auto-derived zones use."""
    return [[0, 0], [size, 0], [size, size], [0, size]]


# ── Per-camera capability assignment (slice 2) ─────────────────────
#
# Operators declare "camera 1 does LPR, cameras 2-3 count people" on the
# camera settings page; the internal endpoint serves it as an
# ``assignments`` list on each camera. The camera keeps streaming,
# recording and Tier-0 detection regardless — assignment governs which
# apps may use it and which SKILL INFERENCE runs on it.
#
# CLOSED BY DEFAULT. An app watches the cameras it was pointed at, and
# no more. This reverses the original additive rule, under which "no
# camera carries this skill" meant "watch everything": that made the
# least-configured install the most expensive one, and let an app read
# cameras nobody had offered it. An app with no assigned cameras now
# watches nothing — visibly, so an operator fixes it, rather than
# silently getting the fleet.


def filter_cameras_for_skill(
    cameras: list[dict[str, Any]], skill: str
) -> list[str] | None:
    """Which of ``cameras`` (a :func:`discover_cameras` payload) are
    assigned ``skill``.

    Returns the camera-id list, EMPTY when no camera carries the skill.
    An empty list means watch nothing: the operator has not pointed this
    skill at anything yet. It used to return ``None`` there, meaning
    "watch everything", which is why an unconfigured app saw the fleet.

    The return type stays ``| None`` for callers that still branch on
    it; ``None`` now only means "could not ask" (an empty skill name),
    never "no restriction".

    Pure — feed it the list you already fetched instead of fetching
    twice (the occupancy example's refresh loop does exactly this).
    """
    want = str(skill).strip().lower()
    if not want:
        return None
    out: list[str] = []
    for cam in cameras:
        if not isinstance(cam, dict):
            continue
        for a in cam.get("assignments") or []:
            if isinstance(a, dict) and str(a.get("skill", "")).lower() == want:
                out.append(str(cam["camera_id"]))
                break
    return out


def cameras_for_skill(
    opennvr_url: str,
    skill: str,
    *,
    api_key: str | None = None,
    timeout: float = 5.0,
) -> list[str] | None:
    """Fetch-and-filter convenience: the camera ids assigned ``skill``.

    ``[]`` means core answered and no camera carries the skill: watch
    nothing. ``None`` means core could not be ASKED — unknown, and
    unknown must not be acted on in either direction: keep whatever
    roster you already had rather than dropping to nothing on a restart
    that raced core, or widening to everything on a network blip.

    Use :func:`filter_cameras_for_skill` when you already hold a
    :func:`discover_cameras` result.
    """
    cameras = _fetch_cameras(opennvr_url, api_key=api_key, timeout=timeout)
    if cameras is None:
        return None
    return filter_cameras_for_skill(cameras, skill)
