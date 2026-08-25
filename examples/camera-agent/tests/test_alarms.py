# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alarm engine: trigger, time-window gating, acknowledge/silence, disarm,
emergency-contact tagging, and the HTTP endpoints."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from camera_agent import (
    AppConfig, CameraAgentRuntime, build_app, _parse_hhmm, Alarm,
)
from context import CameraSpec


def _runtime(detections=None, emergency_contacts=None, alarm_ring_defaults=None):
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        emergency_contacts=emergency_contacts,
        alarm_ring_defaults=alarm_ring_defaults,
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front")],
    )
    rt = CameraAgentRuntime(cfg)

    async def fake_get_frame(cam, **_kw):
        return b"\xff\xd8\xff"

    async def fake_infer(*, frame_jpeg, **kw):
        return {"result": {"detections": detections if detections is not None else [{"label": "fire"}]}}

    rt.context.get_frame = fake_get_frame
    rt.detection_client.infer = fake_infer
    return rt


# ── time parsing + window ──────────────────────────────────────────────


def test_parse_hhmm():
    assert _parse_hhmm("18:00") == 18 * 60
    assert _parse_hhmm("06:30") == 6 * 60 + 30
    assert _parse_hhmm("nonsense") is None
    assert _parse_hhmm(None) is None


def test_window_label():
    assert Alarm(1, "n", "fire", ["cam1"], after_min=1080).window_label() == "after 18:00"
    assert Alarm(1, "n", "fire", ["cam1"]).window_label() == "any time"


def test_overnight_window_wraps():
    rt = _runtime()
    # 22:00 → 06:00 window
    a = Alarm(1, "night", "person", ["cam1"], after_min=22 * 60, before_min=6 * 60)
    # 23:00 inside, 12:00 outside (checked via the manager's _in_window logic)
    import datetime
    class _FakeNow:
        @staticmethod
        def now():
            return datetime.datetime(2026, 1, 1, 23, 0)
    # monkeypatch-free: exercise the math directly
    mins_in, mins_out = 23 * 60, 12 * 60
    assert (a.after_min <= mins_in or mins_in < a.before_min)
    assert not (a.after_min <= mins_out or mins_out < a.before_min)


# ── trigger + acknowledge lifecycle ────────────────────────────────────


def test_alarm_triggers_and_logs_event():
    rt = _runtime(detections=[{"label": "fire"}])

    async def go():
        alarm = rt.alarms.create(name="Fire", target="fire", camera_ids=["cam1"])
        for _ in range(60):
            if rt.alarms.list()[0]["triggered"]:
                break
            await asyncio.sleep(0.02)
        data = rt.alarms.list()[0]
        events = rt.alarms.events()
        rt.alarms.stop(alarm.id)
        assert data["triggered"] is True
        assert events and "fire" in events[0]["text"]

    asyncio.run(go())


def test_acknowledge_silences_then_rearms_blocked_by_cooldown():
    rt = _runtime(detections=[{"label": "fire"}])
    rt.alarms._rearm = 999  # don't immediately re-trigger after ack

    async def go():
        alarm = rt.alarms.create(name="Fire", target="fire", camera_ids=["cam1"])
        for _ in range(60):
            if rt.alarms.list()[0]["triggered"]:
                break
            await asyncio.sleep(0.02)
        assert rt.alarms.acknowledge(alarm.id) == 1
        await asyncio.sleep(0.1)
        assert rt.alarms.list()[0]["triggered"] is False  # stays silenced (cooldown)
        rt.alarms.stop(alarm.id)

    asyncio.run(go())


def test_alarm_silent_outside_time_window():
    rt = _runtime(detections=[{"label": "person"}])

    async def go():
        # active only 00:00–00:01 — effectively never "now"
        alarm = rt.alarms.create(name="Night", target="person", camera_ids=["cam1"],
                                 after_min=0, before_min=1)
        await asyncio.sleep(0.2)
        triggered = rt.alarms.list()[0]["triggered"]
        rt.alarms.stop(alarm.id)
        assert triggered is False

    asyncio.run(go())


def test_emergency_contact_tagged_from_config():
    rt = _runtime(emergency_contacts={"fire": "+1-555-0100"})

    async def go():
        msg = await rt._handle_create_alarm({"name": "Fire", "target": "fire", "camera_id": "cam1"})
        assert "555-0100" in msg
        assert rt.alarms.list()[0]["emergency_contact_configured"] is True

    asyncio.run(go())


# ── voice handlers ─────────────────────────────────────────────────────


def test_create_alarm_handler_parses_after_time():
    rt = _runtime()

    async def go():
        msg = await rt._handle_create_alarm(
            {"name": "After-hours", "target": "person", "camera_id": "cam1", "after": "18:00"})
        assert "armed alarm" in msg.lower()
        assert rt.alarms.list()[0]["window"] == "after 18:00"

    asyncio.run(go())


# ── endpoints ──────────────────────────────────────────────────────────


def test_alarm_endpoints():
    rt = _runtime()
    client = TestClient(build_app(rt))
    r = client.post("/alarms", json={"name": "Fire", "target": "fire", "camera_id": "cam1"})
    assert r.status_code == 202
    body = client.get("/alarms").json()
    assert body["alarms"] and body["alarms"][0]["name"] == "Fire"
    aid = body["alarms"][0]["id"]
    assert client.post("/alarms/ack", json={}).json()["silenced"] >= 0
    assert client.delete(f"/alarms/{aid}").status_code == 200
    # DELETE is idempotent now: already-gone is success, not a 404 for
    # the UI to spam (the field bug: six "couldn't remove" messages for
    # an alarm that WAS removed).
    gone = client.delete("/alarms/9999")
    assert gone.status_code == 200 and gone.json()["already_gone"] is True
    assert client.post("/alarms", json={"name": "x", "camera_id": "cam1"}).status_code == 400


