# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Webcam cameras: a camera whose MediaMTX path is fed by a publisher.

A browser publishes over WHIP, or the host agent pushes a USB webcam over
RTSPS, into ``cam-<id>``; everything downstream reads the path as for any
camera. What these tests pin is everything that differs: the schema (no
address, URL or credentials; a placeholder IP), provisioning (``source:
publisher``, no substream, no transport policy), the IP-camera chores that
must be skipped (SSRF guard, duplicate check, ONVIF drivers), the
watchdog's view of an idle webcam, CSV export/import, and the two
WHIP route.

Run with:
    cd server && pytest tests/test_webcam_cameras.py -v
"""
from __future__ import annotations

import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

import asyncio
import json
import os
import secrets
import sys
import types as _types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.auth as core_auth  # noqa: E402
from core.camera_source import PUBLISH_TOKEN_MINUTES, PUBLISHER_IP_SENTINEL  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, CameraConfig, Permission, Role, RolePermission, User  # noqa: E402
from schemas import CameraCreate, CameraResponse  # noqa: E402

WEBCAM = {"name": "Front desk webcam", "source_type": "publisher"}


# ── Schema ──────────────────────────────────────────────────────────────


def test_a_webcam_gets_the_placeholder_address():
    c = CameraCreate(**WEBCAM)
    assert c.source_type == "publisher"
    assert c.ip_address == PUBLISHER_IP_SENTINEL
    assert c.rtsp_url is None


@pytest.mark.parametrize("extra", [
    {"ip_address": "192.168.1.20"},
    {"rtsp_url": "rtsp://192.168.1.20/s"},
    {"substream_url": "rtsp://192.168.1.20/sub"},
    {"username": "admin"},
    {"password": "secret"},
])
def test_a_webcam_refuses_anything_that_makes_it_an_ip_camera(extra):
    with pytest.raises(ValidationError) as exc:
        CameraCreate(**WEBCAM, **extra)
    assert next(iter(extra)) in str(exc.value)


def test_an_ip_camera_is_unchanged():
    c = CameraCreate(name="Gate", ip_address="10.0.0.7", rtsp_url="rtsp://10.0.0.7/s")
    assert c.source_type == "rtsp"
    with pytest.raises(ValidationError):
        CameraCreate(name="Gate")          # still needs an address


def test_a_camera_from_before_the_column_reads_as_rtsp():
    row = SimpleNamespace(
        id=1, name="Old", description=None, ip_address="10.0.0.7", port=554,
        username=None, rtsp_url="rtsp://10.0.0.7/s", substream_url=None,
        display_aspect_ratio=None, location=None, vlan=None, status="ok",
        manufacturer=None, model=None, firmware_version=None, serial_number=None,
        hardware_id=None, source_type=None, owner_id=1, is_active=True,
        created_at=datetime.now(UTC),
    )
    assert CameraResponse.model_validate(row).source_type == "rtsp"


# ── Provisioning payload ────────────────────────────────────────────────


def test_map_conf_for_a_webcam_is_a_publisher_path():
    from services.mediamtx_admin_service import MediaMtxAdminService

    conf = MediaMtxAdminService._map_conf({
        "source_type": "publisher",
        # Stray keys a generic caller might pass must not leak through.
        "source_url": "rtsp://10.0.0.7/s",
        "rtsp_transport": "tcp",
        "recording": {"enabled": True, "path": "/rec/%path/%Y-%m-%d/%H/%M-%S-%f",
                      "segment_seconds": 60},
    })
    assert conf["source"] == "publisher"
    assert "rtspTransport" not in conf
    assert conf["record"] is True
    assert conf["recordSegmentDuration"] == "60s"


def test_build_provision_config_for_a_webcam():
    from services.mediamtx_startup_service import MediaMtxStartupService

    cam = SimpleNamespace(source_type="publisher", rtsp_url=None, substream_url=None)
    cfg = SimpleNamespace(rtsp_transport="tcp", recording_enabled=False)
    out = MediaMtxStartupService.build_provision_config(cam, cfg, None)
    assert out["source_type"] == "publisher"
    assert "source_url" not in out
    assert out["recording"] == {"enabled": False}


def test_push_publisher_path_never_provisions_a_substream(monkeypatch):
    from services import mediamtx_admin_service as m

    sent = {}

    async def _upsert(camera_id, camera_ip, config, *, transport_security=None):
        sent.update(config=config, transport_security=transport_security)
        return {"status": "ok", "path": f"cam-{camera_id}"}

    monkeypatch.setattr(m.MediaMtxAdminService, "upsert_path", staticmethod(_upsert))
    res = asyncio.run(m.MediaMtxAdminService.push_publisher_path(
        7, PUBLISHER_IP_SENTINEL, enable_recording=True, recording_path="/rec/%path"))
    assert res["status"] == "ok"
    assert sent["config"]["source_type"] == "publisher"
    assert sent["config"]["recording"]["enabled"] is True
    assert sent["transport_security"] is None
    # The substream helper derives from the main source; a webcam has none.
    from services.camera_source_resolver import derive_substream_url

    assert derive_substream_url(sent["config"].get("source_url")) is None
    assert "substream" not in sent["config"]


def test_push_publisher_path_asks_for_the_sub_when_told(monkeypatch):
    from services import mediamtx_admin_service as m

    sent = {}

    async def _upsert(camera_id, camera_ip, config, *, transport_security=None):
        sent.update(config=config)
        return {"status": "ok", "path": f"cam-{camera_id}"}

    monkeypatch.setattr(m.MediaMtxAdminService, "upsert_path", staticmethod(_upsert))
    asyncio.run(m.MediaMtxAdminService.push_publisher_path(7, PUBLISHER_IP_SENTINEL, substream=True))
    assert sent["config"]["substream"] is True


def _capture_substream_calls(monkeypatch):
    """Run _provision_substream with both of its outcomes stubbed: the
    publisher sub helper and the HTTP client a pulled sub would use."""
    from services import mediamtx_admin_service as m

    calls = {"publisher_sub": [], "http": 0}

    async def _push(camera_id, camera_ip):
        calls["publisher_sub"].append(camera_id)

    class _NoHttp:
        def __init__(self, *a, **k):
            calls["http"] += 1
            raise AssertionError("a webcam's sub is never pulled from a source")

    monkeypatch.setattr(m.MediaMtxAdminService, "_push_publisher_substream", staticmethod(_push))
    monkeypatch.setattr(m.httpx, "AsyncClient", _NoHttp)
    return m.MediaMtxAdminService, calls


@pytest.mark.parametrize("extra", [{"substream": True}, {}])
def test_every_provisioner_creates_a_webcam_sub_when_asked(monkeypatch, extra):
    """#900: provision_path and replace_path (so create, edit, the
    provision route and the startup reconcile alike) reach the sub through
    _provision_substream. For a webcam it is a publisher path, made only
    when the config asks for it, and never derived from a source — not even
    from the row's substream_url marker, which names the sub itself."""
    svc, calls = _capture_substream_calls(monkeypatch)
    config = {"source_type": "publisher",
              "substream_url": "rtsp://mediamtx:8554/cam-7-sub", **extra}
    asyncio.run(svc._provision_substream(7, PUBLISHER_IP_SENTINEL, config))
    assert calls["publisher_sub"] == ([7] if extra else [])
    assert calls["http"] == 0


