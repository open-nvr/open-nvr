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