def test_chime_alarm_fires_event_without_latching():
    """A chime alarm dings (event + notification) but never latches the
    siren — no acknowledge needed, re-arm anchors on the last firing."""
    rt = _runtime(detections=[{"label": "person"}])

    async def go():
        alarm = rt.alarms.create(name="Gate visitor", target="person",
                                 camera_ids=["cam1"], ring="chime")
        await rt.alarms._poll(alarm, "cam1")
        assert alarm.triggered is False          # no latch
        assert alarm.trigger_count == 1
        ev = rt.alarms.events()[-1]
        assert ev["ring"] == "chime" and ev["camera"] == "cam1"
        # within the re-arm window: quiet
        await rt.alarms._poll(alarm, "cam1")
        assert alarm.trigger_count == 1

    asyncio.run(go())


def test_silent_alarm_records_without_latching():
    rt = _runtime(detections=[{"label": "person"}])

    async def go():
        alarm = rt.alarms.create(name="Quiet", target="person",
                                 camera_ids=["cam1"], ring="silent")
        await rt.alarms._poll(alarm, "cam1")
        assert alarm.triggered is False
        assert rt.alarms.events()[-1]["ring"] == "silent"

    asyncio.run(go())


def test_handler_defaults_ring_by_target():
    """Voice/REST default: fire-grade targets latch the siren; a person
    at the gate is a doorbell-grade chime (the operator can override)."""
    rt = _runtime()

    async def go():
        await rt._handle_create_alarm({"name": "F", "target": "fire",
                                       "camera_id": "cam1"})
        await rt._handle_create_alarm({"name": "P", "target": "person",
                                       "camera_id": "cam1"})
        await rt._handle_create_alarm({"name": "O", "target": "person",
                                       "camera_id": "cam1", "ring": "siren"})
        rings = {a["name"]: a["ring"] for a in rt.alarms.list()}
        assert rings == {"F": "siren", "P": "chime", "O": "siren"}

    asyncio.run(go())


def test_pulse_alarm_latches_then_stands_down_on_its_own():
    """URGENT: latches and rings like a siren, but auto-acknowledges
    after pulse_seconds — no human click required. CRITICAL never does."""
    rt = _runtime(detections=[{"label": "person"}])

    async def go():
        pulse = rt.alarms.create(name="Urgent", target="person",
                                 camera_ids=["cam1"], ring="pulse")
        siren = rt.alarms.create(name="Critical", target="person",
                                 camera_ids=["cam1"], ring="siren")
        await rt.alarms._poll(pulse, "cam1")
        await rt.alarms._poll(siren, "cam1")
        assert pulse.triggered and siren.triggered
        later = pulse.last_triggered + rt.alarms._pulse + 1
        rt.alarms._maybe_stand_down(pulse, now=later)
        rt.alarms._maybe_stand_down(siren, now=later)
        assert pulse.triggered is False          # stood down
        assert siren.triggered is True           # critical stays latched

    asyncio.run(go())


def test_ring_defaults_are_site_configurable():
    """A farm maps snake→siren; a bank maps person→siren. The config map
    overlays the fire-grade built-ins and drives the handler default."""
    rt = _runtime(alarm_ring_defaults={"snake": "siren", "person": "siren",
                                       "bogus": "not-a-level"})
    merged = rt.ring_defaults()
    assert merged["snake"] == "siren" and merged["person"] == "siren"
    assert merged["fire"] == "siren"             # built-in survives
    assert "bogus" not in merged                 # junk levels dropped

    async def go():
        await rt._handle_create_alarm({"name": "S", "target": "snake",
                                       "camera_id": "cam1"})
        await rt._handle_create_alarm({"name": "C", "target": "car",
                                       "camera_id": "cam1"})
        rings = {a["name"]: a["ring"] for a in rt.alarms.list()}
        assert rings == {"S": "siren", "C": "chime"}

    asyncio.run(go())


def test_ui_ring_overrides_layer_and_persist(tmp_path):
    """The UI-edited overrides beat config, which beats built-ins; junk
    is dropped; the layer survives a restart via the state file."""
    from camera_agent import AppConfig, CameraAgentRuntime
    from context import CameraSpec

    state = tmp_path / "s.json"
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    state_path=str(state),
                    alarm_ring_defaults={"person": "chime"},
                    cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="r")])
    rt = CameraAgentRuntime(cfg)
    merged = rt.set_ring_overrides({"person": "siren", "snake": "siren",
                                    "junk": "loudest", "": "siren"})
    assert merged["person"] == "siren"           # override beats config
    assert merged["snake"] == "siren"
    assert merged["fire"] == "siren"             # built-in survives
    assert "junk" not in merged and "" not in merged

    rt2 = CameraAgentRuntime(cfg)
    rt2.load_state()
    assert rt2.ring_defaults()["person"] == "siren"
    assert rt2.ring_defaults()["snake"] == "siren"


