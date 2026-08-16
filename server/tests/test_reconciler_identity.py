# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Reconciler identity gate: adopt / quarantine / ownerless scan. Run with:

    cd server && pytest tests/test_reconciler_identity.py -v

Coverage:
* Upgrade with intact DB: unmarked dir whose footage postdates the camera is
  adopted, stamped and fully indexed.
* Simulated DB wipe: footage predating the re-added camera is NEVER indexed;
  the tree is quarantined to orphaned/ and a fresh stamped dir takes its place.
* Ownerless scan: dirs resolving to no camera are quarantined; an EMPTY
  cameras table skips the scan entirely (misconfigured-DB guard); a dir whose
  marker uuid matches a live camera is left alone.
* Retention: the age sweep skips ownerless and conflicted dirs; orphan aging
  deletes only past-cutoff files under orphaned/.
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

sys.modules.pop("core.logging_config", None)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import services.camera_identity as ci  # noqa: E402
from core.database import Base  # noqa: E402
from models import Camera, Recording, Role, User  # noqa: E402
from services.recording_reconciler import (  # noqa: E402
    quarantine_ownerless_dirs,
    reconcile_camera,
)


@pytest.fixture(autouse=True)
def _utc_tz(monkeypatch):

    import services.recording_paths as rp

    monkeypatch.setattr(rp, "get_recording_tz", lambda: UTC)
    ci.reset_ingest_cache()


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    s = session_factory()
    role = Role(name="admin")
    s.add(role)
    s.commit()
    owner = User(username="t", email="t@t.io", hashed_password="x", role_id=role.id)
    s.add(owner)
    s.commit()
    s.info["owner_id"] = owner.id
    yield s
    s.close()


def _add_camera(db, cam_id=None, uuid_=None, created_at=None, ip="10.0.0.9"):
    import uuid as _uuid

    cam = Camera(
        name=f"Cam {cam_id or '?'}",
        ip_address=ip,
        owner_id=db.info["owner_id"],
        uuid=uuid_ or str(_uuid.uuid4()),
    )
    if cam_id is not None:
        cam.id = cam_id
    db.add(cam)
    db.commit()
    if created_at is not None:
        cam.created_at = created_at
        db.commit()
    db.refresh(cam)
    return cam


def _seg(root: Path, cam: str, day: str, hour: str, name: str, age_s=600) -> Path:
    """A closed (old-mtime) segment file, so the reconciler doesn't skip it as
    still-being-written."""
    p = root / cam / day / hour / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 10)
    old = time.time() - age_s
    os.utime(p, (old, old))
    return p


def test_upgrade_intact_db_adopts_and_indexes(db, tmp_path):
    cam = _add_camera(db, created_at=datetime(2026, 8, 1, tzinfo=UTC))
    _seg(tmp_path, f"cam-{cam.id}", "2026-08-10", "12", "00-00-000000.mp4")
    _seg(tmp_path, f"cam-{cam.id}", "2026-08-10", "12", "01-00-000000.mp4")

    ins, dele = reconcile_camera(db, cam.id, tmp_path, None)
    assert ins == 2 and dele == 0
    marker, _ = ci.read_marker(tmp_path / f"cam-{cam.id}")
    assert marker["camera_uuid"] == cam.uuid
    assert marker["stamped_by"] == "reconciler"
    assert not (tmp_path / ci.ORPHANED_DIR_NAME).exists()


def test_db_wipe_scenario_quarantines_instead_of_indexing(db, tmp_path):
    # Old footage on disk from the pre-wipe camera 1...
    _seg(tmp_path, "cam-1", "2026-08-10", "12", "00-00-000000.mp4")
    _seg(tmp_path, "cam-1", "2026-08-11", "09", "30-00-000000.mp4")
    # ...and a freshly re-added camera that got id=1 again.
    cam = _add_camera(db, cam_id=1, created_at=datetime.now(UTC))

    ins, _dele = reconcile_camera(db, cam.id, tmp_path, None)

    assert ins == 0, "inherited footage must never be indexed as the new camera's"
    assert db.query(Recording).count() == 0
    orphans = list((tmp_path / ci.ORPHANED_DIR_NAME).iterdir())
    assert len(orphans) == 1
    assert len(list(orphans[0].rglob("*.mp4"))) == 2  # footage preserved
    marker, _ = ci.read_marker(tmp_path / "cam-1")
    assert marker["camera_uuid"] == cam.uuid  # fresh dir stamped for new camera


