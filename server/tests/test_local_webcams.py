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
    monkeypatch.delenv(lw.LOCAL_WEBCAMS_ENV, raising=False)
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


def _cam(source_type: str, source_device: str | None):
    from types import SimpleNamespace
    return SimpleNamespace(id=7, ip_address="0.0.0.0", source_type=source_type,
                           source_device=source_device)


def test_provisioning_for_mints_a_capture_command_only_for_local_webcams():
    # A host-agent webcam: no capture command (the agent runs ffmpeg), but
    # its ffmpeg publishes <path>-sub too, so the sub path is asked for (#900).
    assert lw.provisioning_for(_cam("publisher", "USB Camera (046d:0825)")) == {"substream": True}


def test_provisioning_for_asks_nothing_for_a_browser_webcam_or_an_ip_camera():
    assert lw.provisioning_for(_cam("publisher", None)) == {}
    assert lw.provisioning_for(_cam("rtsp", None)) == {}


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


def test_the_local_list_says_which_camera_a_webcam_feeds(monkeypatch):
    """GET /webcams/local marks a webcam already feeding a camera, so the
    dialog shows it as taken (one webcam, one camera)."""
    from types import SimpleNamespace

    from routers import webcams as r

    dev = "/dev/v4l/by-id/usb-a-video-index0"
    monkeypatch.setattr(r, "available", lambda: True)
    monkeypatch.setattr(r, "list_local_webcams", lambda: [
        lw.LocalWebcam(id=dev, name="C920", node="video0"),
        lw.LocalWebcam(id="/dev/v4l/by-id/usb-b-video-index0", name="Other", node="video2"),
    ])
    cams = [SimpleNamespace(id=4, name="Desk", source_device=dev),
            SimpleNamespace(id=5, name="Agent cam", source_device="USB Camera (046d:0825)")]

    class _Q:
        def filter(self, *a):
            return self

        def all(self):
            return cams

    out = r.local_webcams(db=SimpleNamespace(query=lambda *_: _Q()), _user=None)
    assert out["available"] is True
    by_id = {w["id"]: w for w in out["webcams"]}
    assert by_id[dev]["assigned_camera_id"] == 4 and by_id[dev]["assigned_camera_name"] == "Desk"
    assert by_id["/dev/v4l/by-id/usb-b-video-index0"]["assigned_camera_id"] is None


# ── #904: the overlay's own flag, and the server's OS ───────────────


def test_the_overlay_flag_counts_with_no_webcam_plugged_in(monkeypatch, tmp_path):
    """udev creates /dev/v4l/by-id only while a webcam is plugged in. With
    the overlay in place and nothing plugged in, the dialog must say "plug
    one in", not fall back to the host agent."""
    monkeypatch.setenv(lw.BY_ID_DIR_ENV, str(tmp_path / "absent"))
    monkeypatch.setenv(lw.LOCAL_WEBCAMS_ENV, "on")
    assert lw.available() is True
    assert lw.list_local_webcams() == []
    monkeypatch.setenv(lw.LOCAL_WEBCAMS_ENV, "off")
    assert lw.available() is False


@pytest.mark.parametrize("raw, want", [
    ("linux", "linux"), (" Windows ", "windows"), ("darwin", "darwin"),
    ("", None), ("freebsd", None),
])
def test_host_os_takes_only_the_three_known_values(monkeypatch, raw, want):
    monkeypatch.setenv(lw.HOST_OS_ENV, raw)
    assert lw.host_os() == want


def test_host_os_unset_is_none(monkeypatch):
    monkeypatch.delenv(lw.HOST_OS_ENV, raising=False)
    assert lw.host_os() is None


def test_the_local_list_says_which_os_the_server_runs(monkeypatch):
    """The dialog decides between "turn WEBCAMS on" (Linux) and the host
    agent (Docker Desktop) from this, never from the browser's OS."""
    from types import SimpleNamespace

    from routers import webcams as r

    monkeypatch.setenv(lw.HOST_OS_ENV, "linux")
    monkeypatch.setattr(r, "available", lambda: False)
    out = r.local_webcams(db=SimpleNamespace(), _user=None)
    assert out == {"available": False, "webcams": [], "host_os": "linux"}


def test_the_agent_view_falls_back_to_the_launchers_os(monkeypatch):
    """Before any agent has connected, its OS is the launcher's, so the
    install command shown is this server's (a Windows browser looking at a
    Linux server was told to run start.ps1)."""
    from services import host_agent as ha

    monkeypatch.setenv(lw.HOST_OS_ENV, "linux")
    monkeypatch.setattr(ha.state, "snapshot", lambda: (None, None))
    monkeypatch.setattr(ha.state, "connected", lambda: False)
    monkeypatch.setattr(ha, "_stored_report", lambda db: None)

    class _Q:
        def filter(self, *a):
            return self

        def all(self):
            return []

    from types import SimpleNamespace
    view = ha.public_view(SimpleNamespace(query=lambda *_: _Q()))
    assert view["os"] == "linux"

    monkeypatch.setattr(ha.state, "snapshot", lambda: ({"os": "windows"}, 1.0))
    assert ha.public_view(SimpleNamespace(query=lambda *_: _Q()))["os"] == "windows",         "the agent's own report wins"
