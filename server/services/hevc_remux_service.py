# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
Pure-Python HEVC recording remux — make H.265 recordings play in the browser
WITHOUT ffmpeg, without transcoding, and without touching the camera.

Browsers decode HEVC in MSE/WebCodecs, but reject the container OpenNVR records:
MediaMTX writes an ``hev1`` (in-band params) video track plus a raw-PCM (``ipcm``)
audio track. MSE needs ``hvc1`` (out-of-band params, which the ``hvcC`` box
already carries) and can't decode PCM at all — so it stalls (black screen).

This service repackages the recording, sample-for-sample (NO re-encode):
  * retag the video sample entry ``hev1`` -> ``hvc1`` (a 4-byte swap — the
    parameter sets already live in ``hvcC``), and
  * drop the incompatible audio track,
producing a flat, seekable, video-only MP4 the browser plays natively.

It is memory-safe (indexes fragments via seeks, streams sample runs — never
loads the multi-GB file into RAM), cached (remux once per recording, on demand),
and concurrency-locked (no double-remux under parallel playback).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import struct
from pathlib import Path
from typing import BinaryIO

from core.config import settings
from core.logging_config import recording_logger

# ---------------------------------------------------------------------------
# ISO-BMFF helpers (seek-based; never loads mdat payloads into memory)
# ---------------------------------------------------------------------------

_CONTAINER = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"mvex", b"edts"}


def _read_box_header(f: BinaryIO, pos: int) -> tuple[bytes, int, int] | None:
    """Return (type, header_size, box_size) at file position ``pos``, or None."""
    f.seek(pos)
    head = f.read(8)
    if len(head) < 8:
        return None
    size = struct.unpack(">I", head[:4])[0]
    typ = head[4:8]
    hdr = 8
    if size == 1:
        ext = f.read(8)
        if len(ext) < 8:
            return None
        size = struct.unpack(">Q", ext)[0]
        hdr = 16
    elif size == 0:
        size = os.fstat(f.fileno()).st_size - pos
    if size < hdr:
        return None
    return typ, hdr, size


def _iter_top_level(f: BinaryIO):
    """Yield (type, box_start, header_size, box_size) for each top-level box."""
    size_total = os.fstat(f.fileno()).st_size
    pos = 0
    while pos + 8 <= size_total:
        info = _read_box_header(f, pos)
        if info is None:
            break
        typ, hdr, size = info
        yield typ, pos, hdr, size
        pos += size


def _find(buf: bytes, start: int, end: int, path: tuple[bytes, ...]):
    """Find first nested box by path inside an in-memory buffer."""
    want = path[0]
    o = start
    while o + 8 <= end:
        size = struct.unpack(">I", buf[o:o + 4])[0]
        typ = buf[o + 4:o + 8]
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", buf[o + 8:o + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - o
        if size < hdr or o + size > end:
            break
        if typ == want:
            if len(path) == 1:
                return o, hdr, size
            r = _find(buf, o + hdr, o + size, path[1:])
            if r:
                return r
        o += size
    return None


def _find_all(buf: bytes, start: int, end: int, want: bytes):
    out = []
    o = start
    while o + 8 <= end:
        size = struct.unpack(">I", buf[o:o + 4])[0]
        typ = buf[o + 4:o + 8]
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", buf[o + 8:o + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - o
        if size < hdr or o + size > end:
            break
        if typ == want:
            out.append((o, hdr, size))
        o += size
    return out


# ---------------------------------------------------------------------------
# Codec probe
# ---------------------------------------------------------------------------


def probe_video_codec(path: str | Path) -> str | None:
    """Return the video sample-entry fourcc lowercased (``hev1``/``hvc1``/``avc1``
    …) or None. Cheap: reads only the moov box."""
    try:
        with open(path, "rb") as f:
            for typ, pos, hdr, size in _iter_top_level(f):
                if typ != b"moov":
                    continue
                f.seek(pos)
                moov = f.read(size)
                # find the VIDEO track's stsd sample entry
                for to, th, ts in _find_all(moov, hdr, size, b"trak"):
                    hd = _find(moov, to + th, to + ts, (b"mdia", b"hdlr"))
                    if not hd:
                        continue
                    ho, hh, _hs = hd
                    if moov[ho + hh + 8:ho + hh + 12] != b"vide":
                        continue
                    stsd = _find(moov, to + th, to + ts,
                                 (b"mdia", b"minf", b"stbl", b"stsd"))
                    if not stsd:
                        return None
                    so, sh, _ss = stsd
                    e = so + sh + 8
                    return moov[e + 4:e + 8].decode("latin-1", "replace").lower()
                return None
    except Exception as e:
        recording_logger.warning("probe_video_codec(%s) failed: %s", path, e)
    return None


