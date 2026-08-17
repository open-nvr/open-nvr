# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""GET /recordings/frame — resolve the clip containing an instant, extract one
JPEG. Covers the clip-resolution logic and the extract failure path (the parts
that don't need a running ffmpeg + real footage; end-to-end is a QA check)."""
from __future__ import annotations

import os
import secrets
import sys
import types
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
sys.modules.pop("core.logging_config", None)

from routers.recordings import _clip_for_instant, _extract_recording_frame  # noqa: E402


class _Q:
    def __init__(self, clip):
        self._clip = clip
    def filter(self, *a, **k):
        return self
    def order_by(self, *a, **k):
        return self
    def first(self):
        return self._clip


class _DB:
    def __init__(self, clip):
        self._clip = clip
    def query(self, *a, **k):
        return _Q(self._clip)


def _clip(start, end):
    return types.SimpleNamespace(start_time=start, end_time=end, file_path="cam-1/x.mp4")


START = datetime(2026, 8, 14, 15, 12, 0, tzinfo=UTC)


def test_returns_clip_containing_instant():
    clip = _clip(START, START + timedelta(seconds=60))
    assert _clip_for_instant(_DB(clip), 1, START + timedelta(seconds=30)) is clip


def test_none_when_instant_falls_in_gap_after_clip():
    clip = _clip(START, START + timedelta(seconds=60))
    # 10 minutes after this clip ends -> a gap, not this clip.
    assert _clip_for_instant(_DB(clip), 1, START + timedelta(seconds=600)) is None


def test_none_when_no_clip_before_instant():
    assert _clip_for_instant(_DB(None), 1, START) is None


def test_slack_past_clip_end_still_matches():
    clip = _clip(START, START + timedelta(seconds=60))
    # 1s past the end is within the boundary slack.
    assert _clip_for_instant(_DB(clip), 1, START + timedelta(seconds=61)) is clip


def test_extract_missing_file_returns_none():
    assert _extract_recording_frame("/no/such/recording.mp4", 1.0, timeout=5) is None
