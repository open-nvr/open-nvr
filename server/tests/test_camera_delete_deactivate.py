# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Delete vs deactivate semantics + duplicate detection (issue #243).

Delete and deactivate used to be the identical operation (is_active=False),
neither of which stopped the MediaMTX stream. These tests pin the new
contract:

* DELETE = irreversible soft delete: stamps deleted_at, deactivates, tears
  down the MediaMTX path, hides the camera from every normal endpoint, and
  lists it in the bin (GET /cameras/deleted).
* PUT is_active=false/true = reversible pause: unprovisions / re-provisions
  immediately; the flag persists even when MediaMTX errors.
* POST /cameras/ 409s with code=duplicate_camera on an owner's IP or RTSP
  URL match against non-deleted cameras; ?force=true bypasses.
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_camera_delete_test.db")
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
# Force-assign (not setdefault): an earlier-collected test may have installed
# a narrower stub without a __getattr__ fallback, which breaks
# `from core.logging_config import mediamtx_logger` at import time.
sys.modules["core.logging_config"] = _lm

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.auth as core_auth  # noqa: E402
import services.camera_identity as camera_identity  # noqa: E402
from core.auth import create_access_token, get_password_hash  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, Role, User  # noqa: E402
from routers import cameras as cameras_router  # noqa: E402
from services.mediamtx_admin_service import MediaMtxAdminService  # noqa: E402
from services.mediamtx_startup_service import MediaMtxStartupService  # noqa: E402

PASSWORD = "Str0ng!passw0rd"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(cameras_router, "camera_logger", _L(), raising=False)

    # Never touch MediaMTX or the recordings disk from these tests.
    unprovision_calls: list[tuple[int, str]] = []
    provision_calls: list[int] = []
    upsert_calls: list[tuple] = []

    async def _fake_unprovision(camera_id, camera_ip):
        unprovision_calls.append((camera_id, camera_ip))
        return {"status": "success"}

    async def _fake_provision_by_id(camera_id, force=False):
        provision_calls.append(camera_id)
        return {"status": "success"}

    # A URL edit now re-provisions the path, and does so transactionally — an
    # unstubbed call would reach for a real MediaMTX and the edit would be
    # rejected with 502. See test_camera_edit_reprovision.py for the tests that
    # exercise that contract; here it just has to succeed.
    async def _fake_upsert(camera_id, camera_ip, config, *, transport_security=None):
        upsert_calls.append((camera_id, camera_ip, dict(config)))
        return {"status": "ok"}

    monkeypatch.setattr(
        MediaMtxAdminService, "unprovision_path", staticmethod(_fake_unprovision)
    )
    monkeypatch.setattr(
        MediaMtxAdminService, "upsert_path", staticmethod(_fake_upsert)
    )
    monkeypatch.setattr(
        MediaMtxStartupService,
        "provision_camera_by_id",
        staticmethod(_fake_provision_by_id),
    )
    monkeypatch.setattr(
        camera_identity, "protect_camera_dir", lambda *a, **k: "match"
    )

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    session_factory = sessionmaker(bind=eng)

    app = FastAPI()
    app.include_router(cameras_router.router, prefix="/api/v1")

    def _get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db

    db = session_factory()
    role = Role(name="admin", description="test role")
    db.add(role)
    db.flush()
    owner = User(
        username="owner",
        email="owner@example.com",
        hashed_password=get_password_hash(PASSWORD),
        is_active=True,
        is_superuser=False,
        password_set=True,
        role_id=role.id,
    )
    db.add(owner)
    db.commit()

    def make_camera(**kw) -> Camera:
        defaults = dict(
            name="Front Door",
            ip_address="10.0.0.9",
            port=554,
            owner_id=owner.id,
            is_active=True,
            status="unknown",
        )
        defaults.update(kw)
        cam = Camera(**defaults)
        db.add(cam)
        db.commit()
        db.refresh(cam)
        return cam

    client = TestClient(app)
    try:
        yield _types.SimpleNamespace(
            client=client,
            db=db,
            owner=owner,
            make_camera=make_camera,
            unprovision_calls=unprovision_calls,
            provision_calls=provision_calls,
            upsert_calls=upsert_calls,
        )
    finally:
        db.close()


