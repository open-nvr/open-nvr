# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The operator alert inbox — consumer, acknowledge flow, ring config.

The gap this closes: every SDK app has published §11.5 alerts onto
``opennvr.alerts.>`` since the alert stack shipped, and the LPR config
even promised the operator-UI inbox picks them up — but no core
consumer existed. An armed "alarm on unknown vehicle" fired into a log
nobody watches: no row, no ring, no acknowledgement. Pinned here:

* every well-formed alert on the bus becomes exactly ONE inbox row —
  at-least-once redelivery (same ``alert_id``) never rings twice;
* malformed envelopes are dropped, unknown severities surface as
  ``high`` (an app bug in one field must not hide a fired alert, and
  must not escalate it to critical either);
* acknowledge is idempotent and records WHO silenced the alarm;
* the ring policy (none | ping | continuous per severity) round-trips,
  rejects typos loudly, and degrades corrupt stored state to defaults —
  never to silence-on-critical.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_alerts_test.db")
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
sys.modules.setdefault("core.logging_config", _lm)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.database as cdb  # noqa: E402
import models  # noqa: E402
from services.alerts_inbox import (  # noqa: E402
    DEFAULT_RING_CONFIG,
    apply_alert,
    normalize_ring_config,
)


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.database", cdb)
    monkeypatch.setitem(sys.modules, "models", models)
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng)
    monkeypatch.setattr(cdb, "SessionLocal", SessionLocal)
    # apply_alert fires alarm actions on a daemon thread that opens its
    # own session. On this StaticPool — ONE sqlite connection shared by
    # every session — that thread's close() is a ROLLBACK on the same
    # connection, and when it lands while the next apply_alert holds an
    # uncommitted INSERT, that insert vanishes: ObjectDeletedError on the
    # post-commit refresh, seen as a CI-only flake. Production sessions
    # have their own connections; the action tests below call
    # dispatch_alarm_actions directly. Keep the thread out of here.
    import services.alarm_actions as _aa

    monkeypatch.setattr(_aa, "dispatch_in_background", lambda alert: None)
    s = SessionLocal()
    role = models.Role(name="admin")
    s.add(role)
    s.commit()
    # A superuser: these tests exercise the inbox MECHANICS (ack, poll,
    # ring policy). Camera scoping has its own tests further down.
    user = models.User(username="op", email="op@x", hashed_password="x",
                       role_id=role.id, is_superuser=True)
    s.add(user)
    s.commit()
    user_id = user.id
    s.close()
    yield SessionLocal, user_id


def _envelope(alert_id="alrt_abc123", severity="high", **extra):
    return {
        "alert_id": alert_id,
        "fired_at": "2026-09-03T10:00:00+00:00",
        "title": "Unregistered vehicle at the gate",
        "description": "Plate ZZ999XX is not in the register",
        "severity": severity,
        "source": {"kind": "app", "name": "license-plate-recognition",
                   "version": "1.0.0"},
        "camera_id": "cam1",
        "correlation_id": "corr-1",
        "evidence": {"plate": "ZZ999XX"},
        "tags": ["lpr", "unknown-vehicle"],
        **extra,
    }


def _rows(SessionLocal):
    s = SessionLocal()
    try:
        return s.query(models.AppAlert).order_by(models.AppAlert.id).all()
    finally:
        s.close()


# ── the consumer core ──────────────────────────────────────────────


def test_alert_becomes_one_inbox_row(db):
    SessionLocal, _ = db
    assert apply_alert(_envelope()) == "stored"
    rows = _rows(SessionLocal)
    assert len(rows) == 1
    a = rows[0]
    assert a.alert_id == "alrt_abc123"
    assert a.severity == "high"
    assert a.title == "Unregistered vehicle at the gate"
    assert a.source_name == "license-plate-recognition"
    assert a.camera_id == "cam1"
    assert json.loads(a.evidence) == {"plate": "ZZ999XX"}
    assert json.loads(a.tags) == ["lpr", "unknown-vehicle"]
    assert a.acknowledged_at is None


