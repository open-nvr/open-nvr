# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Editing a camera must re-point the MediaMTX path.

Before this, PUT /cameras/{id} touched MediaMTX only on an is_active
transition: a corrected RTSP URL, a moved camera or a rotated password was
written to the database and the media server kept pulling from the old source
forever. Not even a restart fixed it — the startup re-provisioner read
CameraConfig.source_url, a column written once at create time.

These tests pin the new contract:

* A change to what the camera streams FROM (rtsp_url, substream_url, or the
  derived path name) re-provisions MediaMTX, and does so TRANSACTIONALLY: the
  media server is updated before the row is committed, so a refusal rejects
  the whole edit rather than leaving the two disagreeing.
* Credential edits are folded into the URL, because that is the only place
  MediaMTX reads them from.
* Edits that change nothing MediaMTX can see (name, location, port) cost no
  round trip.
* is_active transitions stay best-effort — the flag is the source of truth and
  a MediaMTX hiccup must never block a pause or a resume.
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

os.environ.setdefault("DATABASE_URL", "sqlite:///./_camera_edit_test.db")
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
# Force-assign (not setdefault): an earlier-collected test may have installed a
# narrower stub without a __getattr__ fallback, which breaks
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
from core.config import settings  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, CameraConfig, Permission, Role, RolePermission, User  # noqa: E402
from routers import cameras as cameras_router  # noqa: E402
from services.mediamtx_admin_service import MediaMtxAdminService  # noqa: E402
from services.mediamtx_startup_service import MediaMtxStartupService  # noqa: E402
from services.transport_probe_service import TransportPolicyViolation  # noqa: E402

PASSWORD = "Str0ng!passw0rd"
OLD_URL = "rtsp://admin:secret@10.0.0.9:554/Streaming/Channels/101"
NEW_URL = "rtsp://admin:secret@10.0.0.9:554/Streaming/Channels/201"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(cameras_router, "camera_logger", _L(), raising=False)
    # Default path mode, restored per-test by monkeypatch. Pinned rather than
    # inherited so a deployment-flavoured .env can't flip these tests.
    monkeypatch.setattr(settings, "mediamtx_path_mode", "id", raising=False)
    monkeypatch.setattr(settings, "mediamtx_stream_prefix", "cam-", raising=False)

    # One ordered log for every MediaMTX call, so tests can assert not just
    # *what* was called but in what order — the re-path case must create the
    # new path before deleting the old one.
    calls: list[tuple] = []
    # Each entry is either a dict (returned as-is) or an Exception (raised).
    upsert_results: list = []

    async def _fake_upsert(camera_id, camera_ip, config, *, transport_security=None):
        calls.append(("upsert", camera_id, camera_ip, dict(config)))
        outcome = upsert_results.pop(0) if upsert_results else {"status": "ok"}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def _fake_unprovision(camera_id, camera_ip):
        calls.append(("unprovision", camera_id, camera_ip))
        return {"status": "ok"}

    async def _fake_provision_by_id(camera_id, force=False):
        calls.append(("provision_by_id", camera_id))
        return {"status": "success"}

    monkeypatch.setattr(
        MediaMtxAdminService, "upsert_path", staticmethod(_fake_upsert)
    )
    monkeypatch.setattr(
        MediaMtxAdminService, "unprovision_path", staticmethod(_fake_unprovision)
    )
    monkeypatch.setattr(
        MediaMtxStartupService,
        "provision_camera_by_id",
        staticmethod(_fake_provision_by_id),
    )
    monkeypatch.setattr(camera_identity, "protect_camera_dir", lambda *a, **k: "match")

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
    # Editing a camera is gated on cameras.manage as well as ownership.
    manage = Permission(name="cameras.manage", description="")
    db.add(manage)
    db.flush()
    db.add(RolePermission(role_id=role.id, permission_id=manage.id))
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

    def make_camera(with_config=True, **kw) -> Camera:
        defaults = dict(
            name="Front Door",
            ip_address="10.0.0.9",
            port=554,
            username="admin",
            owner_id=owner.id,
            is_active=True,
            status="provisioned",
            rtsp_url=OLD_URL,
        )
        defaults.update(kw)
        password = defaults.pop("password", "secret")
        cam = Camera(**defaults)
        cam.password = password
        db.add(cam)
        db.commit()
        db.refresh(cam)
        if with_config:
            db.add(
                CameraConfig(
                    camera_id=cam.id,
                    stream_protocol="rtsp",
                    source_url=cam.rtsp_url,
                    recording_enabled=True,
                    rtsp_transport="tcp",
                    recording_segment_seconds=60,
                )
            )
            db.commit()
        return cam

    client = TestClient(app)
    try:
        yield _types.SimpleNamespace(
            client=client,
            db=db,
            owner=owner,
            make_camera=make_camera,
            calls=calls,
            upsert_results=upsert_results,
        )
    finally:
        db.close()


