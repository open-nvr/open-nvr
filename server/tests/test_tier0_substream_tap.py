# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Tier-0 substream tap: the internal camera-agent /cameras endpoint serves
the LOW-RES ``{name}-sub`` MediaMTX tap whenever the camera has a sub
source — a STORED substream_url only. A derivable-but-unstored vendor
convention still provisions the agent's on-demand sub path, but never
steers the always-on detector: a wrong guess there would kill detection
for that camera outright. This is the change that makes "configure the
camera's substream" actually reach the detect-pipeline (~5x decode
CPU). Cameras with no stored sub keep the main tap exactly as before.

Run with:

    cd server && pytest tests/test_tier0_substream_tap.py -v
"""
from __future__ import annotations

import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017 — only runs where UTC is absent

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules["core.logging_config"] = _lm

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.config import settings  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, Role, User  # noqa: E402
from routers import internal_camera_agent as internal_router  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    """Three cameras behind the internal router with the MediaMTX tap ON:
    one with a stored substream_url, one whose main URL is a derivable
    Hikvision convention, one with neither. JWT minting is stubbed out
    (no keys in tests) — bare tap URLs are the documented fallback."""
    monkeypatch.setattr(settings, "inference_use_mediamtx_tap", True)
    monkeypatch.setattr(internal_router, "_mint_mediamtx_jwt", lambda: None)

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine, tables=[Camera.__table__, User.__table__, Role.__table__]
    )
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    session.add(Role(id=1, name="admin"))
    session.add(User(id=1, username="op", email="op@x",
                     hashed_password="x", role_id=1))
    session.add(Camera(                      # stored substream_url wins
        id=1, name="Stored", ip_address="10.0.0.1", owner_id=1, is_active=True,
        rtsp_url="rtsp://u:p@10.0.0.1:554/avstream/channel=1/stream=0.sdp",
        substream_url="rtsp://u:p@10.0.0.1:554/avstream/channel=1/stream=1.sdp",
    ))
    session.add(Camera(                      # derivable vendor convention
        id=2, name="Hik", ip_address="10.0.0.2", owner_id=1, is_active=True,
        rtsp_url="rtsp://u:p@10.0.0.2:554/Streaming/Channels/101",
    ))
    session.add(Camera(                      # no sub source at all
        id=3, name="Plain", ip_address="10.0.0.3", owner_id=1, is_active=True,
        rtsp_url="rtsp://u:p@10.0.0.3:554/opaque/onvif/path",
    ))
    session.commit()
    session.close()

    app = FastAPI()
    app.include_router(internal_router.router)

    def _override_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[internal_router._require_internal_key] = lambda: None
    with TestClient(app) as c:
        yield c


def _cams(client) -> dict[str, dict]:
    body = client.get("/internal/camera-agent/cameras").json()
    return {c["camera_id"]: c for c in body["cameras"]}


def test_stored_substream_serves_the_sub_tap(client):
    cam = _cams(client)["cam1"]
    assert cam["frame_url"].endswith("-sub")
    assert cam["source"] == "mediamtx-sub"


def test_derivable_but_unstored_url_keeps_the_main_tap(client):
    """A Hikvision-SHAPED main URL is only a guess about the substream —
    the camera may have its sub stream disabled, and an always-on
    detector pointed at a dead URL is a camera with no detection. Only
    the operator's stored substream_url steers Tier-0."""
    cam = _cams(client)["cam2"]
    assert not cam["frame_url"].endswith("-sub")
    assert cam["source"] == "mediamtx"


def test_no_sub_source_keeps_the_main_tap(client):
    cam = _cams(client)["cam3"]
    assert not cam["frame_url"].endswith("-sub")
    assert cam["source"] == "mediamtx"


# ── Tap policy: auto | sub | main ──────────────────────────────────


def test_auto_prefers_main_when_hardware_decode_is_configured(client, monkeypatch):
    """A GPU/hwaccel box can afford full-res decode (and gets full-res
    evidence crops) — 'auto' picks the MAIN stream there."""
    monkeypatch.setattr(settings, "detect_hwaccel", "vaapi")
    cams = _cams(client)
    assert cams["cam1"]["source"] == "mediamtx"
    assert not cams["cam1"]["frame_url"].endswith("-sub")


def test_forced_main_ignores_the_substream(client, monkeypatch):
    monkeypatch.setattr(settings, "inference_tap_stream", "main")
    assert _cams(client)["cam1"]["source"] == "mediamtx"


def test_forced_sub_uses_it_even_with_hardware_decode(client, monkeypatch):
    monkeypatch.setattr(settings, "inference_tap_stream", "sub")
    monkeypatch.setattr(settings, "detect_hwaccel", "vaapi")
    cams = _cams(client)
    assert cams["cam1"]["source"] == "mediamtx-sub"
    # ...but cameras without a STORED sub still use main — forcing 'sub'
    # never turns a derivation guess into the detector's source either.
    assert cams["cam2"]["source"] == "mediamtx"
    assert cams["cam3"]["source"] == "mediamtx"
