# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""An alarm the state machine raised must reach somebody.

``tick`` advances every camera and RETURNS the alerts its transitions
produced. Four of its six call sites dropped that list — including
``state_snapshot``, which the dashboard polls about once a second. So an
entry delay expiring during a poll went BREACH -> ALARM inside
``/state``, the alerts were discarded, the camera was left in ALARM, and
the sweep a second later saw a state that had already moved and raised
nothing. The page said "in alarm". Nobody was told.

These pin the fix from both sides: every lossy path now dispatches, and
the two paths that already consumed the list must not start
double-firing.
"""
from __future__ import annotations

import datetime as _dt
import time as _time
from typing import Any

import pytest

import intrusion_detection as idet
from intrusion_detection import (
    ALARM,
    AppConfig,
    CameraWatch,
    IntrusionDetector,
)
from opennvr_app_sdk.geometry import Zone

BASE = _time.time()


def _ts(seconds: float) -> str:
    dt = _dt.datetime.fromtimestamp(BASE + seconds, _dt.timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _event(camera_id: str = "cam1", at: float = 0.0,
           track: str = "t1") -> dict[str, Any]:
    return {
        "correlation_id": "corr-1", "adapter": "yolov8", "adapter_version": "1",
        "camera_id": camera_id, "completed_at": _ts(at),
        "result": {"detections": [{
            "label": "person", "confidence": 0.9, "track_id": track,
            "bbox": {"x": 0.45, "y": 0.45, "w": 0.1, "h": 0.2},
        }]},
    }


def _camera(camera_id: str = "cam1") -> CameraWatch:
    zone = Zone.from_config("yard", [[480, 270], [1440, 270], [1440, 810], [480, 810]])
    return CameraWatch(camera_id=camera_id, zone=zone,
                       frame_width=1920, frame_height=1080)


def _config(**knobs) -> AppConfig:
    knobs.setdefault("arm_mode", "always")
    knobs.setdefault("min_presence_seconds", 0.0)
    knobs.setdefault("alarm_cooldown_seconds", 0.0)
    cam = _camera()
    return AppConfig(
        nats_url="nats://x:4222", nats_token=None,
        subject_pattern="opennvr.inference.>", watch_labels=["person"],
        cameras={cam.camera_id: cam}, webhook_url=None,
        attach_snapshot=False, **knobs,
    )


class _Recorder:
    """Stands in for the alert dispatcher and keeps what it was given."""

    def __init__(self) -> None:
        self.fired: list[Any] = []

    def fire(self, alert):  # noqa: ANN001
        self.fired.append(alert)
        return {}


class _Broken(_Recorder):
    def fire(self, alert):  # noqa: ANN001
        self.fired.append(alert)
        raise RuntimeError("webhook timed out")


@pytest.fixture
def clock(monkeypatch):
    now = [BASE]

    def _set(t: float) -> float:
        now[0] = t
        return t

    monkeypatch.setattr(idet.time, "time", lambda: now[0])
    return _set


def _armed_and_breached(dispatcher, clock, entry_delay: float = 30.0):
    """A camera armed, with an intruder inside and the entry delay
    running — one tick away from the alarm."""
    d = IntrusionDetector(_config(entry_delay_seconds=entry_delay), dispatcher)
    clock(BASE)
    d.tick(BASE)                                  # arms
    d.handle_event(_event(at=0))                  # breach starts
    dispatcher.fired.clear()
    return d


# ── the losing case ──────────────────────────────────────────────────


def test_an_alarm_that_ripens_during_a_dashboard_poll_is_delivered(clock):
    """The bug, exactly. /state is polled about once a second; the entry
    delay expires inside one of those polls."""
    rec = _Recorder()
    d = _armed_and_breached(rec, clock)

    clock(BASE + 31)
    snap = d.state_snapshot()

    assert snap["in_alarm"] == 1, "the page knows"
    assert rec.fired, "...and so does somebody else"
    assert d._cam_state("cam1").state == ALARM


def test_the_sweep_cannot_recover_it_afterwards(clock):
    """Why dropping it is not survivable: the transition has already
    happened, so the next tick raises nothing. If the poll does not
    deliver the alarm, nothing ever will."""
    rec = _Recorder()
    d = _armed_and_breached(rec, clock)

    clock(BASE + 31)
    d.state_snapshot()
    rec.fired.clear()

    clock(BASE + 32)
    assert d.tick() == [], "the state machine has moved on"


@pytest.mark.parametrize("action,params", [
    ("arm", {}),
    ("clear_override", {}),
])
def test_an_alarm_that_ripens_during_an_operator_action_is_delivered(
        clock, action, params):
    """Same shape, different door: these tick, and dropped what the tick
    raised."""
    rec = _Recorder()
    d = _armed_and_breached(rec, clock)

    clock(BASE + 31)
    d.on_action(action, params)

    assert rec.fired, f"{action} swallowed the alarm"


@pytest.mark.parametrize("action,params", [
    ("disarm", {}),
    ("bypass", {"camera": "cam1", "minutes": 0}),
])
def test_disarming_inside_the_entry_delay_still_stops_the_alarm(
        clock, action, params):
    """The other direction, and it must not be broken by the fix: the
    entry delay exists so somebody authorised can disarm before the
    alarm. These move the camera out of BREACH before the tick, so the
    tick raises nothing — no alarm to deliver, which is the point."""
    rec = _Recorder()
    d = _armed_and_breached(rec, clock)

    clock(BASE + 31)
    d.on_action(action, params)

    assert rec.fired == [], f"{action} let the alarm through anyway"
    assert d._cam_state("cam1").state != ALARM


# ── and the paths that already worked still work ─────────────────────


def test_the_sweep_loop_still_delivers_exactly_once(clock):
    rec = _Recorder()
    d = _armed_and_breached(rec, clock)

    clock(BASE + 31)
    d._tick_and_fire()

    assert len(rec.fired) == 1


def test_an_event_driven_alarm_goes_out_exactly_once(clock):
    """``on_event`` RETURNS its alerts and the SDK base dispatches them.
    Putting the fire inside ``tick`` itself — the obvious shortcut —
    would have sent every event-driven alarm twice, once from the tick
    and once from the return. Which is why the helper is a separate
    call site rather than a change to tick."""
    rec = _Recorder()
    d = IntrusionDetector(_config(entry_delay_seconds=0.0), rec)
    clock(BASE)
    d.tick(BASE)

    returned = d.handle_event(_event(at=0))

    assert len(returned) == 1
    assert len(rec.fired) == 1, "one alarm, one delivery"


# ── a status page must not break on a bad webhook ────────────────────


def test_a_dispatch_failure_does_not_break_the_status_page(clock):
    rec = _Broken()
    d = _armed_and_breached(rec, clock)

    clock(BASE + 31)
    snap = d.state_snapshot()            # must not raise

    assert snap["in_alarm"] == 1
    assert rec.fired, "it was attempted"


def test_one_bad_alert_does_not_lose_the_rest_of_the_batch(clock):
    """Two cameras alarming in the same tick: the first channel failing
    must not take the second alarm with it."""
    cams = [_camera("cam1"), _camera("cam2")]
    cfg = AppConfig(
        nats_url="nats://x:4222", nats_token=None,
        subject_pattern="opennvr.inference.>", watch_labels=["person"],
        cameras={c.camera_id: c for c in cams}, webhook_url=None,
        attach_snapshot=False, arm_mode="always", min_presence_seconds=0.0,
        alarm_cooldown_seconds=0.0, entry_delay_seconds=30.0,
    )
    rec = _Broken()
    d = IntrusionDetector(cfg, rec)
    clock(BASE)
    d.tick(BASE)
    d.handle_event(_event("cam1", at=0))
    d.handle_event(_event("cam2", at=0))
    rec.fired.clear()

    clock(BASE + 31)
    d.state_snapshot()

    assert len(rec.fired) == 2, "both were attempted"


# ── the guard against a seventh call site ────────────────────────────


def test_no_call_site_throws_the_alerts_away():
    """The failure this fixes is invisible — the state machine is right,
    the page is right, and only the alarm is missing. A new caller that
    ticks and drops the list reintroduces it silently, so the shape is
    pinned here rather than left to review.
    """
    import pathlib
    import re

    src = (pathlib.Path(idet.__file__).read_text()).splitlines()
    offenders = []
    for i, line in enumerate(src, 1):
        stripped = line.strip()
        if not re.match(r"self\.tick\(", stripped):
            continue
        # A bare `self.tick(...)` as a statement discards the alerts.
        offenders.append(f"line {i}: {stripped}")
    assert offenders == [], (
        "these tick and drop what it raised; call _tick_and_fire "
        f"instead: {offenders}")


def test_the_helper_dispatches_what_it_ticks():
    import inspect

    src = inspect.getsource(IntrusionDetector._tick_and_fire)
    assert "self._dispatcher.fire(alert)" in src
    assert "self.tick(now)" in src