def _auth(username: str = "owner") -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': username})}"}


def _kinds(calls) -> list[str]:
    return [c[0] for c in calls]


def _config_of(env, cam_id) -> CameraConfig:
    env.db.expire_all()
    return (
        env.db.query(CameraConfig).filter(CameraConfig.camera_id == cam_id).first()
    )


# The payload the real edit form sends: every field, every time, changed or
# not. Tests that assert "no re-provision" MUST go through this — asserting
# against a one-key body would pass even if the router keyed off field
# presence instead of a value diff, which is the bug this guards.
def _full_form(cam, **overrides) -> dict:
    body = {
        "name": cam.name,
        "ip_address": cam.ip_address,
        "port": cam.port,
        "username": cam.username,
        "rtsp_url": cam.rtsp_url,
        "substream_url": cam.substream_url,
        "display_aspect_ratio": cam.display_aspect_ratio,
        "is_active": cam.is_active,
    }
    body.update(overrides)
    return body


# ---- A changed source reaches MediaMTX ----


def test_rtsp_url_change_reprovisions_and_syncs_config(env):
    cam = env.make_camera()
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, rtsp_url=NEW_URL),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text

    assert _kinds(env.calls) == ["upsert"]
    assert env.calls[0][3]["source_url"] == NEW_URL

    env.db.expire_all()
    assert env.db.get(Camera, cam.id).rtsp_url == NEW_URL
    # The column the startup re-provisioner reads. Leaving this stale is what
    # made an edited URL revert on the next restart.
    assert _config_of(env, cam.id).source_url == NEW_URL
    assert resp.json()["stream_action"] == "reprovision"


def test_substream_url_change_reprovisions(env):
    cam = env.make_camera()
    sub = "rtsp://admin:secret@10.0.0.9:554/Streaming/Channels/102"
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, substream_url=sub),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert _kinds(env.calls) == ["upsert"]
    # The sub URL must reach the provisioner, or _provision_substream falls
    # back to guessing it from vendor convention.
    assert env.calls[0][3]["substream_url"] == sub


def test_password_change_rewrites_url_credentials_and_reprovisions(env):
    """MediaMTX authenticates from the userinfo embedded in the source URL, and
    the edit form re-submits the URL it was prefilled with. Without the rewrite
    a rotated password would be stored and never take effect."""
    cam = env.make_camera()
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, password="rotated"),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text

    env.db.expire_all()
    row = env.db.get(Camera, cam.id)
    assert row.rtsp_url == "rtsp://admin:rotated@10.0.0.9:554/Streaming/Channels/101"
    assert _kinds(env.calls) == ["upsert"]
    assert env.calls[0][3]["source_url"] == row.rtsp_url


def test_username_cleared_strips_userinfo(env):
    cam = env.make_camera()
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, username=None),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    env.db.expire_all()
    assert (
        env.db.get(Camera, cam.id).rtsp_url
        == "rtsp://10.0.0.9:554/Streaming/Channels/101"
    )