def is_browser_incompatible_video(codec: str | None) -> bool:
    """True for video codecs the browser MSE pipeline can't play as recorded.

    HEVC in the ``hev1`` flavor (what MediaMTX writes for XM/Sofia cams) is the
    case we remux. ``hvc1`` and ``avc1`` play as-is.
    """
    return (codec or "") in ("hev1", "hvc1", "hevc")


# ---------------------------------------------------------------------------
# The remux (video-only, hev1 -> hvc1). Blocking; run via a thread.
# ---------------------------------------------------------------------------


def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _fullbox(typ: bytes, ver: int, flags: int, payload: bytes) -> bytes:
    return _box(typ, bytes([ver]) + struct.pack(">I", flags)[1:] + payload)


def remux_to_browser_mp4(src_path: str | Path, dst_path: str | Path) -> None:
    """Write a flat, video-only, ``hvc1`` MP4 (no re-encode) to ``dst_path``.

    Streams each fragment's contiguous video run from the source — never holds
    the whole recording in memory. Raises on structural failure (caller falls
    back to serving the original).
    """
    src_path = str(src_path)
    with open(src_path, "rb") as f:
        # -- locate moov, extract video track metadata + retagged sample entry --
        moov = None
        for typ, pos, hdr, size in _iter_top_level(f):
            if typ == b"moov":
                f.seek(pos)
                moov = f.read(size)
                moov_hdr = hdr
                break
        if moov is None:
            raise ValueError("no moov box")

        video_track_id = timescale = None
        stsd_entry = None
        for to, th, ts in _find_all(moov, moov_hdr, len(moov), b"trak"):
            hd = _find(moov, to + th, to + ts, (b"mdia", b"hdlr"))
            if not hd or moov[hd[0] + hd[1] + 8:hd[0] + hd[1] + 12] != b"vide":
                continue
            tk = _find(moov, to + th, to + ts, (b"tkhd",))
            tver = moov[tk[0] + tk[1]]
            tid_off = tk[0] + tk[1] + (20 if tver == 1 else 12)
            video_track_id = struct.unpack(">I", moov[tid_off:tid_off + 4])[0]
            md = _find(moov, to + th, to + ts, (b"mdia", b"mdhd"))
            mver = moov[md[0] + md[1]]
            tsc_off = md[0] + md[1] + (20 if mver == 1 else 12)
            timescale = struct.unpack(">I", moov[tsc_off:tsc_off + 4])[0]
            stsd = _find(moov, to + th, to + ts,
                         (b"mdia", b"minf", b"stbl", b"stsd"))
            e = stsd[0] + stsd[1] + 8
            esize = struct.unpack(">I", moov[e:e + 4])[0]
            stsd_entry = bytearray(moov[e:e + esize])
            stsd_entry[4:8] = b"hvc1"  # retag hev1 -> hvc1 (params already in hvcC)
            break
        if video_track_id is None or stsd_entry is None:
            raise ValueError("no video track / sample entry")

        # trex defaults for the video track
        d_dur = d_size = d_flags = 0
        mvex = _find(moov, moov_hdr, len(moov), (b"mvex",))
        if mvex:
            for xo, xh, _xs in _find_all(moov, mvex[0] + mvex[1],
                                         mvex[0] + mvex[2], b"trex"):
                if struct.unpack(">I", moov[xo + xh + 4:xo + xh + 8])[0] == video_track_id:
                    d_dur = struct.unpack(">I", moov[xo + xh + 12:xo + xh + 16])[0]
                    d_size = struct.unpack(">I", moov[xo + xh + 16:xo + xh + 20])[0]
                    d_flags = struct.unpack(">I", moov[xo + xh + 20:xo + xh + 24])[0]

        # -- index all fragments: per-sample (size,dur,sync) + contiguous runs --
        sample_sizes: list[int] = []
        sample_durs: list[int] = []
        sync_samples: list[int] = []  # 1-based indices
        runs: list[tuple[int, int]] = []  # (file_offset, run_length) video-only
        sample_no = 0
        for typ, pos, hdr, size in _iter_top_level(f):
            if typ != b"moof":
                continue
            f.seek(pos)
            moof = f.read(size)
            for tro, trh, trs in _find_all(moof, hdr, size, b"traf"):
                tfhd = _find(moof, tro + trh, tro + trs, (b"tfhd",))
                to2, th2, _ts2 = tfhd
                tf_flags = struct.unpack(">I", moof[to2 + th2:to2 + th2 + 4])[0] & 0xFFFFFF
                p = to2 + th2 + 4
                track_id = struct.unpack(">I", moof[p:p + 4])[0]
                p += 4
                base = pos  # default-base-is-moof
                if tf_flags & 0x01:
                    base = struct.unpack(">Q", moof[p:p + 8])[0]
                    p += 8
                if tf_flags & 0x02:
                    p += 4
                td, tsz, tfl = d_dur, d_size, d_flags
                if tf_flags & 0x08:
                    td = struct.unpack(">I", moof[p:p + 4])[0]
                    p += 4
                if tf_flags & 0x10:
                    tsz = struct.unpack(">I", moof[p:p + 4])[0]
                    p += 4
                if tf_flags & 0x20:
                    tfl = struct.unpack(">I", moof[p:p + 4])[0]
                    p += 4
                if track_id != video_track_id:
                    continue
                trun = _find(moof, tro + trh, tro + trs, (b"trun",))
                uo, uh, _us = trun
                tr_flags = struct.unpack(">I", moof[uo + uh:uo + uh + 4])[0] & 0xFFFFFF
                q = uo + uh + 4
                count = struct.unpack(">I", moof[q:q + 4])[0]
                q += 4
                data_off = 0
                if tr_flags & 0x01:
                    data_off = struct.unpack(">i", moof[q:q + 4])[0]
                    q += 4
                first_flags = None
                if tr_flags & 0x04:
                    first_flags = struct.unpack(">I", moof[q:q + 4])[0]
                    q += 4
                run_start = base + data_off
                run_len = 0
                for idx in range(count):
                    sdur, ssz, sfl = td, tsz, tfl
                    if tr_flags & 0x100:
                        sdur = struct.unpack(">I", moof[q:q + 4])[0]
                        q += 4
                    if tr_flags & 0x200:
                        ssz = struct.unpack(">I", moof[q:q + 4])[0]
                        q += 4
                    if tr_flags & 0x400:
                        sfl = struct.unpack(">I", moof[q:q + 4])[0]
                        q += 4
                    if tr_flags & 0x800:
                        q += 4
                    if idx == 0 and first_flags is not None:
                        sfl = first_flags
                    sample_no += 1
                    sample_sizes.append(ssz)
                    sample_durs.append(sdur)
                    if (sfl & 0x10000) == 0:  # sync (not non-sync)
                        sync_samples.append(sample_no)
                    run_len += ssz
                if run_len:
                    runs.append((run_start, run_len))

        if not sample_sizes:
            raise ValueError("no video samples")

        total_dur = sum(sample_durs)

        # -- build moov (video-only) --
        stsd = _fullbox(b"stsd", 0, 0, struct.pack(">I", 1) + bytes(stsd_entry))
        stts_runs: list[list[int]] = []
        for dur in sample_durs:
            if stts_runs and stts_runs[-1][1] == dur:
                stts_runs[-1][0] += 1
            else:
                stts_runs.append([1, dur])
        stts = _fullbox(b"stts", 0, 0, struct.pack(">I", len(stts_runs))
                        + b"".join(struct.pack(">II", c, d) for c, d in stts_runs))
        stss = _fullbox(b"stss", 0, 0, struct.pack(">I", len(sync_samples))
                        + b"".join(struct.pack(">I", n) for n in sync_samples))
        stsz = _fullbox(b"stsz", 0, 0, struct.pack(">II", 0, len(sample_sizes))
                        + b"".join(struct.pack(">I", s) for s in sample_sizes))
        # all samples in one chunk (mdat is a single contiguous run of them)
        stsc = _fullbox(b"stsc", 0, 0, struct.pack(">I", 1)
                        + struct.pack(">III", 1, len(sample_sizes), 1))
        stco = _fullbox(b"stco", 0, 0, struct.pack(">II", 1, 0))  # patched below
        stbl = _box(b"stbl", stsd + stts + stss + stsc + stsz + stco)
        vmhd = _fullbox(b"vmhd", 0, 1, struct.pack(">HHHH", 0, 0, 0, 0))
        dref = _fullbox(b"dref", 0, 0, struct.pack(">I", 1)
                        + _fullbox(b"url ", 0, 1, b""))
        minf = _box(b"minf", vmhd + _box(b"dinf", dref) + stbl)
        hdlr = _fullbox(b"hdlr", 0, 0, struct.pack(">I", 0) + b"vide"
                        + struct.pack(">III", 0, 0, 0) + b"VideoHandler\x00")
        mdhd = _fullbox(b"mdhd", 0, 0, struct.pack(">IIII", 0, 0, timescale, total_dur)
                        + struct.pack(">HH", 0x55C4, 0))
        mdia = _box(b"mdia", mdhd + hdlr + minf)
        tkhd = _fullbox(b"tkhd", 0, 7, struct.pack(">IIIII", 0, 0, 1, 0, total_dur)
                        + b"\x00" * 8 + struct.pack(">hhhh", 0, 0, 0, 0)
                        + struct.pack(">IIIIIIIII", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
                        + struct.pack(">II", 0, 0))
        trak = _box(b"trak", tkhd + mdia)
        mvhd = _fullbox(b"mvhd", 0, 0, struct.pack(">IIII", 0, 0, timescale, total_dur)
                        + struct.pack(">IH", 0x10000, 0x0100) + b"\x00" * 10
                        + struct.pack(">IIIIIIIII", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
                        + b"\x00" * 24 + struct.pack(">I", 2))
        moov_out = _box(b"moov", mvhd + trak)
        ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isomiso2mp41hvc1")

        mdat_size = sum(rl for _, rl in runs)
        # 64-bit mdat if needed
        if mdat_size + 8 > 0xFFFFFFFF:
            mdat_header = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", mdat_size + 16)
        else:
            mdat_header = struct.pack(">I", mdat_size + 8) + b"mdat"
        mdat_data_offset = len(ftyp) + len(moov_out) + len(mdat_header)

        # patch stco -> absolute offset of the first sample byte
        idx = moov_out.find(b"stco")
        moov_out = (moov_out[:idx + 8 + 4]
                    + struct.pack(">I", mdat_data_offset)
                    + moov_out[idx + 8 + 8:])

        # -- stream the output --
        tmp = str(dst_path) + ".partial"
        with open(tmp, "wb") as out:
            out.write(ftyp)
            out.write(moov_out)
            out.write(mdat_header)
            for run_start, run_len in runs:
                f.seek(run_start)
                remaining = run_len
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))  # 1 MiB
                    if not chunk:
                        raise ValueError("truncated source while copying samples")
                    out.write(chunk)
                    remaining -= len(chunk)
        os.replace(tmp, dst_path)


