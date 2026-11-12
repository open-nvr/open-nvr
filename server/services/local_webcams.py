# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""USB webcams on the OpenNVR machine, served by MediaMTX itself.

On Linux the webcam's device node is passed into the MediaMTX container
(``docker-compose.webcams.yml``: ``/dev`` with a cgroup rule for the
video4linux major, the ``video`` group) and MediaMTX runs ffmpeg for the
camera's path through ``runOnInit``, restarting it when the camera is
unplugged and plugged back. No host install, no sudo, nothing outside
Docker.
Docker Desktop on macOS and Windows cannot see USB; there the host agent
(``services/host_agent.py``) does the same job from the host.

Two things live here:

* **Enumeration.** Core reads ``/dev/v4l/by-id`` (mounted read-only by the
  overlay) for the stable, port-independent device names and
  ``/sys/class/video4linux/<node>/name`` for the human name. A webcam
  exposes two nodes (capture and metadata); only the capture one
  (``index0``) is a camera.
* **The ffmpeg command** MediaMTX runs: one capture, two outputs — the
  main path at the webcam's size (up to 720p) and ``<path>-sub`` at 640
  wide, 10 fps, so detection reads the small stream and evidence the
  large one, exactly as for an IP camera with a substream. Published to
  MediaMTX's own RTSP listener on localhost with a publish-only JWT for
  the two paths.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import Any

#: Where the overlay mounts the host's stable device names and the sysfs
#: names into the core container. Overridable for tests.
BY_ID_DIR_ENV = "WEBCAM_BY_ID_DIR"
SYSFS_DIR_ENV = "WEBCAM_SYSFS_DIR"
DEFAULT_BY_ID_DIR = "/dev/v4l/by-id"
DEFAULT_SYSFS_DIR = "/sys/class/video4linux"

#: ``source_device`` of a webcam served by MediaMTX starts with this; the
#: host agent's ids never do (dshow / avfoundation / v4l2 names).
LOCAL_DEVICE_PREFIX = "/dev/v4l/by-id/"

#: Encoder for the two H.264 outputs. ``libx264`` is everywhere; a board
#: with a V4L2 M2M encoder (Raspberry Pi, some ARM SoCs) can set
#: ``WEBCAM_ENCODER=h264_v4l2m2m``. Measured elsewhere, not assumed here.
ENCODER_ENV = "WEBCAM_ENCODER"
DEFAULT_ENCODER = "libx264"
_ENCODERS = ("libx264", "h264_v4l2m2m", "h264_vaapi", "h264_qsv")

DEFAULT_SIZE = "1280x720"
DEFAULT_FPS = 25
DEFAULT_SUB_WIDTH = 640
DEFAULT_SUB_FPS = 10

_SIZE_RE = re.compile(r"^\d{3,4}x\d{3,4}$")


@dataclass(frozen=True)
class LocalWebcam:
    id: str          # /dev/v4l/by-id/usb-…-video-index0 — stable across ports
    name: str        # "HD Pro Webcam C920" from sysfs, else the id's middle
    node: str        # video0 — the node the symlink points at (informational)

    def to_wire(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "node": self.node}


def is_local_device(device: str | None) -> bool:
    return bool(device) and str(device).startswith(LOCAL_DEVICE_PREFIX)


def by_id_dir() -> str:
    return os.environ.get(BY_ID_DIR_ENV, "").strip() or DEFAULT_BY_ID_DIR


def sysfs_dir() -> str:
    return os.environ.get(SYSFS_DIR_ENV, "").strip() or DEFAULT_SYSFS_DIR


#: Set to ``on`` by docker-compose.webcams.yml on core (#904).
LOCAL_WEBCAMS_ENV = "OPENNVR_LOCAL_WEBCAMS"
#: The host's OS as the launchers write it (start.sh / start.ps1).
HOST_OS_ENV = "OPENNVR_HOST_OS"
_HOST_OSES = ("linux", "darwin", "windows")


def available() -> bool:
    """Is the overlay in place? False on macOS/Windows and on a Linux
    install without ``WEBCAMS=on``; the Add Camera dialog then says how to
    turn it on (Linux) or offers the host agent (Docker Desktop).

    The overlay's own flag decides. ``/dev/v4l/by-id`` cannot on its own:
    udev creates it only while a webcam is plugged in, so with the overlay
    in place and nothing plugged in the dialog offered the host agent
    instead of "plug one in" (#904). The directory still counts, for a core
    started by an overlay older than the flag."""
    if os.environ.get(LOCAL_WEBCAMS_ENV, "").strip().lower() == "on":
        return True
    return os.path.isdir(by_id_dir())


def host_os() -> str | None:
    """``linux``/``darwin``/``windows`` when a launcher said so, else None.
    Core runs in a Linux container everywhere, so it cannot read this off
    itself; the dialog needs it to name the server's own commands rather
    than guess from the browser's OS (#904)."""
    value = os.environ.get(HOST_OS_ENV, "").strip().lower()
    return value if value in _HOST_OSES else None


def _pretty_from_id(entry: str) -> str:
    # usb-046d_HD_Pro_Webcam_C920_ABCDEF-video-index0 → "046d HD Pro Webcam C920 ABCDEF"
    core = re.sub(r"^usb-", "", entry)
    core = re.sub(r"-video-index\d+$", "", core)
    return core.replace("_", " ").strip() or entry


