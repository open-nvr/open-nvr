# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Function-based health for the Tier-0 service.

"Healthy" means the service is DOING ITS JOB, not merely that the process is
up. Every silent-blindness incident on record was a zombie — alive, "healthy",
doing nothing (model missing, bus unauthenticated, no frames) — so the health
answer is computed from function:

* the configured detector actually loaded (requested onnx but degraded to the
  stub = unhealthy: the container would burn decode CPU and detect nothing);
* frames are flowing (when enabled and at least one worker is running, the
  newest processed frame must be younger than ``frame_stale_s``);
* the event bus is connected (when configured), after a startup grace.

The compose healthcheck polls ``/health`` (served by metrics.serve_metrics),
so an unhealthy state is visible in ``docker ps`` and actionable by an
autoheal watcher instead of being a log line nobody reads.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class HealthState:
    """Mutable inputs, wired once at startup; cheap to evaluate per probe."""

    enabled: bool = True
    detector_requested: str = "stub"
    detector_actual: str = "stub"
    nats_configured: bool = False
    nats_connected: Callable[[], bool] = lambda: False
    workers_running: Callable[[], int] = lambda: 0
    newest_frame_age_s: Callable[[], float | None] = lambda: None
    # Cameras whose own last frame is older than frame_stale_s. Reported by
    # NAME because newest_frame_age_s is a fleet MAX: one healthy camera
    # holds it near zero while every other feed is dead. Listing them does
    # not by itself flip the service unhealthy — a camera being offline is
    # normal operations, and alerting policy belongs on the metric.
    stale_cameras: Callable[[float], list[str]] = lambda _threshold: []
    started_at: float = field(default_factory=time.time)
    frame_stale_s: float = 60.0
    startup_grace_s: float = 90.0


def evaluate(st: HealthState, now: float | None = None) -> tuple[bool, dict]:
    """Return (healthy, detail). Pure given the state's callables."""
    now = time.time() if now is None else now
    in_grace = (now - st.started_at) < st.startup_grace_s
    problems: list[str] = []

    if not st.enabled:
        return True, {"status": "ok", "note": "service disabled by config (idle)"}

    degraded = (
        st.detector_requested not in ("stub", "")
        and st.detector_actual == "StubDetector"
    )
    if degraded:
        problems.append(
            f"detector '{st.detector_requested}' degraded to stub "
            "(model missing or backend unavailable) — detecting nothing"
        )

    if st.nats_configured and not st.nats_connected() and not in_grace:
        problems.append("event bus configured but not connected — events are dropped")

    workers = st.workers_running()
    age = st.newest_frame_age_s()
    if workers > 0 and not in_grace:
        if age is None or age > st.frame_stale_s:
            problems.append(
                f"{workers} worker(s) running but newest frame is "
                f"{'absent' if age is None else f'{age:.0f}s old'} "
                f"(stale > {st.frame_stale_s:.0f}s)"
            )

    stalled: list[str] = []
    if workers > 0 and not in_grace:
        try:
            stalled = list(st.stale_cameras(st.frame_stale_s))
        except Exception:  # pragma: no cover - never let a probe raise
            stalled = []

    detail = {
        "status": "ok" if not problems else "unhealthy",
        "workers": workers,
        "newest_frame_age_s": None if age is None else round(age, 1),
        # Named so a single dead feed in a healthy fleet is actionable
        # instead of invisible behind the fleet-wide max.
        "stale_cameras": stalled,
        "detector": {"requested": st.detector_requested, "actual": st.detector_actual},
        "nats": ("connected" if st.nats_connected() else "disconnected")
        if st.nats_configured else "unconfigured",
        "in_startup_grace": in_grace,
        "problems": problems,
    }
    return not problems, detail