# ---- Edits MediaMTX cannot see cost nothing ----


def test_non_stream_field_change_touches_no_stream(env):
    """The form re-sends ip/port/username/rtsp_url unchanged on every save, so
    keying off field *presence* would re-provision on a rename."""
    cam = env.make_camera()
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, name="Back Door", location="lobby"),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert env.calls == []
    assert resp.json()["stream_action"] == "none"


def test_port_only_change_does_not_reprovision(env):
    """Camera.port is create-time metadata for source resolution; the port
    MediaMTX actually dials lives inside rtsp_url."""
    cam = env.make_camera()
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}", json=_full_form(cam, port=8554), headers=_auth()
    )
    assert resp.status_code == 200, resp.text
    assert env.calls == []


def test_display_aspect_change_does_not_reprovision(env):
    """The display aspect is a rendering hint the browser applies — it never
    reaches MediaMTX. Bouncing the stream to change how a tile is shaped would
    drop every viewer for nothing (issue #354)."""
    cam = env.make_camera()
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, display_aspect_ratio="16:9"),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert env.calls == []
    assert resp.json()["stream_action"] in (None, "none")

    env.db.expire_all()
    assert env.db.get(Camera, cam.id).display_aspect_ratio == "16:9"


def test_display_aspect_auto_is_stored_as_null(env):
    """"auto" and "" are spellings of "no override"; the DB keeps exactly one
    representation of it so the frontend never has to test for three."""
    cam = env.make_camera()
    env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, display_aspect_ratio="16:9"),
        headers=_auth(),
    )
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, display_aspect_ratio="auto"),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    env.db.expire_all()
    assert env.db.get(Camera, cam.id).display_aspect_ratio is None


def test_display_aspect_rejects_junk(env):
    cam = env.make_camera()
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, display_aspect_ratio="1080N"),
        headers=_auth(),
    )
    assert resp.status_code == 422, resp.text
    assert env.calls == []


def test_ip_change_in_id_path_mode_does_not_repath(env, monkeypatch):
    """In the default mode the path is cam-<id>: the address never reaches
    MediaMTX as data, so moving a camera costs no round trip."""
    monkeypatch.setattr(settings, "mediamtx_path_mode", "id", raising=False)
    cam = env.make_camera()
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, ip_address="10.0.0.77"),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert env.calls == []


# ---- Path rename (MEDIAMTX_PATH_MODE=ip) ----


def test_ip_change_in_ip_path_mode_creates_new_path_then_tears_down_old(
    env, monkeypatch
):
    """The path name is derived from the address in this mode, so an IP edit is
    a rename. Create-new-then-delete-old, never the reverse: delete + re-add is
    not atomic and a lost race leaves the camera with no path at all (#218)."""
    monkeypatch.setattr(settings, "mediamtx_path_mode", "ip", raising=False)
    cam = env.make_camera()
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, ip_address="10.0.0.77"),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text

    assert _kinds(env.calls) == ["upsert", "unprovision"]
    assert env.calls[0][2] == "10.0.0.77"  # new path keyed on the new address
    assert env.calls[1][1:] == (cam.id, "10.0.0.9")  # old path, old address
    assert resp.json()["stream_action"] == "repathed"


def test_clearing_the_rtsp_url_tears_the_path_down(env):
    """Removing the source must stop the stream. Leaving the path up would keep
    MediaMTX pulling and recording from a camera the database says has no
    source at all."""
    cam = env.make_camera()
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, rtsp_url=None),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert env.calls == [("unprovision", cam.id, "10.0.0.9")]
    assert resp.json()["stream_action"] == "unprovision"
    assert _config_of(env, cam.id).source_url is None


# ---- Failure rejects the whole edit ----


