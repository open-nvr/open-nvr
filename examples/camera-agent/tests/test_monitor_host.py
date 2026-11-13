# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""MonitorHost — create_monitor's convergence onto the App SDK
(app-sdk-spec §07 "one rule library, two front doors").

Covers: count/crossing watches instantiating the example apps' own SDK
Detector classes in-process, count/tally parity with the legacy loops,
programmatic param validation with LLM-relayable errors, the alert
bridge into the agent's notify machinery, per-monitor ContextVar alert
identity, the NATS front door (feed_event), and clean lifecycle
(create → fire → stop → no more alerts, no leaked tasks)."""
from __future__ import annotations

import asyncio

import pytest

import monitor_host as mh
from camera_agent import AppConfig, CameraAgentRuntime
from context import CameraSpec
from monitor_host import MonitorHost
from opennvr_app_sdk.alerts import get_default_source


# ── Helpers ────────────────────────────────────────────────────────────


def _runtime(*, detections=None, track_frames=None):
    """Runtime with fakes matching the legacy monitor tests' pattern:
    ``detections`` for count monitors, ``track_frames`` (one 'tracks'
    list per poll) for crossing monitors."""
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        cameras=[
            CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front"),
            CameraSpec(camera_id="cam2", frame_url="http://x/2.jpg", role="gate"),
        ],
    )
    rt = CameraAgentRuntime(cfg)
    state = {"i": 0}

    async def fake_get_frame(cam, **_kw):
        return b"\xff\xd8\xff"

    async def fake_infer(*, frame_jpeg, extra=None, **kw):
        if track_frames is not None:
            i = state["i"]; state["i"] += 1
            frame = track_frames[i] if i < len(track_frames) else []
            return {"result": {"tracks": frame}}
        return {"result": {"detections": detections if detections is not None else []}}

    rt.context.get_frame = fake_get_frame
    rt.detection_client.infer = fake_infer
    return rt


def _standalone_host(*, detections=None, track_frames=None, notify=None):
    """A MonitorHost with inline fakes, independent of the runtime."""
    state = {"i": 0}

    async def get_frame(cam):
        return b"\xff\xd8\xff"

    async def infer(*, frame_jpeg, extra=None, **kw):
        if track_frames is not None:
            i = state["i"]; state["i"] += 1
            frame = track_frames[i] if i < len(track_frames) else []
            return {"result": {"tracks": frame}}
        return {"result": {"detections": detections if detections is not None else []}}

    return MonitorHost(get_frame=get_frame, infer=infer, notify=notify)


async def _wait_for(predicate, *, timeout=1.5, step=0.02):
    for _ in range(int(timeout / step)):
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


def _sdk_monitor_tasks():
    return [t for t in asyncio.all_tasks()
            if t.get_name().startswith("sdk-monitor-") and not t.done()]


# ── Convergence: the tool's kinds map onto the SDK rule classes ────────


async def test_count_monitor_is_the_occupancy_example_detector():
    rt = _runtime(detections=[{"label": "person"}, {"label": "person"}])
    mon = rt.monitors.create(kind="count", camera_ids=["cam1"], target="person",
                             interval_s=0.02)
    try:
        hosted = rt.monitors.host.get(mon.id)
        assert hosted is not None and hosted.rule == "occupancy"
        # The canonical example class, imported from the examples package —
        # not a re-implementation.
        occupancy = mh._load_rule_module("occupancy")
        assert isinstance(hosted.detector, occupancy.OccupancyCounter)
        # Legacy count semantics: live + peak counts per camera.
        assert await _wait_for(lambda: rt.monitors.list()[0]["current"].get("cam1") == 2)
        assert rt.monitors.list()[0]["peak"]["cam1"] == 2
        # Counting-only watches never alert (no threshold configured).
        assert rt.monitors.notifications() == []
    finally:
        rt.monitors.stop(mon.id)


async def test_crossing_monitor_is_the_line_crossing_example_detector():
    # track 1 crosses left→right (legacy "out"); track 2 right→left ("in").
    frames = [
        [{"track_id": 1, "label": "person", "center": [0.2, 0.5]},
         {"track_id": 2, "label": "person", "center": [0.8, 0.6]}],
        [{"track_id": 1, "label": "person", "center": [0.8, 0.5]},
         {"track_id": 2, "label": "person", "center": [0.2, 0.6]}],
    ]
    rt = _runtime(track_frames=frames)
    mon = rt.monitors.create(kind="crossing", camera_ids=["cam1"], target="person",
                             interval_s=0.02, line=[0.5, 0.0, 0.5, 1.0])
    try:
        hosted = rt.monitors.host.get(mon.id)
        assert hosted is not None and hosted.rule == "line_crossing"
        line_crossing = mh._load_rule_module("line_crossing")
        assert isinstance(hosted.detector, line_crossing.LineCrossingDetector)
        assert await _wait_for(lambda: hosted.tallies["cam1"]["in"] == 1)
        assert hosted.tallies["cam1"] == {"in": 1, "out": 1}
        # Legacy LineCounter semantics: current = net, peak = max "in".
        m = rt.monitors.list()[0]
        assert m["current"]["cam1"] == 0
        assert m["peak"]["cam1"] == 1
        # One SDK alert per crossing was fired (into the bridge, silently).
        assert hosted.alerts_fired == 2
        assert rt.monitors.notifications() == []
    finally:
        rt.monitors.stop(mon.id)


async def test_notify_kind_stays_on_the_legacy_loop():
    rt = _runtime(detections=[{"label": "person"}])
    mon = rt.monitors.create(kind="notify", camera_ids=["cam1"], target="person",
                             interval_s=0.02)
    try:
        assert rt.monitors.host.list() == []           # not converged
        assert mon.id in rt.monitors._tasks            # legacy loop task
    finally:
        rt.monitors.stop(mon.id)


# ── Param validation (LLM-relayable rejections, no orphans) ────────────


async def test_degenerate_line_rejected_via_tool_with_clear_message():
    rt = _runtime()
    before = len(_sdk_monitor_tasks())
    msg = await rt._handle_create_monitor({
        "kind": "crossing", "target": "person", "camera_id": "cam1",
        "line": [0.5, 0.5, 0.5, 0.5],   # A == B
    })
    assert msg.startswith("ERROR:") and "differ" in msg
    # Rejected cleanly: nothing registered anywhere, no task spawned.
    assert rt.monitors.list() == []
    assert rt.monitors.host.list() == []
    assert len(_sdk_monitor_tasks()) == before


async def test_host_param_validation_messages():
    host = _standalone_host()
    with pytest.raises(ValueError, match="unknown rule"):
        host.create("nope", ["cam1"], {"target": "person"})
    with pytest.raises(ValueError, match="camera_id"):
        host.create("occupancy", [], {"target": "person"})
    with pytest.raises(ValueError, match="target"):
        host.create("occupancy", ["cam1"], {})
    with pytest.raises(ValueError, match="interval_s"):
        host.create("occupancy", ["cam1"], {"target": "person", "interval_s": 0})
    with pytest.raises(ValueError, match="max_count"):
        host.create("occupancy", ["cam1"], {"target": "person", "max_count": "many"})
    with pytest.raises(ValueError, match="min_count.*<=.*max_count"):
        host.create("occupancy", ["cam1"],
                    {"target": "person", "max_count": 1, "min_count": 3})
    with pytest.raises(ValueError, match="'line' is required"):
        host.create("line_crossing", ["cam1"], {"target": "person"})
    with pytest.raises(ValueError, match="count_direction"):
        host.create("line_crossing", ["cam1"],
                    {"target": "person", "line": [0, 0, 1, 1], "direction": "sideways"})
    with pytest.raises(ValueError, match="track_ttl_seconds"):
        host.create("line_crossing", ["cam1"],
                    {"target": "person", "line": [0, 0, 1, 1], "track_ttl_seconds": -1})
    assert host.list() == []


# ── Alert bridge into the agent's notify machinery ─────────────────────


async def test_occupancy_threshold_alert_routes_to_agent_notifications():
    rt = _runtime(detections=[{"label": "person", "bbox": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2}},
                              {"label": "person", "bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}}])
    # The threshold front door ("alert if more than 1 person"), driven
    # straight at the host; create_monitor's max_count/min_count reach the
    # same params (tests/test_monitor_thresholds.py).
    mid = rt.monitors.host.create(
        "occupancy", ["cam1"],
        {"target": "person", "max_count": 1, "interval_s": 0.02},
    )
    try:
        assert await _wait_for(lambda: rt.monitors.notifications())
        note = rt.monitors.notifications()[0]
        assert note["monitor_id"] == mid
        assert "Over-occupancy" in note["text"] and "cam1" in note["text"]
        # Edge-triggered: a persistently crowded frame fires ONCE.
        count = len(rt.monitors.notifications())
        await asyncio.sleep(0.15)
        assert len(rt.monitors.notifications()) == count
    finally:
        rt.monitors.host.stop(mid)


# ── ContextVar-scoped per-monitor identity ─────────────────────────────


async def test_each_monitor_alerts_with_its_own_source_identity():
    fired = []
    host = _standalone_host(notify=lambda mid, alert: fired.append((mid, alert)))
    ambient = get_default_source()
    m1 = host.create("occupancy", ["cam1"],
                     {"target": "person", "max_count": 0, "interval_s": 60})
    m2 = host.create("occupancy", ["cam1"],
                     {"target": "person", "max_count": 0, "interval_s": 60})
    try:
        event = {"camera_id": "cam1",
                 "result": {"detections": [
                     {"label": "person", "bbox": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2}}]}}
        alerts = host.feed_event(event)
        assert len(alerts) == 2 and len(fired) == 2
        sources = {a.source.name for _, a in fired}
        assert sources == {
            f"occupancy-counting.monitor-{m1}",
            f"occupancy-counting.monitor-{m2}",
        }
        # The scoped identity was reset after each handler call — the
        # process-wide ambient default is untouched.
        assert get_default_source() == ambient
    finally:
        host.stop_all()


# ── NATS front door ────────────────────────────────────────────────────


async def test_feed_event_drives_hosted_detectors_and_counts():
    counts = []
    host = _standalone_host()
    mid = host.create("occupancy", ["cam1"], {"target": "person", "interval_s": 60},
                      counts_sink=lambda cam, cur, peak: counts.append((cam, cur, peak)))
    try:
        dets = [{"label": "person", "bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}},
                {"label": "person", "bbox": {"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2}},
                {"label": "car", "bbox": {"x": 0.7, "y": 0.7, "w": 0.2, "h": 0.2}}]
        host.feed_event({"camera_id": "cam1", "result": {"detections": dets}})
        assert counts and counts[-1] == ("cam1", 2, 2)   # cars not counted
        # Events for cameras this monitor doesn't watch are dropped by the
        # detector itself (SDK behavior) — no state, no counts.
        n = len(counts)
        host.feed_event({"camera_id": "cam9", "result": {"detections": dets}})
        assert len(counts) == n
        snap = host.snapshot(mid)[str(mid)]
        assert snap["detector_state"]["cameras"]["cam1"]["last_count"] == 2
    finally:
        host.stop_all()


# ── Lifecycle: create → fire → stop → no more alerts, no leaks ─────────


async def test_create_fire_stop_then_silence_and_no_leaked_tasks():
    # A track that ping-pongs across the line fires one alert per poll —
    # a continuously-firing monitor, the sharpest stop test.
    frames = [[{"track_id": 1, "label": "person", "center": [0.2 if i % 2 == 0 else 0.8, 0.5]}]
              for i in range(1000)]
    fired = []
    host = _standalone_host(track_frames=frames,
                            notify=lambda mid, alert: fired.append(alert))
    baseline = len(_sdk_monitor_tasks())
    mid = host.create(
        "line_crossing", ["cam1"],
        {"target": "person", "line": [0.5, 0.0, 0.5, 1.0],
         "interval_s": 0.02, "notify_on_alert": True},
    )
    hosted = host.get(mid)
    assert len(_sdk_monitor_tasks()) == baseline + 1

    # fire: at least one crossing alert reaches the bridge + notify path.
    assert await _wait_for(lambda: len(fired) >= 1)

    # stop: task cancelled, monitor forgotten, and NOTHING fires afterwards.
    assert host.stop(mid) is True
    await asyncio.sleep(0)          # let the cancellation propagate
    seen = len(fired)
    await asyncio.sleep(0.15)       # several would-be poll intervals
    assert len(fired) == seen
    assert hosted.alerts_fired == seen
    assert host.list() == [] and host.get(mid) is None
    assert len(_sdk_monitor_tasks()) == baseline
    assert host.stop(mid) is False  # idempotent


async def test_stop_mid_poll_drops_the_inflight_alert():
    """Regression: cancellation only lands at the poll task's next await,
    so a stop() that arrives while the poll is awaiting inference lets the
    resumed task run one more sync handle_event — its alert must be
    dropped, not routed to notify for a deleted monitor."""
    fired = []
    hooks: dict = {}
    over = [{"label": "person", "bbox": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2}}]

    async def get_frame(cam):
        return b"\xff\xd8\xff"

    async def infer(*, frame_jpeg, extra=None, **kw):
        # stop() lands while this poll is in flight; the task resumes
        # and runs the handler over a frame that WOULD fire OVER.
        hooks["stop"]()
        return {"result": {"detections": list(over)}}

    host = MonitorHost(get_frame=get_frame, infer=infer,
                       notify=lambda mid, alert: fired.append((mid, alert)))
    mid = host.create("occupancy", ["cam1"],
                      {"target": "person", "max_count": 0, "interval_s": 0.01,
                       "notify_on_alert": True})
    hosted = host.get(mid)
    hooks["stop"] = lambda: host.stop(mid)
    assert await _wait_for(lambda: host.get(mid) is None)  # stop() ran
    await asyncio.sleep(0.05)   # the resumed handler runs, then settles
    assert fired == [] and hosted.alerts_fired == 0
    assert _sdk_monitor_tasks() == []
    # The bridge itself is the guard: even a direct straggler handler
    # call after stop() must not append a notification.
    hosted.detector.handle_event({"camera_id": "cam1",
                                  "result": {"detections": list(over)}})
    assert fired == [] and hosted.alerts_fired == 0
    assert list(hosted.recent_alerts) == []


async def test_missing_rule_library_is_a_relayable_tool_error(monkeypatch):
    """Regression: when the sibling example module isn't shipped,
    _load_rule_module raises RuntimeError — the create_monitor tool must
    relay it as an ERROR: string, not crash the conversation."""
    rt = _runtime()

    def gone(rule):
        raise RuntimeError(f"rule library module for {rule!r} not found")

    monkeypatch.setattr(mh, "_load_rule_module", gone)
    msg = await rt._handle_create_monitor(
        {"kind": "count", "target": "person", "camera_id": "cam1"})
    assert msg.startswith("ERROR:") and "'occupancy' rule library" in msg
    msg = await rt._handle_create_monitor(
        {"kind": "crossing", "target": "person", "camera_id": "cam1",
         "line": [0.5, 0.0, 0.5, 1.0]})
    assert msg.startswith("ERROR:") and "'line_crossing' rule library" in msg
    # Rejected cleanly: nothing registered anywhere, no task spawned.
    assert rt.monitors.list() == [] and rt.monitors.host.list() == []
    assert _sdk_monitor_tasks() == []


async def test_poll_task_death_surfaces_as_error_status():
    """Regression: a poll task killed by an unexpected exception must not
    leave a silent zombie — the monitor stays listed but flips to an
    'error: …' status in list()/snapshot() and the manager's /monitors
    payload."""
    # Poisoned frame source: the track's center can't be parsed, which
    # blows up outside _poll's guarded frame/handler sections.
    frames = [[{"track_id": 1, "label": "person", "center": ["poison", 0.5]}]]
    rt = _runtime(track_frames=frames)
    mon = rt.monitors.create(kind="crossing", camera_ids=["cam1"],
                             target="person", interval_s=0.01,
                             line=[0.5, 0.0, 0.5, 1.0])
    try:
        hosted = rt.monitors.host.get(mon.id)
        assert await _wait_for(lambda: hosted.task.done())
        # Host view: still listed, but no longer claiming to be active.
        listed = rt.monitors.host.list()[0]
        assert listed["active"] is False
        assert listed["status"].startswith("error:") and "poison" in listed["status"]
        snap = rt.monitors.host.snapshot(mon.id)[str(mon.id)]
        assert snap["status"].startswith("error:")
        # Manager view (what GET /monitors returns): same truth.
        d = rt.monitors.list()[0]
        assert d["active"] is False and d["status"].startswith("error:")
    finally:
        rt.monitors.stop(mon.id)


async def test_tool_created_monitor_stops_cleanly_via_stop_monitor():
    rt = _runtime(detections=[{"label": "person"}])
    baseline = len(_sdk_monitor_tasks())
    msg = await rt._handle_create_monitor(
        {"kind": "count", "target": "person", "camera_id": "cam1"})
    assert "watch #" in msg
    mid = rt.monitors.list()[0]["id"]
    assert len(_sdk_monitor_tasks()) == baseline + 1
    stop_msg = await rt._handle_stop_monitor({"monitor_id": mid})
    assert f"#{mid}" in stop_msg
    await asyncio.sleep(0)
    assert rt.monitors.host.list() == []
    assert len(_sdk_monitor_tasks()) == baseline


def test_rules_dir_env_override(tmp_path, monkeypatch):
    """The container flattens agent modules under /app and copies the rule
    libraries to a separate dir, so OPENNVR_RULES_DIR must override the
    dev-tree parent.parent default. Regression guard for the packaging bug
    where monitor_host / the rule modules weren't shipped in the image."""
    import importlib
    import shutil
    import sys
    from pathlib import Path

    here = Path(__file__).resolve().parent.parent
    rules = tmp_path / "rules"
    (rules / "occupancy-counting").mkdir(parents=True)
    (rules / "line-crossing").mkdir(parents=True)
    shutil.copy(here.parent / "occupancy-counting" / "occupancy_counting.py",
                rules / "occupancy-counting" / "occupancy_counting.py")
    shutil.copy(here.parent / "line-crossing" / "line_crossing.py",
                rules / "line-crossing" / "line_crossing.py")
    monkeypatch.setenv("OPENNVR_RULES_DIR", str(rules))
    for m in ("monitor_host", "occupancy_counting", "line_crossing"):
        sys.modules.pop(m, None)
    try:
        mh = importlib.import_module("monitor_host")
        assert mh._EXAMPLES_DIR == rules.resolve()
        assert mh._load_rule_module("occupancy").OccupancyCounter is not None
        assert mh._load_rule_module("line_crossing").LineCrossingDetector is not None
    finally:
        for m in ("monitor_host", "occupancy_counting", "line_crossing"):
            sys.modules.pop(m, None)


async def test_nonfinite_params_rejected_loudly():
    """Regression (review F7): json.loads accepts NaN/Infinity. A NaN line
    endpoint passes Tripwire's degenerate-line check but every crossing()
    comparison is False — the watch would be created "successfully" and
    silently never fire. Non-finite numerics must be refused up front."""
    host = _standalone_host()
    nan, inf = float("nan"), float("inf")
    with pytest.raises(ValueError, match="finite"):
        host.create("line_crossing", ["cam1"],
                    {"target": "person", "line": [0, 0, nan, 1]})
    with pytest.raises(ValueError, match="finite"):
        host.create("line_crossing", ["cam1"],
                    {"target": "person", "line": [0, 0, 1, inf]})
    with pytest.raises(ValueError, match="interval_s"):
        host.create("occupancy", ["cam1"],
                    {"target": "person", "interval_s": inf})
    with pytest.raises(ValueError, match="track_ttl_seconds"):
        host.create("line_crossing", ["cam1"],
                    {"target": "person", "line": [0, 0, 1, 1],
                     "track_ttl_seconds": nan})
    assert host.list() == []