def test_redelivery_never_rings_twice(db):
    """NATS is at-least-once and the consumer reconnects: the same
    alert_id redelivered must be a no-op, not a second ringing row."""
    SessionLocal, _ = db
    assert apply_alert(_envelope()) == "stored"
    assert apply_alert(_envelope()) == "duplicate"
    assert len(_rows(SessionLocal)) == 1


def test_malformed_envelopes_are_dropped(db):
    SessionLocal, _ = db
    assert apply_alert(None) == "malformed"
    assert apply_alert("not-a-dict") == "malformed"
    assert apply_alert({"title": "no id"}) == "malformed"
    assert apply_alert({"alert_id": "a1", "title": "  "}) == "malformed"
    assert _rows(SessionLocal) == []


def test_unknown_severity_surfaces_as_high(db):
    """An app bug in one field must not HIDE a fired alert — and must
    not escalate it to critical either."""
    SessionLocal, _ = db
    assert apply_alert(_envelope(severity="URGENT!!")) == "stored"
    assert _rows(SessionLocal)[0].severity == "high"


def test_info_is_a_notice_not_an_escalation(db):
    """Field bug: the LPR app fires routine "Plate X read" at "info", a
    level the inbox lacks; the unknown-severity fallback promoted every
    one to HIGH and rang the beep — while the operator's "alarm on
    unknown vehicles" toggle (which governs a different alert entirely)
    appeared to do nothing. Levels apps commonly use map onto ours;
    only genuinely unknown values escalate."""
    SessionLocal, _ = db
    for i, (given, expected) in enumerate([
        ("info", "low"), ("INFO", "low"), ("notice", "low"),
        ("warning", "medium"), ("error", "high"), (" Low ", "low"),
    ]):
        assert apply_alert(_envelope(alert_id=f"alrt_sev{i}",
                                     severity=given)) == "stored"
    got = {r.alert_id: r.severity for r in _rows(SessionLocal)}
    assert got == {"alrt_sev0": "low", "alrt_sev1": "low", "alrt_sev2": "low",
                   "alrt_sev3": "medium", "alrt_sev4": "high",
                   "alrt_sev5": "low"}


def test_unparseable_fired_at_still_stores(db):
    SessionLocal, _ = db
    assert apply_alert(_envelope(fired_at="not-a-date")) == "stored"
    assert _rows(SessionLocal)[0].fired_at is not None


# ── the API (list / ack / ring config) ─────────────────────────────