def test_the_provision_route_keeps_a_webcams_sub(monkeypatch):
    """POST /cameras/{id}/provision-mediamtx used to push a bare publisher
    path: no capture command, no sub (#900)."""
    from routers import cameras as r
    from services import mediamtx_admin_service as m

    cam = SimpleNamespace(id=7, ip_address=PUBLISHER_IP_SENTINEL, source_type="publisher",
                          source_device="USB Camera (046d:0825)", status="pending")
    sent = {}

    async def _push(**kwargs):
        sent.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(r.CameraService, "get_camera_by_id", staticmethod(lambda **_: cam))
    monkeypatch.setattr(m.MediaMtxAdminService, "push_publisher_path", staticmethod(_push))
    monkeypatch.setattr(r, "_log_provision_start", lambda *a, **k: None)
    monkeypatch.setattr(r, "_log_provision_result", lambda *a, **k: None)
    db = SimpleNamespace(commit=lambda: None)
    asyncio.run(r.provision_camera_mediamtx(
        7, db=db, current_user=SimpleNamespace(id=1), request=None))
    assert sent["substream"] is True
    assert sent["run_on_init"] is None


def _boot_cfg():
    return SimpleNamespace(source_url=None, rtsp_transport="tcp", recording_enabled=True,
                           recording_path=None, recording_segment_seconds=60)


