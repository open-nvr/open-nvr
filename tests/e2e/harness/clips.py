# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test footage: what the suite needs, and which half ffmpeg can fake.

The fake-camera rig turns a folder of video files into RTSP streams, so a clip
is how any test gets a camera that actually publishes. Two kinds are needed and
only one of them can be generated.

**Synthetic clips cover everything that does not need object detection.**
Streaming, recording, segment rotation, playback and export do not care what is
in the picture — only that frames keep arriving. ffmpeg draws those, they are a
few hundred kilobytes, they are byte-identical on every machine, and they add
no binaries to the repo.

**Detection and LPR need real footage, and there is no way around it.** Tier-0
gates on motion and then runs YOLOv8, which classifies COCO objects. A drawn
rectangle is not a person or a car, so a synthetic clip produces motion, no
detection, no track, no visit — and every downstream assertion would fail for a
reason that has nothing to do with OpenNVR. Plate OCR needs a genuinely legible
plate on top of that. So those tiers use real clips from a folder you point at,
and skip with a clear message when it is empty.

Even the synthetic clips have a shape requirement. Tier-0's motion detector
must *calibrate* before it will ever run the detector, and calibration needs a
comparatively quiet frame — under 5% of the frame moving. Continuous motion
from the first frame never calibrates, so the generated clips open still and
start moving several seconds in. That is also what a real fixed camera looks
like, which is what Tier-0 was tuned for.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Container image used to render clips. The fake-camera rig already requires
#: it, so this adds no pull — and it means neither the host nor the runner
#: image needs its own ffmpeg for clip preparation.
FFMPEG_IMAGE = "bluenviron/mediamtx:1.15.4-ffmpeg"

#: Extensions the rig picks up (docs/FAKE_CAMERAS.md).
VIDEO_SUFFIXES = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".ts", ".webm"}

#: Small on purpose. Every camera costs a decode on the rig and a decode plus
#: motion pass on the stack, and the suite may run several at once on a laptop.
WIDTH, HEIGHT, FPS = 640, 480, 10


@dataclass(frozen=True)
class ClipSpec:
    """One generated clip. ``name`` becomes the stream and the camera name."""

    name: str
    seconds: int
    #: ffmpeg -vf filter chain, or None for an unfiltered source.
    filters: str | None
    purpose: str


#: The still seconds at the start of every moving clip. Tier-0 needs a quiet
#: frame to finish calibrating; without one it skips every frame forever and
#: reports `tier0_detector_skipped_total{reason="calibrating"}`.
QUIET_LEAD_SECONDS = 8


SYNTHETIC_CLIPS: tuple[ClipSpec, ...] = (
    ClipSpec(
        name="e2e-static",
        seconds=30,
        filters=None,
        purpose=(
            "A frame that never changes. Proves streaming, recording and "
            "playback work without involving motion at all, so a failure here "
            "is unambiguously about the media path."
        ),
    ),
    ClipSpec(
        name="e2e-motion",
        seconds=40,
        # A box that sits off-frame for the quiet lead, then crosses. `t` is
        # the frame timestamp, so the whole animation is one expression and
        # the clip stays deterministic.
        filters=(
            f"drawbox=x='if(lt(t,{QUIET_LEAD_SECONDS}),-200,"
            f"(t-{QUIET_LEAD_SECONDS})*70-100)'"
            ":y=180:w=120:h=90:color=white@1.0:t=fill"
        ),
        purpose=(
            "Still, then a moving block. Enough to exercise the motion gate "
            "and segment rotation. NOT enough for detection: YOLOv8 will not "
            "call a white rectangle a person or a car."
        ),
    ),
)


def ffmpeg_command(spec: ClipSpec, out_dir: str = "/out") -> list[str]:
    """The ffmpeg argv that renders one spec. Pure, so it can be unit-tested.

    Encoding choices that matter:

    * ``libx264`` + ``yuv420p`` — the rig can then stream-copy it, and every
      decoder in the stack accepts it.
    * ``-g`` one keyframe per second — the rig loops these files forever, and
      frequent keyframes keep the loop seam cheap to recover from.
    * ``-t`` an exact duration so a clip is the same length everywhere.
    """
    source = f"color=c=gray:s={WIDTH}x{HEIGHT}:r={FPS}"
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        source,
        "-t",
        str(spec.seconds),
    ]
    if spec.filters:
        args += ["-vf", spec.filters]
    args += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(FPS),
        f"{out_dir}/{spec.name}.mp4",
    ]
    return args


def real_clips(directory: Path) -> list[Path]:
    """Video files in ``directory``, one level deep, sorted.

    Mirrors what the rig itself picks up so the suite's idea of "is there
    footage" matches the rig's.
    """
    if not directory.is_dir():
        return []
    found = [
        path
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
    ]
    return sorted(found)


def synthetic_names() -> set[str]:
    """Stream names the suite generates, so real footage can be told apart."""
    return {spec.name for spec in SYNTHETIC_CLIPS}


__all__ = [
    "ClipSpec",
    "SYNTHETIC_CLIPS",
    "FFMPEG_IMAGE",
    "VIDEO_SUFFIXES",
    "QUIET_LEAD_SECONDS",
    "ffmpeg_command",
    "real_clips",
    "synthetic_names",
]
