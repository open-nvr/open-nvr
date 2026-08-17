# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""System resource monitoring: the threshold state machine (sustained raise,
hysteresis resolve, dead band), and the retention fail-safe / no-progress
guards it depends on."""

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

os.environ.setdefault("DATABASE_URL", "sqlite:///./_sysmon_test.db")
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
from models import Recording, SystemEvent  # noqa: E402
from schemas import SystemMonitoringSettings  # noqa: E402
from services.retention_service import RetentionService  # noqa: E402
from services.system_monitor_service import (  # noqa: E402
    ALERT_CPU_HIGH,
    ALERT_DISK_LOW,
    ALERT_DISK_STAT_ERROR,
    SystemMonitorService,
)


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------- state machine


def _mk_monitor() -> SystemMonitorService:
    m = SystemMonitorService()
    m._active = {}  # skip DB seeding — tests drive the machine directly
    return m


def _sample(ts: float, cpu=None, mem_pct=None, disk=None, disk_error=None):
    return {
        "ts": ts,
        "cpu_percent": cpu,
        "memory": {"total": 100, "used": mem_pct or 0, "percent": mem_pct}
        if mem_pct is not None
        else None,
        "disk": disk,
        "disk_error": disk_error,
    }


def _settings(**kw) -> SystemMonitoringSettings:
    base = dict(
        cpu_percent_threshold=90,
        memory_percent_threshold=None,
        sustained_seconds=60,
        disk_min_free_gb=None,
        disk_used_percent_threshold=None,
        resolve_hysteresis_percent=5,
    )
    base.update(kw)
    return SystemMonitoringSettings(**base)


def test_cpu_alert_requires_sustained_breach():
    m = _mk_monitor()
    s = _settings()
    # First breach starts the timer — no alert yet.
    assert m.evaluate(_sample(0, cpu=95), s) == []
    # Still inside the sustain window.
    assert m.evaluate(_sample(30, cpu=97), s) == []
    # Past the window -> raise.
    out = m.evaluate(_sample(60, cpu=96), s)
    assert [(t["event_type"], t["state"]) for t in out] == [(ALERT_CPU_HIGH, "active")]


def test_cpu_spike_below_window_never_alerts():
    m = _mk_monitor()
    s = _settings()
    m.evaluate(_sample(0, cpu=95), s)
    # Dips below threshold before the window elapses -> timer resets.
    assert m.evaluate(_sample(30, cpu=50), s) == []
    assert m.evaluate(_sample(45, cpu=95), s) == []
    assert m.evaluate(_sample(90, cpu=95), s) == []  # only 45s into new breach
    out = m.evaluate(_sample(106, cpu=95), s)
    assert [(t["event_type"], t["state"]) for t in out] == [(ALERT_CPU_HIGH, "active")]


def test_cpu_resolve_needs_hysteresis_band():
    m = _mk_monitor()
    s = _settings()
    m.evaluate(_sample(0, cpu=95), s)
    m.evaluate(_sample(60, cpu=95), s)  # raised
    # 88% is below threshold but inside the dead band (>= 90-5) -> stays active.
    assert m.evaluate(_sample(120, cpu=88), s) == []
    # Clear band, but must be sustained too.
    assert m.evaluate(_sample(180, cpu=50), s) == []
    out = m.evaluate(_sample(240, cpu=50), s)
    assert [(t["event_type"], t["state"]) for t in out] == [
        (ALERT_CPU_HIGH, "inactive")
    ]


def test_disk_low_by_percent_and_severity_escalation():
    m = _mk_monitor()
    s = _settings(cpu_percent_threshold=None, disk_used_percent_threshold=90)
    gib = 1024**3
    disk = {"path": "/r", "total": 100 * gib, "used": 99 * gib,
            "free": 1 * gib, "percent": 99.0}
    m.evaluate(_sample(0, disk=disk), s)
    out = m.evaluate(_sample(15, disk=disk), s)  # one confirming sample
    assert len(out) == 1
    assert out[0]["event_type"] == ALERT_DISK_LOW
    assert out[0]["severity"] == "critical"  # >= 98% used


def test_disk_stat_error_raises_and_resolves():
    m = _mk_monitor()
    s = _settings(cpu_percent_threshold=None)
    m.evaluate(_sample(0, disk_error="boom"), s)
    out = m.evaluate(_sample(15, disk_error="boom"), s)
    assert [(t["event_type"], t["state"]) for t in out] == [
        (ALERT_DISK_STAT_ERROR, "active")
    ]
    gib = 1024**3
    disk = {"path": "/r", "total": 100 * gib, "used": 10 * gib,
            "free": 90 * gib, "percent": 10.0}
    m.evaluate(_sample(30, disk=disk), s)
    out = m.evaluate(_sample(45, disk=disk), s)
    assert [(t["event_type"], t["state"]) for t in out] == [
        (ALERT_DISK_STAT_ERROR, "inactive")
    ]


# ------------------------------------------------------------------- retention


def test_disk_free_space_fail_safe(monkeypatch):
    import shutil as _shutil

    def boom(_p):
        raise OSError("mount gone")

    monkeypatch.setattr(_shutil, "disk_usage", boom)
    assert RetentionService._get_disk_free_space_gb("/nope") is None


def test_check_disk_pressure_skips_on_stat_error(db, monkeypatch, tmp_path):
    monkeypatch.setattr(
        RetentionService, "_get_disk_free_space_gb", staticmethod(lambda _p: None)
    )
    monkeypatch.setattr(
        "services.retention_service.get_effective_recordings_base_path",
        lambda _db: str(tmp_path),
    )
    monkeypatch.setattr(
        RetentionService,
        "_get_retention_settings",
        staticmethod(lambda _db: {"min_free_space_gb": 10, "protect_flagged": True}),
    )
    assert RetentionService.check_disk_pressure(db) is None


def test_cleanup_by_space_no_progress_guard(db, tmp_path, monkeypatch):
    """Rows whose files are already gone must not spin-delete the whole index."""
    now = datetime.now(UTC)
    for i in range(1200):
        db.add(
            Recording(
                filename=f"f{i}.mp4",
                file_path=f"cam-1/{i}.mp4",  # never exists on disk
                start_time=now - timedelta(minutes=1200 - i),
                camera_id=1,
            )
        )
    db.commit()

    # Disk stays below target forever (no bytes are ever freed).
    monkeypatch.setattr(
        RetentionService, "_get_disk_free_space_gb", staticmethod(lambda _p: 1.0)
    )
    # Keep the exhausted-event edge writer away from the global SessionLocal.
    monkeypatch.setattr(
        RetentionService, "_set_purge_exhausted", staticmethod(lambda *a, **k: None)
    )

    stats = RetentionService._cleanup_by_space(
        db, tmp_path, min_free_space_gb=10, protect_flagged=True
    )
    # One drifted batch is consumed, then the guard breaks — the other 700
    # index rows survive.
    assert stats["deleted_files"] == 0
    assert stats["exhausted"] is True
    assert db.query(Recording).count() == 1200 - RetentionService.DELETE_BATCH_SIZE


def test_cleanup_by_space_purges_fs_orphans_when_index_empty(db, tmp_path, monkeypatch):
    """With no indexed rows, the space purge falls back to filesystem orphans."""
    cam_dir = tmp_path / "cam-1" / "2026-08-17" / "10"
    cam_dir.mkdir(parents=True)
    orphan = cam_dir / "00-00-000000.mp4"
    orphan.write_bytes(b"x" * 1024)

    calls = {"n": 0}

    def fake_free(_p):
        # Below target until the orphan is gone, then above.
        calls["n"] += 1
        return 100.0 if not orphan.exists() else 1.0

    monkeypatch.setattr(
        RetentionService, "_get_disk_free_space_gb", staticmethod(fake_free)
    )
    monkeypatch.setattr(
        RetentionService, "_set_purge_exhausted", staticmethod(lambda *a, **k: None)
    )

    stats = RetentionService._cleanup_by_space(
        db, tmp_path, min_free_space_gb=10, protect_flagged=True
    )
    assert not orphan.exists()
    assert stats["deleted_files"] == 1


def test_system_events_pruned_by_age_and_cap(db):
    old = SystemEvent(
        event_type="disk_low",
        severity="warning",
        created_at=datetime.now(UTC) - timedelta(days=45),
    )
    fresh = SystemEvent(
        event_type="disk_low",
        severity="warning",
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    db.add_all([old, fresh])
    db.commit()

    stats = RetentionService.cleanup_auxiliary(db, retention_days=30)
    assert stats["deleted_system_events"] == 1
    assert db.query(SystemEvent).count() == 1