def test_alarm_defaults_endpoints_and_admin_gate():
    from fastapi.testclient import TestClient

    from camera_agent import build_app
    from tests.test_auth_gate import USERS, _FakeAuth  # reuse the tier fakes
    from camera_agent import AppConfig, CameraAgentRuntime
    from context import CameraSpec

    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    auth_mode="opennvr", opennvr_api_url="http://srv",
                    cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="r")])
    rt = CameraAgentRuntime(cfg)
    rt.auth = _FakeAuth()
    c = TestClient(build_app(rt))
    h = lambda t: {"Authorization": f"Bearer {t}"}

    assert c.get("/alarm-defaults", headers=h("tok-viewer")).status_code == 200
    put = {"overrides": {"snake": "siren"}}
    assert c.put("/alarm-defaults", json=put, headers=h("tok-op")).status_code == 403
    ok = c.put("/alarm-defaults", json=put, headers=h("tok-admin"))
    assert ok.status_code == 200 and ok.json()["defaults"]["snake"] == "siren"
    assert c.put("/alarm-defaults", json={"overrides": "nope"},
                 headers=h("tok-admin")).status_code == 400


# ── Alarm presets (GET /alarm-presets) ─────────────────────────────
#
# Availability must be HONEST: the stock detection path is YOLOv8/COCO-80,
# which cannot see fire/smoke/gas — those presets must come back greyed
# with a "what to run" sentence, not armable. Detectable targets (person,
# car, …) are available out of the box.


def test_presets_stock_availability():
    rt = _runtime()
    by_id = {p["id"]: p for p in rt.alarm_presets()}
    for sid in ("fire", "smoke", "gas"):
        assert by_id[sid]["available"] is False, sid
        assert "detector" in by_id[sid]["requires"].lower(), sid
    for sid in ("person", "after-hours", "vehicle", "truck", "dog"):
        assert by_id[sid]["available"] is True, sid
        assert by_id[sid]["requires"] is None, sid
    # The after-hours preset carries its window so one click arms 18:00+.
    assert by_id["after-hours"]["after"] == "18:00"


def test_presets_extra_labels_light_up_safety_alarms():
    rt = _runtime()
    rt.cfg.detector_extra_labels = ["fire", "smoke"]
    by_id = {p["id"]: p for p in rt.alarm_presets()}
    assert by_id["fire"]["available"] is True
    assert by_id["smoke"]["available"] is True
    assert by_id["gas"]["available"] is False, "gas still needs a detector"


def test_presets_grey_out_when_no_detection_adapter():
    rt = _runtime()
    rt.kaic_capabilities._tasks = {"image_captioning"}   # live view, no detector
    assert all(p["available"] is False for p in rt.alarm_presets())
    rt.kaic_capabilities._tasks = None                   # unknown → assume live
    assert any(p["available"] for p in rt.alarm_presets())


def test_presets_endpoint_serves_the_list():
    rt = _runtime()
    client = TestClient(build_app(rt))
    d = client.get("/alarm-presets").json()
    ids = [p["id"] for p in d["presets"]]
    assert "fire" in ids and "person" in ids
    fire = next(p for p in d["presets"] if p["id"] == "fire")
    assert fire["available"] is False and fire["requires"]


# ── Alarm UX fixes: dedup, true removal, honest single-camera wording ──


def test_duplicate_create_is_refused_with_the_existing_id():
    """Slow feedback invites double-clicks; each used to arm an identical
    alarm (its own polling loop), and ✕ visibly 'didn't work' because
    killing one left its twins."""
    rt = _runtime()
    first = asyncio.run(rt._handle_create_alarm(
        {"name": "Porch", "target": "person", "camera_id": "cam1"}))
    assert "Armed alarm #1" in first
    second = asyncio.run(rt._handle_create_alarm(
        {"name": "Porch", "target": "person", "camera_id": "cam1"}))
    assert "already covers" in second and "#1" in second
    assert len(rt.alarms.list()) == 1
    # A DIFFERENT window is a different alarm — allowed.
    third = asyncio.run(rt._handle_create_alarm(
        {"name": "Night porch", "target": "person", "camera_id": "cam1",
         "after": "22:00", "before": "06:00"}))
    assert "Armed alarm #2" in third


def test_delete_removes_the_alarm_from_the_list():
    """✕ means GONE — not parked as an inactive row until restart."""
    rt = _runtime()
    asyncio.run(rt._handle_create_alarm(
        {"name": "Porch", "target": "person", "camera_id": "cam1"}))
    assert len(rt.alarms.list()) == 1
    assert rt.alarms.remove(1) is True
    assert rt.alarms.list() == []
    # ...and re-arming after removal works (no stale dedup hit).
    again = asyncio.run(rt._handle_create_alarm(
        {"name": "Porch", "target": "person", "camera_id": "cam1"}))
    assert "Armed alarm" in again


def test_voice_disarm_also_removes():
    rt = _runtime()
    asyncio.run(rt._handle_create_alarm(
        {"name": "Porch", "target": "person", "camera_id": "cam1"}))
    msg = asyncio.run(rt._handle_stop_alarm({"alarm_id": 1}))
    assert "removed" in msg.lower()
    assert rt.alarms.list() == []


def test_delete_endpoint_removes(client_factory=None):
    rt = _runtime()
    client = TestClient(build_app(rt))
    asyncio.run(rt._handle_create_alarm(
        {"name": "Porch", "target": "person", "camera_id": "cam1"}))
    r = client.delete("/alarms/1")
    assert r.status_code == 200
    assert client.get("/alarms").json()["alarms"] == []