def test_cores_boot_loop_asks_for_a_host_agent_webcams_sub():
    """main.py upserts every camera's path at each core start, from
    boot_path_config. It used to send a bare publisher path (#900)."""
    from services.mediamtx_startup_service import MediaMtxStartupService

    cam = SimpleNamespace(id=7, ip_address=PUBLISHER_IP_SENTINEL, source_type="publisher",
                          source_device="USB Camera (046d:0825)", rtsp_url=None,
                          substream_url="rtsp://mediamtx:8554/cam-7-sub")
    out = MediaMtxStartupService.boot_path_config(cam, _boot_cfg())
    assert out["source_type"] == "publisher"
    assert out["substream"] is True
    assert "run_on_init" not in out


def test_cores_boot_loop_keeps_a_linux_webcams_capture_command(monkeypatch):
    from services import local_webcams as lw
    from services.mediamtx_startup_service import MediaMtxStartupService

    monkeypatch.setattr(lw, "provisioning_for",
                        lambda cam: {"run_on_init": "ffmpeg …", "substream": True})
    cam = SimpleNamespace(id=7, ip_address=PUBLISHER_IP_SENTINEL, source_type="publisher",
                          source_device="/dev/v4l/by-id/usb-a-video-index0", rtsp_url=None,
                          substream_url=None)
    out = MediaMtxStartupService.boot_path_config(cam, _boot_cfg())
    assert out["run_on_init"] == "ffmpeg …" and out["substream"] is True


def test_cores_boot_loop_config_for_an_ip_camera_is_unchanged():
    from services.mediamtx_startup_service import MediaMtxStartupService

    cam = SimpleNamespace(id=9, ip_address="192.168.1.10", source_type="rtsp", source_device=None,
                          rtsp_url="rtsp://cam/main", substream_url=None)
    assert MediaMtxStartupService.boot_path_config(cam, _boot_cfg()) == {
        "source_url": "rtsp://cam/main", "source_type": "rtsp", "substream_url": None,
        "rtsp_transport": "tcp", "recording_enabled": True, "recording_path": None,
        "recording_segment_seconds": 60,
    }


def test_the_startup_reconcile_asks_for_a_host_agent_webcams_sub():
    from services.mediamtx_startup_service import MediaMtxStartupService

    cam = SimpleNamespace(id=7, ip_address=PUBLISHER_IP_SENTINEL, source_type="publisher",
                          source_device="USB Camera (046d:0825)", rtsp_url=None, substream_url=None)
    cfg = SimpleNamespace(rtsp_transport="tcp", recording_enabled=False)
    out = MediaMtxStartupService.build_provision_config(cam, cfg, None)
    assert out["source_type"] == "publisher"
    assert out["substream"] is True
    assert "run_on_init" not in out


# ── IP-camera chores that must be skipped ───────────────────────────────


def test_the_ssrf_guard_lets_a_webcam_through():
    """0.0.0.0 is refused as a host, so the guard must not see the
    placeholder as one — and nothing dials it."""
    from routers.cameras import _reject_external_camera_hosts

    _reject_external_camera_hosts(CameraCreate(**WEBCAM))   # must not raise


def test_a_webcam_never_resolves_a_camera_driver():
    from fastapi import HTTPException

    from services.camera_drivers import registry

    class _Q:
        def filter(self, *a):
            return self

        def first(self):
            return SimpleNamespace(id=3, source_type="publisher",
                                   ip_address=PUBLISHER_IP_SENTINEL)

    db = SimpleNamespace(query=lambda *_: _Q())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(registry.get_driver_for_camera(db, 3))
    assert exc.value.status_code == 409


