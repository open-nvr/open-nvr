# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Where a camera's video comes from.

Most cameras are IP cameras: MediaMTX *pulls* their ``rtsp_url``. A webcam
(a browser's camera over WHIP, or a USB webcam pushed by the host agent)
works the other way round: something *publishes* into the camera's MediaMTX
path. Everything downstream — live view, recording, snapshots,
detection — reads the path, so the only things that differ are how the path
is provisioned and which IP-camera chores (ONVIF, probes, substreams, the
duplicate check) are skipped.

``Camera.source_type`` is NULL on every camera from before the column, which
means ``rtsp``.

A publisher camera has no address, but ``cameras.ip_address`` is NOT NULL and
read in many places, so it stores ``PUBLISHER_IP_SENTINEL``. Nothing should
show or dial it: check ``is_publisher`` first.
"""

from __future__ import annotations

from typing import Any

SOURCE_RTSP = "rtsp"
SOURCE_PUBLISHER = "publisher"
SOURCE_TYPES = (SOURCE_RTSP, SOURCE_PUBLISHER)

PUBLISHER_IP_SENTINEL = "0.0.0.0"


def is_publisher(camera: Any) -> bool:
    """True for a camera whose MediaMTX path is fed by a publisher."""
    return getattr(camera, "source_type", None) == SOURCE_PUBLISHER


def is_host_webcam(camera: Any) -> bool:
    """A webcam fed by the host agent: a USB webcam on the OpenNVR machine.

    The other kind of webcam (``source_device`` NULL) is published from a
    browser tab.
    """
    return is_publisher(camera) and bool(getattr(camera, "source_device", None))


#: Lifetime of a MediaMTX publish token for a webcam. MediaMTX checks the
#: token only when a publisher connects, so a long session outlives it; a
#: short one limits what a leaked token (browser memory, a process list)
#: is good for.
PUBLISH_TOKEN_MINUTES = 5