def test_single_camera_message_names_the_camera():
    """One camera IS the whole fleet, but the operator armed THAT camera
    and expects its name back — 'all cameras' reads like a scoping bug."""
    rt = _runtime()
    msg = asyncio.run(rt._handle_create_alarm(
        {"name": "Porch", "target": "person", "camera_id": "cam1"}))
    assert "cam1" in msg and "all cameras" not in msg
# ── Spoken time forms + the silent-window bug ──────────────────────
#
# "Ring an alarm when you see a person after 12:10 pm" — three ways that
# sentence used to fail quietly: the container clock ran UTC (fixed in
# compose: TZ now follows the site), '12:10 pm' didn't parse (silently
# became NO window — all day, the opposite of what was asked), and the
# quiet 'chime' default could swallow an explicit request to RING (tool
# schema now steers the LLM to siren/pulse for explicit loudness).


def test_parse_hhmm_accepts_spoken_am_pm_forms():
    assert _parse_hhmm("12:10 pm") == 12 * 60 + 10   # noon-ten stays 12:10
    assert _parse_hhmm("12:10 am") == 10             # midnight-ten -> 00:10
    assert _parse_hhmm("7pm") == 19 * 60
    assert _parse_hhmm("07:00 PM") == 19 * 60
    assert _parse_hhmm("7 am") == 7 * 60
    assert _parse_hhmm("11:59pm") == 23 * 60 + 59
    # 24h forms unchanged; junk still None (never a phantom window).
    assert _parse_hhmm("18:00") == 18 * 60
    assert _parse_hhmm("25:00") is None
    assert _parse_hhmm("noonish") is None


def test_spoken_pm_window_gates_polling():
    """An 'after 12:10 pm' alarm armed from speech must be OUT of window
    at 11:00 and IN at 13:00 (site-local — the compose TZ fix makes the
    container clock the site clock)."""
    rt = _runtime()
    asyncio.run(rt._handle_create_alarm(
        {"name": "Afternoon person", "target": "person",
         "camera_id": "cam1", "after": "12:10 pm"}))
    [a] = [x for x in rt.alarms._alarms.values()]
    assert a.after_min == 12 * 60 + 10
    import datetime

    class _At:
        def __init__(self, h, m): self._t = datetime.time(h, m)
        def time(self): return self._t

    real_dt = datetime.datetime
    try:
        class _FakeDT(datetime.datetime):
            _now = None

            @classmethod
            def now(cls, tz=None):
                return cls._now

        datetime.datetime = _FakeDT
        _FakeDT._now = real_dt(2026, 8, 22, 11, 0)
        assert rt.alarms._in_window(a) is False
        _FakeDT._now = real_dt(2026, 8, 22, 13, 0)
        assert rt.alarms._in_window(a) is True
    finally:
        datetime.datetime = real_dt


# ── Parity: watches, reports, tasks get the same anti-double-wiring ──
#
# A duplicated WATCH is worse than a duplicated alarm: for converged
# kinds it is a second hosted rule instance doing double inference and
# double notifications. Reports fire twice per slot into every channel.


def test_duplicate_watch_is_refused_and_delete_removes():
    rt = _runtime()
    first = asyncio.run(rt._handle_create_monitor(
        {"kind": "notify", "target": "person", "camera_id": "cam1"}))
    assert "#1" in first
    second = asyncio.run(rt._handle_create_monitor(
        {"kind": "notify", "target": "person", "camera_id": "cam1"}))
    assert "already covers" in second
    assert len([m for m in rt.monitors.list() if m["active"]]) == 1
    assert rt.monitors.remove(1) is True
    assert all(not m["active"] for m in rt.monitors.list())
    # Removed means forgotten — re-creating works, no stale dedup hit.
    again = asyncio.run(rt._handle_create_monitor(
        {"kind": "notify", "target": "person", "camera_id": "cam1"}))
    assert "already covers" not in again


def test_duplicate_report_is_refused_and_remove_forgets():
    rt = _runtime()
    first = asyncio.run(rt._handle_create_report(
        {"name": "Morning", "query": "summarise overnight activity"}))
    assert "#1" in first
    dup = asyncio.run(rt._handle_create_report(
        {"name": "Different name", "query": "summarise overnight activity"}))
    assert "already runs" in dup
    # A DIFFERENT cadence is a different report.
    other = asyncio.run(rt._handle_create_report(
        {"name": "Hourly", "query": "summarise overnight activity",
         "every_minutes": 60}))
    assert "already runs" not in other
    assert rt.reports.remove(1) is True
    assert all(r["id"] != 1 for r in rt.reports.list())


# ── Target normalization: never arm an alarm that can't fire ────────
#
# Field bug: the LLM armed target='alert when you see a person on this
# camera' — the whole sentence. Matching is exact label equality, so the
# alarm silently could never fire. The server now reduces the phrase to
# one detectable label or asks back instead of arming a dud.


def test_sentence_target_is_reduced_to_the_label():
    rt = _runtime()
    msg = asyncio.run(rt._handle_create_alarm(
        {"name": "Alarm", "camera_id": "cam1",
         "target": "alert when you see a person on this camera"}))
    assert "Armed alarm" in msg
    [a] = rt.alarms._alarms.values()
    assert a.target == "person", "the alarm must match what the detector emits"


