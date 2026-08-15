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
import types as _types
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

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

# Force a deterministic recording timezone (UTC) for local-named dir mapping.

from services.recording_paths import iter_recording_files_between  # noqa: E402

# tz must not depend on settings load order (settings is a frozen singleton):
import services.recording_paths as _rp  # noqa: E402
from datetime import timezone as _pytz  # noqa: E402
_rp.get_recording_tz = lambda: _pytz.utc


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
