# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""Camera discovery: apps ask OpenNVR for the camera list instead of
making operators retype ids that must match exactly."""
from __future__ import annotations

import httpx
import pytest

from opennvr_app_sdk.cameras import (
    UNIT_FRAME,
    cameras_for_skill,
    discover_cameras,
    filter_cameras_for_skill,
    full_frame_polygon,
    internal_api_key,
)


def _patch_get(monkeypatch, handler):
    monkeypatch.setattr(httpx, "get", handler)


def test_discovers_cameras_and_sends_the_internal_key(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"], seen["headers"] = url, headers or {}
        return httpx.Response(200, json={"cameras": [
            {"camera_id": "cam1", "name": "Front"},
            {"camera_id": "cam2", "name": "Back"},
        ]}, request=httpx.Request("GET", url))

    _patch_get(monkeypatch, fake_get)
    cams = discover_cameras("http://core:8000/", api_key="secret")
    assert [c["camera_id"] for c in cams] == ["cam1", "cam2"]
    assert seen["url"].endswith("/api/v1/internal/camera-agent/cameras")
    assert seen["headers"]["X-Internal-Api-Key"] == "secret"


@pytest.mark.parametrize("payload", [
    {"cameras": "nope"}, {}, [], {"cameras": [{"name": "no id"}, "junk"]},
])
def test_malformed_payloads_yield_no_cameras(monkeypatch, payload):
    _patch_get(monkeypatch, lambda url, headers=None, timeout=None: httpx.Response(
        200, json=payload, request=httpx.Request("GET", url)))
    assert discover_cameras("http://core:8000") == []


def test_errors_never_raise(monkeypatch):
    """Discovery runs at boot; an app that refuses to start because core
    was still coming up is worse than one that starts and says so."""
    def boom(url, headers=None, timeout=None):
        raise httpx.ConnectError("core not up yet")

    _patch_get(monkeypatch, boom)
    assert discover_cameras("http://core:8000") == []

    _patch_get(monkeypatch, lambda url, headers=None, timeout=None: httpx.Response(
        401, request=httpx.Request("GET", url)))
    assert discover_cameras("http://core:8000") == []
    assert discover_cameras("") == []


def test_internal_api_key_prefers_explicit_then_env(monkeypatch):
    monkeypatch.setenv("OPENNVR_INTERNAL_API_KEY", "from-env")
    assert internal_api_key("explicit") == "explicit"
    assert internal_api_key(None) == "from-env"
    monkeypatch.setenv("OPENNVR_INTERNAL_API_KEY", "   ")
    assert internal_api_key(None) is None


def test_full_frame_polygon_covers_the_unit_space():
    poly = full_frame_polygon()
    assert poly == [[0, 0], [UNIT_FRAME, 0], [UNIT_FRAME, UNIT_FRAME], [0, UNIT_FRAME]]


# ── Per-camera assignment (slice 2) ────────────────────────────────
#
# "Camera 1 does LPR, cameras 2-3 count people." The back-compat rule:
# restriction exists only once at least one camera carries the skill —
# no camera assigned (or an older core with no assignments field) means
# "no restriction declared", and the caller keeps watching everything.


_ASSIGNED = [
    {"camera_id": "cam1",
     "assignments": [{"skill": "license_plate_recognition"}]},
    {"camera_id": "cam2",
     "assignments": [{"skill": "occupancy_counting"}]},
    {"camera_id": "cam3",
     "assignments": [{"skill": "occupancy_counting",
                      "labels": ["person"]},
                     {"skill": "object_detection"}]},
    {"camera_id": "cam4", "assignments": []},
]


def test_filter_returns_only_cameras_assigned_the_skill():
    assert filter_cameras_for_skill(_ASSIGNED, "occupancy_counting") == ["cam2", "cam3"]
    assert filter_cameras_for_skill(_ASSIGNED, "license_plate_recognition") == ["cam1"]


def test_filter_none_means_no_restriction_declared():
    # Nobody carries the skill → None, NOT [] — the caller must fall back
    # to watching everything, exactly as before assignments existed.
    assert filter_cameras_for_skill(_ASSIGNED, "face_recognition") is None
    # Older core: cameras with no assignments field at all.
    legacy = [{"camera_id": "cam1"}, {"camera_id": "cam2"}]
    assert filter_cameras_for_skill(legacy, "occupancy_counting") is None
    assert filter_cameras_for_skill([], "occupancy_counting") is None
    assert filter_cameras_for_skill(_ASSIGNED, "") is None


def test_filter_matches_case_insensitively_and_ignores_junk():
    messy = [
        {"camera_id": "cam9",
         "assignments": [{"skill": "Occupancy_Counting"}, "junk", 42]},
        "not-a-dict",
    ]
    assert filter_cameras_for_skill(messy, "occupancy_counting") == ["cam9"]


def test_cameras_for_skill_fetches_then_filters(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return httpx.Response(200, json={"cameras": _ASSIGNED},
                              request=httpx.Request("GET", url))

    _patch_get(monkeypatch, fake_get)
    assert cameras_for_skill("http://core:8000", "occupancy_counting") == ["cam2", "cam3"]


def test_cameras_for_skill_discovery_failure_is_no_restriction(monkeypatch):
    def boom(url, headers=None, timeout=None):
        raise httpx.ConnectError("down")

    _patch_get(monkeypatch, boom)
    # Failure means "unknown", and unknown must never narrow the set.
    assert cameras_for_skill("http://core:8000", "occupancy_counting") is None
