# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# The hardware-accelerated decode/scale argument strings below are derived from
# Frigate's ``frigate/ffmpeg_presets.py`` — MIT licensed, Frigate, Inc.,
# reviewed at commit 6f80bcd19 (v0.18-beta). See the repo-root NOTICE file.
"""
FFmpeg command construction for the Tier-0 decode loop.

OpenNVR does *not* pull frames from cameras directly for inference — MediaMTX is
the stream hub (it ingests each camera once, records it, and republishes over
RTSP). The detect pipeline pulls the **substream** from MediaMTX's RTSP republish
and hardware-decodes it to raw ``yuv420p`` frames on stdout, which the capture
loop reads in fixed ``width*height*3//2`` byte chunks.

Recording is entirely MediaMTX's job (``record: yes``) and is never touched here
— which satisfies the design's "recording is never gated" invariant for free.

Everything in this module is a pure function returning an ``argv`` list; nothing
here spawns a process (that's ``frame_source``), so it is trivially unit-tested.
"""
from __future__ import annotations

from enum import Enum


class HwAccel(str, Enum):
    """Supported hardware-decode backends (plus CPU software decode)."""

    CPU = "cpu"
    VAAPI = "vaapi"          # Intel iGPU / AMD via VA-API
    QSV = "qsv"              # Intel Quick Sync
    NVIDIA = "nvidia"        # NVIDIA NVDEC / CUDA
    JETSON = "jetson"        # NVIDIA Jetson (nvmpi, scales in decoder)
    RKMPP = "rkmpp"          # Rockchip Media Process Platform
    RPI = "rpi"              # Raspberry Pi V4L2 M2M


# Decode-stage args, inserted BEFORE ``-i <url>``. ``{device}`` is substituted
# with the render node / device index; ``{w}``/``{h}`` only used where the
# decoder itself scales (Jetson). Ported from Frigate PRESETS_HW_ACCEL_DECODE.
_DECODE: dict[HwAccel, list[str]] = {
    HwAccel.CPU: [],
    HwAccel.VAAPI: [
        "-hwaccel_flags", "allow_profile_mismatch",
        "-hwaccel", "vaapi",
        "-hwaccel_device", "{device}",
        "-hwaccel_output_format", "vaapi",
    ],
    HwAccel.QSV: [
        "-hwaccel", "qsv",
        "-qsv_device", "{device}",
        "-hwaccel_output_format", "qsv",
    ],
    HwAccel.NVIDIA: [
        "-hwaccel_device", "{device}",
        "-hwaccel", "cuda",
        "-hwaccel_output_format", "cuda",
    ],
    HwAccel.JETSON: ["-c:v", "{nvmpi}", "-resize", "{w}x{h}"],
    HwAccel.RKMPP: ["-hwaccel", "rkmpp", "-hwaccel_output_format", "drm_prime"],
    HwAccel.RPI: ["-c:v", "{v4l2m2m}"],
}

# Scale-stage ``-vf`` filter that also brings GPU frames back to system memory
# and normalizes to planar I420 (``yuv420p``) for the raw pipe. The
# ``hwdownload,format=nv12`` step is the load-bearing part on GPU backends.
# Ported from Frigate PRESETS_HW_ACCEL_SCALE, with a trailing ``format=yuv420p``
# so the capture loop always reads standard I420 (w*h*3//2 bytes).
_SCALE: dict[HwAccel, str] = {
    HwAccel.CPU: "fps={fps},scale={w}:{h},format=yuv420p",
    HwAccel.VAAPI: "fps={fps},scale_vaapi=w={w}:h={h},hwdownload,format=nv12,format=yuv420p",
    HwAccel.QSV: "vpp_qsv=w={w}:h={h}:format=nv12,hwdownload,format=nv12,fps={fps},format=yuv420p",
    HwAccel.NVIDIA: "fps={fps},scale_cuda=w={w}:h={h},hwdownload,format=nv12,format=yuv420p",
    HwAccel.JETSON: "fps={fps},format=yuv420p",          # scaled in the decoder
    HwAccel.RKMPP: "fps={fps},hwdownload,format=nv12,format=yuv420p",
    HwAccel.RPI: "fps={fps},scale={w}:{h},format=yuv420p",
}

