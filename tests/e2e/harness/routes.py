# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every API path the suite touches, named once.

Tests call ``routes.CAMERA_PROVISION(cam_id)``, never an f-string. When a route
moves, this file is the single edit — instead of a grep across every test that
happened to hard-code the path.

Paths here are relative to the API prefix (``/api/v1``, ``settings.api_prefix``
in ``server/core/config.py``); ``OpenNVRClient`` joins them. The handful of
routes that live OUTSIDE the prefix are spelled with a leading ``ABS_`` and
carry their full path.

Grouping mirrors ``server/routers/``. Where a router is mounted with no prefix
of its own (``timeline_events``, ``events``, ``occupancy``) the paths below
still start at the API root, exactly as FastAPI serves them.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Outside the API prefix
# ---------------------------------------------------------------------------
API_PREFIX = "/api/v1"


def api(path: str) -> str:
    """Prefix a route with the API root.

    ``OpenNVRClient`` does this itself, so tests never need it. It exists for
    the few places that talk to core with a bare httpx client and no client
    object yet — bootstrap, which runs before a session exists.
    """
    return f"{API_PREFIX}{path}"


ABS_HEALTH = "/health"
ABS_JWKS = "/.well-known/jwks.json"

# ---------------------------------------------------------------------------
# auth  (server/routers/auth.py)
# ---------------------------------------------------------------------------
AUTH_CHECK_SETUP = "/auth/check-setup"
AUTH_FIRST_TIME_SETUP = "/auth/first-time-setup"
AUTH_LOGIN = "/auth/login"
AUTH_LOGIN_JSON = "/auth/login-json"
AUTH_REFRESH = "/auth/refresh"
AUTH_LOGOUT = "/auth/logout"
AUTH_ME = "/auth/me"
AUTH_MFA_SETUP = "/auth/mfa/setup"
AUTH_MFA_VERIFY = "/auth/mfa/verify"

# ---------------------------------------------------------------------------
# users / roles / permissions
# ---------------------------------------------------------------------------
USERS = "/users/"
USERS_ME = "/users/me"
USERS_ME_PERMISSIONS = "/users/me/permissions"
ROLES = "/roles/"
PERMISSIONS = "/permissions"


def USER(user_id: int | str) -> str:
    return f"/users/{user_id}"


# ---------------------------------------------------------------------------
# cameras  (server/routers/cameras.py)
# ---------------------------------------------------------------------------
CAMERAS = "/cameras/"
CAMERAS_DELETED = "/cameras/deleted"
CAMERAS_ASSIGNABLE_SKILLS = "/cameras/assignable-skills"


def CAMERA(camera_id: int | str) -> str:
    return f"/cameras/{camera_id}"


def CAMERA_MEDIAMTX_STATUS(camera_id: int | str) -> str:
    return f"/cameras/{camera_id}/mediamtx-status"


def CAMERA_PROVISION(camera_id: int | str) -> str:
    return f"/cameras/{camera_id}/provision-mediamtx"


def CAMERA_STREAM_URLS(camera_id: int | str) -> str:
    return f"/cameras/{camera_id}/stream/urls"


def CAMERA_SNAPSHOT(camera_id: int | str) -> str:
    return f"/cameras/{camera_id}/snapshot"


def CAMERA_HARD_DELETE(camera_id: int | str) -> str:
    return f"/cameras/{camera_id}/hard-delete"


def CAMERA_PERMISSIONS(camera_id: int | str) -> str:
    return f"/cameras/{camera_id}/permissions"


def CAMERA_PERMISSION(camera_id: int | str, user_id: int | str) -> str:
    return f"/cameras/{camera_id}/permissions/{user_id}"


# ---------------------------------------------------------------------------
# streams  (server/routers/streams.py)
# ---------------------------------------------------------------------------
def STREAM_HLS(camera_id: int | str) -> str:
    return f"/streams/hls/{camera_id}"


def STREAM_WEBRTC(camera_id: int | str) -> str:
    return f"/streams/webrtc/{camera_id}"


def STREAM_INFO(camera_id: int | str) -> str:
    return f"/streams/{camera_id}/info"


# ---------------------------------------------------------------------------
# recordings  (server/routers/recordings.py)
# ---------------------------------------------------------------------------
RECORDINGS_PLAYBACK_LIST = "/recordings/playback/list"
RECORDINGS_PLAYBACK_CAMERAS = "/recordings/playback/cameras"
RECORDINGS_PLAYBACK_HLS = "/recordings/playback/hls"
RECORDINGS_EXPORT_TICKET = "/recordings/export/ticket"
RECORDINGS_EXPORT = "/recordings/export"
RECORDINGS_FRAME = "/recordings/frame"
RECORDINGS_LIST = "/recordings/list"
RECORDINGS_STATS = "/recordings/stats"
RECORDINGS_RETENTION = "/recordings/retention"
RECORDINGS_STORAGE = "/recordings/storage"
RECORDINGS_FLAG = "/recordings/flag"


