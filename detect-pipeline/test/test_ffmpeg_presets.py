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
