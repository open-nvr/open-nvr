# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Retention housekeeping for the bounded-growth side stores:
camera_events (age + hard row cap) and stale pending trusted_devices."""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_ret_test.db")
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

from core.database import Base  # noqa: E402
from models import CameraEvent, DeviceStatus, TrustedDevice  # noqa: E402
from services.retention_service import RetentionService  # noqa: E402


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    try:
        yield s
    finally:
        s.close()


def _event(db, camera_id: int, age_days: float) -> CameraEvent:
    row = CameraEvent(
        camera_id=camera_id,
        event_type="VMD",
        created_at=datetime.now(UTC) - timedelta(days=age_days),
    )
    db.add(row)
    return row


def test_camera_events_pruned_by_age(db):
    _event(db, 1, age_days=45)
    _event(db, 1, age_days=1)
    db.commit()

    stats = RetentionService.cleanup_auxiliary(db, retention_days=30)
    assert stats["deleted_camera_events"] == 1
    assert db.query(CameraEvent).count() == 1


def test_camera_events_fallback_window_when_keep_forever(db):
    # retention_days=0 (keep recordings forever) must NOT mean events grow
    # forever — the fallback window applies.
    _event(db, 1, age_days=RetentionService.CAMERA_EVENTS_FALLBACK_DAYS + 5)
    _event(db, 1, age_days=1)
    db.commit()

    stats = RetentionService.cleanup_auxiliary(db, retention_days=0)
    assert stats["deleted_camera_events"] == 1
    assert db.query(CameraEvent).count() == 1


def test_camera_events_hard_row_cap(db, monkeypatch):
    monkeypatch.setattr(RetentionService, "CAMERA_EVENTS_MAX_ROWS", 5)
    for _ in range(9):
        _event(db, 1, age_days=0.1)  # all recent: age pruning removes none
    db.commit()

    RetentionService.cleanup_auxiliary(db, retention_days=30)
    assert db.query(CameraEvent).count() == 5
    # the survivors are the NEWEST rows (highest ids)
    ids = [r.id for r in db.query(CameraEvent).all()]
    assert min(ids) > 4


def test_stale_pending_devices_pruned_admin_decisions_kept(db):
    old = datetime.now(UTC) - timedelta(days=60)
    db.add(TrustedDevice(ip_address="10.0.0.1", status=DeviceStatus.pending, last_seen=old))
    db.add(TrustedDevice(ip_address="10.0.0.2", status=DeviceStatus.pending))  # recent
    db.add(TrustedDevice(ip_address="10.0.0.3", status=DeviceStatus.approved, last_seen=old))
    db.add(TrustedDevice(ip_address="10.0.0.4", status=DeviceStatus.blocked, last_seen=old))
    db.commit()

    stats = RetentionService.cleanup_auxiliary(db, retention_days=30)
    assert stats["deleted_pending_devices"] == 1
    ips = {d.ip_address for d in db.query(TrustedDevice).all()}
    assert ips == {"10.0.0.2", "10.0.0.3", "10.0.0.4"}


# ── events store + inbox ─────────────────────────────────────────────


def _visit(db, *, age_days: float, with_sidecars: bool = True):
    from models import EventEmbedding, EventText, TimelineEvent, VisitDescriptor
    at = datetime.now(UTC) - timedelta(days=age_days)
    row = TimelineEvent(camera_id=1, label="car", event_type="visit", source="tier0",
                        started_at=at, ended_at=at + timedelta(seconds=20))
    db.add(row)
    db.commit()
    if with_sidecars:
        db.add(EventText(event_id=row.id, caption="a car", source="t"))
        db.add(EventEmbedding(event_id=row.id, vector=b"\x00" * 16, dim=4, model="t"))
        db.add(VisitDescriptor(event_id=row.id, kind="colour", value="red",
                               source_task="vqa", binding="direct"))
        db.commit()
    return row.id


def test_visit_rows_age_out_with_their_evidence(db):
    """RFC-0001: one retention policy, tied to recordings. The JPEGs
    were purged on that window; the rows that pointed at them never
    were, so evidence_url dangled and the table grew without bound."""
    from models import EventEmbedding, EventText, TimelineEvent, VisitDescriptor
    old = _visit(db, age_days=40)
    fresh = _visit(db, age_days=2)
    stats = RetentionService.cleanup_auxiliary(db, retention_days=30)
    assert stats["deleted_events"] == 1
    assert db.get(TimelineEvent, old) is None
    assert db.get(TimelineEvent, fresh) is not None
    # sidecars go with the row — explicitly, so SQLite agrees with Postgres
    for side in (EventText, EventEmbedding, VisitDescriptor):
        assert db.query(side).filter(side.event_id == old).count() == 0
        assert db.query(side).filter(side.event_id == fresh).count() == 1


def test_events_retention_can_differ_from_recordings(db, monkeypatch):
    """EVENTS_RETENTION_DAYS overrides; recordings kept forever (0) keeps
    the rows forever unless the operator says otherwise."""
    from core.config import settings
    from models import TimelineEvent
    old = _visit(db, age_days=40, with_sidecars=False)
    monkeypatch.setattr(settings, "events_retention_days", 0, raising=False)
    assert RetentionService.cleanup_auxiliary(db, retention_days=0)["deleted_events"] == 0
    assert db.get(TimelineEvent, old) is not None
    monkeypatch.setattr(settings, "events_retention_days", 7, raising=False)
    assert RetentionService.cleanup_auxiliary(db, retention_days=0)["deleted_events"] == 1


def _alert(db, *, age_days: float, n: int = 1):
    from models import AppAlert
    for i in range(n):
        db.add(AppAlert(alert_id=f"al-{age_days}-{i}-{secrets.token_hex(3)}",
                        fired_at=datetime.now(UTC) - timedelta(days=age_days),
                        severity="high", title="t"))
    db.commit()


def test_the_inbox_is_pruned_by_age_then_capped(db, monkeypatch):
    """A busy site rings thousands of alerts a day and nothing ever
    removed one."""
    from core.config import settings
    from models import AppAlert
    monkeypatch.setattr(settings, "alerts_inbox_retention_days", 30, raising=False)
    _alert(db, age_days=45, n=3)
    _alert(db, age_days=1, n=5)
    stats = RetentionService.cleanup_auxiliary(db, retention_days=0)
    assert stats["deleted_app_alerts"] == 3
    assert db.query(AppAlert).count() == 5
    monkeypatch.setattr(RetentionService, "APP_ALERTS_MAX_ROWS", 2)
    stats = RetentionService.cleanup_auxiliary(db, retention_days=0)
    assert stats["deleted_app_alerts"] == 3
    assert db.query(AppAlert).count() == 2