def _auth(username: str = "owner") -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': username})}"}


# ---- DELETE = irreversible soft delete ----


def test_delete_stamps_tombstone_and_unprovisions(env):
    cam = env.make_camera()
    resp = env.client.delete(f"/api/v1/cameras/{cam.id}", headers=_auth())
    assert resp.status_code == 200, resp.text
    env.db.expire_all()
    row = env.db.get(Camera, cam.id)
    assert row.is_active is False
    assert row.deleted_at is not None
    assert env.unprovision_calls == [(cam.id, cam.ip_address)]


def test_deleted_camera_is_not_editable_or_redeletable(env):
    cam = env.make_camera()
    env.client.delete(f"/api/v1/cameras/{cam.id}", headers=_auth())

    # Any per-camera endpoint 404s, so it cannot be revived or deleted twice.
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}", json={"is_active": True}, headers=_auth()
    )
    assert resp.status_code == 404
    resp = env.client.delete(f"/api/v1/cameras/{cam.id}", headers=_auth())
    assert resp.status_code == 404
    resp = env.client.get(f"/api/v1/cameras/{cam.id}", headers=_auth())
    assert resp.status_code == 404
    env.db.expire_all()
    assert env.db.get(Camera, cam.id).is_active is False


def test_deleted_camera_hidden_from_lists_but_in_bin(env):
    kept = env.make_camera(name="Kept", ip_address="10.0.0.1")
    binned = env.make_camera(name="Binned", ip_address="10.0.0.2")
    env.client.delete(f"/api/v1/cameras/{binned.id}", headers=_auth())

    # Hidden even with active_only unchecked (that used to revive "deleted"
    # cameras).
    resp = env.client.get(
        "/api/v1/cameras/", params={"active_only": False}, headers=_auth()
    )
    ids = {c["id"] for c in resp.json()["cameras"]}
    assert kept.id in ids and binned.id not in ids

    resp = env.client.get("/api/v1/cameras/deleted", headers=_auth())
    assert resp.status_code == 200
    bin_items = resp.json()["cameras"]
    assert [c["id"] for c in bin_items] == [binned.id]
    assert bin_items[0]["path"]  # playback deep-link key


# ---- Deactivate / reactivate = reversible pause ----


def test_deactivate_unprovisions_and_reactivate_provisions(env):
    cam = env.make_camera()

    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}", json={"is_active": False}, headers=_auth()
    )
    assert resp.status_code == 200, resp.text
    assert env.unprovision_calls == [(cam.id, cam.ip_address)]
    env.db.expire_all()
    row = env.db.get(Camera, cam.id)
    assert row.is_active is False
    assert row.deleted_at is None  # paused, not binned

    # Still visible (and editable) with active_only unchecked.
    resp = env.client.get(
        "/api/v1/cameras/", params={"active_only": False}, headers=_auth()
    )
    assert cam.id in {c["id"] for c in resp.json()["cameras"]}

    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}", json={"is_active": True}, headers=_auth()
    )
    assert resp.status_code == 200, resp.text
    assert env.provision_calls == [cam.id]
    env.db.expire_all()
    assert env.db.get(Camera, cam.id).is_active is True


def test_deactivate_persists_flag_when_mediamtx_fails(env, monkeypatch):
    cam = env.make_camera()

    async def _boom(camera_id, camera_ip):
        raise RuntimeError("mediamtx down")

    monkeypatch.setattr(
        MediaMtxAdminService, "unprovision_path", staticmethod(_boom)
    )
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}", json={"is_active": False}, headers=_auth()
    )
    assert resp.status_code == 200, resp.text
    env.db.expire_all()
    assert env.db.get(Camera, cam.id).is_active is False


def test_update_without_is_active_touches_no_stream(env):
    cam = env.make_camera()
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}", json={"location": "lobby"}, headers=_auth()
    )
    assert resp.status_code == 200, resp.text
    assert env.unprovision_calls == []
    assert env.provision_calls == []


# ---- Duplicate detection on create ----


def _payload(**kw):
    p = {"name": "New Cam", "ip_address": "10.0.0.9", "port": 554}
    p.update(kw)
    return p


