# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""USB webcams served by MediaMTX itself: what core lists, the capture command
it hands MediaMTX, and how a publisher path carries it."""
from __future__ import annotations

import os
import shlex

import pytest

from services import local_webcams as lw


@pytest.fixture()
def fake_devices(tmp_path, monkeypatch):
    by_id = tmp_path / "by-id"; by_id.mkdir()
    sysfs = tmp_path / "video4linux"; sysfs.mkdir()
    for node, name in (("video0", "HD Pro Webcam C920: HD Pro Webcam C920 (usb-0000:00:14.0-1)"),
                       ("video2", "Integrated Camera: Integrated C")):
        (sysfs / node).mkdir(); (sysfs / node / "name").write_text(name + "\n")
    os.symlink("../../video0", by_id / "usb-046d_HD_Pro_Webcam_C920_ABCDEF-video-index0")
    os.symlink("../../video1", by_id / "usb-046d_HD_Pro_Webcam_C920_ABCDEF-video-index1")   # metadata node
    os.symlink("../../video2", by_id / "usb-Chicony_Integrated_Camera_0001-video-index0")
    monkeypatch.setenv(lw.BY_ID_DIR_ENV, str(by_id))
    monkeypatch.setenv(lw.SYSFS_DIR_ENV, str(sysfs))
    return by_id


def test_lists_capture_nodes_with_sysfs_names(fake_devices):
    cams = lw.list_local_webcams()
    assert [c.name for c in cams] == ["HD Pro Webcam C920: HD Pro Webcam C920", "Integrated Camera: Integrated C"]
    assert cams[0].id.endswith("C920_ABCDEF-video-index0") and cams[0].node == "video0"
    assert all("index1" not in c.id for c in cams), "the metadata node is not a camera"
    assert lw.available() is True
    assert lw.find_local_webcam(cams[1].id) == cams[1]
    assert lw.find_local_webcam("/dev/v4l/by-id/usb-nope-video-index0") is None


def test_nothing_without_the_overlay(monkeypatch, tmp_path):
    monkeypatch.setenv(lw.BY_ID_DIR_ENV, str(tmp_path / "absent"))
    assert lw.available() is False and lw.list_local_webcams() == []


def test_the_capture_command_feeds_main_and_sub_with_one_token(monkeypatch):
    monkeypatch.delenv(lw.ENCODER_ENV, raising=False)
    cmd = lw.run_on_init_command("/dev/v4l/by-id/usb-x-video-index0", "cam-7", "cam-7-sub", "TOK")
    argv = shlex.split(cmd)
    assert argv[0] == "ffmpeg" and "-nostdin" in argv
    assert argv[argv.index("-i") + 1] == "/dev/v4l/by-id/usb-x-video-index0"
    assert "split=2" in argv[argv.index("-filter_complex") + 1]
    assert "scale=640:-2,fps=10" in argv[argv.index("-filter_complex") + 1]
    assert argv.count("libx264") == 2, "both outputs encoded"
    assert "rtsp://127.0.0.1:8554/cam-7?jwt=TOK" in argv and "rtsp://127.0.0.1:8554/cam-7-sub?jwt=TOK" in argv
    monkeypatch.setenv(lw.ENCODER_ENV, "h264_v4l2m2m")
    assert "h264_v4l2m2m" in lw.run_on_init_command("/dev/v4l/by-id/u-video-index0", "a", "a-sub", "T")
    monkeypatch.setenv(lw.ENCODER_ENV, "not-an-encoder")
    assert "libx264" in lw.run_on_init_command("/dev/v4l/by-id/u-video-index0", "a", "a-sub", "T")


def test_is_local_device_and_marker():
    assert lw.is_local_device("/dev/v4l/by-id/usb-a-video-index0")
    assert not lw.is_local_device("USB Camera (046d:0825)")      # a host-agent id
    assert not lw.is_local_device(None)
    assert lw.substream_marker("cam-7-sub") == "rtsp://mediamtx:8554/cam-7-sub"


def test_provisioning_for_only_touches_local_webcams():
    class Cam:
        id = 7; ip_address = "0.0.0.0"; source_type = "publisher"; source_device = "USB Camera (046d:0825)"
    assert lw.provisioning_for(Cam()) == {}


def test_map_conf_carries_run_on_init_for_a_publisher_path():
    from services.mediamtx_admin_service import MediaMtxAdminService
    conf = MediaMtxAdminService._map_conf({"source_type": "publisher", "run_on_init": "ffmpeg -i x",
                                           "recording": {"enabled": True, "path": "/r", "segment_seconds": 60}})
    assert conf["source"] == "publisher" and conf["runOnInit"] == "ffmpeg -i x" and conf["runOnInitRestart"] is True
    plain = MediaMtxAdminService._map_conf({"source_type": "publisher"})
    assert "runOnInit" not in plain


def test_camera_source_predicates():
    from core.camera_source import is_host_webcam, is_local_webcam

    class Cam:
        def __init__(self, dev): self.source_type = "publisher"; self.source_device = dev
    assert is_local_webcam(Cam("/dev/v4l/by-id/usb-a-video-index0")) and not is_host_webcam(Cam("/dev/v4l/by-id/usb-a-video-index0"))
    assert is_host_webcam(Cam("USB Camera (046d:0825)")) and not is_local_webcam(Cam("USB Camera (046d:0825)"))
    assert not is_host_webcam(Cam(None)) and not is_local_webcam(Cam(None))
