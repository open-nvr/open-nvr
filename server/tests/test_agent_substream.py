# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Low-res agent substream: derive the sub URL, provision a source-on-demand
`-sub` MediaMTX path, and offer it to the camera-agent via /streams/{id}/info
as `urls.webrtc_sub` (main UI keeps the full-res `webrtc`). No DB — handlers
and services are exercised directly with mocked collaborators."""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
try:
    from cryptography.fernet import Fernet

    os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
except Exception:
    pass
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")

# A sibling test module may have installed a minimal STUB of
# core.logging_config (via sys.modules) lacking some loggers our imports
# need (e.g. mediamtx_logger). Backfill defensively so importing the
# services/routers under test works whichever module's stub won — the same
# guard test_camera_snapshot uses.
import core.logging_config as _lc  # noqa: E402


class _L:  # permissive no-op logger
    def __getattr__(self, _n):
        return lambda *a, **kw: None


for _name in (
    "main_logger", "auth_logger", "camera_logger", "recording_logger",
    "rtsp_logger", "api_logger", "mediamtx_logger", "config_logger",
    "storage_logger", "stream_logger", "ai_logger", "system_logger",
    "security_logger",
):
    _obj = getattr(_lc, _name, None)
    if _obj is None:
        setattr(_lc, _name, _L())
        continue
    for _m in ("info", "warning", "error", "debug", "exception",
               "critical", "log", "log_action"):
        if not hasattr(_obj, _m):
            try:
                setattr(_obj, _m, lambda *a, **kw: None)
            except (AttributeError, TypeError):
                pass

from services.camera_source_resolver import derive_substream_url  # noqa: E402
from services.stream_service import substream_name  # noqa: E402
# Import the modules under test HERE (with the backfill active) so they're
# cached in sys.modules — a sibling that re-stubs core.logging_config after
# this point can't break a fresh import at test-run time.
from services import mediamtx_admin_service as mmadmin  # noqa: E402
from routers import streams as streams_mod  # noqa: E402


# ── derivation ─────────────────────────────────────────────────────────


def test_derive_substream_url_vendor_conventions():
    assert derive_substream_url(
        "rtsp://u:p@1.2.3.4:554/cam/realmonitor?channel=1&subtype=0"
    ) == "rtsp://u:p@1.2.3.4:554/cam/realmonitor?channel=1&subtype=1"
    assert derive_substream_url(
        "rtsp://1.2.3.4:554/Streaming/Channels/101"
    ) == "rtsp://1.2.3.4:554/Streaming/Channels/102"
    assert derive_substream_url(
        "rtsp://1.2.3.4/Streaming/Channels/201"
    ) == "rtsp://1.2.3.4/Streaming/Channels/202"
    # Unknown convention → None (caller must not guess; falls back to main).
    assert derive_substream_url("rtsp://1.2.3.4:554/live/main") is None
    assert derive_substream_url(None) is None


def test_substream_name():
    assert substream_name("cam-1") == "cam-1-sub"


# ── provisioning: a source-on-demand `-sub` path is added ──────────────


def test_provision_substream_adds_on_demand_path(monkeypatch):
    mod = mmadmin
    MediaMtxAdminService = mmadmin.MediaMtxAdminService

    posts: list[tuple[str, dict]] = []

    class _Resp:
        status_code = 200

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            posts.append((url, json)); return _Resp()

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda *a, **k: _Client())
    monkeypatch.setattr(MediaMtxAdminService, "_base", staticmethod(lambda: "http://mtx/v3"))
    monkeypatch.setattr(MediaMtxAdminService, "_headers", staticmethod(lambda: {}))
    monkeypatch.setattr(mod.settings, "mediamtx_stream_prefix", "cam-")
    monkeypatch.setattr(mod.settings, "mediamtx_path_mode", "id", raising=False)

    cfg = {"source_url": "rtsp://1.2.3.4:554/Streaming/Channels/101",
           "rtsp_transport": "tcp"}
    asyncio.run(MediaMtxAdminService._provision_substream(1, "1.2.3.4", cfg))

    assert len(posts) == 1
    url, body = posts[0]
    assert url.endswith("/config/paths/add/cam-1-sub")
    assert body["source"] == "rtsp://1.2.3.4:554/Streaming/Channels/102"
    assert body["sourceOnDemand"] is True


def test_provision_substream_prefers_stored_url(monkeypatch):
    # An operator-stored substream_url wins over the vendor-derived default —
    # covers cameras whose sub path isn't a Hikvision/Dahua convention.
    mod = mmadmin
    MediaMtxAdminService = mmadmin.MediaMtxAdminService
    posts: list[tuple[str, dict]] = []

    class _Resp:
        status_code = 200

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            posts.append((url, json)); return _Resp()

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda *a, **k: _Client())
    monkeypatch.setattr(MediaMtxAdminService, "_base", staticmethod(lambda: "http://mtx/v3"))
    monkeypatch.setattr(MediaMtxAdminService, "_headers", staticmethod(lambda: {}))
    monkeypatch.setattr(mod.settings, "mediamtx_stream_prefix", "cam-")
    monkeypatch.setattr(mod.settings, "mediamtx_path_mode", "id", raising=False)

    cfg = {"source_url": "rtsp://1.2.3.4/live/main",       # not derivable
           "substream_url": "rtsp://1.2.3.4:554/vendor/lowres"}
    asyncio.run(MediaMtxAdminService._provision_substream(1, "1.2.3.4", cfg))

    assert len(posts) == 1
    assert posts[0][1]["source"] == "rtsp://1.2.3.4:554/vendor/lowres"


def test_provision_substream_skips_when_not_derivable(monkeypatch):
    mod = mmadmin
    MediaMtxAdminService = mmadmin.MediaMtxAdminService

    posts = []

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            posts.append(url)

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda *a, **k: _Client())
    monkeypatch.setattr(MediaMtxAdminService, "_base", staticmethod(lambda: "http://mtx/v3"))
    monkeypatch.setattr(MediaMtxAdminService, "_headers", staticmethod(lambda: {}))
    monkeypatch.setattr(mod.settings, "mediamtx_stream_prefix", "cam-")
    monkeypatch.setattr(mod.settings, "mediamtx_path_mode", "id", raising=False)

    # Unknown RTSP convention → no sub path is added (agent uses the main).
    asyncio.run(MediaMtxAdminService._provision_substream(
        1, "1.2.3.4", {"source_url": "rtsp://1.2.3.4/live/main"}))
    assert posts == []


# ── /streams/{id}/info offers webrtc_sub + a token covering both ───────


def _fake_camera():
    class _Cam:
        id = 1
        ip_address = "1.2.3.4"
        name = "Dock"
        status = "online"
    return _Cam()


def _call_info(monkeypatch, *, enabled: bool):
    mod = streams_mod

    monkeypatch.setattr(mod.settings, "agent_live_use_substream", enabled)
    monkeypatch.setattr(mod.settings, "mediamtx_stream_prefix", "cam-")
    monkeypatch.setattr(mod.settings, "mediamtx_path_mode", "id", raising=False)
    monkeypatch.setattr(mod, "_check_camera_permission",
                        lambda db, cid, user: _fake_camera())
    captured = {}

    def _tok(**kw):
        captured["camera_path"] = kw.get("camera_path")
        return "TOK"

    monkeypatch.setattr(mod.MediaMtxJwtService, "create_stream_token", staticmethod(_tok))

    class _User:
        id = 5
        username = "op"
    result = asyncio.run(mod.get_stream_info(1, db=None, current_user=_User()))
    return result, captured


def test_info_offers_substream_and_widened_token_when_enabled(monkeypatch):
    result, captured = _call_info(monkeypatch, enabled=True)
    urls = result["urls"]
    assert urls["webrtc"].endswith("/cam-1/whep")           # main unchanged
    assert urls["webrtc_sub"].endswith("/cam-1-sub/whep")   # agent's low-res
    # token scope is a regex covering BOTH the main path and its -sub sibling
    import re
    assert captured["camera_path"] == f"~^{re.escape('cam-1')}(-sub)?$"


def test_info_omits_substream_when_disabled(monkeypatch):
    result, captured = _call_info(monkeypatch, enabled=False)
    assert "webrtc_sub" not in result["urls"]
    assert captured["camera_path"] == "cam-1"               # exact-match, main only


def test_substream_token_regex_cannot_escalate_to_sibling_camera():
    """Auth-scope guard for the widened MediaMTX token pattern
    (routers/streams.py ~L276): the `(-sub)?$` anchor must authorize a
    camera's own main + `-sub` path and NOTHING else. A greedy/unanchored
    variant would let "cam-1"'s token also match "cam-10" (a different
    camera). Derive the pattern the same way the source does so this test
    tracks the source, then strip MediaMTX's leading `~` regex marker."""
    import re

    stream_name = "cam-1"
    token_path = f"~^{re.escape(stream_name)}(-sub)?$"   # mirrors streams.py
    pattern = token_path.lstrip("~")                     # MediaMTX regex marker

    # Own paths: authorized.
    assert re.match(pattern, "cam-1")
    assert re.match(pattern, "cam-1-sub")

    # Sibling / lookalike paths: rejected (no auth-scope escalation).
    assert re.match(pattern, "cam-10") is None           # different camera
    assert re.match(pattern, "cam-1-subextra") is None   # trailing junk
    assert re.match(pattern, "cam-1x") is None            # suffix past anchor