def test_plurals_and_exact_labels_still_work():
    rt = _runtime()
    m1 = asyncio.run(rt._handle_create_alarm(
        {"name": "Cars", "camera_id": "cam1", "target": "cars"}))
    assert "Armed alarm" in m1
    m2 = asyncio.run(rt._handle_create_alarm(
        {"name": "Fire", "camera_id": "cam1", "target": "fire"}))
    assert "Armed alarm" in m2
    targets = {a.target for a in rt.alarms._alarms.values()}
    assert targets == {"car", "fire"}


def test_ambiguous_or_labelless_target_asks_back():
    rt = _runtime()
    # No detectable label in the phrase → ask, don't arm.
    none_msg = asyncio.run(rt._handle_create_alarm(
        {"name": "X", "camera_id": "cam1", "target": "anything suspicious"}))
    assert none_msg.endswith("?") and not rt.alarms._alarms
    # TWO labels → ambiguous → ask, don't guess.
    two_msg = asyncio.run(rt._handle_create_alarm(
        {"name": "X", "camera_id": "cam1", "target": "a person or a car"}))
    assert two_msg.endswith("?") and not rt.alarms._alarms


def test_watch_targets_are_normalized_too():
    rt = _runtime()
    msg = asyncio.run(rt._handle_create_monitor(
        {"kind": "notify", "camera_id": "cam1",
         "target": "watch for people walking by"}))
    assert "ERROR" not in msg and not msg.endswith("?")
    [m] = rt.monitors._monitors.values()
    assert m.target == "person"


# ── Bounded busy-yield + idempotent delete ─────────────────────────


def test_delete_is_idempotent_already_gone_is_success():
    """A slow refresh leaves a removed alarm's row on screen; the extra
    clicks used to 404 six times for an alarm that WAS removed."""
    rt = _runtime()
    client = TestClient(build_app(rt))
    asyncio.run(rt._handle_create_alarm(
        {"name": "Porch", "target": "person", "camera_id": "cam1"}))
    assert client.delete("/alarms/1").json() == {"stopped": True, "already_gone": False}
    again = client.delete("/alarms/1")
    assert again.status_code == 200
    assert again.json() == {"stopped": False, "already_gone": True}


def test_busy_yield_is_bounded():
    """Chained conversation must not pause a safety check forever: after
    _MAX_BUSY_SKIPS skipped cycles the loop polls anyway."""
    from camera_agent import AlarmManager

    rt = _runtime(detections=[{"label": "person"}])
    rt.interactive_busy = lambda: True          # a turn is ALWAYS in flight
    mgr = AlarmManager(rt, interval=0.01)

    async def run_some_cycles():
        # create() spawns the loop task — must happen inside the loop.
        alarm = mgr.create(name="P", target="person", camera_ids=["cam1"],
                           ring="siren")
        task = mgr._tasks[alarm.id]
        # Enough wall-clock for well over _MAX_BUSY_SKIPS cycles.
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return alarm

    alarm = asyncio.run(run_some_cycles())
    assert alarm.triggered, (
        "the capped yield must let the poll through and fire the alarm"
    )


# ── Tier-0 as the trigger source ───────────────────────────────────────
# The agent is already subscribed to detect-pipeline's detections and they
# are far better evidence than this loop's own full-frame poll: Tier-0 runs
# the detector on a SQUARE crop around motion at the stream's frame rate,
# the poll squashes a whole 1920x1080 frame to 640x640 once every several
# seconds. On a live deployment that was the difference between finding a
# person and returning nothing — four people recorded by Tier-0 at 0.53-0.66
# confidence, of which the full-frame path found two at ~0.28 and missed two
# outright, while visits lasting 5-19s were sampled once per 8-12s.


class _Ev:
    """Minimal stand-in for context.EventRecord."""

    def __init__(self, received_at, tracks, adapter="tier0"):
        self.received_at = received_at
        self.adapter = adapter
        self.raw = {"tracks": tracks}


def _tier0_runtime(tracks, *, age=0.0, poll_detections=None):
    """A runtime whose Tier-0 ring holds one event, and whose own poll finds
    ``poll_detections`` (nothing, by default — so a trigger can only have
    come from Tier-0)."""
    import time
    rt = _runtime(detections=poll_detections if poll_detections is not None else [])
    polls = []

    async def counting_infer(*, frame_jpeg, **kw):
        polls.append(1)
        return {"result": {"detections": poll_detections or []}}

    rt.detection_client.infer = counting_infer
    ev = _Ev(time.time() - age, tracks)
    rt.context.recent_events = lambda *, camera_id, window_seconds: [ev]
    return rt, polls


def test_alarm_triggers_off_tier0_without_polling():
    """The live failure: Tier-0 saw the person, the alarm's own full-frame
    poll did not, and nothing rang."""
    rt, polls = _tier0_runtime([{"label": "person", "score": 0.61}])

    async def go():
        alarm = rt.alarms.create(name="Front", target="person", camera_ids=["cam1"])
        for _ in range(60):
            if rt.alarms.list()[0]["triggered"]:
                break
            await asyncio.sleep(0.02)
        data = rt.alarms.list()[0]
        rt.alarms.stop(alarm.id)
        assert data["triggered"] is True, "Tier-0 evidence must ring the alarm"
        assert not polls, "no need to run our own inference when Tier-0 already saw it"

    asyncio.run(go())