def test_duplicate_ip_conflicts_with_details(env):
    env.make_camera(name="Existing", ip_address="10.0.0.9")
    resp = env.client.post("/api/v1/cameras/", json=_payload(), headers=_auth())
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "duplicate_camera"
    assert [d["name"] for d in detail["duplicates"]] == ["Existing"]


def test_duplicate_rtsp_url_conflicts_even_on_other_ip(env):
    env.make_camera(
        name="Existing",
        ip_address="10.0.0.1",
        rtsp_url="rtsp://10.0.0.1:554/ch1",
    )
    resp = env.client.post(
        "/api/v1/cameras/",
        json=_payload(ip_address="10.0.0.2", rtsp_url="rtsp://10.0.0.1:554/ch1"),
        headers=_auth(),
    )
    assert resp.status_code == 409


def test_inactive_camera_still_counts_as_duplicate(env):
    env.make_camera(name="Paused", ip_address="10.0.0.9", is_active=False)
    resp = env.client.post("/api/v1/cameras/", json=_payload(), headers=_auth())
    assert resp.status_code == 409


def test_deleted_camera_does_not_count_as_duplicate(env):
    cam = env.make_camera(name="Binned", ip_address="10.0.0.9")
    env.client.delete(f"/api/v1/cameras/{cam.id}", headers=_auth())
    resp = env.client.post("/api/v1/cameras/", json=_payload(), headers=_auth())
    assert resp.status_code == 200, resp.text


def test_force_bypasses_duplicate_conflict(env):
    env.make_camera(name="Existing", ip_address="10.0.0.9")
    resp = env.client.post(
        "/api/v1/cameras/", params={"force": True}, json=_payload(), headers=_auth()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ip_address"] == "10.0.0.9"


# ---- `port` mirrors the RTSP URL ----
# `cameras.port` is the RTSP port, but nothing kept it in step with `rtsp_url`.
# A camera added with an explicit URL on 8554 kept the 554 default, so the
# cameras list rendered "host:554" — an address nothing answers on, and the
# first thing an operator checks when the stream drops.


def test_create_takes_port_from_the_rtsp_url(env):
    resp = env.client.post(
        "/api/v1/cameras/",
        json=_payload(ip_address="10.0.0.5", rtsp_url="rtsp://10.0.0.5:8554/"),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["port"] == 8554
    stored = env.db.query(Camera).filter(Camera.id == resp.json()["id"]).first()
    assert stored.port == 8554


def test_create_falls_back_to_the_scheme_default_port(env):
    """A URL with no port streams on 554 — say 554, not whatever was typed."""
    resp = env.client.post(
        "/api/v1/cameras/",
        json=_payload(ip_address="10.0.0.5", port=8000, rtsp_url="rtsp://10.0.0.5/ch1"),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["port"] == 554


def test_create_without_an_rtsp_url_keeps_the_given_port(env):
    """Nothing to derive from — the operator's value stands rather than being
    overwritten with a guess."""
    resp = env.client.post(
        "/api/v1/cameras/",
        json=_payload(ip_address="10.0.0.5", port=8000),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["port"] == 8000


def test_update_repoints_port_when_the_rtsp_url_moves(env):
    cam = env.make_camera(ip_address="10.0.0.5", port=554,
                          rtsp_url="rtsp://10.0.0.5:554/ch1")
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json={"rtsp_url": "rtsp://10.0.0.5:8554/ch1"},
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["port"] == 8554


def test_update_url_wins_over_a_port_sent_in_the_same_request(env):
    """MediaMTX pulls the URL, so the URL's port is the only one worth showing —
    the alternative is leaving the two fields contradicting each other."""
    cam = env.make_camera(ip_address="10.0.0.5", rtsp_url="rtsp://10.0.0.5:554/ch1")
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json={"rtsp_url": "rtsp://10.0.0.5:8554/ch1", "port": 554},
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["port"] == 8554


def test_update_leaves_port_alone_when_the_url_is_untouched(env):
    cam = env.make_camera(ip_address="10.0.0.5", port=8000,
                          rtsp_url="rtsp://10.0.0.5:8554/ch1")
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}", json={"name": "Renamed"}, headers=_auth()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["port"] == 8000
