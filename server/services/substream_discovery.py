# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Find a camera's substream when it is added, and keep it only if it answers.

Tier-0 detects on a camera's low-res second stream when one is STORED on
the camera (core's tap listing refuses to guess for an always-on detector),
and since the main-stream evidence work that stream costs nothing in
evidence quality. So the one thing standing between a fresh install and
the ~5-10x decode saving is whether anybody typed the substream URL in.
Core already stores the ONVIF-reported sub when it resolves the camera
itself (no URL given); this module covers the other, more common case: an
operator (or the import, or a discovery tool) supplied the main URL.

Candidates, in order:

1. the device's own ONVIF profiles — the smallest-resolution profile with a
   stream URI that differs from the main one (``_pick_substream_uri``), with
   the camera's credentials injected; needs credentials;
2. the vendor-convention URL derived from the main one (Hikvision/Uniview
   ``/101`` → ``/102``, Dahua/CP Plus ``subtype=0`` → ``subtype=1``).

Each candidate is PROBED (ffprobe over RTSP/TCP, a few seconds) before it
is stored. A candidate the camera refuses — 404 path, 401 after the
credentials, connection refused — is dropped and the next one tried. A
candidate that cannot be verified at all (ffprobe missing, the camera
unreachable right now) is still stored when it came from the device's
own ONVIF answer, and never when it is a vendor guess: the detect-pipeline
falls back to the main stream if a stored sub turns out dead, so an
unverified device-reported URL costs at worst a warning, while a wrong
guess stored unverified would be exactly the thing this module exists to
prevent. Never raises; None means "nothing found, keep the main stream".
"""
from __future__ import annotations

import asyncio
import html
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger("opennvr.substream_discovery")

#: Seconds a single candidate probe may take. Cameras answer DESCRIBE in
#: well under a second, but ffprobe then waits for a keyframe to read the
#: size — 4.8 s measured against the fake rig — so the budget covers a
#: camera with a long GOP that is otherwise fine.
DEFAULT_PROBE_TIMEOUT_S = 8.0

#: ``(outcome, (width, height) | None)`` — outcome is ``"ok"``, ``"refused"``
#: (the camera answered and said no) or ``"inconclusive"`` (no answer at all,
#: or no tool to ask with).
ProbeResult = tuple[str, tuple[int, int] | None]
Prober = Callable[[str, float], Awaitable[ProbeResult]]


@dataclass(frozen=True)
class DiscoveredSubstream:
    url: str
    source: str                   # "onvif" | "vendor"
    verified: bool                # a probe answered with a video stream
    width: int | None = None
    height: int | None = None

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.width and self.height else "unverified"


# ── the probe ────────────────────────────────────────────────────────

_REFUSAL_MARKERS = (
    "404", "401", "403", "not found", "unauthorized", "forbidden",
    "connection refused", "method describe failed", "no route to host",
)


def parse_ffprobe_outcome(returncode: int | None, stdout: str, stderr: str) -> ProbeResult:
    """Classify one ffprobe run: dimensions on success, ``refused`` when the
    camera answered and said no, ``inconclusive`` for everything else."""
    if returncode == 0:
        for line in stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                w, h = int(parts[0]), int(parts[1])
                if w > 0 and h > 0:
                    return "ok", (w, h)
        # It opened but reported no video dimensions (audio-only path, or a
        # build that prints nothing): it answered, so it is not refused —
        # but a sub with no video is no sub.
        return "refused", None
    text = (stderr or "").lower()
    if any(m in text for m in _REFUSAL_MARKERS):
        return "refused", None
    return "inconclusive", None


async def ffprobe_rtsp(url: str, timeout: float = DEFAULT_PROBE_TIMEOUT_S) -> ProbeResult:
    """Open ``url`` with ffprobe over RTSP/TCP and read the video size."""
    if shutil.which("ffprobe") is None:
        return "inconclusive", None
    argv = [
        "ffprobe", "-v", "error", "-rtsp_transport", "tcp",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0", url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except Exception:
        return "inconclusive", None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # pragma: no cover - defensive
            pass
        return "inconclusive", None
    return parse_ffprobe_outcome(proc.returncode, out.decode(errors="replace"),
                                 err.decode(errors="replace"))


# ── candidates ───────────────────────────────────────────────────────

async def _onvif_candidate(ip: str, username: str, password: str) -> str | None:
    """The camera's substream from its own ONVIF profiles, with the
    credentials injected; None when ONVIF does not answer or the device
    advertises one profile."""
    from services import onvif_digest_service as ods
    from services.camera_source_resolver import _pick_substream_uri, inject_credentials

    try:
        info = await ods.connect_and_get_profiles(ip, username, password)
    except Exception:
        return None
    with_uri = [p for p in (info or {}).get("profiles", []) if p.get("stream_uri")]
    if not with_uri:
        return None
    main_uri = html.unescape(with_uri[0]["stream_uri"])
    sub = _pick_substream_uri(with_uri, main_uri)
    return inject_credentials(sub, username, password) if sub else None


async def discover_substream(
    main_url: str | None,
    ip: str | None,
    username: str | None,
    password: str | None,
    *,
    prober: Prober | None = None,
    onvif: Callable[[str, str, str], Awaitable[str | None]] | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT_S,
) -> DiscoveredSubstream | None:
    """The substream to store for a camera added with ``main_url``, or None.

    ``prober`` and ``onvif`` are injectable for tests; the defaults are
    ffprobe and the device's ONVIF profiles.
    """
    if not main_url:
        return None
    from services.camera_source_resolver import derive_substream_url

    probe = prober or ffprobe_rtsp
    candidates: list[tuple[str, str]] = []
    if ip and username and password:
        try:
            url = await (onvif or _onvif_candidate)(ip, username, password)
        except Exception:
            url = None
        if url:
            candidates.append((url, "onvif"))
    vendor = derive_substream_url(main_url)
    if vendor:
        candidates.append((vendor, "vendor"))

    seen: set[str] = {main_url}
    for url, source in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            outcome, dims = await probe(url, timeout)
        except Exception:
            outcome, dims = "inconclusive", None
        if outcome == "ok":
            w, h = dims or (None, None)
            return DiscoveredSubstream(url, source, True, w, h)
        if outcome == "inconclusive" and source == "onvif":
            # The device said this is its sub; we just could not check it
            # right now. Stored, because the pipeline degrades to the main
            # stream with a warning if it turns out dead.
            return DiscoveredSubstream(url, source, False)
        logger.info("substream candidate (%s) for the camera at %s: %s — skipped",
                    source, ip or "?", outcome)
    return None
