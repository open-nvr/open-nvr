# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Audit coverage for state-changing endpoints that used to write none (HA-003).

Each of these changes what the system does or keeps, and none left an audit
row before: PTZ move/stop, silencing alerts, protecting and exporting
footage, device-firewall administration, and switching an AI app on/off for
a camera. With API tokens coming (Home Assistant), an investigator must be
able to attribute every one of them.

Each test calls the endpoint coroutine directly with mocked collaborators
(the pattern of test_camera_snapshot.py) and captures what reaches
``write_audit_log``. The last test pins the helper's contract: an audit
failure never fails the action being audited.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import types
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

import core.logging_config as _lc  # noqa: E402


class _L:
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
if not hasattr(_lc, "setup_logging"):
    _lc.setup_logging = lambda *a, **kw: None

import services.audit_service as audit_service  # noqa: E402

REQ = types.SimpleNamespace(
    client=types.SimpleNamespace(host="10.0.0.9"),
    headers={"user-agent": "pytest"},
)
ADMIN = types.SimpleNamespace(id=1, username="op", is_superuser=True)


@pytest.fixture()
def audited(monkeypatch):
    """Capture every write_audit_log call made through audit_request."""
    rows: list[dict] = []

    def _capture(db, **kw):
        rows.append(kw)
        return types.SimpleNamespace(**kw)

    monkeypatch.setattr(audit_service, "write_audit_log", _capture)
    # audit_request opens its own session; hand it a harmless stand-in.
    monkeypatch.setattr(audit_service, "_audit_session",
                        lambda: types.SimpleNamespace(rollback=lambda: None,
                                                      close=lambda: None))
    return rows


def _one(rows, action):
    matching = [r for r in rows if r["action"] == action]
    assert len(matching) == 1, rows
    row = matching[0]
    assert row["ip"] == "10.0.0.9" and row["user_agent"] == "pytest"
    return row


# ── PTZ ─────────────────────────────────────────────────────────────────


def _ptz_setup(monkeypatch):
    from routers import cameras as cameras_router
    from services.camera_service import CameraService
    import services.ptz_service as ptz

    cam = types.SimpleNamespace(id=3, username="u", password="p",
                                ip_address="192.0.2.3", port=80)
    monkeypatch.setattr(CameraService, "get_camera_by_id",
                        staticmethod(lambda db, cid, uid: cam))

    async def _ok(**kw):
        return {"success": True, "camera_id": kw["camera_id"]}

    monkeypatch.setattr(ptz.PTZService, "move", staticmethod(_ok))
    monkeypatch.setattr(ptz.PTZService, "stop", staticmethod(_ok))
    return cameras_router


def test_ptz_move_is_audited(monkeypatch, audited):
    r = _ptz_setup(monkeypatch)
    asyncio.run(r.ptz_move(camera_id=3, x=0.5, y=-0.25, z=0.0, request=REQ,
                           db=object(), current_user=ADMIN))
    row = _one(audited, "ptz.move")
    assert row["entity_type"] == "camera" and row["entity_id"] == 3
    assert row["user_id"] == 1
    assert row["details"] == {"x": 0.5, "y": -0.25, "z": 0.0, "success": True}


def test_ptz_stop_is_audited(monkeypatch, audited):
    r = _ptz_setup(monkeypatch)
    asyncio.run(r.ptz_stop(camera_id=3, request=REQ, db=object(),
                           current_user=ADMIN))
    assert _one(audited, "ptz.stop")["details"] == {"success": True}


# ── Alerts inbox acknowledge ────────────────────────────────────────────


def test_alert_ack_is_audited_with_ids(monkeypatch, audited):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import core.database as cdb
    import models
    import services.alarm_actions as aa
    from routers import alerts_inbox as inbox_router
    from services.alerts_inbox import apply_alert

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    monkeypatch.setattr(cdb, "SessionLocal", Session)
    monkeypatch.setattr(aa, "dispatch_in_background", lambda alert: None)
    db = Session()
    for aid in ("a1", "a2"):
        assert apply_alert({
            "alert_id": aid, "fired_at": "2026-09-03T10:00:00+00:00",
            "title": "t", "severity": "high",
            "source": {"kind": "app", "name": "lpr", "version": "1"},
            "camera_id": "cam1",
        }, db=db) == "stored"
    ids = [row.id for row in db.query(models.AppAlert).all()]

    out = asyncio.run(inbox_router.acknowledge(
        payload=inbox_router.AckIn(ids=ids), request=REQ,
        current_user=ADMIN, db=db))
    assert out == {"acknowledged": 2}
    row = _one(audited, "alerts.ack")
    assert row["details"]["count"] == 2
    assert sorted(row["details"]["ids"]) == sorted(ids)

    # A no-op acknowledge (nothing left to silence) writes nothing.
    audited.clear()
    asyncio.run(inbox_router.acknowledge(
        payload=inbox_router.AckIn(ids=ids), request=REQ,
        current_user=ADMIN, db=db))
    assert audited == []
    db.close()


# ── Recordings: protect flag and export ─────────────────────────────────


