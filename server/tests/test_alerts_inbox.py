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
    s = SessionLocal()
    role = models.Role(name="admin")
    s.add(role)
    s.commit()
    user = models.User(username="op", email="op@x", hashed_password="x",
                       role_id=role.id)
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
