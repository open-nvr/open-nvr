# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Authorization on the IP-keyed ONVIF routes (CWE-862 / CWE-639).

Reported by Furkan Arslan: every ``/camera/{ip}/...`` route and
``/connect`` in routers/onvif.py checked only that the IP lay inside the
camera LAN, so any authenticated user — one with no cameras at all —
could PTZ, read the stream URI of, or relay credentials at ANY camera on
the LAN, while the sibling ``/cameras/{id}/ptz/...`` routes correctly
required ownership. The routes now require ownership, a CameraPermission
grant (can_view for reads, can_manage for control) or superuser on a
registered camera; onboarding an UNREGISTERED device (the Add Camera
wizard's connect → profiles → stream-uri) still works for any active
user, but PTZ against an unregistered IP is refused.
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_onvif_authz.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


for _name in ("main_logger", "api_logger", "auth_logger"):
    setattr(_lm, _name, _L())
_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.auth import get_current_active_user  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, CameraPermission, Role, User  # noqa: E402
from routers import onvif as onvif_router  # noqa: E402

OWNER, VIEWER, MANAGER, STRANGER, ROOT = 1, 2, 3, 4, 5
CAM_IP = "10.0.0.7"
NEW_IP = "10.0.0.99"           # nobody has registered this device


@pytest.fixture()
def harness(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    s = factory()
    s.add(Role(id=1, name="admin"))
    for uid, name in ((OWNER, "owner"), (VIEWER, "viewer"), (MANAGER, "manager"),
                      (STRANGER, "stranger")):
        s.add(User(id=uid, username=name, email=f"{name}@x",
                   hashed_password="x", role_id=1))
    s.add(User(id=ROOT, username="root", email="root@x", hashed_password="x",
               role_id=1, is_superuser=True))
    s.add(Camera(id=1, name="Gate", ip_address=CAM_IP, owner_id=OWNER,
                 is_active=True, rtsp_url="rtsp://gate/stream"))
    s.add(CameraPermission(user_id=VIEWER, camera_id=1, can_view=True,
                           can_manage=False))
    s.add(CameraPermission(user_id=MANAGER, camera_id=1, can_view=True,
                           can_manage=True))
    s.commit()
    s.close()

    # The camera LAN check is a network rule, not what is under test;
    # the ONVIF calls themselves are stubbed — no device is contacted.
    monkeypatch.setattr(onvif_router, "_assert_ip_in_camera_lan",
                        lambda ip, db: None)

    async def _ok(*a, **k):
        return {"ok": True}

    for fn in ("ptz_continuous_move", "ptz_stop", "ptz_presets",
               "connect_and_get_profiles", "fetch_profiles_digest",
               "fetch_profiles", "get_stream_uri_digest", "get_stream_uri",
               "get_system_datetime"):
        monkeypatch.setattr(onvif_router, fn, _ok)

    app = FastAPI()
    app.include_router(onvif_router.router)
    state = {"uid": STRANGER}

    def _db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    def _user():
        session = factory()
        try:
            return session.get(User, state["uid"])
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_active_user] = _user
    with TestClient(app) as client:
        yield client, state, factory


_CREDS = {"username": "admin", "password": "secret", "profileToken": "p0"}


def _as(state, uid):
    state["uid"] = uid


def test_stranger_is_refused_everything_on_a_registered_camera(harness):
    client, state, _ = harness
    _as(state, STRANGER)
    assert client.post(f"/camera/{CAM_IP}/ptz/move", params={**_CREDS, "x": 0.5}).status_code == 403
    assert client.post(f"/camera/{CAM_IP}/ptz/stop", params=_CREDS).status_code == 403
    assert client.post(f"/camera/{CAM_IP}/ptz/preset",
                       params={**_CREDS, "action": "getPresets"}).status_code == 403
    assert client.get(f"/camera/{CAM_IP}/stream-uri", params=_CREDS).status_code == 403
    assert client.get(f"/camera/{CAM_IP}/profiles", params=_CREDS).status_code == 403
    assert client.get(f"/camera/{CAM_IP}/time").status_code == 403
    assert client.post("/connect", params={"ip": CAM_IP, "username": "a",
                                          "password": "b"}).status_code == 403


def test_owner_and_superuser_pass(harness):
    client, state, _ = harness
    for uid in (OWNER, ROOT):
        _as(state, uid)
        assert client.post(f"/camera/{CAM_IP}/ptz/move", params={**_CREDS, "x": 0.5}).status_code == 200
        assert client.get(f"/camera/{CAM_IP}/stream-uri", params=_CREDS).status_code == 200
        assert client.post("/connect", params={"ip": CAM_IP, "username": "a",
                                              "password": "b"}).status_code == 200


def test_grants_are_tiered_view_versus_manage(harness):
    client, state, _ = harness
    _as(state, VIEWER)
    assert client.get(f"/camera/{CAM_IP}/profiles", params=_CREDS).status_code == 200
    assert client.get(f"/camera/{CAM_IP}/time").status_code == 200
    assert client.post(f"/camera/{CAM_IP}/ptz/move", params={**_CREDS, "x": 0.5}).status_code == 403
    assert client.post(f"/camera/{CAM_IP}/ptz/preset",
                       params={**_CREDS, "action": "setPreset", "name": "x"}).status_code == 403
    _as(state, MANAGER)
    assert client.post(f"/camera/{CAM_IP}/ptz/move", params={**_CREDS, "x": 0.5}).status_code == 200
    assert client.post(f"/camera/{CAM_IP}/ptz/preset",
                       params={**_CREDS, "action": "setPreset", "name": "x"}).status_code == 200


def test_onboarding_an_unregistered_device_still_works_but_not_ptz(harness):
    """The Add Camera wizard: connect → profiles → stream-uri on a device
    nobody has registered yet must keep working for any active user.
    Driving its PTZ is not onboarding."""
    client, state, _ = harness
    _as(state, STRANGER)
    assert client.post("/connect", params={"ip": NEW_IP, "username": "a",
                                          "password": "b"}).status_code == 200
    assert client.get(f"/camera/{NEW_IP}/profiles", params=_CREDS).status_code == 200
    assert client.get(f"/camera/{NEW_IP}/stream-uri", params=_CREDS).status_code == 200
    assert client.post(f"/camera/{NEW_IP}/ptz/move", params={**_CREDS, "x": 0.5}).status_code == 403
    assert client.post(f"/camera/{NEW_IP}/ptz/preset",
                       params={**_CREDS, "action": "getPresets"}).status_code == 403


def test_a_deleted_camera_does_not_grant_or_block(harness):
    """Soft-deleted rows are not registered cameras: the IP behaves as
    unregistered (onboarding allowed, PTZ refused) even for the old owner."""
    client, state, factory = harness
    session = factory()
    session.get(Camera, 1).deleted_at = datetime.now(timezone.utc)
    session.commit()
    session.close()
    _as(state, OWNER)
    assert client.post(f"/camera/{CAM_IP}/ptz/move", params={**_CREDS, "x": 0.5}).status_code == 403
    _as(state, STRANGER)
    assert client.get(f"/camera/{CAM_IP}/profiles", params=_CREDS).status_code == 200
