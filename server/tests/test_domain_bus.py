# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Core's domain events: visit.recorded.v1 when a visit row is committed,
visit.enriched.v1 when its caption or claims land, both through one
shared NATS client, never raising into the writer, and no-ops without a
bus."""

from __future__ import annotations

import asyncio
import json
import os
import secrets

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite:///./_domain_bus_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402

from services import domain_bus  # noqa: E402


class _FakeClient:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.is_closed = False

    async def publish(self, subject, data):
        self.sent.append((subject, json.loads(data)))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def bus(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "nats_url", "nats://bus:4222", raising=False)
    fake = _FakeClient()

    async def _connection():
        return fake

    monkeypatch.setattr(domain_bus, "_connection", _connection)
    return fake


def test_envelope_subject_and_camera_handle(bus):
    ok = _run(domain_bus.publish_domain("visit.recorded.v1", camera_id=23,
                                        payload={"event_id": 9}, ts="2026-11-13T07:00:00Z"))
    assert ok and len(bus.sent) == 1
    subject, env = bus.sent[0]
    assert subject == "opennvr.events.visit.recorded.v1.cam23"
    assert env["schema"] == "visit.recorded.v1" and env["camera_id"] == "cam23"
    assert env["producer"] == "core" and env["id"].startswith("evt_") and env["ts"] == "2026-11-13T07:00:00Z"
    assert env["payload"] == {"event_id": 9}
    # a handle given as a handle stays one
    _run(domain_bus.publish_domain("visit.recorded.v1", camera_id="cam7", payload={}))
    assert bus.sent[1][0].endswith(".cam7")


def test_without_a_bus_nothing_is_sent_and_nothing_raises(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "nats_url", "", raising=False)
    monkeypatch.setattr(domain_bus, "_client", None)
    before = domain_bus.STATS["no_bus"]
    assert _run(domain_bus.publish_domain("visit.recorded.v1", camera_id=1, payload={})) is False
    assert domain_bus.STATS["no_bus"] == before + 1


def test_a_failing_publish_is_counted_and_the_client_remade(monkeypatch, bus):
    async def boom(subject, data):
        raise RuntimeError("bus down")

    bus.publish = boom
    monkeypatch.setattr(domain_bus, "_client", bus)
    before = domain_bus.STATS["failed"]
    assert _run(domain_bus.publish_domain("visit.enriched.v1", camera_id=2, payload={})) is False
    assert domain_bus.STATS["failed"] == before + 1 and domain_bus._client is None


def test_recording_a_visit_publishes_it(monkeypatch, bus):
    """record_track_visit is synchronous; the publish is scheduled, and
    the row is committed whether or not the bus is there."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from core.database import Base
    from models import Camera, Role, User
    from services import timeline_service

    engine = create_engine("sqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, future=True)()
    db.add(Role(id=1, name="admin", description="t")); db.commit()
    db.add(User(id=1, username="o", email="o@x.test", hashed_password="x", is_active=True, role_id=1)); db.commit()
    db.add(Camera(id=5, name="c", ip_address="10.0.0.1", rtsp_url="rtsp://x/1", owner_id=1)); db.commit()
    scheduled: list[dict] = []
    monkeypatch.setattr(domain_bus, "publish_soon",
                        lambda schema, **kw: scheduled.append({"schema": schema, **kw}))
    now = datetime.now(UTC)
    row = timeline_service.record_track_visit(db, camera_id=5, label="car", started_at=now,
                                              ended_at=now + timedelta(seconds=4), evidence_path="ab/abc.jpg")
    assert row.id and len(scheduled) == 1
    ev = scheduled[0]
    assert ev["schema"] == "visit.recorded.v1" and ev["camera_id"] == 5
    assert ev["payload"]["event_id"] == row.id and ev["payload"]["label"] == "car"
    assert ev["payload"]["evidence_path"] == "ab/abc.jpg" and ev["payload"]["ended_at"].endswith("Z")
    db.close(); engine.dispose()


def test_the_enriched_payload_carries_the_whole_state(monkeypatch, bus):
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import core.database
    from core.database import Base
    from models import Camera, EventText, Role, TimelineEvent, User, VisitDescriptor

    engine = create_engine("sqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, future=True)()
    db.add(Role(id=1, name="admin", description="t")); db.commit()
    db.add(User(id=1, username="o", email="o@x.test", hashed_password="x", is_active=True, role_id=1)); db.commit()
    db.add(Camera(id=3, name="c", ip_address="10.0.0.1", rtsp_url="rtsp://x/1", owner_id=1)); db.commit()
    now = datetime.now(UTC)
    db.add(TimelineEvent(id=41, camera_id=3, source="tier0", event_type="track", label="car",
                         started_at=now, ended_at=now, evidence_path="ab/abc.jpg",
                         payload={"enriched_by": ["vqa"], "reused_from": 40}))
    db.add(EventText(event_id=41, caption="A white van.", source="litertlm"))
    db.add(VisitDescriptor(event_id=41, kind="colour", value="white", source_task="vqa", source_adapter="litertlm"))
    db.commit()

    class _Shared:
        def __init__(self, s): self._s = s
        def __getattr__(self, n): return getattr(self._s, n)
        def close(self): pass

    monkeypatch.setattr(core.database, "SessionLocal", lambda: _Shared(db))
    assert _run(domain_bus.publish_visit_enriched(41)) is True
    subject, env = bus.sent[-1]
    assert subject == "opennvr.events.visit.enriched.v1.cam3"
    p = env["payload"]
    assert p["event_id"] == 41 and p["caption"] == "A white van." and p["producer"] == "litertlm"
    assert p["attributes"] == {"colour": "white"} and p["enriched_by"] == ["vqa"] and p["reused_from"] == 40
    db.close(); engine.dispose()


def test_the_sdk_types_the_enriched_event():
    from opennvr_app_sdk import VisitEnriched, typed_payload

    t = typed_payload("visit.enriched.v1", {"event_id": 41, "label": "car", "caption": "A white van.",
                                            "attributes": {"colour": "white"}, "enriched_by": ["vqa"]})
    assert isinstance(t, VisitEnriched) and t.attributes["colour"] == "white" and t.reused_from is None
    assert "reused_from" not in t.to_payload()