def test_edit_form_resubmits_are_dropped_and_real_values_refused():
    from fastapi import HTTPException

    from routers.cameras import _strip_webcam_stream_fields

    cam = SimpleNamespace(ip_address=PUBLISHER_IP_SENTINEL, port=554, username=None,
                          password=None, rtsp_url=None, substream_url=None)
    fields = {"name": "New name", "ip_address": PUBLISHER_IP_SENTINEL, "port": 554,
              "rtsp_url": "", "substream_url": None, "username": ""}
    _strip_webcam_stream_fields(cam, fields)
    # "" against the stored NULL would read as "source removed" and tear
    # the path down: it must be gone, not passed on.
    assert fields == {"name": "New name"}

    with pytest.raises(HTTPException) as exc:
        _strip_webcam_stream_fields(cam, {"rtsp_url": "rtsp://10.0.0.9/s"})
    assert exc.value.status_code == 422


def test_inference_reads_a_webcam_through_the_tap_even_when_the_tap_is_off(monkeypatch):
    from core.config import settings
    from services.kai_c_service import KaiCService

    monkeypatch.setattr(settings, "inference_use_mediamtx_tap", False)
    monkeypatch.setattr(settings, "mediamtx_rtsp_url", "rtsp://mediamtx:8554")
    monkeypatch.setattr(settings, "mediamtx_path_mode", "id")
    svc = KaiCService.__new__(KaiCService)
    monkeypatch.setattr(svc, "_get_inference_mediamtx_jwt", lambda: "tok", raising=False)
    assert svc._resolve_inference_rtsp_url(5, "").startswith("rtsp://mediamtx:8554/cam-5")
    # An IP camera keeps its own URL while the tap is off.
    assert svc._resolve_inference_rtsp_url(5, "rtsp://10.0.0.7/s") == "rtsp://10.0.0.7/s"


# ── Watchdog ────────────────────────────────────────────────────────────


def test_watchdog_judges_a_webcam_only_once_its_publisher_has_been_up(monkeypatch):
    from services import camera_status_service as css
    from services.recording_watchdog import STALL_THRESHOLD_SECONDS, _webcam_publishing_ids

    now = datetime.now(UTC)
    online = {1: True, 2: True, 3: False}
    since = {1: now - timedelta(seconds=STALL_THRESHOLD_SECONDS + 5),
             2: now - timedelta(seconds=30), 3: None}
    fake = SimpleNamespace(snapshot=lambda ids: {i: online.get(i) for i in ids},
                           online_since=lambda ids: {i: since.get(i) for i in ids})
    monkeypatch.setattr(css, "get_camera_status_service", lambda: fake)
    cams = [SimpleNamespace(id=i, source_type="publisher") for i in (1, 2, 3)]
    cams.append(SimpleNamespace(id=4, source_type=None))
    # 1: publishing long enough. 2: just connected (no segment closed yet).
    # 3: nobody publishing. 4: an IP camera — not this helper's business.
    assert _webcam_publishing_ids(cams, now) == {1}


# ── CSV export / import ────────────────────────────────────────────────


def test_export_leaves_webcams_out_and_import_never_matches_one():
    from services import camera_io

    ip_cam = SimpleNamespace(name="Gate", location=None, ip_address="10.0.0.7", port=554,
                             username=None, password=None, rtsp_url="rtsp://10.0.0.7/s",
                             substream_url=None, description=None, vlan=None,
                             is_active=True, source_type="rtsp")
    webcam = SimpleNamespace(name="Desk", location=None, ip_address=PUBLISHER_IP_SENTINEL,
                             port=554, username=None, password=None, rtsp_url=None,
                             substream_url=None, description=None, vlan=None,
                             is_active=True, source_type="publisher", id=9)
    rows = camera_io.export_rows([ip_cam, webcam], include_passwords=False)
    assert [r[0] for r in rows] == ["Gate"]

    planned = camera_io.plan_rows([(2, {"name": "Desk", "ip_address": PUBLISHER_IP_SENTINEL})])
    camera_io.match_existing(planned, [webcam], None)
    assert all(p.status != "update" for p in planned)