def _recordings_setup(monkeypatch):
    from routers import recordings as rec_router

    async def _auth(request, db):
        return ADMIN

    monkeypatch.setattr(rec_router, "_authenticate_request", _auth)
    monkeypatch.setattr(rec_router, "_require_camera_view",
                        lambda user, camera, db: None)

    cam = types.SimpleNamespace(id=4, ip_address="192.0.2.4")

    class _Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return cam

        def update(self, *a, **k):
            return 5

    class _DB:
        def query(self, *a, **k):
            return _Q()

        def commit(self):
            pass

    return rec_router, _DB()


def test_recording_protect_and_unprotect_are_audited(monkeypatch, audited):
    r, db = _recordings_setup(monkeypatch)
    asyncio.run(r.set_recording_flag(
        camera_id=4, start="2026-09-18T10:00:00Z", end="2026-09-18T10:10:00Z",
        flagged=True, request=REQ, db=db))
    row = _one(audited, "recording.protect")
    assert row["entity_id"] == 4 and row["details"]["updated_clips"] == 5

    asyncio.run(r.set_recording_flag(
        camera_id=4, start="2026-09-18T10:00:00Z", end="2026-09-18T10:10:00Z",
        flagged=False, request=REQ, db=db))
    _one(audited, "recording.unprotect")


def test_recording_export_is_audited(monkeypatch, audited):
    r, db = _recordings_setup(monkeypatch)
    out = asyncio.run(r.create_export_ticket(
        camera_id=4, start="2026-09-18T10:00:00Z", duration=600.0,
        filename="gate incident", request=REQ, db=db))
    assert out["ticket"]
    row = _one(audited, "recording.export")
    assert row["details"] == {"start": "2026-09-18T10:00:00Z",
                              "duration": 600.0, "filename": "gateincident.mp4"}


# ── Device firewall administration ──────────────────────────────────────


def test_device_firewall_admin_actions_are_audited(monkeypatch, audited):
    from routers import device_firewall as dfw_router

    dev = types.SimpleNamespace()
    monkeypatch.setattr(dfw_router.dfw, "set_enforcement", lambda db, on: on)
    monkeypatch.setattr(dfw_router.dfw, "approve", lambda db, i, **k: dev)
    monkeypatch.setattr(dfw_router.dfw, "block", lambda db, i: dev)
    monkeypatch.setattr(dfw_router.dfw, "delete", lambda db, i: True)
    monkeypatch.setattr(dfw_router, "_serialize", lambda d: {"ok": True})

    asyncio.run(dfw_router.set_enforcement(
        payload=dfw_router.EnforcementUpdate(active=True), request=REQ,
        user=ADMIN, db=object()))
    asyncio.run(dfw_router.approve_device(
        device_id=11, request=REQ, payload=None, user=ADMIN, db=object()))
    asyncio.run(dfw_router.block_device(device_id=12, request=REQ,
                                        user=ADMIN, db=object()))
    asyncio.run(dfw_router.delete_device(device_id=13, request=REQ,
                                         user=ADMIN, db=object()))

    assert _one(audited, "device_firewall.enforcement")["details"] == {
        "requested": True, "effective": True}
    assert _one(audited, "device_firewall.approve")["entity_id"] == 11
    assert _one(audited, "device_firewall.block")["entity_id"] == 12
    assert _one(audited, "device_firewall.delete")["entity_id"] == 13


def test_deleting_a_missing_device_writes_no_audit(monkeypatch, audited):
    from routers import device_firewall as dfw_router

    monkeypatch.setattr(dfw_router.dfw, "delete", lambda db, i: False)
    out = asyncio.run(dfw_router.delete_device(device_id=99, request=REQ,
                                               user=ADMIN, db=object()))
    assert out == {"deleted": False}
    assert audited == []


# ── Skill picks: an AI app on/off for a camera ──────────────────────────


def test_skill_claim_and_release_are_audited(monkeypatch, audited):
    from routers import skills as skills_router

    monkeypatch.setattr(skills_router, "_require_camera_manage",
                        lambda db, user, cid: None)
    monkeypatch.setattr(skills_router.skill_assignments, "declare",
                        lambda db, **k: None)
    monkeypatch.setattr(skills_router.skill_assignments, "release",
                        lambda db, **k: True)
    monkeypatch.setattr(skills_router.skill_assignments, "skill_view",
                        lambda db, sid: {"skill": sid})

    class _DB:
        def commit(self):
            pass

    asyncio.run(skills_router.declare_skill_camera(
        skill_id="loitering_detection", camera_id=2, request=REQ,
        consumer="app:loitering-detection", params=None, db=_DB(),
        current_user=ADMIN))
    asyncio.run(skills_router.release_skill_camera(
        skill_id="loitering_detection", camera_id=2,
        consumer="app:loitering-detection", request=REQ, db=_DB(),
        current_user=ADMIN))

    claim = _one(audited, "skill.claim")
    assert claim["entity_id"] == 2
    assert claim["details"] == {"skill": "loitering_detection",
                                "consumer": "app:loitering-detection"}
    _one(audited, "skill.release")


