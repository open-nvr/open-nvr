# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Camera identity markers, classification and quarantine. Run with:

    cd server && pytest tests/test_camera_identity.py -v

Coverage:
* Marker round-trip, atomic write (no tmp leftovers), corrupt marker handling.
* classify_dir state machine: EMPTY / MATCH / ADOPTABLE / CONFLICT, including
  the created_at rule with its skew grace on unmarked (pre-upgrade) dirs.
* quarantine_dir: rename-only move-aside, .orphan-info.json, layout preserved,
  never overwrites an existing destination.
* verify_segment_identity webhook gate: conflict -> refuse, adoptable ->
  stamp + allow.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
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

import services.camera_identity as ci  # noqa: E402
from models import Camera  # noqa: E402


@pytest.fixture(autouse=True)
def _utc_tz(monkeypatch):

    import services.recording_paths as rp

    monkeypatch.setattr(rp, "get_recording_tz", lambda: UTC)
    ci.reset_ingest_cache()


def _camera(cam_id=1, uuid="11111111-aaaa-bbbb-cccc-000000000001", **kw):
    kw.setdefault("name", f"Cam {cam_id}")
    kw.setdefault("ip_address", "10.0.0.5")
    kw.setdefault("created_at", datetime(2026, 8, 1, tzinfo=UTC))
    cam = Camera(id=cam_id, uuid=uuid, **kw)
    return cam


def _seg(root: Path, cam: str, day: str, hour: str, name: str) -> Path:
    p = root / cam / day / hour / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


# ---- marker I/O -----------------------------------------------------------


def test_marker_round_trip_and_atomicity(tmp_path):
    cam = _camera()
    d = tmp_path / "cam-1"
    ci.stamp_marker(d, cam, "test")
    marker, corrupt = ci.read_marker(d)
    assert not corrupt
    assert marker["camera_uuid"] == cam.uuid
    assert marker["camera_id"] == 1
    assert marker["camera_name"] == "Cam 1"
    assert marker["stamped_by"] == "test"
    # atomic write leaves no tmp files behind
    assert [p.name for p in d.iterdir()] == [ci.MARKER_FILENAME]


def test_corrupt_marker_reads_as_missing(tmp_path):
    d = tmp_path / "cam-1"
    d.mkdir()
    (d / ci.MARKER_FILENAME).write_text("{not json", encoding="utf-8")
    marker, corrupt = ci.read_marker(d)
    assert marker is None and corrupt


# ---- classify_dir ---------------------------------------------------------


def test_classify_missing_or_fileless_dir_is_empty(tmp_path):
    cam = _camera()
    assert ci.classify_dir(tmp_path / "cam-1", cam) == (ci.EMPTY, None)
    (tmp_path / "cam-1").mkdir()
    assert ci.classify_dir(tmp_path / "cam-1", cam) == (ci.EMPTY, None)


def test_classify_matching_marker(tmp_path):
    cam = _camera()
    d = tmp_path / "cam-1"
    _seg(tmp_path, "cam-1", "2026-08-10", "12", "00-00-000000.mp4")
    ci.stamp_marker(d, cam, "test")
    assert ci.classify_dir(d, cam) == (ci.MATCH, None)


def test_classify_mismatched_marker_is_conflict(tmp_path):
    old = _camera(uuid="22222222-aaaa-bbbb-cccc-000000000002")
    new = _camera(uuid="33333333-aaaa-bbbb-cccc-000000000003")
    d = tmp_path / "cam-1"
    _seg(tmp_path, "cam-1", "2026-08-10", "12", "00-00-000000.mp4")
    ci.stamp_marker(d, old, "test")
    assert ci.classify_dir(d, new) == (ci.CONFLICT, ci.REASON_UUID_MISMATCH)


def test_classify_unmarked_newer_than_camera_is_adoptable(tmp_path):
    # Normal upgrade: camera row is older than all its footage.
    cam = _camera(created_at=datetime(2026, 8, 1, tzinfo=UTC))
    _seg(tmp_path, "cam-1", "2026-08-10", "12", "00-00-000000.mp4")
    assert ci.classify_dir(tmp_path / "cam-1", cam) == (ci.ADOPTABLE, None)


def test_classify_unmarked_predating_camera_is_conflict(tmp_path):
    # Wipe + re-add: footage exists from before the camera row was created.
    cam = _camera(created_at=datetime(2026, 8, 15, tzinfo=UTC))
    _seg(tmp_path, "cam-1", "2026-08-10", "12", "00-00-000000.mp4")
    assert ci.classify_dir(tmp_path / "cam-1", cam) == (
        ci.CONFLICT,
        ci.REASON_PREDATES_CAMERA,
    )


def test_classify_created_at_grace_absorbs_small_skew(tmp_path):
    # File 30 minutes older than created_at: within the 1h grace -> adoptable.
    cam = _camera(created_at=datetime(2026, 8, 10, 12, 30, tzinfo=UTC))
    _seg(tmp_path, "cam-1", "2026-08-10", "12", "00-00-000000.mp4")
    assert ci.classify_dir(tmp_path / "cam-1", cam) == (ci.ADOPTABLE, None)


