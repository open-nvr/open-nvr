# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The host agent's side of core (services/host_agent.py, routers/host_agent.py).

What these pin: the key (a file holding only its sha256, re-read when it
changes), that the agent routes are refused through the reverse proxy even
with the right key, that sync hands out only the cameras fed by a USB webcam
on the host, that a publish token exists only for those, that the slow part
of a report is stored only when it changes, and that the monitor prefers
the host's own metrics while they are fresh.

Run with:
    cd server && pytest tests/test_host_agent.py -v
"""
from __future__ import annotations

import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

import hashlib
import json
import os
import secrets
import sys
import time
import types as _types
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
from core.database import Base, get_db  # noqa: E402
from models import Camera, Permission, Role, RolePermission, User  # noqa: E402
from schemas import CameraCreate  # noqa: E402
from services import host_agent  # noqa: E402

KEY = "ohk_" + "k" * 43


def _write_hash(path: Path, key: str) -> None:
    path.write_text(hashlib.sha256(key.encode()).hexdigest() + "\n", encoding="utf-8")
    # A rotation must be seen even within the filesystem's mtime granularity.
    t = time.time() + (os.stat(path).st_mtime_ns % 7) + 1
    os.utime(path, (t, t))


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    p = tmp_path / "agent.key.sha256"
    _write_hash(p, KEY)
    monkeypatch.setenv(host_agent.KEY_HASH_FILE_ENV, str(p))
    host_agent.state.reset()
    yield p
    host_agent.state.reset()


# ── Key and proxy ───────────────────────────────────────────────────────


def test_the_key_is_checked_against_the_stored_hash(key_file):
    assert host_agent.verify_key(KEY)
    assert not host_agent.verify_key(KEY + "x")
    assert not host_agent.verify_key(None)
    assert not host_agent.verify_key("")


def test_a_rotated_key_takes_effect_without_a_restart(key_file):
    assert host_agent.verify_key(KEY)
    new = "ohk_" + "n" * 43
    _write_hash(key_file, new)
    assert host_agent.verify_key(new)
    assert not host_agent.verify_key(KEY)


def test_no_key_set_up_means_nothing_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv(host_agent.KEY_HASH_FILE_ENV, str(tmp_path / "missing"))
    assert not host_agent.verify_key(KEY)


def test_proxied_requests_are_recognised():
    assert host_agent.came_through_proxy({"x-forwarded-for": "192.168.1.5"})
    assert host_agent.came_through_proxy({"x-real-ip": "192.168.1.5"})
    assert not host_agent.came_through_proxy({"content-type": "application/json"})


# ── Schema ──────────────────────────────────────────────────────────────


def test_source_device_is_only_for_a_webcam():
    c = CameraCreate(name="Desk", source_type="publisher", source_device="usb:hp")
    assert c.source_device == "usb:hp"
    with pytest.raises(ValidationError):
        CameraCreate(name="Gate", ip_address="10.0.0.7", rtsp_url="rtsp://10.0.0.7/s",
                     source_device="usb:hp")


# ── Monitor ─────────────────────────────────────────────────────────────


def test_the_monitor_prefers_fresh_host_metrics_and_falls_back(key_file, monkeypatch):
    from services.system_monitor_service import SystemMonitorService

    host_agent.state.update({"metrics": {
        "cpu_percent": 42.0, "cpu_count": 8,
        "memory_total_bytes": 32 * 2**30, "memory_used_bytes": 8 * 2**30,
        "disks": [{"mount": "D:\\", "total_bytes": 10, "free_bytes": 4}],
        "temperatures": [{"label": "cpu", "celsius": 71.0}],
    }})
    out = {"measured_by": "core", "cpu_percent": 97.0, "memory": None}
    SystemMonitorService._apply_host_agent_metrics(out)
    assert out["measured_by"] == "host-agent"
    assert out["cpu_percent"] == 42.0
    assert out["memory"]["percent"] == 25.0
    assert out["temperatures"][0]["celsius"] == 71.0

    # Stale: the container's own numbers stay.
    host_agent.state.last_seen = time.time() - host_agent.METRICS_FRESH_S - 1
    out = {"measured_by": "core", "cpu_percent": 97.0, "memory": None}
    SystemMonitorService._apply_host_agent_metrics(out)
    assert out == {"measured_by": "core", "cpu_percent": 97.0, "memory": None}


# ── Routes ──────────────────────────────────────────────────────────────


REPORT = {
    "version": "0.1.10+abc", "os": "windows", "arch": "amd64", "hostname": "nvr-1",
    "ffmpeg": {"found": True, "version": "7.1"},
    "webcams": [{"id": "dshow:hp", "name": "HP HD Camera"},
                {"id": "dshow:usb", "name": "USB Camera"}],
    "publishers": [{"camera_id": 1, "state": "live"}],
    "metrics": {"cpu_percent": 12.5},
}


@pytest.fixture
def env(key_file, monkeypatch):
    from routers import cameras as cameras_router
    from routers import host_agent as host_agent_router
    from services import audit_service
    from services import mediamtx_jwt_service as jwt_mod

    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(cameras_router, "camera_logger", _L(), raising=False)
    monkeypatch.setattr(audit_service, "audit_request", lambda *a, **k: None)
    monkeypatch.setattr(
        jwt_mod.MediaMtxJwtService, "create_publish_token",
        classmethod(lambda cls, **kw: json.dumps({"path": kw["camera_path"],
                                                  "minutes": kw["expiry_minutes"]})),
    )
    host_agent_router._calls.clear()

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
    owner = users["operator"].id
    cams = {
        "usb": Camera(name="Desk", ip_address="0.0.0.0", port=554, owner_id=owner,
                      is_active=True, source_type="publisher", source_device="dshow:hp"),
        "browser": Camera(name="Laptop", ip_address="0.0.0.0", port=554, owner_id=owner,
                          is_active=True, source_type="publisher"),
        "paused": Camera(name="Paused", ip_address="0.0.0.0", port=554, owner_id=owner,
                         is_active=False, source_type="publisher", source_device="dshow:usb"),
        "ip": Camera(name="Gate", ip_address="10.0.0.7", port=554, owner_id=owner,
                     is_active=True, rtsp_url="rtsp://10.0.0.7/s"),
    }
    db.add_all(cams.values())
    db.commit()
    ids = {k: c.id for k, c in cams.items()}
    for u in users.values():
        db.refresh(u)
        _ = [p.name for p in (u.role.permissions or [])]
        db.expunge(u)
    db.close()

    app = FastAPI()
    app.include_router(host_agent_router.router, prefix="/api/v1")
    app.include_router(cameras_router.router, prefix="/api/v1")

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
        yield SimpleNamespace(tc=tc, ids=ids, users=users, current=current,
                              SessionLocal=SessionLocal)


H = {"X-Host-Agent-Key": KEY}


def test_sync_needs_the_key_and_hides_why(env):
    for headers in ({}, {"X-Host-Agent-Key": "ohk_wrong"}):
        r = env.tc.post("/api/v1/host-agent/sync", json=REPORT, headers=headers)
        assert r.status_code == 404


def test_the_right_key_through_the_proxy_is_refused(env):
    r = env.tc.post("/api/v1/host-agent/sync", json=REPORT,
                    headers={**H, "X-Forwarded-For": "192.168.1.50"})
    assert r.status_code == 404


def test_sync_hands_out_only_active_usb_webcams(env):
    r = env.tc.post("/api/v1/host-agent/sync", json=REPORT, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [d["camera_id"] for d in body["desired"]] == [env.ids["usb"]]
    assert body["desired"][0]["device_id"] == "dshow:hp"
    assert body["poll_after_s"] == host_agent.POLL_AFTER_S
    assert body["core_build"]
    assert host_agent.state.connected()


def test_a_report_is_stored_only_when_it_changes(env, monkeypatch):
    from services import site_settings

    writes = []
    real = site_settings.set_json
    monkeypatch.setattr(site_settings, "set_json",
                        lambda db, k, v: (writes.append(k), real(db, k, v)))
    for _ in range(3):
        env.tc.post("/api/v1/host-agent/sync", json=REPORT, headers=H)
    changed = {**REPORT, "webcams": REPORT["webcams"][:1]}
    env.tc.post("/api/v1/host-agent/sync", json=changed, headers=H)
    # Metrics change every poll; only a new webcam list is worth a write.
    metrics_only = {**changed, "metrics": {"cpu_percent": 99.0}}
    env.tc.post("/api/v1/host-agent/sync", json=metrics_only, headers=H)
    assert writes == [host_agent.REPORT_SETTING_KEY] * 2


def test_publish_token_only_for_a_camera_the_agent_feeds(env):
    ok = env.tc.post(f"/api/v1/host-agent/publish-token/{env.ids['usb']}", headers=H)
    assert ok.status_code == 200, ok.text
    claims = json.loads(ok.json()["token"])
    assert claims == {"path": f"cam-{env.ids['usb']}", "minutes": 5}
    for other in ("browser", "paused", "ip"):
        r = env.tc.post(f"/api/v1/host-agent/publish-token/{env.ids[other]}", headers=H)
        assert r.status_code == 404, other
    r = env.tc.post(f"/api/v1/host-agent/publish-token/{env.ids['usb']}",
                    headers={"X-Host-Agent-Key": "ohk_wrong"})
    assert r.status_code == 404


def test_the_web_app_sees_webcams_and_what_they_feed(env):
    env.tc.post("/api/v1/host-agent/sync", json=REPORT, headers=H)
    r = env.tc.get("/api/v1/host-agent")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connected"] is True and body["installed"] is True
    by_id = {w["id"]: w for w in body["webcams"]}
    assert by_id["dshow:hp"]["assigned_camera_id"] == env.ids["usb"]
    # A paused camera still owns its webcam: picking it twice would fight.
    assert by_id["dshow:usb"]["assigned_camera_id"] == env.ids["paused"]

    env.current["user"] = env.users["viewer"]
    assert env.tc.get("/api/v1/host-agent").status_code == 403


def test_the_webcam_list_survives_a_core_restart(env):
    env.tc.post("/api/v1/host-agent/sync", json=REPORT, headers=H)
    host_agent.state.reset()          # core restarted: nothing in memory
    body = env.tc.get("/api/v1/host-agent").json()
    assert body["connected"] is False
    assert {w["id"] for w in body["webcams"]} == {"dshow:hp", "dshow:usb"}


def test_source_device_cannot_be_put_on_an_ip_camera(env):
    r = env.tc.put(f"/api/v1/cameras/{env.ids['ip']}", json={"source_device": "dshow:hp"})
    assert r.status_code == 422


def test_a_webcam_moved_to_another_port_keeps_its_name(env):
    """The id includes the USB port; the name is how the agent finds the
    same webcam again, so core must remember it after the old id is gone."""
    env.tc.post("/api/v1/host-agent/sync", json=REPORT, headers=H)
    moved = {**REPORT, "webcams": [{"id": "dshow:hp-other-port", "name": "HP HD Camera"}]}
    body = env.tc.post("/api/v1/host-agent/sync", json=moved, headers=H).json()
    assert body["desired"][0]["device_id"] == "dshow:hp"
    assert body["desired"][0]["device_name"] == "HP HD Camera"