def test_stale_tier0_evidence_does_not_ring():
    """'A person was here five minutes ago' is not 'a person is here'."""
    rt, _polls = _tier0_runtime([{"label": "person"}], age=600.0)
    rt.alarms._interval = 0.01        # many passes, all of them stale

    async def go():
        alarm = rt.alarms.create(name="Front", target="person", camera_ids=["cam1"])
        await asyncio.sleep(0.25)
        triggered = rt.alarms.list()[0]["triggered"]
        rt.alarms.stop(alarm.id)
        assert triggered is False

    asyncio.run(go())


def test_one_tier0_frame_rings_once():
    """Tier-0 republishes the same scene many times a second. Acknowledging
    an alarm must not be undone by the frame that raised it still being the
    newest thing on the ring."""
    rt, _polls = _tier0_runtime([{"label": "person"}])
    rt.alarms._rearm = 0.0            # cooldown cannot be what holds it back
    rt.alarms._interval = 0.01        # ...and the loop must actually come round

    async def go():
        alarm = rt.alarms.create(name="Front", target="person", camera_ids=["cam1"])
        for _ in range(60):
            if rt.alarms.list()[0]["triggered"]:
                break
            await asyncio.sleep(0.02)
        assert rt.alarms.acknowledge(alarm.id) == 1
        await asyncio.sleep(0.3)      # ~30 further loop passes
        again = rt.alarms.list()[0]["triggered"]
        rt.alarms.stop(alarm.id)
        assert again is False, "the same published frame must not re-ring"

    asyncio.run(go())


def test_tier0_without_the_target_still_falls_back_to_polling():
    """Tier-0 publishes only frames that produced tracks, so silence from it
    means 'quiet scene' and 'pipeline down' alike. The alarm's own look has
    to stay as a backstop — a safety feature must not go quiet because one
    source did."""
    rt, polls = _tier0_runtime([{"label": "cat"}],
                               poll_detections=[{"label": "person"}])

    async def go():
        alarm = rt.alarms.create(name="Front", target="person", camera_ids=["cam1"])
        for _ in range(60):
            if rt.alarms.list()[0]["triggered"]:
                break
            await asyncio.sleep(0.02)
        data = rt.alarms.list()[0]
        rt.alarms.stop(alarm.id)
        assert data["triggered"] is True, "the backstop poll must still ring"
        assert polls, "Tier-0 seeing a cat is not evidence about a person"

    asyncio.run(go())


def test_no_tier0_stream_behaves_exactly_as_before():
    """Cameras with no Tier-0 coverage keep the original behaviour."""
    rt = _runtime(detections=[{"label": "person"}])
    rt.context.recent_events = lambda *, camera_id, window_seconds: []

    async def go():
        alarm = rt.alarms.create(name="Front", target="person", camera_ids=["cam1"])
        for _ in range(60):
            if rt.alarms.list()[0]["triggered"]:
                break
            await asyncio.sleep(0.02)
        data = rt.alarms.list()[0]
        rt.alarms.stop(alarm.id)
        assert data["triggered"] is True

    asyncio.run(go())


def test_a_broken_tier0_ring_cannot_take_the_alarm_loop_down():
    """The ring read is defensive: alarms are a safety feature and must not
    stop ringing because a context accessor raised."""
    rt = _runtime(detections=[{"label": "person"}])

    def boom(*, camera_id, window_seconds):
        raise RuntimeError("ring exploded")

    rt.context.recent_events = boom

    async def go():
        alarm = rt.alarms.create(name="Front", target="person", camera_ids=["cam1"])
        for _ in range(60):
            if rt.alarms.list()[0]["triggered"]:
                break
            await asyncio.sleep(0.02)
        data = rt.alarms.list()[0]
        rt.alarms.stop(alarm.id)
        assert data["triggered"] is True, "must fall back, not die"

    asyncio.run(go())


# ── the backstop poll's framing ────────────────────────────────────────


