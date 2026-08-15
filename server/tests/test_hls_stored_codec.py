# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HLS session prefers the codec stored at segment-complete over re-probing.

The webhook records the clip codec (via the same probe_video_codec used at
playback), so a webhook-indexed session can skip re-opening the file.
Reconciler/legacy rows have NULL codec and must fall back to probing.
"""

from __future__ import annotations

import os
import secrets
import sys
from datetime import UTC, datetime
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


from services.hls_playback_service import HlsPlaybackService  # noqa: E402


class _Q:
    def __init__(self, value):
        self._value = value

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def scalar(self):
        return self._value


class _DB:
    def __init__(self, value):
        self._value = value

    def query(self, *a, **k):
        return _Q(self._value)


START = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def test_returns_stored_codec_when_present():
    assert HlsPlaybackService._stored_codec(1, START, _DB("hev1")) == "hev1"


def test_returns_none_when_column_null():
    assert HlsPlaybackService._stored_codec(1, START, _DB(None)) is None


def test_none_db_is_safe():
    # A broken/None db must not raise; None -> caller falls back to probing.
    assert HlsPlaybackService._stored_codec(1, START, None) is None
