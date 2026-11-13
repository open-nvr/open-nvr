# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Which playback path a session gets: the HLS byte-range path, or the
video-only remux. H.264 recorded with G.711 audio (ipcm) used to take the HLS
path and stall in every browser; it must take the remux, while H.264 with AAC
keeps HLS (and its audio)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.hls_playback_service import HlsPlaybackService, PlaybackSession
from tests.test_hevc_remux import _fragment, _init_segment


def _clip(path, vcodec: bytes, acodec: bytes) -> None:
    path.write_bytes(
        _init_segment(vcodec, acodec)
        + _fragment(1, [b"IDR0", b"P1"], [b"A0"])
        + _fragment(2, [b"IDR2"], [b"A1"])
    )


def _session(start: datetime) -> PlaybackSession:
    return PlaybackSession(
        session_id="s", user_id=1, username="u", camera_id=7,
        camera_path="cam-7", start_time=start,
        end_time=start + timedelta(minutes=2), created_at=0, expires_at=0,
    )


@pytest.mark.parametrize(
    "vcodec, acodec, remux",
    [
        (b"avc1", b"ipcm", True),   # G.711 camera: the reported bug
        (b"avc1", b"mp4a", False),  # AAC camera: unchanged, keeps audio
        (b"hev1", b"mp4a", True),   # H.265: unchanged
    ],
)
def test_session_path_follows_audio_and_video_codecs(
    tmp_path, monkeypatch, vcodec, acodec, remux
):
    start = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    _clip(first, vcodec, acodec)
    _clip(second, vcodec, acodec)
    rows = [(first, start, 60.0), (second, start + timedelta(seconds=60), 60.0)]
    monkeypatch.setattr(
        HlsPlaybackService, "_resolve_recording_rows", classmethod(lambda cls, *a: rows)
    )
    # No stored codec (a reconciler row): the session probes the file.
    monkeypatch.setattr(
        HlsPlaybackService, "_stored_codec", classmethod(lambda cls, *a: None)
    )

    session = _session(start)
    HlsPlaybackService._attach_byte_index(session, 7, object(), 120.0)

    assert session.needs_remux is remux
    assert session.video_codec == vcodec.decode()
    # The remux is per-file: a remuxed session stops at its first clip.
    assert len(session.files) == (1 if remux else 2)
    assert (session.remux_index is not None) is remux


def test_a_remux_session_at_a_file_boundary_gets_the_next_file(tmp_path, monkeypatch):
    """The player asks for the instant the current file ends. The resolver also
    returns the file that just ended (its clock-skew allowance), and serving
    that one sent the player back to the end of the same file forever, so
    playback stalled at the first file boundary."""
    start = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    _clip(first, b"avc1", b"ipcm")
    _clip(second, b"avc1", b"ipcm")
    boundary = start + timedelta(seconds=60)
    # As the index stores them: whole-second starts, and a duration that runs
    # past the next file's start (media length, not the start-to-start gap).
    rows = [(first, start, 60.5), (second, boundary, 60.0)]
    monkeypatch.setattr(
        HlsPlaybackService, "_resolve_recording_rows", classmethod(lambda cls, *a: rows)
    )
    monkeypatch.setattr(
        HlsPlaybackService, "_stored_codec", classmethod(lambda cls, *a: None)
    )

    session = _session(boundary)
    HlsPlaybackService._attach_byte_index(session, 7, object(), 60.0)

    assert session.needs_remux is True
    assert [f.path for f in session.files] == [str(second)]
    assert session.file_offset_seconds == 0.0
    assert session.remux_index.src_path == str(second.resolve())