# Per-backend codec token for decoders that name an explicit ``-c:v``.
_CODEC_TOKEN: dict[HwAccel, dict[str, str]] = {
    HwAccel.JETSON: {"h264": "h264_nvmpi", "h265": "hevc_nvmpi"},
    HwAccel.RPI: {"h264": "h264_v4l2m2m", "h265": "hevc_v4l2m2m"},
}


def frame_size_bytes(width: int, height: int) -> int:
    """Bytes per raw ``yuv420p`` (I420) frame — what the capture loop reads."""
    if width <= 0 or height <= 0:
        raise ValueError(f"width/height must be positive, got {width}x{height}")
    return width * height * 3 // 2


# Backends that decode through a device NODE the container must have been
# given. docker-compose ships the `devices:` mapping COMMENTED OUT, so
# "DETECT_HWACCEL=vaapi" without also uncommenting it is the single most
# likely misconfiguration — and it would fail every camera at once.
_NEEDS_DEVICE_NODE = frozenset({HwAccel.VAAPI, HwAccel.QSV, HwAccel.RPI, HwAccel.RKMPP})


def resolve_hwaccel(
    requested: str | None, *, device: str = "/dev/dri/renderD128", _exists=None
) -> tuple[HwAccel, str | None]:
    """The hwaccel we can ACTUALLY use, plus a reason if it was downgraded.

    Degrading to CPU costs frames-per-core; failing to degrade costs the
    entire fleet, because ffmpeg exits immediately on a missing render node
    and every worker just restart-loops. Same "degrade loudly, never
    crash-loop" rule the decode-skip parser follows.
    """
    import os as _os

    exists = _exists or _os.path.exists
    name = (requested or "cpu").strip().lower() or "cpu"
    try:
        accel = HwAccel(name)
    except ValueError:
        return HwAccel.CPU, f"unknown hwaccel {requested!r}"
    if accel in _NEEDS_DEVICE_NODE and not exists(device):
        return HwAccel.CPU, (
            f"{accel.value} needs {device}, which this container cannot see "
            f"(pass it through with `devices: - /dev/dri:/dev/dri`)"
        )
    return accel, None


def decode_args(hwaccel: HwAccel, *, device: str = "/dev/dri/renderD128",
                codec: str = "h264", width: int = 0, height: int = 0) -> list[str]:
    """Args to place before ``-i`` for the given backend."""
    raw = _DECODE[hwaccel]
    token = _CODEC_TOKEN.get(hwaccel, {}).get(codec)
    out: list[str] = []
    for a in raw:
        out.append(
            a.replace("{device}", device)
            .replace("{w}", str(width))
            .replace("{h}", str(height))
            .replace("{nvmpi}", token or "")
            .replace("{v4l2m2m}", token or "")
        )
    return out


def scale_filter(hwaccel: HwAccel, *, width: int, height: int, fps: int) -> str:
    """The ``-vf`` value that scales, downloads to system memory, and yields I420."""
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    return (
        _SCALE[hwaccel]
        .replace("{w}", str(width))
        .replace("{h}", str(height))
        .replace("{fps}", str(fps))
    )


# Decoder frame-skip levels (ffmpeg ``-skip_frame``, an input option placed
# before ``-i``). Decode — not detection — is Tier-0's dominant CPU cost: the
# camera's full frame rate is decoded even though only DETECT_FPS frames/s are
# analyzed (the ``fps`` filter runs POST-decode). ``-skip_frame`` moves the
# drop to the DECODER, so skipped frames are never decompressed at all:
#   none    decode every frame — full motion/track granularity (this function's
#           default; the SERVICE defaults to nonref via DETECT_DECODE_SKIP)
#   bidir   skip B-frames — moderate saving, output still ≥ typical DETECT_FPS
#   nonref  skip non-reference frames — PROVABLY lossless (a dropped frame is
#           one nothing else depends on); the service default
#   nokey   decode keyframes ONLY — the big one (~one frame per GOP, usually
#           0.5-1 fps): decode cost drops by roughly the stream's GOP length.
#           The ``fps`` filter then pads by duplicating frames, which is
#           nearly free downstream (zero pixel diff → the motion gate skips
#           them), but real motion/track granularity IS the keyframe rate —
#           fine for "is someone there" alarms, coarse for fast events.
DECODE_SKIP_MODES = ("none", "bidir", "nonref", "nokey")

