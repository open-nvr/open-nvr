# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Wall-clock anchoring of a completed segment's end_time.

Cameras with counter-stamped RTP timestamps (delivered fps below the declared
fps) produce segments whose media duration under-counts the wall time they
cover — a "60s" segment spanning ~72 real seconds. Deriving end_time purely
from media duration fabricated 10-15s phantom gaps between back-to-back
segments. compute_segment_end_time takes the later of the media end and the
file's close mtime, so the timeline reflects real coverage.
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

from routers.mediamtx_hooks import compute_segment_end_time  # noqa: E402

START = datetime(2026, 8, 14, 13, 29, 32, tzinfo=UTC)


def _mtime(dt: datetime) -> float:
    return dt.timestamp()


def test_honest_camera_uses_wall_close():
    # mtime trails the media end by flush lag; wall clock wins.
    end = compute_segment_end_time(START, 60.0, _mtime(START + timedelta(seconds=60.5)))
    assert end == START + timedelta(seconds=60.5)


def test_slow_clock_camera_extends_to_wall_close():
    # The .104 pathology: 60s of media delivered over 72 wall seconds.
    end = compute_segment_end_time(START, 60.0, _mtime(START + timedelta(seconds=72)))
    assert end == START + timedelta(seconds=72)


def test_bogus_mtime_capped_at_twice_duration():
    end = compute_segment_end_time(START, 60.0, _mtime(START + timedelta(seconds=300)))
    assert end == START + timedelta(seconds=120)


def test_mtime_before_media_end_keeps_media_end():
    end = compute_segment_end_time(START, 60.0, _mtime(START + timedelta(seconds=10)))
    assert end == START + timedelta(seconds=60)


def test_no_mtime_falls_back_to_media_end():
    assert compute_segment_end_time(START, 60.0, None) == START + timedelta(seconds=60)


def test_no_duration_uses_wall_close():
    end = compute_segment_end_time(START, None, _mtime(START + timedelta(seconds=30)))
    assert end == START + timedelta(seconds=30)


def test_no_duration_no_mtime_is_start():
    assert compute_segment_end_time(START, None, None) == START


def test_never_before_start():
    end = compute_segment_end_time(START, None, _mtime(START - timedelta(seconds=5)))
    assert end == START