# ---------------------------------------------------------------------------
# On-demand cache (remux once per recording, concurrency-locked)
# ---------------------------------------------------------------------------

_locks: dict[str, asyncio.Lock] = {}


def _cache_dir() -> Path:
    d = Path(settings.recordings_base_path) / ".hevc_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path(src_path: str) -> Path:
    st = os.stat(src_path)
    key = hashlib.sha1(
        f"{os.path.abspath(src_path)}:{st.st_size}:{int(st.st_mtime)}".encode()
    ).hexdigest()
    return _cache_dir() / f"{key}.mp4"


async def get_browser_playable(src_path: str) -> Path | None:
    """Return a cached, browser-playable ``hvc1`` MP4 for ``src_path``.

    Remuxes on first call (off the event loop), caches keyed by path+size+mtime,
    and serializes concurrent requests so a file is remuxed only once. Returns
    None on failure so the caller can fall back to the original file.
    """
    src_path = os.path.abspath(src_path)
    try:
        dst = _cache_path(src_path)
    except FileNotFoundError:
        return None
    if dst.exists() and dst.stat().st_size > 0:
        return dst

    lock = _locks.setdefault(src_path, asyncio.Lock())
    async with lock:
        if dst.exists() and dst.stat().st_size > 0:
            return dst
        try:
            await asyncio.to_thread(remux_to_browser_mp4, src_path, str(dst))
            recording_logger.info("HEVC remux cached: %s -> %s", src_path, dst.name)
            return dst
        except Exception as e:
            recording_logger.error("HEVC remux failed for %s: %s", src_path, e)
            # clean any partial
            for p in (dst, Path(str(dst) + ".partial")):
                with contextlib.suppress(Exception):
                    p.unlink(missing_ok=True)
            return None
    return None