def test_classify_legacy_layout_predating_camera_is_conflict(tmp_path):
    cam = _camera(created_at=datetime(2026, 8, 15, tzinfo=UTC))
    p = tmp_path / "cam-1" / "2026" / "07" / "01" / "10-00-00-000000.mp4"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    assert ci.classify_dir(tmp_path / "cam-1", cam) == (
        ci.CONFLICT,
        ci.REASON_PREDATES_CAMERA,
    )


def test_classify_corrupt_marker_falls_back_to_created_at_rule(tmp_path):
    cam = _camera(created_at=datetime(2026, 8, 1, tzinfo=UTC))
    d = tmp_path / "cam-1"
    _seg(tmp_path, "cam-1", "2026-08-10", "12", "00-00-000000.mp4")
    (d / ci.MARKER_FILENAME).write_text("garbage", encoding="utf-8")
    assert ci.classify_dir(d, cam) == (ci.ADOPTABLE, None)


# ---- quarantine -----------------------------------------------------------


def test_quarantine_moves_tree_and_writes_info(tmp_path):
    old = _camera(uuid="22222222-aaaa-bbbb-cccc-000000000002")
    d = tmp_path / "cam-1"
    seg = _seg(tmp_path, "cam-1", "2026-08-10", "12", "00-00-000000.mp4")
    ci.stamp_marker(d, old, "test")
    marker, _ = ci.read_marker(d)

    dest = ci.quarantine_dir(d, tmp_path, ci.REASON_UUID_MISMATCH, marker, "test")
    assert dest is not None
    assert not d.exists()
    assert dest.parent == tmp_path / ci.ORPHANED_DIR_NAME
    assert dest.name.startswith("cam-1--22222222--")
    # internal layout preserved, old marker traveled along
    assert (dest / "2026-08-10" / "12" / seg.name).is_file()
    assert (dest / ci.MARKER_FILENAME).is_file()
    info = json.loads((dest / ci.ORPHAN_INFO_FILENAME).read_text(encoding="utf-8"))
    assert info["reason"] == ci.REASON_UUID_MISMATCH
    assert info["original_dir"] == "cam-1"
    assert info["old_marker"]["camera_uuid"] == old.uuid


def test_quarantine_never_overwrites_existing_destination(tmp_path, monkeypatch):
    # Freeze the timestamp so both quarantines target the same base name.
    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 16, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(ci, "datetime", _FrozenDT)
    for _ in range(2):
        _seg(tmp_path, "cam-1", "2026-08-10", "12", "00-00-000000.mp4")
        assert ci.quarantine_dir(
            tmp_path / "cam-1", tmp_path, ci.REASON_NO_OWNER, None, "test"
        )
    names = sorted(p.name for p in (tmp_path / ci.ORPHANED_DIR_NAME).iterdir())
    assert len(names) == 2 and len(set(names)) == 2  # distinct dirs, no clobber


def test_resolve_conflict_quarantines_and_restamps(tmp_path):
    old = _camera(uuid="22222222-aaaa-bbbb-cccc-000000000002")
    new = _camera(uuid="33333333-aaaa-bbbb-cccc-000000000003")
    d = tmp_path / "cam-1"
    _seg(tmp_path, "cam-1", "2026-08-10", "12", "00-00-000000.mp4")
    ci.stamp_marker(d, old, "test")

    assert ci.resolve_conflict(d, tmp_path, new, ci.REASON_UUID_MISMATCH, "test")
    # fresh dir, stamped for the new camera, no footage inside
    marker, _ = ci.read_marker(d)
    assert marker["camera_uuid"] == new.uuid
    assert not list(d.rglob("*.mp4"))
    # old tree intact in orphaned/
    orphans = list((tmp_path / ci.ORPHANED_DIR_NAME).iterdir())
    assert len(orphans) == 1
    assert list(orphans[0].rglob("*.mp4"))


# ---- webhook gate ---------------------------------------------------------


def test_verify_segment_identity_conflict_refuses(tmp_path):
    old = _camera(uuid="22222222-aaaa-bbbb-cccc-000000000002")
    new = _camera(uuid="33333333-aaaa-bbbb-cccc-000000000003")
    d = tmp_path / "cam-1"
    _seg(tmp_path, "cam-1", "2026-08-10", "12", "00-00-000000.mp4")
    ci.stamp_marker(d, old, "test")

    assert ci.verify_segment_identity(new, "cam-1", tmp_path) is False
    # cached decision is stable
    assert ci.verify_segment_identity(new, "cam-1", tmp_path) is False


def test_verify_segment_identity_adopts_and_stamps(tmp_path):
    cam = _camera(created_at=datetime(2026, 8, 1, tzinfo=UTC))
    _seg(tmp_path, "cam-1", "2026-08-10", "12", "00-00-000000.mp4")

    assert ci.verify_segment_identity(cam, "cam-1", tmp_path) is True
    marker, _ = ci.read_marker(tmp_path / "cam-1")
    assert marker["camera_uuid"] == cam.uuid
    assert marker["stamped_by"] == "webhook"