def list_local_webcams(*, by_id: str | None = None, sysfs: str | None = None) -> list[LocalWebcam]:
    """The capture nodes under ``/dev/v4l/by-id``, with sysfs names. Sorted by
    name so the picker is stable. Empty when the directory is absent."""
    by_id = by_id or by_id_dir()
    sysfs = sysfs or sysfs_dir()
    out: list[LocalWebcam] = []
    try:
        entries = sorted(os.listdir(by_id))
    except OSError:
        return out
    for entry in entries:
        if not entry.endswith("-video-index0"):
            continue          # index1 is the metadata node of the same camera
        link = os.path.join(by_id, entry)
        try:
            node = os.path.basename(os.readlink(link))
        except OSError:
            node = ""
        name = ""
        if node:
            try:
                with open(os.path.join(sysfs, node, "name"), encoding="utf-8", errors="replace") as f:
                    name = f.read().strip()
            except OSError:
                name = ""
        # sysfs names often carry the node's role: "C920: HD Pro Webcam C920 (usb-…)"
        name = re.sub(r"\s*\(usb-[^)]*\)\s*$", "", name).strip()
        out.append(LocalWebcam(id=link, name=name or _pretty_from_id(entry), node=node))
    return sorted(out, key=lambda w: (w.name.lower(), w.id))


def find_local_webcam(device: str) -> LocalWebcam | None:
    for w in list_local_webcams():
        if w.id == device:
            return w
    return None


def encoder() -> str:
    raw = os.environ.get(ENCODER_ENV, "").strip() or DEFAULT_ENCODER
    return raw if raw in _ENCODERS else DEFAULT_ENCODER


def _size() -> str:
    raw = os.environ.get("WEBCAM_SIZE", "").strip()
    return raw if _SIZE_RE.match(raw) else DEFAULT_SIZE


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


def _encode_args(enc: str, gop: int) -> list[str]:
    if enc == "libx264":
        return ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-pix_fmt", "yuv420p", "-g", str(gop), "-bf", "0"]
    if enc == "h264_v4l2m2m":
        return ["-c:v", "h264_v4l2m2m", "-pix_fmt", "yuv420p", "-g", str(gop), "-b:v", "3M"]
    if enc == "h264_vaapi":
        return ["-c:v", "h264_vaapi", "-g", str(gop)]
    return ["-c:v", enc, "-g", str(gop)]


def run_on_init_command(device: str, main_path: str, sub_path: str, token: str, *,
                        rtsp_port: int = 8554, enc: str | None = None) -> str:
    """The ``runOnInit`` line for a webcam's main path. MediaMTX runs it when
    the path is created and again whenever it exits (``runOnInitRestart``),
    which is what survives an unplug.

    One ffmpeg, one capture, two outputs: the main path at the capture size
    and the ``-sub`` path at 640 wide, 10 fps. The token is publish-only for
    those two paths; the listener is MediaMTX's own, on localhost, so the
    token never leaves the container.
    """
    enc = enc or encoder()
    fps = _int_env("WEBCAM_FPS", DEFAULT_FPS, 5, 30)
    sub_w = _int_env("WEBCAM_SUB_WIDTH", DEFAULT_SUB_WIDTH, 320, 960)
    sub_fps = _int_env("WEBCAM_SUB_FPS", DEFAULT_SUB_FPS, 2, 15)
    base = f"rtsp://127.0.0.1:{rtsp_port}"
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
        "-f", "v4l2", "-framerate", str(fps), "-video_size", _size(),
        "-i", device,
        "-filter_complex",
        f"[0:v]split=2[m][s];[s]scale={sub_w}:-2,fps={sub_fps}[sub]",
        "-map", "[m]", *_encode_args(enc, fps * 2),
        "-f", "rtsp", "-rtsp_transport", "tcp", f"{base}/{main_path}?jwt={token}",
        "-map", "[sub]", *_encode_args(enc, sub_fps * 2),
        "-f", "rtsp", "-rtsp_transport", "tcp", f"{base}/{sub_path}?jwt={token}",
    ]
    return " ".join(shlex.quote(a) for a in args)


def substream_marker(sub_path: str, *, rtsp_base: str = "rtsp://mediamtx:8554") -> str:
    """What the camera row's ``substream_url`` holds for a MediaMTX-served
    webcam: the sub path's own address. Nothing dials it (publisher cameras
    skip every probe); it is the fact that a sub stream exists, which is what
    the detect tap looks at."""
    return f"{rtsp_base.rstrip('/')}/{sub_path}"


def provisioning_for(camera: Any) -> dict[str, Any]:
    """The extra path config a webcam camera needs.

    * A webcam MediaMTX serves itself: ``{"run_on_init": …, "substream":
      True}``. Mints the publish token for the two paths; the token lives
      inside MediaMTX's path config and reaches only its own listener on
      localhost. Long-lived on purpose: MediaMTX restarts the command on its
      own (``runOnInitRestart``) for as long as the path exists, and a token
      that expired between core restarts would strand the webcam; every core
      start re-provisions and re-mints anyway.
    * A webcam the host agent streams: ``{"substream": True}``. The agent's
      ffmpeg publishes the main path and ``<path>-sub`` from one capture, so
      both paths must exist; without the sub, MediaMTX refuses that output
      and ffmpeg exits, taking the main stream with it (issue #900).
    * Any other camera (a browser webcam publishes the main path only):
      ``{}``."""
    from core.camera_source import is_host_webcam
    from core.config import settings
    from services.mediamtx_jwt_service import MediaMtxJwtService
    from services.stream_service import _build_stream_name, substream_name

    device = getattr(camera, "source_device", None)
    if not is_local_device(device):
        return {"substream": True} if is_host_webcam(camera) else {}
    name = _build_stream_name(settings.mediamtx_stream_prefix, camera.id, camera.ip_address)
    sub = substream_name(name)
    token = MediaMtxJwtService.create_publish_token(
        camera_id=camera.id, camera_path=name, expiry_minutes=365 * 24 * 60, extra_paths=[sub])
    return {"run_on_init": run_on_init_command(str(device), name, sub, token), "substream": True}
