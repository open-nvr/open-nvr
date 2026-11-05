# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Disk rotation (retention_service): the automatic floor, the proportional
margin, photos never deleted for space, the unreachable-floor verdict and
the rotation event the dashboard reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.test_system_monitoring import db  # noqa: F401 - shared in-memory DB fixture


def _svc():
    from services import retention_service as rs
    return rs


def test_the_floor_is_automatic_unless_set_and_zero_is_off(tmp_path, monkeypatch):
    rs = _svc()
    S = rs.RetentionService
    monkeypatch.setattr(S, "_get_disk_total_gb", staticmethod(lambda _p: 55.0))
    assert S.effective_min_free_gb({}, tmp_path) == 2.0, "small disk: 2 GB"
    monkeypatch.setattr(S, "_get_disk_total_gb", staticmethod(lambda _p: 4000.0))
    assert S.effective_min_free_gb({"min_free_space_gb": None}, tmp_path) == 80.0, "2 % of 4 TB"
    assert S.effective_min_free_gb({"min_free_space_gb": 10}, tmp_path) == 10.0, "the operator's value wins"
    assert S.effective_min_free_gb({"min_free_space_gb": 0}, tmp_path) is None, "0 = off"
    monkeypatch.setattr(S, "_get_disk_total_gb", staticmethod(lambda _p: None))
    assert S.effective_min_free_gb({}, tmp_path) == 2.0, "no disk reading: the fixed default"


def test_the_margin_is_a_quarter_of_the_floor_at_least_half_a_gb():
    S = _svc().RetentionService
    assert S.hysteresis_gb(2.0) == 0.5
    assert S.hysteresis_gb(1.0) == 0.5
    assert S.hysteresis_gb(10.0) == 2.5
    assert S.hysteresis_gb(80.0) == 20.0


def test_rotation_stops_at_floor_plus_margin_not_five_gb_above(db, tmp_path, monkeypatch):  # noqa: F811
    """Floor 2 GB: the purge stops once 2.5 GB are free, so one pass takes an
    hour of footage, not the whole archive."""
    from models import Recording
    rs = _svc(); S = rs.RetentionService
    now = datetime.now(UTC)
    files = []
    for i in range(20):
        f = tmp_path / "cam-1" / f"{i}.mp4"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * 10)
        files.append(f)
        db.add(Recording(filename=f.name, file_path=f"cam-1/{i}.mp4", file_size=10.0,
                         start_time=now - timedelta(minutes=20 - i), camera_id=1))
    db.commit()
    # each deleted file "frees" 0.1 GB from a start of 1.9 GB
    def fake_free(_p):
        return 1.9 + 0.1 * sum(1 for f in files if not f.exists())
    monkeypatch.setattr(S, "_get_disk_free_space_gb", staticmethod(fake_free))
    monkeypatch.setattr(S, "_set_purge_exhausted", staticmethod(lambda *a, **k: None))
    stats = S._cleanup_by_space(db, tmp_path, min_free_space_gb=2.0, protect_flagged=True)
    assert stats["deleted_files"] == 6, "1.9 → 2.5 GB is six files, not sixty"
    assert db.query(Recording).count() == 14
    assert "exhausted" not in stats


def test_photos_are_never_deleted_for_space(db, tmp_path, monkeypatch):  # noqa: F811
    S = _svc().RetentionService
    ev = tmp_path / ".evidence" / "ab" / "abcdef.jpg"
    ev.parent.mkdir(parents=True)
    ev.write_bytes(b"\xff\xd8" + b"x" * 100)
    orphan = tmp_path / "cam-1" / "2026-08-17" / "10" / "00-00-000000.mp4"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"x" * 1024)
    monkeypatch.setattr(S, "_get_disk_free_space_gb", staticmethod(lambda _p: 1.0))   # never clears
    monkeypatch.setattr(S, "_set_purge_exhausted", staticmethod(lambda *a, **k: None))
    stats = S._cleanup_by_space(db, tmp_path, min_free_space_gb=2.0, protect_flagged=True)
    assert not orphan.exists(), "a filesystem-orphan clip is still fair game"
    assert ev.exists(), "the event's photo is not"
    assert stats["exhausted"] is True


