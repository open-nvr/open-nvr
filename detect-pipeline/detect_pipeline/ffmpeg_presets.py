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
) -> list[str]:
    """
    Full ffmpeg argv that pulls ``rtsp_url`` (MediaMTX substream republish),
    hardware-decodes + scales it, and writes raw ``yuv420p`` frames to stdout.

    Read frames from stdout in ``frame_size_bytes(width, height)`` chunks.
    """
    if not rtsp_url.startswith(("rtsp://", "rtsps://")):
        raise ValueError(f"expected an rtsp(s):// substream URL, got {rtsp_url!r}")
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-rtsp_transport", rtsp_transport,
        *decode_args(hwaccel, device=device, codec=codec, width=width, height=height),
        "-i", rtsp_url,
        "-vf", scale_filter(hwaccel, width=width, height=height, fps=fps),
        "-f", "rawvideo",
        "-pix_fmt", "yuv420p",
        "pipe:1",
    ]