@pytest.fixture()
def client(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import core.auth as auth_mod
    from core.database import get_db
    from routers.alerts_inbox import router

    SessionLocal, user_id = db
    app = FastAPI()
    app.include_router(router)

    def _fake_db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    s = SessionLocal()
    user = s.get(models.User, user_id)
    s.expunge(user)
    s.close()

    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[auth_mod.get_current_active_user] = lambda: user
    app.dependency_overrides[auth_mod.get_current_superuser] = lambda: user
    return TestClient(app), SessionLocal, user_id


def test_list_unacked_and_ack_flow(client):
    tc, SessionLocal, user_id = client
    apply_alert(_envelope("a1", severity="critical"))
    apply_alert(_envelope("a2", severity="low"))

    out = tc.get("/alerts-inbox", params={"unacked": True}).json()
    assert out["unacked_count"] == 2
    assert [a["alert_id"] for a in out["alerts"]] == ["a2", "a1"]  # newest first

    first_id = out["alerts"][1]["id"]
    r = tc.post("/alerts-inbox/ack", json={"ids": [first_id]})
    assert r.json() == {"acknowledged": 1}
    out = tc.get("/alerts-inbox", params={"unacked": True}).json()
    assert out["unacked_count"] == 1
    # WHO silenced it is recorded.
    acked = _rows(SessionLocal)[0]
    assert acked.acknowledged_by == user_id
    # Idempotent: re-acking changes nothing (first silencer wins).
    stamp = acked.acknowledged_at
    assert tc.post("/alerts-inbox/ack",
                   json={"ids": [first_id]}).json() == {"acknowledged": 0}
    assert _rows(SessionLocal)[0].acknowledged_at == stamp

    # Empty body = ack ALL remaining.
    assert tc.post("/alerts-inbox/ack", json={}).json() == {"acknowledged": 1}
    assert tc.get("/alerts-inbox",
                  params={"unacked": True}).json()["unacked_count"] == 0


def test_after_id_polling_window(client):
    tc, *_ = client
    apply_alert(_envelope("a1"))
    apply_alert(_envelope("a2"))
    all_rows = tc.get("/alerts-inbox").json()["alerts"]
    newest = all_rows[0]["id"]
    out = tc.get("/alerts-inbox", params={"after_id": newest}).json()
    assert out["alerts"] == []
    apply_alert(_envelope("a3"))
    out = tc.get("/alerts-inbox", params={"after_id": newest}).json()
    assert [a["alert_id"] for a in out["alerts"]] == ["a3"]


def test_ring_config_defaults_roundtrip_and_validation(client):
    tc, *_ = client
    out = tc.get("/alerts-inbox/ring-config").json()
    assert out["ring"] == DEFAULT_RING_CONFIG

    r = tc.put("/alerts-inbox/ring-config",
               json={"ring": {"low": "ping", "critical": "continuous"}})
    assert r.status_code == 200
    assert r.json()["ring"]["low"] == "ping"
    assert tc.get("/alerts-inbox/ring-config").json()["ring"]["low"] == "ping"

    # Typos are rejected LOUDLY — never stored, never silently defaulted.
    assert tc.put("/alerts-inbox/ring-config",
                  json={"ring": {"critical": "continuos"}}).status_code == 422
    assert tc.put("/alerts-inbox/ring-config",
                  json={"ring": {"fatal": "ping"}}).status_code == 422


def test_corrupt_stored_ring_config_degrades_to_defaults():
    """A corrupt stored value must never mean silence-on-critical."""
    assert normalize_ring_config(None) == DEFAULT_RING_CONFIG
    assert normalize_ring_config("garbage") == DEFAULT_RING_CONFIG
    assert normalize_ring_config(
        {"critical": "nonsense", "low": "ping"}
    ) == {**DEFAULT_RING_CONFIG, "low": "ping"}


def test_test_alarm_takes_the_real_path(client):
    """The 'is it working?' button must exercise the same chain a real
    alert takes — a row in the same table, ringing and acknowledging
    like any other — and reject severities the ring policy doesn't
    know."""
    tc, SessionLocal, _ = client
    r = tc.post("/alerts-inbox/test", json={"severity": "critical"})
    assert r.status_code == 200 and r.json()["status"] == "fired"
    rows = _rows(SessionLocal)
    assert len(rows) == 1
    assert rows[0].severity == "critical"
    assert rows[0].source_name == "alarm-test"
    assert rows[0].alert_id.startswith("alrt_test_")
    # Counts as unacked (it must ring), acks like any other.
    assert tc.get("/alerts-inbox",
                  params={"unacked": True}).json()["unacked_count"] == 1
    assert tc.post("/alerts-inbox/ack", json={}).json()["acknowledged"] == 1
    # Unknown severity is a loud 422, not a silent default.
    assert tc.post("/alerts-inbox/test",
                   json={"severity": "fatal"}).status_code == 422


# ── alarm actions (call / SMS / hooter) ────────────────────────────


def _actions_cfg(**over):
    base = {
        "min_severity": "high",
        "twilio": {"enabled": True, "account_sid": "ACxx",
                   "auth_token": "secret-token",
                   "from_number": "+1000", "to_numbers": ["+1911"],
                   "mode": "both"},
        "webhook": {"enabled": True, "url": "http://relay.local/on",
                    "method": "POST"},
    }
    base.update(over)
    return base


def test_actions_config_masks_the_secret_and_keeps_it_on_blank(client):
    tc, *_ = client
    out = tc.put("/alerts-inbox/actions", json=_actions_cfg()).json()["actions"]
    assert out["twilio"]["auth_token_set"] is True
    assert "auth_token" not in out["twilio"]
    assert "auth_token_enc" not in out["twilio"], (
        "even the ciphertext must not leave the server")
    # Update WITHOUT a token: the stored secret survives.
    out = tc.put("/alerts-inbox/actions",
                 json={"twilio": {"from_number": "+2000"}}).json()["actions"]
    assert out["twilio"]["auth_token_set"] is True
    assert out["twilio"]["from_number"] == "+2000"
    assert out["twilio"]["to_numbers"] == ["+1911"]   # merge, not replace
    assert tc.get("/alerts-inbox/actions").json()["actions"]["twilio"][
        "auth_token_set"] is True
    # Bad severity is loud.
    assert tc.put("/alerts-inbox/actions",
                  json={"min_severity": "fatal"}).status_code == 422


def test_stored_alert_dispatches_actions_only_at_or_above_min_severity(
        client, monkeypatch):
    """The severity gate is the difference between 'the guard's phone
    rings for an intruder' and 'the guard's phone rings for every parked
    car'."""
    import services.alarm_actions as aa

    tc, *_ = client
    tc.put("/alerts-inbox/actions", json=_actions_cfg(min_severity="high"))

    calls: list[dict] = []
    monkeypatch.setattr(aa, "_dispatch_twilio",
                        lambda tw, alert: calls.append(
                            {"kind": "twilio", "sev": alert["severity"]}) or [])
    monkeypatch.setattr(aa, "_dispatch_webhook",
                        lambda wh, alert: calls.append(
                            {"kind": "webhook", "sev": alert["severity"]})
                        or {"action": "webhook", "ok": True, "detail": ""})

    aa.dispatch_alarm_actions({"severity": "low", "title": "quiet"})
    assert calls == [], "below min_severity must dispatch NOTHING"
    aa.dispatch_alarm_actions({"severity": "critical", "title": "loud"})
    assert {c["kind"] for c in calls} == {"twilio", "webhook"}


def test_twilio_dispatch_shapes_the_rest_calls(client, monkeypatch):
    """Lockstep with Twilio's API: basic-auth (sid, token), Calls.json
    with inline TwiML, Messages.json with a Body — and the DECRYPTED
    token, proving the vault round-trip."""
    import services.alarm_actions as aa

    tc, *_ = client
    tc.put("/alerts-inbox/actions", json=_actions_cfg())

    posts: list[dict] = []

    class _Resp:
        status_code = 201

        def json(self):
            return {}

    class _FakeHttpx:
        @staticmethod
        def post(url, data=None, auth=None, timeout=None):
            posts.append({"url": url, "data": data, "auth": auth})
            return _Resp()

    # The REAL vault decrypts (the PUT above encrypted with it); only
    # the network is faked.
    import sys as _sys
    monkeypatch.setitem(_sys.modules, "httpx", _FakeHttpx)
    results = aa.dispatch_alarm_actions(
        {"severity": "critical", "title": "Unknown vehicle",
         "camera_id": "cam1"}, force=True)

    urls = [p["url"] for p in posts]
    assert any(u.endswith("/Calls.json") for u in urls)
    assert any(u.endswith("/Messages.json") for u in urls)
    for p in posts:
        assert p["auth"] == ("ACxx", "secret-token"), (
            "dispatch must use the DECRYPTED stored token")
        assert p["data"]["To"] == "+1911"
    call = next(p for p in posts if p["url"].endswith("/Calls.json"))
    assert "<Say" in call["data"]["Twiml"]
    sms = next(p for p in posts if p["url"].endswith("/Messages.json"))
    assert "Unknown vehicle" in sms["data"]["Body"]
    assert all(r["ok"] for r in results if r["action"].startswith("twilio"))



# ── camera scope (RBAC): you get the alarms for YOUR cameras ───────


@pytest.fixture()
def scoped_client(db, monkeypatch):
    """Two cameras (owner: someone else), one operator granted can_view
    on the first only, plus a superuser — the same app, two callers."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import core.auth as auth_mod
    from core.database import get_db
    from routers.alerts_inbox import router

    SessionLocal, admin_id = db
    s = SessionLocal()
    role = s.query(models.Role).first()
    op = models.User(username="guard", email="g@x", hashed_password="x",
                     role_id=role.id)
    s.add(op)
    s.commit()
    gate = models.Camera(name="Gate", ip_address="10.0.0.1", owner_id=admin_id)
    yard = models.Camera(name="Yard", ip_address="10.0.0.2", owner_id=admin_id)
    s.add_all([gate, yard])
    s.commit()
    s.add(models.CameraPermission(user_id=op.id, camera_id=gate.id,
                                  can_view=True, can_manage=False))
    s.commit()
    gate_id, yard_id = gate.id, yard.id
    admin = s.get(models.User, admin_id)
    s.refresh(admin)
    s.refresh(op)
    s.expunge(admin)
    s.expunge(op)
    s.close()

    app = FastAPI()
    app.include_router(router)

    def _fake_db():
        sess = SessionLocal()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_db] = _fake_db
    current = {"user": op}
    app.dependency_overrides[auth_mod.get_current_active_user] = \
        lambda: current["user"]

    def _as_superuser():
        from fastapi import HTTPException
        if not current["user"].is_superuser:
            raise HTTPException(status_code=403, detail="Not enough permissions")
        return current["user"]

    app.dependency_overrides[auth_mod.get_current_superuser] = _as_superuser
    return TestClient(app), current, op, admin, gate_id, yard_id


def test_inbox_lists_counts_and_acks_only_visible_cameras(scoped_client):
    tc, current, op, admin, gate_id, yard_id = scoped_client
    apply_alert(_envelope("on-gate", camera_id=f"cam{gate_id}"))
    apply_alert(_envelope("on-yard", camera_id=f"cam{yard_id}"))
    apply_alert(_envelope("site-wide", camera_id=None))

    # The guard: the gate alarm and the camera-less notice, never the yard.
    out = tc.get("/alerts-inbox", params={"unacked": True}).json()
    assert sorted(a["alert_id"] for a in out["alerts"]) == ["on-gate", "site-wide"]
    assert out["unacked_count"] == 2

    # Ack-all silences MY inbox only; the yard alarm keeps ringing for
    # whoever can see the yard.
    assert tc.post("/alerts-inbox/ack", json={}).json() == {"acknowledged": 2}
    current["user"] = admin
    out = tc.get("/alerts-inbox", params={"unacked": True}).json()
    assert [a["alert_id"] for a in out["alerts"]] == ["on-yard"]
    assert out["unacked_count"] == 1

    # An explicit id on someone else's camera is not acked (not found).
    yard_row = out["alerts"][0]["id"]
    current["user"] = op
    assert tc.post("/alerts-inbox/ack",
                   json={"ids": [yard_row]}).json() == {"acknowledged": 0}
    assert tc.get("/alerts-inbox").json()["alerts"] and all(
        a["alert_id"] != "on-yard" for a in tc.get("/alerts-inbox").json()["alerts"])


def test_user_granted_nothing_sees_only_camera_less_alerts(scoped_client, db):
    tc, current, op, admin, gate_id, yard_id = scoped_client
    SessionLocal, _ = db
    s = SessionLocal()
    s.query(models.CameraPermission).delete()
    s.commit()
    s.close()
    apply_alert(_envelope("on-gate", camera_id=f"cam{gate_id}"))
    apply_alert(_envelope("notice", camera_id=""))
    out = tc.get("/alerts-inbox").json()
    assert [a["alert_id"] for a in out["alerts"]] == ["notice"]
    assert out["unacked_count"] == 1


def test_alarm_policy_is_superuser_only(scoped_client):
    tc, current, op, admin, *_ = scoped_client
    # The bell needs the ring policy to know HOW to ring: readable by all.
    assert tc.get("/alerts-inbox/ring-config").status_code == 200
    # Changing the site's alarm policy, its actions, or firing test
    # alarms into everyone's inbox is not.
    assert tc.put("/alerts-inbox/ring-config",
                  json={"ring": {"low": "ping"}}).status_code == 403
    assert tc.get("/alerts-inbox/actions").status_code == 403
    assert tc.put("/alerts-inbox/actions",
                  json={"min_severity": "high"}).status_code == 403
    assert tc.post("/alerts-inbox/actions/test").status_code == 403
    assert tc.post("/alerts-inbox/test", json={"severity": "high"}).status_code == 403
    current["user"] = admin
    assert tc.put("/alerts-inbox/ring-config",
                  json={"ring": {"low": "ping"}}).status_code == 200
    assert tc.get("/alerts-inbox/actions").status_code == 200
