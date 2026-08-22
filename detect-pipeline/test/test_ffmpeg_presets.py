# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the ffmpeg preset / decode-command construction."""
from __future__ import annotations

import pytest

from detect_pipeline.ffmpeg_presets import (
    HwAccel,
    build_decode_command,
    decode_args,
    frame_size_bytes,
    scale_filter,
)

RTSP = "rtsp://127.0.0.1:8554/cam-front_sub"


def test_frame_size_is_i420_12bpp():
    assert frame_size_bytes(640, 360) == 640 * 360 * 3 // 2
    assert frame_size_bytes(1280, 720) == 1_382_400


@pytest.mark.parametrize("w,h", [(0, 360), (640, 0), (-1, 10)])
def test_frame_size_rejects_nonpositive(w, h):
    with pytest.raises(ValueError):
        frame_size_bytes(w, h)


def test_cpu_has_no_hwaccel_and_software_scales():
    assert decode_args(HwAccel.CPU) == []
    vf = scale_filter(HwAccel.CPU, width=640, height=360, fps=5)
    assert vf == "fps=5,scale=640:360,format=yuv420p"


def test_vaapi_decode_substitutes_device_and_downloads_to_i420():
    args = decode_args(HwAccel.VAAPI, device="/dev/dri/renderD128")
    assert "-hwaccel" in args and "vaapi" in args
    assert "/dev/dri/renderD128" in args
    vf = scale_filter(HwAccel.VAAPI, width=640, height=360, fps=5)
    # the load-bearing hwdownload step, ending in planar I420
    assert "scale_vaapi=w=640:h=360" in vf
    assert "hwdownload" in vf
    assert vf.endswith("format=yuv420p")


def test_nvidia_uses_cuda_and_scale_cuda():
    args = decode_args(HwAccel.NVIDIA, device="0")
    assert "cuda" in args
    vf = scale_filter(HwAccel.NVIDIA, width=1280, height=720, fps=10)
    assert "scale_cuda=w=1280:h=720" in vf and "hwdownload" in vf


def test_jetson_scales_in_decoder_and_picks_codec_token():
    args = decode_args(HwAccel.JETSON, codec="h264", width=640, height=360)
    assert "h264_nvmpi" in args
    assert "640x360" in " ".join(args)   # -resize WxH
    # h265 selects the hevc token
    assert "hevc_nvmpi" in decode_args(HwAccel.JETSON, codec="h265", width=1, height=1)


def test_rpi_v4l2m2m_codec_token():
    assert "h264_v4l2m2m" in decode_args(HwAccel.RPI, codec="h264")
    assert "hevc_v4l2m2m" in decode_args(HwAccel.RPI, codec="h265")


def test_build_command_is_ordered_and_pipes_rawvideo():
    cmd = build_decode_command(
        RTSP, width=640, height=360, fps=5, hwaccel=HwAccel.VAAPI, device="/dev/dri/renderD128"
    )
    assert cmd[0] == "ffmpeg"
    # decode args must come BEFORE -i, scale/output AFTER
    i_idx = cmd.index("-i")
    assert cmd[i_idx + 1] == RTSP
    assert cmd.index("-hwaccel") < i_idx
    assert cmd.index("-vf") > i_idx
    assert cmd[-3:] == ["-pix_fmt", "yuv420p", "pipe:1"]
    assert "-rtsp_transport" in cmd and "tcp" in cmd


def test_build_command_rejects_non_rtsp_url():
    with pytest.raises(ValueError):
        build_decode_command("http://x/y", width=640, height=360, fps=5)


def test_scale_rejects_nonpositive_fps():
    with pytest.raises(ValueError):
        scale_filter(HwAccel.CPU, width=640, height=360, fps=0)


# ── decode-side frame skip (DETECT_DECODE_SKIP) ────────────────────────


def test_decode_skip_default_adds_nothing():
    cmd = build_decode_command(RTSP, width=640, height=360, fps=5)
    assert "-skip_frame" not in cmd


def test_decode_skip_inserts_before_input():
    """-skip_frame is an input (decoder) option — it must precede -i, and
    must precede the hwaccel decode args so it applies to the decoder."""
    cmd = build_decode_command(
        RTSP, width=640, height=360, fps=5, decode_skip="nokey",
    )
    i_idx = cmd.index("-i")
    s_idx = cmd.index("-skip_frame")
    assert s_idx < i_idx
    assert cmd[s_idx + 1] == "nokey"


def test_decode_skip_all_modes_accepted():
    for mode in ("bidir", "nonref", "nokey"):
        cmd = build_decode_command(
            RTSP, width=640, height=360, fps=5, decode_skip=mode,
        )
        assert cmd[cmd.index("-skip_frame") + 1] == mode


def test_decode_skip_rejects_unknown_mode():
    with pytest.raises(ValueError):
        build_decode_command(
            RTSP, width=640, height=360, fps=5, decode_skip="keyframes",
        )


# ── decoder thread cap + fast decode ───────────────────────────────────


def test_decode_threads_default_two_before_input():
    cmd = build_decode_command(RTSP, width=640, height=360, fps=5)
    t_idx = cmd.index("-threads")
    assert cmd[t_idx + 1] == "2"
    assert t_idx < cmd.index("-i")


def test_decode_threads_zero_means_ffmpeg_auto():
    cmd = build_decode_command(RTSP, width=640, height=360, fps=5, decode_threads=0)
    assert "-threads" not in cmd


def test_fast_decode_cpu_only():
    """The loop-filter skip applies to software decode only — hw decoders
    ignore these AVOptions (deblocking is done in silicon)."""
    cmd = build_decode_command(
        RTSP, width=640, height=360, fps=5, fast_decode=True,
    )
    lf_idx = cmd.index("-skip_loop_filter")
    assert cmd[lf_idx + 1] == "all"
    assert "-flags2" in cmd and lf_idx < cmd.index("-i")
    hw = build_decode_command(
        RTSP, width=640, height=360, fps=5, fast_decode=True, hwaccel=HwAccel.VAAPI,
    )
    assert "-skip_loop_filter" not in hw


def test_fast_decode_off_by_default():
    cmd = build_decode_command(RTSP, width=640, height=360, fps=5)
    assert "-skip_loop_filter" not in cmd