def RECORDINGS_SEGMENTS(camera_id: int | str) -> str:
    return f"/recordings/segments/{camera_id}"


def RECORDINGS_STATUS(camera_id: int | str) -> str:
    return f"/recordings/status/{camera_id}"


# ---------------------------------------------------------------------------
# events / visits / plates  (server/routers/timeline_events.py, events.py)
# ---------------------------------------------------------------------------
EVENTS = "/events"
EVENTS_WS_TICKET = "/events/ws-ticket"
EVENTS_WS = "/events/ws"
EVENTS_PLATE_STATS = "/events/plate-stats"
EVENTS_PLATE_SUMMARY = "/events/plate-summary"
EVENTS_PLATE_SESSIONS = "/events/plate-sessions"
EVENTS_GATE_OCCUPANCY = "/events/gate-occupancy"
EVENTS_VEHICLE_REPORT = "/events/vehicle-report"


def EVENT_EVIDENCE(event_id: int | str) -> str:
    return f"/events/{event_id}/evidence"


def EVENT_PLATE_EVIDENCE(event_id: int | str) -> str:
    return f"/events/{event_id}/plate-evidence"


def EVENT_SCENE_EVIDENCE(event_id: int | str) -> str:
    return f"/events/{event_id}/scene-evidence"


def EVENT_PLATE_FRAME(event_id: int | str) -> str:
    return f"/events/{event_id}/plate-frame"


# ---------------------------------------------------------------------------
# internal camera-agent door  (X-Internal-Api-Key, not a user JWT)
# ---------------------------------------------------------------------------
INTERNAL_EVENTS = "/internal/camera-agent/events"
INTERNAL_PLATE_ATTEMPT = "/internal/camera-agent/plates/attempt"
INTERNAL_DETECT_CONFIG = "/internal/camera-agent/detect-config"
INTERNAL_CAMERAS = "/internal/camera-agent/cameras"
INTERNAL_SKILLS = "/internal/camera-agent/skills"

# ---------------------------------------------------------------------------
# occupancy  (server/routers/occupancy.py)
# ---------------------------------------------------------------------------
OCCUPANCY_HISTORY = "/occupancy/history"
OCCUPANCY_HEATMAP = "/occupancy/heatmap"
OCCUPANCY_FOOTFALL = "/occupancy/footfall"
OCCUPANCY_REPORT = "/occupancy/report"

# ---------------------------------------------------------------------------
# apps / alerts / skills
# ---------------------------------------------------------------------------
APPS = "/apps"
APPS_INDEX = "/apps/index"
APPS_REGISTER = "/apps/register"
ALERTS_INBOX = "/alerts-inbox"
ALERTS_INBOX_ACK = "/alerts-inbox/ack"
ALERTS_INBOX_TEST = "/alerts-inbox/test"
SKILLS = "/skills"


def APP(app_id: str) -> str:
    return f"/apps/{app_id}"


def APP_CONFIG(app_id: str) -> str:
    return f"/apps/{app_id}/config"


def APP_STATUS(app_id: str) -> str:
    return f"/apps/{app_id}/status"


def APP_INSTALL(index_id: str) -> str:
    return f"/apps/index/{index_id}/install"


def APP_INSTALL_STATUS(index_id: str) -> str:
    return f"/apps/index/{index_id}/install-status"


# ---------------------------------------------------------------------------
# AI / KAI-C proxy surfaces on core
# ---------------------------------------------------------------------------
AI_MODELS_HEALTH = "/ai-models/health"
AI_MODELS_CAPABILITIES = "/ai-models/capabilities"
AI_MODELS_TIER0_METRICS = "/ai-models/tier0-metrics"
AI_MODELS_TASKS = "/ai-models/tasks"

# ---------------------------------------------------------------------------
# system / security / posture
#
# Several routers mount their collection at ``"/"``, so the path needs the
# trailing slash. Without it FastAPI answers a redirect that httpx follows
# into something that is not JSON, and the failure surfaces as a
# JSONDecodeError with no hint that the URL was the problem.
# ---------------------------------------------------------------------------
SYSTEM_EVENTS = "/system/events"
SYSTEM_POSTURE = "/system/posture"
SYSTEM_RESOURCES = "/system/resources"
AUDIT_LOGS = "/audit-logs/"
DEVICE_FIREWALL_STATUS = "/device-firewall/status"
DEVICE_FIREWALL_DEVICES = "/device-firewall/devices"

# ---------------------------------------------------------------------------
# KAI-C direct (port 8100, X-Internal-Api-Key). NOT under the core API prefix —
# these are absolute paths on the KAI-C base URL.
# ---------------------------------------------------------------------------
KAIC_HEALTH = "/health"
KAIC_ADAPTERS = "/api/v1/adapters"
KAIC_ADAPTERS_REFRESH = "/api/v1/adapters/refresh"


def KAIC_INFER(adapter: str) -> str:
    return f"/api/v1/infer/{adapter}"
