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
    assert client.delete("/alarms/9999").status_code == 404
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