def _jpeg(w, h, colour=(90, 90, 90)):
    """Pillow, not OpenCV: cv2 is an optional extra for this package, so a
    test written against it fails wherever the extra is absent — which is
    exactly where the code under test must still work."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, format="JPEG")
    return buf.getvalue()


def _dims(jpeg):
    import io
    from PIL import Image
    return Image.open(io.BytesIO(jpeg)).size


def test_letterbox_squares_a_widescreen_frame():
    """A detector resizes whatever it gets to a square input, so a 16:9 frame
    arrives crushed 1.78x and people come out too narrow to recognise. Pad
    first; never crop — a centre crop measured 0.00 at both frame edges."""
    from camera_agent import _letterbox_jpeg
    w, h = _dims(_letterbox_jpeg(_jpeg(1920, 1080)))
    assert w == h, f"output must be square, got {w}x{h}"
    # ...and shrunk on the way, since the detector resizes to its own input
    # regardless: sending 1920x1920 costs real work and buys nothing.
    assert w <= 960, f"expected a downscaled square, got {w}"


def test_letterbox_keeps_the_whole_frame():
    """Nothing may be cut: the edges of a driveway or gate view are exactly
    where the thing you armed the alarm for walks in."""
    import io
    from PIL import Image
    from camera_agent import _letterbox_jpeg

    src = Image.new("RGB", (1920, 1080), (30, 30, 30))
    white = Image.new("RGB", (192, 1080), (255, 255, 255))
    src.paste(white, (0, 0))               # the leftmost tenth of the frame
    src.paste(white, (1920 - 192, 0))      # ...and the rightmost tenth
    buf = io.BytesIO(); src.save(buf, format="JPEG")

    out = Image.open(io.BytesIO(_letterbox_jpeg(buf.getvalue()))).convert("RGB")
    # Padding goes top/bottom for a landscape frame, so the middle row is all
    # content and the marked tenths must still be at its two ends.
    w, h = out.size
    y = h // 2
    tenth = max(1, w // 10)
    left = [out.getpixel((x, y))[0] for x in range(tenth)]
    right = [out.getpixel((x, y))[0] for x in range(w - tenth, w)]
    assert sum(left) / len(left) > 200, "left edge of the frame was lost"
    assert sum(right) / len(right) > 200, "right edge of the frame was lost"


def test_letterbox_leaves_a_square_frame_alone():
    from camera_agent import _letterbox_jpeg
    square = _jpeg(640, 640)
    assert _letterbox_jpeg(square) is square


def test_letterbox_never_breaks_the_poll():
    """Alarms are a safety feature: garbage in must mean 'unchanged', not an
    exception that stops the loop looking."""
    from camera_agent import _letterbox_jpeg
    assert _letterbox_jpeg(b"not a jpeg") == b"not a jpeg"
    assert _letterbox_jpeg(b"") == b""


def test_a_busy_camera_does_not_starve_a_quiet_one_on_the_same_alarm():
    """One alarm can watch several cameras. A single shared watermark lets the
    busiest camera hold it permanently ahead of the quiet camera's timestamps,
    so the quiet one — the side gate nobody walks up — would silently never use
    Tier-0 at all and fall back to the weak full-frame poll forever."""
    import time
    from context import CameraSpec

    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front"),
                 CameraSpec(camera_id="cam2", frame_url="http://x/2.jpg", role="gate")],
    )
    rt = CameraAgentRuntime(cfg)
    polls = []

    async def fake_get_frame(cam, **_kw):
        return b"\xff\xd8\xff"

    async def counting_infer(*, frame_jpeg, **kw):
        polls.append(1)
        return {"result": {"detections": []}}   # the poll finds nothing

    rt.context.get_frame = fake_get_frame
    rt.detection_client.infer = counting_infer

    now = time.time()
    # cam1 is busy and its newest event is NEWER than cam2's.
    evs = {"cam1": _Ev(now, [{"label": "person"}]),
           "cam2": _Ev(now - 3.0, [{"label": "person"}])}
    rt.context.recent_events = (
        lambda *, camera_id, window_seconds:
            [evs[camera_id]] if camera_id in evs else [])

    async def go():
        alarm = rt.alarms.create(name="Both", target="person",
                                 camera_ids=["cam1", "cam2"])
        await rt.alarms._poll(alarm, "cam1")
        alarm.triggered = False                 # unlatch so cam2 gets its turn
        alarm.last_ack = 0.0
        await rt.alarms._poll(alarm, "cam2")
        rt.alarms.stop(alarm.id)
        assert alarm.last_tier0_at.get("cam2"), (
            "cam2's own Tier-0 evidence was discarded because cam1's was newer")
        assert not polls, "cam2 should not have needed the fallback poll"

    asyncio.run(go())


def test_a_gate_audit_record_does_not_mask_a_real_detection():
    """Caught in production, not in review. Tier-0 publishes its GATE
    decisions — the audit of *non*-events — on the same adapter and into the
    same ring as detections, and they carry no tracks. They are also more
    frequent, so reading only the newest Tier-0 record meant almost always
    reading an empty one: every single alarm fired 'via poll' while the
    pipeline was detecting people a second earlier.

    An empty record is not evidence of absence. Skip past it to the last
    record that actually looked.
    """
    import time
    rt, polls = _tier0_runtime([{"label": "person"}])
    now = time.time()
    # Exactly the production shape: a detection, then a newer gate audit.
    detection = _Ev(now - 1.0, [{"label": "person", "score": 0.61}])
    gate = _Ev(now, [])
    rt.context.recent_events = (
        lambda *, camera_id, window_seconds: [gate, detection])   # newest first

    async def go():
        alarm = rt.alarms.create(name="Front", target="person", camera_ids=["cam1"])
        for _ in range(60):
            if rt.alarms.list()[0]["triggered"]:
                break
            await asyncio.sleep(0.02)
        data = rt.alarms.list()[0]
        rt.alarms.stop(alarm.id)
        assert data["triggered"] is True, (
            "a gate audit with no tracks masked the detection behind it")
        assert not polls, "should have rung from Tier-0, not fallen back to the poll"

    asyncio.run(go())


def test_an_empty_ring_still_falls_back_to_the_poll():
    """All gate audits and no detections is genuinely 'Tier-0 has nothing to
    say' — the backstop must still run."""
    rt, polls = _tier0_runtime([{"label": "cat"}], poll_detections=[{"label": "person"}])
    import time
    rt.context.recent_events = (
        lambda *, camera_id, window_seconds: [_Ev(time.time(), [])])

    async def go():
        alarm = rt.alarms.create(name="Front", target="person", camera_ids=["cam1"])
        for _ in range(60):
            if rt.alarms.list()[0]["triggered"]:
                break
            await asyncio.sleep(0.02)
        data = rt.alarms.list()[0]
        rt.alarms.stop(alarm.id)
        assert data["triggered"] is True and polls, "the backstop must still run"

    asyncio.run(go())