# Mode name → the token ffmpeg's CLI actually accepts. The mode is named
# after libavcodec's constant (AVDISCARD_NONREF), but the CLI string is
# "noref" — passing "nonref" makes ffmpeg reject the option and exit with
# ZERO frames produced, which the worker sees as a dead stream and
# crash-loops on (field-diagnosed via Dozzle on the first deployed build).
# The real-ffmpeg regression test in test_ffmpeg_presets.py exists so a
# token can never drift from what ffmpeg accepts again.
_FFMPEG_SKIP_TOKEN = {"nonref": "noref"}


# Socket-I/O timeout for RTSP reads. Without it, a half-open TCP session — a
# camera powered off mid-stream, a NAT/firewall dropping the flow — leaves the
# read blocked FOREVER: ffmpeg never exits, so the restart loop never runs and
# the camera is silently dead until the process is restarted.
#
# The flag is ``-timeout`` (microseconds) on the rtsp demuxer, NOT
# ``-rw_timeout``: verified against the shipped image (ffmpeg 7.1.5), where
# ``-timeout 3000000`` reaches the transport as ``tcp://...?timeout=3000000``
# while ``-rw_timeout`` arrives as ``timeout=0`` — accepted, and silently
# doing nothing. Neither errors, so this is only catchable by inspection.
DEFAULT_RTSP_TIMEOUT_S = 10.0


def rtsp_timeout_args(timeout_s: float = DEFAULT_RTSP_TIMEOUT_S) -> list[str]:
    """``-timeout`` in microseconds, or nothing when disabled (<= 0)."""
    if timeout_s <= 0:
        return []
    return ["-timeout", str(int(timeout_s * 1_000_000))]


def build_decode_command(
    rtsp_url: str,
    *,
    width: int,
    height: int,
    fps: int,
    hwaccel: HwAccel = HwAccel.CPU,
    device: str = "/dev/dri/renderD128",
    codec: str = "h264",
    rtsp_transport: str = "tcp",
    rtsp_timeout_s: float = DEFAULT_RTSP_TIMEOUT_S,
    decode_skip: str = "none",
    decode_threads: int = 2,
    fast_decode: bool = False,
) -> list[str]:
    """
    Full ffmpeg argv that pulls ``rtsp_url`` (MediaMTX substream republish),
    hardware-decodes + scales it, and writes raw ``yuv420p`` frames to stdout.

    Read frames from stdout in ``frame_size_bytes(width, height)`` chunks.
    """
    if not rtsp_url.startswith(("rtsp://", "rtsps://")):
        raise ValueError(f"expected an rtsp(s):// substream URL, got {rtsp_url!r}")
    if decode_skip == "noref":          # accept ffmpeg's own spelling too
        decode_skip = "nonref"
    if decode_skip not in DECODE_SKIP_MODES:
        raise ValueError(
            f"decode_skip must be one of {DECODE_SKIP_MODES}, got {decode_skip!r}"
        )
    skip_args = (
        [] if decode_skip == "none"
        else ["-skip_frame", _FFMPEG_SKIP_TOKEN.get(decode_skip, decode_skip)]
    )
    # Decoder thread cap (before -i). ffmpeg's default is AUTO — up to 16
    # frame threads PER CAMERA, which on a small substream is pure scheduling
    # overhead multiplied by the fleet (Frigate pins -threads 2 for the same
    # reason). Thread count never changes decoded output, so capping is
    # lossless. 0 = ffmpeg auto (omit the flag).
    thread_args = ["-threads", str(decode_threads)] if decode_threads > 0 else []
    # Opt-in software-decode shortcuts: skip the h264/h265 in-loop deblocking
    # filter and allow non-spec-compliant speedups. Deblocking exists for
    # VIEWING quality; detection is robust to the slight blockiness, but the
    # decoder drifts a little from the encoder between keyframes, so this is
    # NOT bit-exact — hence opt-in, and CPU decode only (hw decoders ignore
    # these AVOptions or handle deblocking in silicon for free).
    fast_args = (
        ["-skip_loop_filter", "all", "-flags2", "fast"]
        if fast_decode and hwaccel is HwAccel.CPU else []
    )
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-rtsp_transport", rtsp_transport,
        *rtsp_timeout_args(rtsp_timeout_s),
        *thread_args,
        *skip_args,
        *fast_args,
        *decode_args(hwaccel, device=device, codec=codec, width=width, height=height),
        "-i", rtsp_url,
        "-vf", scale_filter(hwaccel, width=width, height=height, fps=fps),
        "-f", "rawvideo",
        "-pix_fmt", "yuv420p",
        "pipe:1",
    ]
