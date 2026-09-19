# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small JPEGs, rendered on demand.

Several endpoints take an image: a visit's best-frame crop, its scene frame,
and plate candidates all arrive at core as base64 JPEG. Testing those paths
needs *a* JPEG, and for most of them any valid one will do — the assertion is
that the image round-trips and comes back byte-for-byte, not that it depicts
anything.

Rendered with ffmpeg (present in the runner image) rather than committed as a
base64 blob. A blob would be opaque, unreviewable and awkward to vary; a
function is readable, takes parameters, and adds nothing binary to the repo.

Only the *transport* is testable this way. A rendered plate is not a
substitute for real footage in an OCR assertion — see ``clips.py`` for why
detection and LPR need genuine video.
"""

from __future__ import annotations

import base64
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path


class ImageRenderFailed(RuntimeError):
    """ffmpeg could not produce the image."""


@lru_cache(maxsize=16)
def jpeg(width: int = 320, height: int = 240, color: str = "gray") -> bytes:
    """A valid JPEG of the given size. Cached, because callers want the bytes.

    Args:
        width, height: pixel dimensions. Keep them modest — core caps evidence
            size, and these travel base64-encoded inside a JSON body.
        color: any ffmpeg colour name or ``#rrggbb``.

    Raises:
        ImageRenderFailed: with ffmpeg's own stderr, which says far more than
            a generic message would.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "frame.jpg"
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi",
                "-i", f"color=c={color}:s={width}x{height}",
                "-frames:v", "1",
                str(target),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0 or not target.exists():
            raise ImageRenderFailed(
                "ffmpeg could not render a test JPEG "
                f"({width}x{height}, {color}):\n"
                + result.stderr.decode("utf-8", errors="replace")[-600:]
            )
        data = target.read_bytes()

    if data[:2] != b"\xff\xd8":
        raise ImageRenderFailed("ffmpeg produced something that is not a JPEG")
    return data


def jpeg_b64(width: int = 320, height: int = 240, color: str = "gray") -> str:
    """The same image, base64-encoded — the shape core's ingest API expects."""
    return base64.b64encode(jpeg(width, height, color)).decode("ascii")


__all__ = ["jpeg", "jpeg_b64", "ImageRenderFailed"]
