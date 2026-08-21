# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Periodic reconciler scans only the window's date dirs, not the whole tree.

iter_recording_files_between must return files inside the window (plus a small
day margin) and must NOT descend into date directories far outside it — so a
large multi-month archive isn't rglob'd every 15 minutes.
"""

from __future__ import annotations

import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

# A sibling test (test_segment_end_time) registers a stub 'core.logging_config'
# in sys.modules via setdefault; drop it so the REAL module loads before we
# import server code that does `from core.logging_config import recording_logger`.
sys.modules.pop("core.logging_config", None)


# Force a deterministic recording timezone (UTC) for local-named dir mapping.

from services.recording_paths import iter_recording_files_between  # noqa: E402


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _force_utc_recording_tz(monkeypatch):
    """Pin the recording tz to UTC for these path-layout tests, restored after
    each test — never a global mutation that leaks into the rest of the suite.
    """
    from datetime import timezone

    import services.recording_paths as rp

    monkeypatch.setattr(rp, "get_recording_tz", lambda: timezone.utc)


def _touch(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


def test_window_scan_includes_in_window_excludes_far_files(tmp_path):
    root = tmp_path
    cam = root / "cam-1"
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)

    today = cam / "2026-08-15" / "12" / "00-00-000000.mp4"
    yesterday = cam / "2026-08-14" / "23" / "59-00-000000.mp4"
    long_ago = cam / "2026-06-01" / "10" / "00-00-000000.mp4"  # ~2.5 months old
    for p in (today, yesterday, long_ago):
        _touch(p)

    window_start = now - timedelta(hours=48)
    got = set(iter_recording_files_between(1, root, window_start, now))

    assert today in got
    assert yesterday in got
    assert long_ago not in got  # far-past dir not walked


def test_window_scan_covers_legacy_utc_layout(tmp_path):
    root = tmp_path
    cam = root / "cam-2"
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    legacy = cam / "2026" / "08" / "15" / "11-30-00-000000.mp4"
    legacy_nested = cam / "2026" / "08" / "14" / "10-00-00-000000" / "cam-2.mp4"
    _touch(legacy)
    _touch(legacy_nested)

    got = set(iter_recording_files_between(2, root, now - timedelta(hours=48), now))
    assert legacy in got
    assert legacy_nested in got


def test_missing_camera_dir_yields_nothing(tmp_path):
    got = list(iter_recording_files_between(9, tmp_path, datetime.now(UTC), datetime.now(UTC)))
    assert got == []