# ── Routes ──────────────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch):
    from routers import cameras as cameras_router
    from routers import streams as streams_router
    from services import audit_service
    from services import camera_service as cs
    from services import mediamtx_jwt_service as jwt_mod

    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(cameras_router, "camera_logger", _L(), raising=False)
    monkeypatch.setattr(cs, "camera_logger", _L(), raising=False)
    monkeypatch.setattr(cameras_router, "audit_request", lambda *a, **k: None)
    monkeypatch.setattr(audit_service, "audit_request", lambda *a, **k: None)
    monkeypatch.setattr(cameras_router, "_record_audit_log", lambda *a, **k: None)

    provisioned: list[dict] = []

    async def _push_publisher_path(**kw):
        provisioned.append(kw)
        return {"status": "ok", "path": f"cam-{kw['camera_id']}"}

    monkeypatch.setattr(cs.MediaMtxAdminService, "push_publisher_path",
                        staticmethod(_push_publisher_path))

    async def _no_rtsp(**kw):  # an RTSP provision here would be the bug
        raise AssertionError("a webcam must not be provisioned as an RTSP stream")

    monkeypatch.setattr(cs.MediaMtxAdminService, "push_rtsp_stream", staticmethod(_no_rtsp))
    import services.camera_identity as ci
    import services.skill_assignments as sa

    monkeypatch.setattr(ci, "protect_camera_dir", lambda *a, **k: "fresh")
    monkeypatch.setattr(sa, "adopt_new_camera", lambda *a, **k: False)

    def _token(camera_id, camera_path, expiry_minutes=1440):
        # Stand-in for the RSA-signed JWT: the claims the real one carries.
        return json.dumps({"perm": [{"action": "publish", "path": camera_path}],
                           "camera_id": camera_id, "minutes": expiry_minutes})

    monkeypatch.setattr(jwt_mod.MediaMtxJwtService, "create_publish_token",
                        classmethod(lambda cls, **kw: _token(**kw)))

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng, expire_on_commit=False)
    db = SessionLocal()
    manage = Permission(name="cameras.manage", description="")
    viewer_role = Role(name="viewer", description="")
    operator_role = Role(name="operator", description="")
    db.add_all([manage, viewer_role, operator_role])
    db.flush()
    db.add(RolePermission(role_id=operator_role.id, permission_id=manage.id))
    users = {}
    for name, role in (("viewer", viewer_role), ("operator", operator_role)):
        u = User(username=name, email=f"{name}@x", hashed_password="x", is_active=True,
                 is_superuser=False, password_set=True, role_id=role.id)
        db.add(u)
        db.flush()
        users[name] = u
    db.commit()
    for u in users.values():
        db.refresh(u)
        _ = [p.name for p in (u.role.permissions or [])]
        db.expunge(u)
    db.close()

    app = FastAPI()
    app.include_router(cameras_router.router, prefix="/api/v1")
    app.include_router(streams_router.router, prefix="/api/v1")

    def _db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    current = {"user": users["operator"]}
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[core_auth.get_current_active_user] = lambda: current["user"]
    with TestClient(app) as tc:
        yield SimpleNamespace(tc=tc, current=current, users=users,
                              SessionLocal=SessionLocal, provisioned=provisioned)


def _add_webcam(env, name="Desk"):
    r = env.tc.post("/api/v1/cameras/", json={**WEBCAM, "name": name})
    assert r.status_code == 200, r.text
    return r.json()


def test_creating_a_webcam_provisions_a_publisher_path(env):
    body = _add_webcam(env)
    assert body["source_type"] == "publisher"
    assert body["ip_address"] == PUBLISHER_IP_SENTINEL
    assert body["mediamtx_provisioned"] is True
    assert env.provisioned[0]["enable_recording"] is True
    s = env.SessionLocal()
    cfg = s.query(CameraConfig).filter(CameraConfig.camera_id == body["id"]).one()
    assert cfg.recording_enabled is True and cfg.source_url is None
    s.close()


def test_a_second_webcam_is_not_a_duplicate(env):
    """Every webcam shares the placeholder IP; the IP duplicate check would
    otherwise answer 409 for the second one."""
    _add_webcam(env, "Desk")
    _add_webcam(env, "Lobby")


HOST_DEVICE = "dshow:@device_pnp_usb_vid_0c45"