def test_reprovision_failure_rolls_the_edit_back(env):
    cam = env.make_camera()
    env.upsert_results.append({"status": "error", "details": {"error": "boom"}})

    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, rtsp_url=NEW_URL, name="Renamed"),
        headers=_auth(),
    )
    assert resp.status_code == 502, resp.text
    assert "boom" in resp.json()["detail"]

    # Nothing was written — not the URL, and not the unrelated rename that
    # rode along in the same request.
    env.db.expire_all()
    row = env.db.get(Camera, cam.id)
    assert row.rtsp_url == OLD_URL
    assert row.name == "Front Door"
    assert _config_of(env, cam.id).source_url == OLD_URL


def test_transport_policy_violation_rejects_with_409_and_no_write(env):
    cam = env.make_camera()
    env.upsert_results.append(
        TransportPolicyViolation(policy="rtsps_required", scheme="rtsp")
    )

    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, rtsp_url=NEW_URL),
        headers=_auth(),
    )
    assert resp.status_code == 409, resp.text

    env.db.expire_all()
    assert env.db.get(Camera, cam.id).rtsp_url == OLD_URL


def test_no_admin_api_still_saves_the_edit(env):
    """A deployment without the MediaMTX admin API has nothing to provision;
    editing must still work rather than 502 on every save."""
    cam = env.make_camera()
    env.upsert_results.append({"status": "no_admin_api"})

    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, rtsp_url=NEW_URL),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    env.db.expire_all()
    assert env.db.get(Camera, cam.id).rtsp_url == NEW_URL


# ---- Interaction with the is_active transitions ----


def test_deactivate_with_ip_change_tears_down_the_old_path(env, monkeypatch):
    """Pausing wins over re-provisioning, and the teardown must use the address
    the live path was actually created with."""
    monkeypatch.setattr(settings, "mediamtx_path_mode", "ip", raising=False)
    cam = env.make_camera()
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, ip_address="10.0.0.77", is_active=False),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert env.calls == [("unprovision", cam.id, "10.0.0.9")]
    assert resp.json()["stream_action"] == "deactivate"


def test_reactivate_with_url_change_provisions_exactly_once(env):
    cam = env.make_camera(is_active=False)
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, rtsp_url=NEW_URL, is_active=True),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    # The transactional branch handles it; provision_camera_by_id must NOT also
    # run, or the camera is provisioned twice per edit.
    assert _kinds(env.calls) == ["upsert"]
    assert env.calls[0][3]["source_url"] == NEW_URL


def test_url_change_on_paused_camera_touches_nothing_but_syncs_config(env):
    """A paused camera has no path — a URL edit must not resurrect it. The new
    source still has to be recorded, so resume/restart picks it up."""
    cam = env.make_camera(is_active=False)
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, rtsp_url=NEW_URL),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert env.calls == []
    assert _config_of(env, cam.id).source_url == NEW_URL


def test_pure_reactivation_stays_best_effort(env, monkeypatch):
    """The flag is the source of truth: a MediaMTX failure warns, it does not
    reject the resume."""

    async def _boom(camera_id, force=False):
        raise RuntimeError("mediamtx down")

    monkeypatch.setattr(
        MediaMtxStartupService, "provision_camera_by_id", staticmethod(_boom)
    )
    cam = env.make_camera(is_active=False)
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}", json={"is_active": True}, headers=_auth()
    )
    assert resp.status_code == 200, resp.text
    env.db.expire_all()
    assert env.db.get(Camera, cam.id).is_active is True
    body = resp.json()
    assert body["stream_action"] == "activate"
    assert "mediamtx down" in body["stream_warning"]


def test_camera_without_config_row_still_reprovisions(env):
    """A camera with an RTSP URL but no CameraConfig is a data anomaly the
    provisioner already tolerates; the edit path must not crash on it."""
    cam = env.make_camera(with_config=False)
    resp = env.client.put(
        f"/api/v1/cameras/{cam.id}",
        json=_full_form(cam, rtsp_url=NEW_URL),
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert _kinds(env.calls) == ["upsert"]