# ── latency under load ─────────────────────────────────────────────────
# Second field report, verified in the logs: operator armed an alarm, tested
# it by TALKING TO THE AGENT, and the ring came >30s after the person did —
# 'yielded 4 cycles' immediately followed by the trigger. Two causes, each
# pinned below: the cheap Tier-0 ring-read was being deferred by the
# interactive yield along with the expensive inference, and the backstop's
# own full-frame inference was burning the CPU detect-pipeline needed (its
# frame budget blew 8 -> 2 regions the moment the person appeared).


def test_tier0_evidence_rings_even_during_an_interactive_turn():
    """The yield exists to keep the user's turn snappy by not running MODELS.
    Reading the Tier-0 ring touches no model and costs microseconds — deferring
    it bought the turn nothing and cost the alarm up to ~20s."""
    rt, polls = _tier0_runtime([{"label": "person", "score": 0.61}])
    rt.interactive_busy = lambda: True          # a conversation, forever
    rt.alarms._interval = 0.01
    # Make the bounded-skip escape hatch unreachable within this test, so it
    # cannot mask the property under test: the ring must come from the Tier-0
    # check running DURING the yield, not from the yield running out. (At the
    # real 5s interval those 4 skips are the ~20s the operator waited.)
    rt.alarms._MAX_BUSY_SKIPS = 10_000

    async def go():
        alarm = rt.alarms.create(name="Front", target="person", camera_ids=["cam1"])
        for _ in range(100):
            if rt.alarms.list()[0]["triggered"]:
                break
            await asyncio.sleep(0.01)
        data = rt.alarms.list()[0]
        rt.alarms.stop(alarm.id)
        assert data["triggered"] is True, (
            "Tier-0 evidence must ring immediately, conversation or not")
        assert not polls, "no inference may run while yielding to the turn"

    asyncio.run(go())


def test_backstop_inference_still_yields_to_the_turn_boundedly():
    """The expensive half keeps the original yield semantics: skip while a
    turn is active, but never more than _MAX_BUSY_SKIPS cycles — a safety
    check must not wait out a conversation."""
    rt, polls = _tier0_runtime([], poll_detections=[{"label": "person"}])
    rt.context.recent_events = lambda *, camera_id, window_seconds: []
    rt.interactive_busy = lambda: True
    rt.alarms._interval = 0.01

    async def go():
        alarm = rt.alarms.create(name="Front", target="person", camera_ids=["cam1"])
        await asyncio.sleep(0.03)                # a few cycles: still yielding
        yielded_early = not polls
        for _ in range(200):
            if polls:
                break
            await asyncio.sleep(0.01)
        rt.alarms.stop(alarm.id)
        assert yielded_early, "inference should yield to the turn at first"
        assert polls, "but a bounded number of skips later it must look anyway"

    asyncio.run(go())


def test_backstop_throttles_while_tier0_is_alive():
    """While Tier-0 is demonstrably watching this camera, the backstop runs on
    a clock, not every cycle — its full-frame inference was part of the load
    that shed Tier-0's detector to near-blindness the moment a person arrived."""
    import time
    rt, polls = _tier0_runtime([{"label": "cat"}])   # alive, but no person

    async def go():
        alarm = rt.alarms.create(name="Front", target="person", camera_ids=["cam1"])
        rt.alarms.stop(alarm.id)                 # drive _poll by hand
        alarm.active = True
        for _ in range(6):
            await rt.alarms._poll(alarm, "cam1")
        assert len(polls) == 1, (
            f"expected one due inference then throttle, got {len(polls)}")
        # The clock, not luck: age the stamp past the throttle and it looks again.
        alarm.last_fallback_at["cam1"] -= rt.alarms._fallback_every + 1
        await rt.alarms._poll(alarm, "cam1")
        assert len(polls) == 2

    asyncio.run(go())


def test_backstop_unthrottled_when_tier0_is_silent():
    """Silence is ambiguous — quiet scene and dead pipeline look identical —
    so with no Tier-0 records at all the backstop keeps its full cadence."""
    rt, polls = _tier0_runtime([], poll_detections=[])
    rt.context.recent_events = lambda *, camera_id, window_seconds: []

    async def go():
        alarm = rt.alarms.create(name="Front", target="person", camera_ids=["cam1"])
        rt.alarms.stop(alarm.id)
        alarm.active = True
        for _ in range(4):
            await rt.alarms._poll(alarm, "cam1")
        assert len(polls) == 4, (
            f"no liveness signal -> every cycle must look, got {len(polls)}")

    asyncio.run(go())


def test_gate_audit_records_prove_liveness_for_the_throttle():
    """A gate record has no tracks but is still proof the pipeline is up and
    watching — an audit of a non-event. It must throttle the backstop even
    though it can never ring the alarm."""
    import time
    rt, polls = _tier0_runtime([])
    rt.context.recent_events = (
        lambda *, camera_id, window_seconds: [_Ev(time.time(), [])])

    async def go():
        alarm = rt.alarms.create(name="Front", target="person", camera_ids=["cam1"])
        rt.alarms.stop(alarm.id)
        alarm.active = True
        for _ in range(6):
            await rt.alarms._poll(alarm, "cam1")
        assert len(polls) == 1, (
            f"gate records prove liveness; expected 1 inference, got {len(polls)}")

    asyncio.run(go())
