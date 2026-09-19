# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preconditions that fail loudly, before the behaviour under test.

OpenNVR's worst failures are silent. ``docs/FAKE_CAMERAS.md`` is largely a
catalogue of them: Tier-0 never leaves motion calibration, ``fast_plate_ocr``
quietly unregisters when core restarts, an app is scoped to a camera that no
longer exists. Every one surfaces the same way -- an empty page -- and every
one would surface in this suite the same way too: a wait that times out a
minute later with an empty list, pointing at the wrong thing entirely.

A guard converts that into a statement of fact at the moment it becomes true.
"the fast_plate_ocr adapter is not registered with KAI-C" is a diagnosis;
"timed out waiting for plate_text" is a shrug.

Guards raise ``PreconditionFailed``, which reads differently from an assertion
on purpose: the distinction between a stack that was never in a testable state
and behaviour that is wrong is most of triage. Where a tier is legitimately
*not configured* rather than broken -- the apps overlay simply is not running
-- use the non-raising checks instead and skip.

Each guard also records a note on the evidence context, so a failure bundle
shows what was verified before the thing that broke.
"""

from __future__ import annotations

import os
import re

import httpx

from .budgets import BUDGETS
from .waiting import eventually


class PreconditionFailed(AssertionError):
    """A precondition for the test was not met. Not a product assertion."""


def _note(ctx, message: str) -> None:
    if ctx is not None:
        ctx.guard_notes.append(message)


# ---------------------------------------------------------------------------
# KAI-C adapter registry
# ---------------------------------------------------------------------------
def _adapter_names(payload) -> set[str]:
    """Adapter names out of whichever shape KAI-C answers with."""
    if isinstance(payload, dict):
        items = payload.get("adapters") or payload.get("items") or []
        if not items and all(isinstance(v, dict) for v in payload.values()):
            return set(payload)  # {name: {...}}
    else:
        items = payload or []
    names = set()
    for item in items:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict) and item.get("name"):
            names.add(item["name"])
    return names


def _list_adapters(config) -> set[str] | None:
    """Registered adapter names, or None if KAI-C could not be asked."""
    try:
        resp = httpx.get(
            f"{config.kaic.rstrip('/')}/api/v1/adapters",
            headers={"X-Internal-Api-Key": config.internal_key},
            timeout=BUDGETS.REQUEST,
            verify=False,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    return _adapter_names(resp.json())


def adapter_registered(config, name: str) -> bool:
    """Whether an adapter is registered, without raising.

    For tiers that are legitimately unavailable rather than broken -- the apps
    overlay is simply not running, say -- where skipping is the honest outcome
    and a failure would be noise.
    """
    names = _list_adapters(config)
    return bool(names and name in names)


def require_adapter(config, name: str, ctx=None) -> None:
    """Fail unless ``name`` is registered with KAI-C right now.

    KAI-C's adapter registry is populated by a one-shot container at compose
    time and, for anything not persisted, is **lost on every core restart** --
    the process holds it in memory. A plate read against a missing adapter
    returns 404 from KAI-C and is swallowed as best-effort by the enrichment
    path, so the only visible symptom is that ``plate_text`` stays null
    forever. That is the single most expensive silent failure in the stack.
    """
    names = _list_adapters(config)
    if names is None:
        raise PreconditionFailed(
            f"Could not reach KAI-C at {config.kaic} to check for the "
            f"{name!r} adapter."
        )
    if name not in names:
        raise PreconditionFailed(
            f"The {name!r} adapter is not registered with KAI-C.\n"
            f"  registered: {sorted(names) or '(none)'}\n"
            "  KAI-C holds this registry in memory, so restarting "
            "opennvr-core silently empties it. Without the adapter, plate "
            "reads 404 and the enrichment path swallows it, so plate_text "
            "just stays null.\n"
            "  Re-run the registrar from the apps overlay, or bring the "
            "stack up again with `python tests/e2e/run.py --fresh`."
        )
    _note(ctx, f"adapter {name!r} is registered with KAI-C")


# ---------------------------------------------------------------------------
# Tier-0 motion calibration
# ---------------------------------------------------------------------------
_SKIPPED = re.compile(
    r'tier0_detector_skipped_total\{[^}]*reason="calibrating"[^}]*\}\s+([0-9.e+]+)'
)
_FRAMES = re.compile(r"^tier0_frames_total(?:\{[^}]*\})?\s+([0-9.e+]+)", re.MULTILINE)


def _metrics_text(url: str) -> str | None:
    try:
        resp = httpx.get(url, timeout=BUDGETS.REQUEST)
    except httpx.HTTPError:
        return None
    return resp.text if resp.status_code == 200 else None


def require_tier0_calibrated(config, ctx=None) -> None:
    """Fail unless Tier-0's motion gate has finished calibrating.

    Tier-0 will not run the detector until its motion detector has calibrated,
    and calibration needs a comparatively quiet frame -- under about 5% of the
    frame moving. Footage with motion from the first frame, or corrupted at a
    loop seam, never provides one. The detector then never runs, no track ever
    forms, and nothing downstream happens: no events, no visits, no plates,
    and no log line saying why.

    Reads the counters the pipeline already exposes rather than guessing.
    """
    url = f"{config.detect.rstrip('/')}/metrics"

    def sample() -> tuple[float, float] | None:
        text = _metrics_text(url)
        if text is None:
            return None
        frames = _FRAMES.search(text)
        if not frames:
            return None
        skipped = _SKIPPED.search(text)
        return (float(skipped.group(1)) if skipped else 0.0, float(frames.group(1)))

    try:
        result = eventually(
            sample,
            # Calibrated means frames have been processed and not all of them
            # were thrown away for calibrating.
            until=lambda pair: pair is not None and pair[1] > 0 and pair[0] < pair[1],
            budget=BUDGETS.TIER0_CALIBRATED,
            describe="Tier-0's motion gate to finish calibrating",
        )
    except AssertionError as exc:
        raise _tier0_diagnosis(url, exc) from exc

    skipped, frames = result
    _note(ctx, f"Tier-0 calibrated ({frames:.0f} frames, {skipped:.0f} skipped)")


def _tier0_diagnosis(url: str, exc: BaseException) -> PreconditionFailed:
    """Two very different failures land here and need different answers.

    ``tier0_frames_total`` does not exist at all until some worker has decoded
    a frame, so its *absence* means nothing is being processed -- which is not
    the same as a gate stuck calibrating, and has a completely different fix.
    """
    text = _metrics_text(url) or ""
    if "tier0_frames_total" not in text:
        return PreconditionFailed(
            "Tier-0 is not processing frames from any camera, so the motion "
            "gate has not even begun.\n"
            "  tier0_frames_total is absent from /metrics entirely, which "
            "means no worker ever decoded a frame: workers start and then "
            "fail to read their source.\n"
            "  Look for probe failures:\n"
            "      docker logs opennvr_e2e_detect_pipeline | grep -i ffprobe\n"
            "  Usual causes: the camera's MediaMTX path is not publishing "
            "yet; a stale camera row whose path was torn down long ago, which "
            "the pipeline retries forever (run with --fresh); or ffprobe "
            "timing out because the host is loaded.\n\n"
            f"{exc}"
        )
    return PreconditionFailed(
        "Tier-0's motion gate never finished calibrating, so the detector "
        "never ran and no event can exist.\n"
        "  It needs a comparatively quiet frame (under ~5% of the frame "
        "moving). Continuous motion, or corruption at a clip's loop seam, "
        "never provides one.\n"
        "  FAKECAM_MODE=transcode rebuilds clean keyframes at the seam and is "
        "what the suite sets by default; generated clips also open with "
        "several still seconds for exactly this reason.\n\n"
        f"{exc}"
    )


# ---------------------------------------------------------------------------
# MediaMTX path readiness
# ---------------------------------------------------------------------------
def require_stream_receiving(client, camera_id: int, ctx=None) -> dict:
    """Fail unless bytes are actually arriving for this camera.

    ``path_configured`` only says MediaMTX accepted the path definition.
    ``path_active`` says data is flowing, which is what recording and
    detection actually need. Asserting the wrong one produces a test that
    passes against a camera receiving nothing.

    Note the budget. This step is fast on an idle host and the first thing to
    slow down on a busy one -- with several cameras recording at once it has
    been seen to take minutes, and ``mediamtx-status`` intermittently answers
    500 while that is happening (tolerated as "not yet", since a retry
    succeeds). Treating that as a product failure would just make the suite
    flaky on exactly the machines people run it on.
    """
    from . import routes

    try:
        status = eventually(
            lambda: client.json(routes.CAMERA_MEDIAMTX_STATUS(camera_id)),
            until=lambda payload: payload.get("path_active") is True,
            budget=BUDGETS.STREAM_RECEIVING,
            describe=f"camera {camera_id} to start receiving video",
        )
    except AssertionError as exc:
        raise PreconditionFailed(
            f"Camera {camera_id} is provisioned but no video is arriving.\n"
            "  Usually the source is not publishing. Check the rig:\n"
            "      docker logs opennvr_e2e_fakecams\n"
            "  A path can be configured and still receive nothing, which is "
            "why this checks path_active rather than path_configured.\n\n"
            f"{exc}"
        ) from exc
    _note(ctx, f"camera {camera_id} is receiving video")
    return status


# ---------------------------------------------------------------------------
# Recorded footage
# ---------------------------------------------------------------------------
def require_recorded_footage(client, camera_id: int, ctx=None) -> dict:
    """Wait until at least one segment has finished recording, and return it.

    A file on disk is not yet recorded history: MediaMTX has to close the
    segment before anything can seek inside it, and a camera deleted a few
    seconds after it starts publishing never gets that far. So this waits for
    a run whose duration has passed one full segment length.

    It would be better to assert on the *database index*, which is what proves
    the segment-complete webhook landed, but there is currently no way to.
    Every recordings endpoint falls back to MediaMTX when the index is empty
    except ``/recordings/frame``, and that one is broken in the shipped image
    (no ffmpeg in opennvr-core; see
    ``test_a_frame_can_be_extracted_from_past_footage``). Once that is fixed,
    this guard should go back to probing the frame endpoint, which is a
    strictly stronger check.
    """
    from . import routes

    segment_seconds = float(os.environ.get("RECORDING_SEGMENT_SECONDS", "60"))

    def probe() -> dict | None:
        day = client.json(routes.RECORDINGS_SEGMENTS(camera_id))
        for segment in day.get("segments") or []:
            if float(segment.get("duration") or 0) >= segment_seconds:
                return segment
        return None

    try:
        segment = eventually(
            probe,
            budget=BUDGETS.SEGMENT_RECORDED,
            describe=(
                f"camera {camera_id} to record a full {segment_seconds:.0f}s segment"
            ),
        )
    except AssertionError as exc:
        raise PreconditionFailed(
            f"Camera {camera_id} never recorded a complete segment.\n"
            "  Check that footage is reaching disk at all:\n"
            "      docker logs opennvr_e2e_mediamtx | grep -i record\n"
            "  A camera deleted moments after it starts publishing also never "
            "produces a completed segment.\n\n"
            f"{exc}"
        ) from exc

    _note(ctx, f"camera {camera_id} has {segment['duration']:.0f}s of footage")
    return segment


__all__ = [
    "PreconditionFailed",
    "adapter_registered",
    "require_adapter",
    "require_tier0_calibrated",
    "require_stream_receiving",
    "require_recorded_footage",
]
