# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Unit tests for the pure-Python HEVC recording remux (no ffmpeg, no fixtures).

Builds a minimal fragmented MP4 in-memory (hev1 video + ipcm audio, one
fragment), runs the remux, and asserts the output is a flat, video-only, hvc1
MP4 whose sample bytes are preserved — i.e. the exact fix for H.265 recordings
not playing in the browser.
"""

from __future__ import annotations

import os
import secrets
import struct
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from services import hevc_remux_service as hrs  # noqa: E402

# --- minimal ISO-BMFF builders --------------------------------------------


def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _full(typ: bytes, ver: int, flags: int, payload: bytes) -> bytes:
    return _box(typ, bytes([ver]) + struct.pack(">I", flags)[1:] + payload)


def _visual_sample_entry(fourcc: bytes) -> bytes:
    # 6 reserved + 2 data_ref_idx + 16 predefined + 2 w + 2 h + 4 hres + 4 vres
    # + 4 reserved + 2 frame_count + 32 compressorname + 2 depth + 2 predefined
    body = (b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 16
            + struct.pack(">HH", 320, 240) + struct.pack(">II", 0x480000, 0x480000)
            + b"\x00" * 4 + struct.pack(">H", 1) + b"\x00" * 32
            + struct.pack(">Hh", 0x18, -1))
    hvcc = _box(b"hvcC", bytes([1]) + b"\x00" * 21 + bytes([0]))  # numArrays=0 (dummy)
    return _box(fourcc, body + hvcc)


def _audio_sample_entry() -> bytes:
    body = (b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 8
            + struct.pack(">HH", 1, 16) + b"\x00" * 4 + struct.pack(">H", 48000) + b"\x00\x00")
    return _box(b"ipcm", body)


def _trak(track_id: int, handler: bytes, sample_entry: bytes, timescale: int) -> bytes:
    tkhd = _full(b"tkhd", 0, 7, struct.pack(">IIIII", 0, 0, track_id, 0, 0)
                 + b"\x00" * 8 + struct.pack(">hhhh", 0, 0, 0, 0)
                 + struct.pack(">IIIIIIIII", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
                 + struct.pack(">II", 0, 0))
    mdhd = _full(b"mdhd", 0, 0, struct.pack(">IIII", 0, 0, timescale, 0) + struct.pack(">HH", 0x55C4, 0))
    hdlr = _full(b"hdlr", 0, 0, struct.pack(">I", 0) + handler + struct.pack(">III", 0, 0, 0) + b"h\x00")
    stsd = _full(b"stsd", 0, 0, struct.pack(">I", 1) + sample_entry)
    empty = struct.pack(">I", 0)
    stbl = _box(b"stbl", stsd + _full(b"stts", 0, 0, empty) + _full(b"stsc", 0, 0, empty)
                + _full(b"stsz", 0, 0, struct.pack(">II", 0, 0)) + _full(b"stco", 0, 0, empty))
    mediahdr = _box(b"vmhd", b"\x00" * 12) if handler == b"vide" else _box(b"smhd", b"\x00" * 8)
    minf = _box(b"minf", mediahdr + _box(b"dinf", _full(b"dref", 0, 0, struct.pack(">I", 0))) + stbl)
    return _box(b"trak", tkhd + _box(b"mdia", mdhd + hdlr + minf))


def _trex(track_id: int) -> bytes:
    return _full(b"trex", 0, 0, struct.pack(">IIIII", track_id, 1, 0, 0, 0))


def _traf(track_id: int, data_offset: int, sizes: list[int], sync_first: bool) -> bytes:
    # tfhd: default-base-is-moof (0x020000)
    tfhd = _full(b"tfhd", 0, 0x020000, struct.pack(">I", track_id))
    # trun: data-offset(0x01) + first-sample-flags(0x04) + sample-size(0x200)
    flags = 0x01 | 0x04 | 0x200
    first_flags = 0x00000000 if sync_first else 0x00010000
    payload = struct.pack(">I", len(sizes)) + struct.pack(">i", data_offset) + struct.pack(">I", first_flags)
    for s in sizes:
        payload += struct.pack(">I", s)
    return _box(b"traf", tfhd + _full(b"trun", 0, flags, payload))


def _init_segment() -> bytes:
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isomiso5")
    mvhd = _full(b"mvhd", 0, 0, struct.pack(">IIII", 0, 0, 1000, 0) + struct.pack(">IH", 0x10000, 0x0100)
                 + b"\x00" * 10 + struct.pack(">IIIIIIIII", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
                 + b"\x00" * 24 + struct.pack(">I", 3))
    vtrak = _trak(1, b"vide", _visual_sample_entry(b"hev1"), 90000)
    atrak = _trak(2, b"soun", _audio_sample_entry(), 48000)
    mvex = _box(b"mvex", _trex(1) + _trex(2))
    return ftyp + _box(b"moov", mvhd + vtrak + atrak + mvex)


def _fragment(seq: int, video_samples: list[bytes], audio_samples: list[bytes]) -> bytes:
    """One moof+mdat pair; data offsets are moof-relative so fragments can be
    concatenated freely."""
    vsize = sum(len(s) for s in video_samples)
    mfhd = _full(b"mfhd", 0, 0, struct.pack(">I", seq))
    # data_offset is relative to moof start; compute after we know moof size.
    # Build traf with a placeholder, then fix the video data_offset.
    vtraf = _traf(1, 0, [len(s) for s in video_samples], sync_first=True)
    ataf = _traf(2, 0, [len(s) for s in audio_samples], sync_first=True)
    moof_len = 8 + len(mfhd) + len(vtraf) + len(ataf)
    mdat_data_start = moof_len + 8  # +8 mdat header
    # rewrite data_offsets: video at mdat_data_start, audio right after video
    vtraf = _traf(1, mdat_data_start, [len(s) for s in video_samples], sync_first=True)
    ataf = _traf(2, mdat_data_start + vsize, [len(s) for s in audio_samples], sync_first=True)
    moof = _box(b"moof", mfhd + vtraf + ataf)
    mdat = _box(b"mdat", b"".join(video_samples) + b"".join(audio_samples))
    return moof + mdat


def _build_fmp4(video_samples: list[bytes], audio_samples: list[bytes]) -> bytes:
    return _init_segment() + _fragment(1, video_samples, audio_samples)


def _build_fmp4_multi(*frags: tuple[list[bytes], list[bytes]]) -> bytes:
    """Multi-fragment file: each frag is (video_samples, audio_samples).
    Video runs are interleaved with audio bytes, so the remux maps to
    multiple non-contiguous source runs."""
    out = _init_segment()
    for i, (vid, aud) in enumerate(frags, start=1):
        out += _fragment(i, vid, aud)
    return out


# --- tests -----------------------------------------------------------------


def test_probe_and_compat_flags(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(_build_fmp4([b"VIDEO_SAMPLE_0"], [b"AUD0"]))
    assert hrs.probe_video_codec(src) == "hev1"
    assert hrs.is_browser_incompatible_video("hev1") is True
    assert hrs.is_browser_incompatible_video("avc1") is False
    assert hrs.is_browser_incompatible_video(None) is False


def test_remux_produces_videoonly_hvc1_preserving_samples(tmp_path):
    v0, v1 = b"KEYFRAME_BYTES_0", b"pframe1"
    src = tmp_path / "in.mp4"
    src.write_bytes(_build_fmp4([v0, v1], [b"AUDIO_A", b"AUDIO_B"]))
    dst = tmp_path / "out.mp4"

    hrs.remux_to_browser_mp4(src, dst)
    out = dst.read_bytes()

    # exactly one trak, and it's the video one retagged hvc1
    assert out.count(b"trak") == 1
    assert b"hvc1" in out and b"hev1" not in out
    assert b"ipcm" not in out  # audio track dropped

    # the mdat holds exactly the video samples, in order, and nothing else
    mi = out.find(b"mdat")
    mdat_payload = out[mi + 4:]
    assert mdat_payload == v0 + v1  # audio bytes excluded, video preserved

    # codec probe of the OUTPUT now reports the browser-friendly tag
    assert hrs.probe_video_codec(dst) == "hvc1"


# --- the virtual (cache-free) file ----------------------------------------


def test_virtual_ranges_match_materialized_remux(tmp_path):
    """The virtual file must be byte-for-byte what the materialized remux
    writes — for the whole body and for arbitrary Range slices (the old
    remux acts as the oracle for the new range-mapped reader)."""
    src = tmp_path / "in.mp4"
    # 3 fragments -> 3 non-contiguous video runs (audio interleaved between)
    src.write_bytes(_build_fmp4_multi(
        ([b"KEYFRAME_BYTES_0", b"pframe1"], [b"AUDIO_A"]),
        ([b"KEY2", b"pframe_2_longer"], [b"AUDIO_BB", b"AUDIO_C"]),
        ([b"KEY_THREE"], [b"D"]),
    ))
    dst = tmp_path / "oracle.mp4"
    hrs.remux_to_browser_mp4(src, dst)
    oracle = dst.read_bytes()

    index = hrs.build_remux_index(src)
    assert index.total_size == len(oracle)
    assert len(index.runs) == 3
    # the virtual data region is exactly the video samples, in order
    assert oracle[len(index.header):] == (
        b"KEYFRAME_BYTES_0" + b"pframe1" + b"KEY2" + b"pframe_2_longer" + b"KEY_THREE"
    )

    # full read
    assert b"".join(hrs.iter_index_range(index, 0, index.total_size - 1)) == oracle

    hlen = len(index.header)
    total = index.total_size
    ranges = [
        (0, 0),                       # first byte
        (total - 1, total - 1),       # last byte
        (0, hlen - 1),                # exactly the header
        (hlen, total - 1),            # exactly the data region
        (hlen - 3, hlen + 3),         # straddling header/data boundary
        (hlen + 1, hlen + 5),         # inside the data region
        (total - 7, total - 1),       # suffix
        (3, total - 4),               # nearly everything, odd bounds
    ]
    # every run boundary, straddled both ways
    for ro in index.run_out_starts:
        ranges.append((max(0, ro - 2), min(total - 1, ro + 2)))
        ranges.append((ro, ro))
    # exhaustive sweep: every (start, end) pair on a small file
    for start in range(0, total, 7):
        for end in range(start, total, 11):
            ranges.append((start, end))
    for start, end in ranges:
        got = b"".join(hrs.iter_index_range(index, start, end))
        assert got == oracle[start:end + 1], f"range {start}-{end} mismatch"

    # tiny chunk size must not change the bytes
    got = b"".join(hrs.iter_index_range(index, 0, total - 1, chunk=3))
    assert got == oracle

    # out-of-bounds ranges yield nothing
    assert b"".join(hrs.iter_index_range(index, total, total + 10)) == b""
    assert b"".join(hrs.iter_index_range(index, 5, 4)) == b""


@pytest.mark.asyncio
async def test_index_rebuilds_when_source_grows(tmp_path):
    """A still-recording file is transparently re-indexed on change — and
    keeps exactly ONE in-RAM entry (this replaces the old disk cache that
    grew by one full copy per size/mtime snapshot)."""
    hrs._indexes.clear()
    src = tmp_path / "rec.mp4"
    src.write_bytes(_build_fmp4([b"AAAA"], [b"zz"]))

    idx1 = await hrs.get_remux_index(str(src))
    assert idx1 is not None
    assert b"".join(hrs.iter_index_range(idx1, len(idx1.header), idx1.total_size - 1)) == b"AAAA"

    # same content -> served from RAM (identical object)
    assert await hrs.get_remux_index(str(src)) is idx1

    # file grows (recording being written) -> rebuilt, single entry, new tail
    src.write_bytes(_build_fmp4([b"AAAA", b"BBBB"], [b"zz"]))
    os.utime(src, (os.stat(src).st_atime, os.stat(src).st_mtime + 2))
    idx2 = await hrs.get_remux_index(str(src))
    assert idx2 is not None and idx2.total_size > idx1.total_size
    assert b"".join(hrs.iter_index_range(idx2, len(idx2.header), idx2.total_size - 1)) == b"AAAABBBB"
    assert len(hrs._indexes) == 1

    # deleted source -> None, entry dropped
    src.unlink()
    assert await hrs.get_remux_index(str(src)) is None
    assert len(hrs._indexes) == 0


@pytest.mark.asyncio
async def test_index_lru_is_bounded(tmp_path, monkeypatch):
    hrs._indexes.clear()
    monkeypatch.setattr(hrs, "MAX_CACHED_INDEXES", 2)
    for i in range(4):
        src = tmp_path / f"rec{i}.mp4"
        src.write_bytes(_build_fmp4([f"VID{i}".encode()], [b"zz"]))
        assert await hrs.get_remux_index(str(src)) is not None
    assert len(hrs._indexes) == 2
    # newest two survive
    assert {Path(p).name for p in hrs._indexes} == {"rec2.mp4", "rec3.mp4"}
