# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Timeline gap/contiguity uses the wall-clock end_time, not media duration.

Regression cover for #229: cameras whose delivered fps trails the nominal fps
produce a "60s" media clip that covers ~72 real seconds. merge_contiguous must
close the gap between such back-to-back clips using each clip's wall-clock
end_time; deriving contiguity from ``start + media_duration`` left phantom
10-15s gaps. This exercises the actual timeline code path (day_segments ->
merge_contiguous), which the pure compute_segment_end_time unit test did not.
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


from services.recording_query_service import merge_contiguous  # noqa: E402

START = datetime(2026, 8, 14, 13, 0, 0, tzinfo=UTC)


def _sec(n: float) -> timedelta:
    return timedelta(seconds=n)


def test_slow_clock_backtoback_clips_merge_without_gap():
    # Two "60s" media clips that each cover 72 wall seconds, recorded
    # back-to-back. end_time reflects the real (wall) coverage.
    rows = [
        (START, 60.0, START + _sec(72)),
        (START + _sec(72), 60.0, START + _sec(144)),
    ]
    merged = merge_contiguous(rows)
    assert len(merged) == 1, f"expected one continuous run, got {merged}"
    assert merged[0]["start"] == START
    assert merged[0]["duration"] == 144.0  # wall span, no phantom gap


def test_old_behaviour_would_have_split_these():
    # Same clips, but with only media duration (end_time = start + duration)
    # produce a 12s gap per boundary and therefore split — this is exactly the
    # #229 regression, asserted here to lock the fix in.
    naive_rows = [
        (START, 60.0, START + _sec(60)),
        (START + _sec(72), 60.0, START + _sec(132)),
    ]
    merged = merge_contiguous(naive_rows)
    assert len(merged) == 2  # 12s gap > 2s tolerance -> split (unchanged/correct)


def test_real_gap_still_splits():
    rows = [
        (START, 60.0, START + _sec(60)),
        (START + _sec(120), 60.0, START + _sec(180)),  # 60s real gap
    ]
    merged = merge_contiguous(rows)
    assert len(merged) == 2


def test_null_end_time_falls_back_to_media_duration():
    # Legacy / reconciler rows may have NULL end_time; behaviour must fall back
    # to start + duration and still merge contiguous clips.
    rows = [
        (START, 60.0, None),
        (START + _sec(60), 60.0, None),
    ]
    merged = merge_contiguous(rows)
    assert len(merged) == 1
    assert merged[0]["duration"] == 120.0


def test_two_tuple_rows_still_supported():
    # Backward-compatible with (start, duration) rows.
    merged = merge_contiguous([(START, 60.0), (START + _sec(60), 60.0)])
    assert len(merged) == 1
    assert merged[0]["duration"] == 120.0