def test_ownerless_scan_quarantines_unclaimed_dirs(db, tmp_path):
    cam = _add_camera(db, created_at=datetime(2026, 8, 1, tzinfo=UTC))
    # cam-<id> owned; cam-99 ownerless with footage; cam-99-sub ignored.
    _seg(tmp_path, f"cam-{cam.id}", "2026-08-10", "12", "00-00-000000.mp4")
    _seg(tmp_path, "cam-99", "2026-08-10", "12", "00-00-000000.mp4")
    _seg(tmp_path, "cam-99-sub", "2026-08-10", "12", "00-00-000000.mp4")

    n = quarantine_ownerless_dirs(db, tmp_path)
    assert n == 1
    assert not (tmp_path / "cam-99").exists()
    assert (tmp_path / f"cam-{cam.id}").exists()
    assert (tmp_path / "cam-99-sub").exists()


def test_ownerless_scan_skipped_when_cameras_table_empty(db, tmp_path):
    # Misconfigured/fresh DATABASE_URL guard: nothing may be quarantined.
    _seg(tmp_path, "cam-1", "2026-08-10", "12", "00-00-000000.mp4")
    _seg(tmp_path, "cam-2", "2026-08-10", "12", "00-00-000000.mp4")

    n = quarantine_ownerless_dirs(db, tmp_path)
    assert n == 0
    assert (tmp_path / "cam-1").exists() and (tmp_path / "cam-2").exists()
    assert not (tmp_path / ci.ORPHANED_DIR_NAME).exists()


def test_ownerless_scan_spares_dir_whose_marker_matches_live_camera(db, tmp_path):
    cam = _add_camera(db, ip="10.0.0.77", created_at=datetime(2026, 8, 1, tzinfo=UTC))
    # Dir under a stale name that no longer resolves (e.g. old ip-mode name),
    # but the marker inside belongs to a live camera.
    stale = tmp_path / "cam-10_0_0_99"
    _seg(tmp_path, "cam-10_0_0_99", "2026-08-10", "12", "00-00-000000.mp4")
    ci.stamp_marker(stale, cam, "test")

    n = quarantine_ownerless_dirs(db, tmp_path)
    assert n == 0
    assert stale.exists()


def test_age_sweep_skips_unowned_and_conflicted_dirs(db, tmp_path):
    from services.retention_service import RetentionService

    cam = _add_camera(db, created_at=datetime(2026, 8, 1, tzinfo=UTC))
    other = Camera(
        id=555, uuid="55555555-aaaa-bbbb-cccc-000000000005",
        name="Old", ip_address="10.9.9.9",
    )

    # All files far older than the cutoff — eligible by age everywhere.
    owned = _seg(tmp_path, f"cam-{cam.id}", "2026-01-05", "12", "00-00-000000.mp4")
    ownerless = _seg(tmp_path, "cam-99", "2026-01-05", "12", "00-00-000000.mp4")
    conflicted_dir = tmp_path / f"cam-{cam.id + 1}"
    conflicted = _seg(
        tmp_path, f"cam-{cam.id + 1}", "2026-01-05", "12", "00-00-000000.mp4"
    )
    _add_camera(db, cam_id=cam.id + 1, created_at=datetime.now(UTC))
    ci.stamp_marker(conflicted_dir, other, "test")  # marker of a DIFFERENT camera

    cutoff = datetime(2026, 6, 1, tzinfo=UTC)
    RetentionService._cleanup_by_age(db, tmp_path, cutoff, protect_flagged=False)

    assert not owned.exists(), "owned unindexed straggler still ages out"
    assert ownerless.exists(), "ownerless footage must await quarantine, not die"
    assert conflicted.exists(), "conflicted footage must await quarantine, not die"


def test_orphan_aging_honors_cutoff(tmp_path):
    from services.retention_service import RetentionService

    orphan = tmp_path / ci.ORPHANED_DIR_NAME / "cam-1--dead--20260101T000000"
    old = orphan / "2026-01-05" / "12" / "00-00-000000.mp4"
    new = orphan / "2026-08-10" / "12" / "00-00-000000.mp4"
    for p in (old, new):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    cutoff = datetime(2026, 6, 1, tzinfo=UTC)
    stats = RetentionService._cleanup_orphans_by_age(tmp_path, cutoff)

    assert stats["deleted_files"] == 1
    assert not old.exists()
    assert new.exists()
    assert orphan.exists()  # still holds footage -> tree stays


def test_orphan_aging_removes_fully_aged_tree(tmp_path):
    from services.retention_service import RetentionService

    orphan = tmp_path / ci.ORPHANED_DIR_NAME / "cam-1--dead--20260101T000000"
    old = orphan / "2026-01-05" / "12" / "00-00-000000.mp4"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"x")
    (orphan / ci.ORPHAN_INFO_FILENAME).write_text("{}", encoding="utf-8")

    RetentionService._cleanup_orphans_by_age(
        tmp_path, datetime(2026, 6, 1, tzinfo=UTC)
    )
    assert not orphan.exists()
