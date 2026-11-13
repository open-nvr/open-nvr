# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turning the fake-camera rig into cameras the suite can assert on.

The rig (``docker-compose.fakecams.yml``) runs MediaMTX plus one looping
ffmpeg per video file, publishing each as its own RTSP path. As far as OpenNVR
is concerned those are ordinary cameras, which is what makes hardware-free
end-to-end testing possible at all.

This module is the runner-side half: ask the rig what it is serving, then
create a camera for the stream a test wants. Clips are put in place by
``run.py`` before the stack starts, because the rig enumerates its folder once
at boot — see ``clips.py``.

**Synthetic and real streams are not interchangeable.** A generated clip is
fine for streaming, recording and playback. Detection and LPR need real
footage, because Tier-0 runs YOLOv8 and a drawn rectangle is not a COCO class.
``pick_stream`` makes a test say which kind it needs, so a detection test that
finds no real footage skips with an explanation instead of failing sixty
seconds later with an empty event list.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .budgets import BUDGETS
from .clips import synthetic_names
from .waiting import eventually


@dataclass(frozen=True)
class RigStream:
    """One path the rig is serving."""

    name: str
    #: MediaMTX's own readiness: true once its ffmpeg is actually publishing.
    ready: bool

    @property
    def synthetic(self) -> bool:
        return self.name in synthetic_names()


class NoFootage(Exception):
    """The rig is serving nothing of the kind a test needs."""


def rig_streams(api_url: str) -> list[RigStream]:
    """Everything the rig is currently publishing.

    ``all_others`` is MediaMTX's catch-all path definition rather than a
    stream, so it is dropped.
    """
    resp = httpx.get(f"{api_url.rstrip('/')}/v3/paths/list?itemsPerPage=1000", timeout=15)
    resp.raise_for_status()
    items = resp.json().get("items") or []
    return sorted(
        (
            RigStream(name=item["name"], ready=bool(item.get("ready")))
            for item in items
            if item.get("name") and item["name"] != "all_others"
        ),
        key=lambda stream: stream.name,
    )


def wait_for_stream(api_url: str, name: str) -> RigStream:
    """Block until the rig reports ``name`` as ready.

    A path exists as soon as it is configured but is not ready until its
    ffmpeg has actually started publishing. Pointing a camera at a
    not-yet-ready path is legal but produces a camera that never receives a
    frame — which then looks like a detection failure much further downstream.
    """
    return eventually(
        lambda: next(
            (s for s in rig_streams(api_url) if s.name == name and s.ready), None
        ),
        budget=BUDGETS.STREAM_READY,
        describe=f"the fake-camera rig to start publishing {name!r}",
    )


def pick_stream(api_url: str, *, real: bool) -> RigStream:
    """Choose a stream of the kind the caller needs.

    Args:
        real: True for detection and LPR, which need actual objects in frame.
            False for media-path tests, which are happy with a generated clip
            and are faster and more deterministic for it.

    Raises:
        NoFootage: with an actionable message. Callers turn this into a skip.
    """
    streams = rig_streams(api_url)
    if not streams:
        raise NoFootage(
            "The fake-camera rig is serving nothing at all.\n"
            "  Clips are staged into tests/e2e/.artifacts/clips before the "
            "stack starts; the rig only enumerates that folder at boot.\n"
            "  Re-run through `python tests/e2e/run.py`, which stages them."
        )

    candidates = [s for s in streams if s.synthetic is not real]
    if not candidates:
        if real:
            raise NoFootage(
                "No real footage is available, only generated clips.\n"
                "  Detection and LPR need it: Tier-0 runs YOLOv8, which "
                "classifies real objects — it will never call a drawn "
                "rectangle a person or a car, so no track and no visit is "
                "ever produced.\n"
                "  Put a few clips in data/fake-cameras/, or point "
                "E2E_CLIP_SOURCE at a folder of them, then re-run with --fresh."
            )
        raise NoFootage(
            "The generated clips are missing — only real footage is being "
            "served. run.py renders them into tests/e2e/.artifacts/clips; "
            "check its output for an ffmpeg failure."
        )

    # Prefer one that is already publishing, so the caller does not have to
    # wait for ffmpeg to spin up.
    ready = [s for s in candidates if s.ready]
    return (ready or candidates)[0]


def camera_for_stream(client, config, stream: RigStream, *, label: str) -> dict:
    """Create a camera pointed at one of the rig's streams.

    Goes through ``client.create_camera``, so the camera is provisioned by the
    ordinary public API exactly like a real one — and is registered for
    teardown automatically.
    """
    return client.create_camera(
        label=label,
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{stream.name}",
        ip_address=config.fakecam_ip,
    )


__all__ = [
    "RigStream",
    "NoFootage",
    "rig_streams",
    "wait_for_stream",
    "pick_stream",
    "camera_for_stream",
]
