# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Watchdog cross-checks the filesystem before raising recording_stalled.

The recordings table is webhook-fed; a webhook lag while recording continues
would look like a stall. _still_stalled_on_disk must clear the suspicion when
a fresh clip exists on disk, and confirm it when it doesn't.
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

from services.recording_watchdog import _still_stalled_on_disk  # noqa: E402

# tz must not depend on settings load order (settings is a frozen singleton):
import services.recording_paths as _rp  # noqa: E402
from datetime import timezone as _pytz  # noqa: E402
_rp.get_recording_tz = lambda: _pytz.utc


def _touch(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
THRESHOLD = NOW - timedelta(seconds=4 * 60)


def test_fresh_clip_on_disk_clears_the_stall(tmp_path):
    # A clip started 1 minute ago (after threshold) -> not really stalled.
    fresh = tmp_path / "cam-1" / "2026-08-15" / "11" / "59-00-000000.mp4"
    _touch(fresh)
    assert _still_stalled_on_disk(1, tmp_path, THRESHOLD, NOW) is False


def test_only_old_clip_confirms_the_stall(tmp_path):
    # Newest clip is 20 minutes old (before threshold) -> genuinely stalled.
    old = tmp_path / "cam-1" / "2026-08-15" / "11" / "40-00-000000.mp4"
    _touch(old)
    assert _still_stalled_on_disk(1, tmp_path, THRESHOLD, NOW) is True


def test_no_files_confirms_the_stall(tmp_path):
    assert _still_stalled_on_disk(1, tmp_path, THRESHOLD, NOW) is True


def test_no_root_trusts_db_verdict():
    assert _still_stalled_on_disk(1, None, THRESHOLD, NOW) is True
