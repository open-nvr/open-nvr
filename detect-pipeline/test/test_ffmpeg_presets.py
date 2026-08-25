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
    # mode name -> the token ffmpeg's CLI accepts (nonref maps to noref)
    for mode, token in (("bidir", "bidir"), ("nonref", "noref"), ("nokey", "nokey")):
        cmd = build_decode_command(
            RTSP, width=640, height=360, fps=5, decode_skip=mode,
        )
        assert cmd[cmd.index("-skip_frame") + 1] == token


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


# ── the token ffmpeg ACTUALLY accepts (field regression) ───────────────
# The nonref mode shipped emitting "-skip_frame nonref"; ffmpeg's CLI token
# is "noref", so ffmpeg rejected the option and produced ZERO frames — every
# worker crash-looped ("5 consecutive restarts with no frames"). These tests
# pin the mapping, and the real-ffmpeg test proves every emitted token is
# one ffmpeg accepts, so a token can never drift again.


def test_nonref_mode_emits_ffmpeg_noref_token():
    cmd = build_decode_command(RTSP, width=640, height=360, fps=5,
                               decode_skip="nonref")
    assert cmd[cmd.index("-skip_frame") + 1] == "noref"


def test_ffmpeg_own_spelling_accepted_as_alias():
    cmd = build_decode_command(RTSP, width=640, height=360, fps=5,
                               decode_skip="noref")
    assert cmd[cmd.index("-skip_frame") + 1] == "noref"


import shutil as _shutil
import subprocess as _subprocess


@pytest.mark.skipif(_shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_accepts_every_emitted_skip_token():
    """Feed each mode's EMITTED token to a real ffmpeg decode: the option
    must parse (no 'Undefined constant' / 'Invalid argument' on stderr)."""
    from detect_pipeline.ffmpeg_presets import DECODE_SKIP_MODES, _FFMPEG_SKIP_TOKEN

    for mode in DECODE_SKIP_MODES:
        if mode == "none":
            continue
        token = _FFMPEG_SKIP_TOKEN.get(mode, mode)
        proc = _subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-skip_frame", token,
             "-f", "lavfi", "-i", "testsrc=duration=0.2:size=64x64",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, (mode, token, proc.stderr)
        assert "Undefined constant" not in proc.stderr, (mode, token, proc.stderr)
        assert "Invalid" not in proc.stderr, (mode, token, proc.stderr)


# ── RTSP socket timeout (the "stalled stream hangs forever" fix) ────

def test_decode_command_carries_rtsp_timeout_before_input():
    from detect_pipeline.ffmpeg_presets import build_decode_command

    cmd = build_decode_command("rtsp://h:8554/cam", width=320, height=240, fps=5)
    assert "-timeout" in cmd
    # microseconds, and it must precede -i to bind to the INPUT
    assert cmd[cmd.index("-timeout") + 1] == str(int(10.0 * 1_000_000))
    assert cmd.index("-timeout") < cmd.index("-i")


def test_decode_command_does_not_use_rw_timeout():
    """-rw_timeout is accepted by ffmpeg for RTSP but SILENTLY does nothing.

    Verified against the shipped image (ffmpeg 7.1.5): `-timeout 3000000`
    reaches the transport as `tcp://...?timeout=3000000`, while
    `-rw_timeout 3000000` arrives as `timeout=0`. Neither errors, so only a
    test can stop someone "simplifying" one into the other.
    """
    from detect_pipeline.ffmpeg_presets import build_decode_command

    cmd = build_decode_command("rtsp://h:8554/cam", width=320, height=240, fps=5)
    assert "-rw_timeout" not in cmd


def test_rtsp_timeout_can_be_disabled():
    from detect_pipeline.ffmpeg_presets import build_decode_command, rtsp_timeout_args

    assert rtsp_timeout_args(0) == []
    cmd = build_decode_command(
        "rtsp://h:8554/cam", width=320, height=240, fps=5, rtsp_timeout_s=0
    )
    assert "-timeout" not in cmd


@pytest.mark.skipif(_shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_rtsp_timeout_flag_is_accepted_by_real_ffmpeg():
    """The flag must PARSE on a real ffmpeg against a real RTSP input.

    ``-timeout`` belongs to the RTSP demuxer specifically — on a non-RTSP
    input ffmpeg rejects it with "Option timeout not found", which is why
    this points at an rtsp:// URL (a closed port: we want the option parsed,
    then a connection error, NOT an option error). A wrong option name here
    would make every camera fail instantly on deploy.
    """
    from detect_pipeline.ffmpeg_presets import build_decode_command

    cmd = build_decode_command(
        "rtsp://127.0.0.1:9/closed", width=64, height=64, fps=5, rtsp_timeout_s=1.0
    )
    proc = _subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert "Unrecognized option" not in proc.stderr, proc.stderr
    assert "Option not found" not in proc.stderr, proc.stderr
    # It should fail to CONNECT (port 9 is discard/closed), proving the
    # option parsed and ffmpeg got as far as dialling.
    assert proc.returncode != 0


# ── hwaccel resolution (degrade loudly, never fail every camera) ────

def test_resolve_hwaccel_keeps_a_usable_backend():
    from detect_pipeline.ffmpeg_presets import HwAccel, resolve_hwaccel

    accel, why = resolve_hwaccel("vaapi", _exists=lambda p: True)
    assert (accel, why) == (HwAccel.VAAPI, None)
    assert resolve_hwaccel("cpu", _exists=lambda p: False)[0] is HwAccel.CPU


def test_resolve_hwaccel_degrades_when_the_render_node_is_missing():
    """docker-compose ships `devices:` COMMENTED OUT, so DETECT_HWACCEL=vaapi
    without uncommenting it is the likeliest misconfiguration — and ffmpeg
    would exit instantly on every camera, not just one."""
    from detect_pipeline.ffmpeg_presets import HwAccel, resolve_hwaccel

    accel, why = resolve_hwaccel("vaapi", _exists=lambda p: False)
    assert accel is HwAccel.CPU
    assert why and "/dev/dri" in why


def test_resolve_hwaccel_degrades_on_an_unknown_name():
    from detect_pipeline.ffmpeg_presets import HwAccel, resolve_hwaccel

    accel, why = resolve_hwaccel("vaapii", _exists=lambda p: True)
    assert accel is HwAccel.CPU and why
    # empty/None mean "unset", not "broken"
    assert resolve_hwaccel(None)[0] is HwAccel.CPU
    assert resolve_hwaccel("")[1] is None


def test_nvidia_needs_no_device_node():
    from detect_pipeline.ffmpeg_presets import HwAccel, resolve_hwaccel

    accel, why = resolve_hwaccel("nvidia", _exists=lambda p: False)
    assert (accel, why) == (HwAccel.NVIDIA, None)


def test_decode_command_carries_hwaccel_args_when_resolved():
    from detect_pipeline.ffmpeg_presets import HwAccel, build_decode_command

    cmd = build_decode_command("rtsp://h/cam", width=704, height=576, fps=2,
                               hwaccel=HwAccel.VAAPI, device="/dev/dri/renderD128")
    assert "-hwaccel" in cmd and cmd[cmd.index("-hwaccel") + 1] == "vaapi"
    assert "/dev/dri/renderD128" in cmd
    assert "scale_vaapi" in cmd[cmd.index("-vf") + 1]