def test_an_unreachable_floor_is_named_as_such(db, tmp_path, monkeypatch):  # noqa: F811
    """8 GB free, no recordings, floor 10: no rotation can help; the verdict
    says the space is taken by other data."""
    S = _svc().RetentionService
    monkeypatch.setattr(S, "_get_disk_free_space_gb", staticmethod(lambda _p: 8.0))
    seen = {}
    monkeypatch.setattr(S, "_set_purge_exhausted",
                        staticmethod(lambda active, free, target, description=None: seen.update(d=description)))
    stats = S._cleanup_by_space(db, tmp_path, min_free_space_gb=10.0, protect_flagged=True)
    assert stats["exhausted"] is True and stats["unreachable"] is True
    assert "taken by other data" in seen["d"] and "8.0 GB free" in seen["d"]


def test_rotation_state_reports_the_effective_retention(db, tmp_path, monkeypatch):  # noqa: F811
    from models import Recording
    S = _svc().RetentionService
    now = datetime.now(UTC)
    db.add(Recording(filename="a.mp4", file_path="cam-1/a.mp4", file_size=1024 ** 3,
                     start_time=now - timedelta(hours=9), camera_id=1))
    db.add(Recording(filename="b.mp4", file_path="cam-1/b.mp4", file_size=1024 ** 3,
                     start_time=now - timedelta(hours=1), camera_id=1))
    db.commit()
    monkeypatch.setattr(S, "_get_disk_free_space_gb", staticmethod(lambda _p: 2.1))
    st = S.rotation_state(db, tmp_path, 2.0)
    assert st["effective_retention_hours"] == 9.0 and st["recordings_gb"] == 2.0
    assert st["unreachable"] is False and st["min_free_gb"] == 2.0


def test_check_disk_pressure_raises_and_clears_the_rotation_event(db, tmp_path, monkeypatch):  # noqa: F811
    rs = _svc(); S = rs.RetentionService
    monkeypatch.setattr("services.retention_service.get_effective_recordings_base_path", lambda _db: str(tmp_path))
    monkeypatch.setattr(S, "_get_retention_settings", staticmethod(lambda _db: {"protect_flagged": True}))
    monkeypatch.setattr(S, "_get_disk_total_gb", staticmethod(lambda _p: 55.0))
    events = []
    import services.system_events as se
    monkeypatch.setattr(se, "record_system_event_edge", lambda **kw: events.append(kw))
    monkeypatch.setattr(S, "_set_purge_exhausted", staticmethod(lambda *a, **k: None))
    from models import Recording
    files = []
    for name, hours in (("a", 5), ("b", 1)):
        f = tmp_path / "cam-1" / f"{name}.mp4"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * 10)
        files.append(f)
        db.add(Recording(filename=f.name, file_path=f"cam-1/{name}.mp4", file_size=3 * 1024 ** 3,
                         start_time=datetime.now(UTC) - timedelta(hours=hours), camera_id=1))
    db.commit()
    # below the automatic 2 GB floor with recordings worth rotating: the
    # oldest goes, the target clears, and rotation is raised with the
    # retention the surviving footage gives
    monkeypatch.setattr(S, "_get_disk_free_space_gb",
                        staticmethod(lambda _p: 1.5 if all(f.exists() for f in files) else 2.6))
    stats = S.check_disk_pressure(db)
    assert stats["deleted_files"] == 1 and not files[0].exists() and files[1].exists()
    assert stats is not None
    rot = [e for e in events if e["event_type"] == rs.ROTATION_EVENT]
    assert rot and rot[-1]["active"] is True and "rotating" in rot[-1]["description"]
    # back above floor + margin: cleared
    monkeypatch.setattr(S, "_get_disk_free_space_gb", staticmethod(lambda _p: 2.6))
    assert S.check_disk_pressure(db) is None
    rot = [e for e in events if e["event_type"] == rs.ROTATION_EVENT]
    assert rot[-1]["active"] is False