# ── The helper's contract ───────────────────────────────────────────────


def test_audit_failure_never_fails_the_action(monkeypatch):
    def _boom(db, **kw):
        raise RuntimeError("audit table unavailable")

    events = []
    own = types.SimpleNamespace(rollback=lambda: events.append("own.rollback"),
                                close=lambda: events.append("own.close"))
    monkeypatch.setattr(audit_service, "write_audit_log", _boom)
    monkeypatch.setattr(audit_service, "_audit_session", lambda: own)
    caller = types.SimpleNamespace(
        rollback=lambda: events.append("CALLER.rollback"))
    audit_service.audit_request(caller, REQ, action="ptz.move")  # must not raise
    # Only the audit's own session is rolled back and closed; the caller's
    # session is never touched.
    assert events == ["own.rollback", "own.close"]


def test_audit_never_commits_the_callers_pending_work(monkeypatch, tmp_path):
    """An audit written while the caller has pending changes must neither
    commit nor discard them. File-backed SQLite so each session has its own
    connection, as in production (a StaticPool would share one connection and
    make any commit commit everything)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import models

    eng = create_engine(f"sqlite:///{tmp_path / 'audit.db'}",
                        connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    monkeypatch.setattr(audit_service, "_audit_session", Session)

    caller = Session()
    caller.add(models.Role(name="pending-role"))  # pending, not committed
    audit_service.audit_request(caller, REQ, action="ptz.move", user_id=1)
    assert caller.new, "the audit must not have flushed or committed the caller"

    caller.rollback()  # the caller changes its mind
    check = Session()
    try:
        assert check.query(models.Role).filter_by(name="pending-role").count() == 0
        assert check.query(models.AuditLog).filter_by(action="ptz.move").count() == 1
    finally:
        check.close()
        caller.close()
        eng.dispose()


def test_audit_request_tolerates_a_missing_request(audited):
    audit_service.audit_request(object(), None, action="x.y", user_id=5)
    assert audited == [{
        "action": "x.y", "user_id": 5, "entity_type": None, "entity_id": None,
        "details": None, "ip": None, "user_agent": None,
    }]


def test_audit_records_the_real_client_behind_a_trusted_proxy(monkeypatch, audited):
    """Behind nginx the socket peer is nginx; the audit row must name the client."""
    import core.client_ip as cip
    import ipaddress

    monkeypatch.setattr(cip, "_trusted_proxy_nets",
                        lambda: (ipaddress.ip_network("172.28.0.0/16"),))
    proxied = types.SimpleNamespace(
        client=types.SimpleNamespace(host="172.28.0.5"),
        headers={"x-forwarded-for": "192.168.1.50, 172.28.0.5",
                 "user-agent": "HomeAssistant"},
    )
    audit_service.audit_request(object(), proxied, action="ptz.move")
    assert audited[-1]["ip"] == "192.168.1.50"

    # An untrusted peer cannot spoof its address with the header.
    spoof = types.SimpleNamespace(
        client=types.SimpleNamespace(host="203.0.113.7"),
        headers={"x-forwarded-for": "10.0.0.1", "user-agent": "x"},
    )
    audit_service.audit_request(object(), spoof, action="ptz.move")
    assert audited[-1]["ip"] == "203.0.113.7"


# ── ONVIF tools (IP-keyed PTZ) ──────────────────────────────────────────


def test_onvif_tools_ptz_is_audited_without_credentials(monkeypatch, audited):
    from routers import onvif as onvif_router

    monkeypatch.setattr(onvif_router, "_assert_ip_in_camera_lan", lambda ip, db: None)
    monkeypatch.setattr(onvif_router, "_authorize_camera_ip",
                        lambda db, user, ip, action: None)

    async def _ok(*a, **k):
        return {"ok": True}

    monkeypatch.setattr(onvif_router, "ptz_continuous_move", _ok)
    monkeypatch.setattr(onvif_router, "ptz_presets", _ok)

    cam = types.SimpleNamespace(id=9)

    class _Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return cam

    db = types.SimpleNamespace(query=lambda *a, **k: _Q())

    asyncio.run(onvif_router.camera_ptz_move(
        ip="192.0.2.9", x=0.1, y=0.0, z=0.0, profile_token="p", port=80,
        username="admin", password="s3cret", request=REQ, db=db,
        current_user=ADMIN))
    for action in ("getPresets", "gotoPreset", "setPreset"):
        asyncio.run(onvif_router.camera_ptz_preset(
            ip="192.0.2.9", action=action, profile_token="p", name="Gate",
            preset_token="2", port=80, username="admin", password="s3cret",
            request=REQ, db=db, current_user=ADMIN))

    assert [r["action"] for r in audited] == [
        "ptz.move", "ptz.preset.goto", "ptz.preset.set"]  # listing is a read
    assert all(r["entity_id"] == 9 for r in audited)       # resolved camera id
    assert "s3cret" not in repr(audited) and "admin" not in repr(
        [r["details"] for r in audited])