def test_one_server_webcam_feeds_one_camera(env):
    """A second camera on the same device would be a second capture that
    fails "device busy" for ever. Refused, whatever ``force`` says."""
    first = env.tc.post("/api/v1/cameras/", json={**WEBCAM, "name": "Desk", "source_device": HOST_DEVICE})
    assert first.status_code == 200, first.text
    for q in ("", "?force=true"):
        r = env.tc.post(f"/api/v1/cameras/{q}", json={**WEBCAM, "name": "Desk 2", "source_device": HOST_DEVICE})
        assert r.status_code == 409, r.text
        assert "Desk" in r.json()["detail"]


def test_a_binned_cameras_webcam_is_free_again(env):
    first = env.tc.post("/api/v1/cameras/", json={**WEBCAM, "name": "Desk", "source_device": HOST_DEVICE}).json()
    assert env.tc.delete(f"/api/v1/cameras/{first['id']}").status_code in (200, 204)
    r = env.tc.post("/api/v1/cameras/", json={**WEBCAM, "name": "Desk again", "source_device": HOST_DEVICE})
    assert r.status_code == 200, r.text


def test_switching_a_camera_to_a_webcam_in_use_is_refused(env):
    env.tc.post("/api/v1/cameras/", json={**WEBCAM, "name": "Desk", "source_device": HOST_DEVICE})
    other = env.tc.post("/api/v1/cameras/", json={**WEBCAM, "name": "Lobby",
                                                 "source_device": "dshow:@device_pnp_usb_vid_30c9"}).json()
    r = env.tc.put(f"/api/v1/cameras/{other['id']}", json={"source_device": HOST_DEVICE})
    assert r.status_code == 409, r.text
    # Its own webcam is not "in use" by itself.
    r = env.tc.put(f"/api/v1/cameras/{other['id']}", json={"source_device": "dshow:@device_pnp_usb_vid_30c9"})
    assert r.status_code == 200, r.text


def test_editing_a_webcam_with_the_form_keeps_its_path(env):
    cam = _add_webcam(env)
    r = env.tc.put(f"/api/v1/cameras/{cam['id']}", json={
        "name": "Desk (renamed)", "ip_address": PUBLISHER_IP_SENTINEL, "port": 554,
        "rtsp_url": "", "substream_url": "", "username": "", "is_active": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Desk (renamed)"
    assert r.json()["stream_action"] in (None, "none")


def test_whip_session_is_a_short_publish_only_token_for_this_path(env):
    cam = _add_webcam(env)
    r = env.tc.post(f"/api/v1/streams/{cam['id']}/whip")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["whip_url"].endswith(f"/cam-{cam['id']}/whip")
    claims = json.loads(body["token"])
    assert claims["perm"] == [{"action": "publish", "path": f"cam-{cam['id']}"}]
    assert claims["minutes"] == PUBLISH_TOKEN_MINUTES


def test_whip_needs_cameras_manage_and_a_webcam(env):
    cam = _add_webcam(env)
    env.current["user"] = env.users["viewer"]
    assert env.tc.post(f"/api/v1/streams/{cam['id']}/whip").status_code == 403

    env.current["user"] = env.users["operator"]
    s = env.SessionLocal()
    ip = Camera(name="Gate", ip_address="10.0.0.7", port=554, rtsp_url="rtsp://10.0.0.7/s",
                owner_id=env.users["operator"].id, is_active=True)
    s.add(ip)
    s.commit()
    ip_id = ip.id
    s.close()
    assert env.tc.post(f"/api/v1/streams/{ip_id}/whip").status_code == 404


def test_startup_reconcile_includes_webcams(env):
    """A webcam has no URL, but its path must still exist after a restart
    or nothing can publish into it."""
    from services.mediamtx_startup_service import MediaMtxStartupService

    cam = _add_webcam(env)
    s = env.SessionLocal()
    s.add(Camera(name="No source", ip_address="10.0.0.8", port=554,
                 owner_id=env.users["operator"].id, is_active=True))
    s.commit()
    ids = [c.id for c, _ in MediaMtxStartupService._get_provisionable_cameras(s)]
    s.close()
    assert ids == [cam["id"]]
