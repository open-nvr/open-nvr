# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The playback live edge.

Only the unfinished TAIL of a still-recording file is withheld from playback.
An earlier revision treated the whole in-progress file as live, which hid ~35
minutes of perfectly playable footage behind a "still recording" notice.
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from datetime import datetime, timedelta
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

from routers.recordings import (  # noqa: E402
    LIVE_EDGE_LAG_SECONDS,
    _live_edge_iso,
)

NOW = datetime(2026, 7, 26, 10, 30, 0)


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.rstrip("Z"))


def test_edge_trails_now_by_the_lag():
    """A 35-minute-old recording keeps all but its last minute playable."""
    file_start = NOW - timedelta(minutes=35)
    edge = _parse(_live_edge_iso(file_start, NOW))
    assert edge == NOW - timedelta(seconds=LIVE_EDGE_LAG_SECONDS)
    # 34 of the 35 minutes are on the playable side of the edge
    assert (edge - file_start).total_seconds() == 34 * 60


def test_lag_is_one_minute():
    assert LIVE_EDGE_LAG_SECONDS == 60


def test_edge_never_precedes_the_file_start():
    """A recording that just began is live in its entirety — the edge must not
    point before footage exists."""
    file_start = NOW - timedelta(seconds=10)
    assert _parse(_live_edge_iso(file_start, NOW)) == file_start


def test_edge_at_exactly_the_lag_boundary():
    file_start = NOW - timedelta(seconds=LIVE_EDGE_LAG_SECONDS)
    assert _parse(_live_edge_iso(file_start, NOW)) == file_start


def test_edge_advances_with_wall_clock():
    """The green zone slides forward as recording continues, so footage becomes
    playable as it ages past the lag."""
    file_start = NOW - timedelta(minutes=10)
    first = _parse(_live_edge_iso(file_start, NOW))
    later = _parse(_live_edge_iso(file_start, NOW + timedelta(minutes=5)))
    assert later - first == timedelta(minutes=5)


def test_custom_lag_is_honoured():
    file_start = NOW - timedelta(minutes=10)
    edge = _parse(_live_edge_iso(file_start, NOW, lag_seconds=180))
    assert edge == NOW - timedelta(seconds=180)


def test_emits_utc_marker_like_the_rest_of_the_router():
    assert _live_edge_iso(NOW - timedelta(minutes=5), NOW).endswith("Z")
