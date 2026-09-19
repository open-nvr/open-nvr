# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every timeout in the suite, named once.

A test never writes a bare number. It names a budget, and the budget is tuned
here — or overridden per-environment without touching a single test:

    E2E_BUDGET_VISIT_APPEARS=300 pytest -m detection

That indirection is the whole point. A 2-core CI runner, a laptop under load
and a workstation want wildly different numbers for the *same* assertions, and
the alternative to this file is grepping forty test files for ``timeout=90``.

Budgets are wall-clock seconds and deliberately generous: an E2E budget is a
*failure* threshold, not a performance assertion. If you want to assert that
something is fast, assert on a measured duration explicitly — don't encode it
as a tight timeout, which only buys you a flaky suite.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_PREFIX = "E2E_BUDGET_"


def _seconds(name: str, default: float) -> float:
    """Read one budget from the environment, falling back to ``default``."""
    raw = os.environ.get(f"{_PREFIX}{name}")
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{_PREFIX}{name}={raw!r} is not a number. Budgets are wall-clock "
            f"seconds, e.g. {_PREFIX}{name}=120"
        ) from exc
    if value <= 0:
        raise ValueError(f"{_PREFIX}{name} must be positive, got {value}")
    return value


@dataclass(frozen=True)
class Budgets:
    """Named waits. Attribute name == env suffix, so the error messages and the
    override knob are always the same string."""

    # --- stack lifecycle -------------------------------------------------
    # Cold start: image pull + alembic against an empty DB + create_initial_data
    # + uvicorn bind. publish-images.yml budgets 180s for core alone on a cold
    # GitHub runner; the full stack adds mediamtx, nats and the adapter.
    STACK_READY: float = _seconds("STACK_READY", 420.0)
    # detect-pipeline reports healthy only once frames are actually flowing,
    # so it trails core by however long the first RTSP connect takes.
    PIPELINE_READY: float = _seconds("PIPELINE_READY", 180.0)

    # --- cameras and media ----------------------------------------------
    # Camera created -> MediaMTX accepts the path definition.
    STREAM_READY: float = _seconds("STREAM_READY", 90.0)
    # Path defined -> bytes actually arriving. Deliberately far longer than
    # STREAM_READY: this waits on ffmpeg connecting and decoding, and it is
    # the step that suffers first when the host is busy. Measured at ~5s idle
    # and seen to exceed 90s with several cameras recording at once on a
    # thermally throttled laptop, so the budget is set for the bad case.
    STREAM_RECEIVING: float = _seconds("STREAM_RECEIVING", 210.0)
    # MediaMTX writes 60s segments; a segment-complete webhook cannot land
    # sooner than one segment boundary, so this must clear 60s comfortably.
    SEGMENT_RECORDED: float = _seconds("SEGMENT_RECORDED", 180.0)
    HLS_SESSION_READY: float = _seconds("HLS_SESSION_READY", 60.0)

    # --- detection -------------------------------------------------------
    # Tier-0's motion gate must finish calibrating before it ever detects.
    # Two paths get there and the slow one sets this budget: on footage with
    # continuous motion the gate never settles and instead force-opens after
    # 150 frames. At Tier-0's default 2 fps that is ~75s on an idle host, but
    # the detector is the first thing starved when the host is busy -- frame
    # latency of 13s against a 0.5s budget has been observed with another
    # stack running alongside, which turns 150 frames into several minutes.
    TIER0_CALIBRATED: float = _seconds("TIER0_CALIBRATED", 420.0)
    # A visit row is only POSTed when a track *ends*, so this waits out the
    # whole track lifetime, not just the first detection -- and the track can
    # only start once the gate above has opened.
    VISIT_APPEARS: float = _seconds("VISIT_APPEARS", 420.0)

    # --- plates ----------------------------------------------------------
    # OCR is a FastAPI BackgroundTask behind a Semaphore(2), then a NATS
    # round-trip through plate_event_consumer before the row is updated.
    PLATE_ENRICHED: float = _seconds("PLATE_ENRICHED", 180.0)

    # --- apps ------------------------------------------------------------
    APP_REACHABLE: float = _seconds("APP_REACHABLE", 120.0)
    APP_INSTALLED: float = _seconds("APP_INSTALLED", 300.0)
    ALERT_DELIVERED: float = _seconds("ALERT_DELIVERED", 90.0)

    # --- generic ---------------------------------------------------------
    # For "this should already be true, I am just avoiding a race" waits.
    QUICK: float = _seconds("QUICK", 20.0)
    # Single HTTP request timeout. Not a poll budget.
    REQUEST: float = _seconds("REQUEST", 30.0)


BUDGETS = Budgets()

__all__ = ["BUDGETS", "Budgets"]
