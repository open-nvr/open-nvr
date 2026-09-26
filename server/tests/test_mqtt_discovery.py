# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""MQTT integration and Home Assistant MQTT discovery (HA-401..403).

The bridge runs against a fake MQTT client: every publish is recorded, and
messages (Home Assistant's status, commands) are fed in.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import json
import types

import pytest

from services import ha_mqtt_discovery as hd
from services.mqtt_settings import MqttSettings
from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture

TOKENISH = {"broker_url": "mqtt://broker.lan", "api_token_id": 1}


# ── settings ────────────────────────────────────────────────────────────


def test_settings_defaults_and_tls():
    s = MqttSettings.from_config(TOKENISH)
    assert (s.host, s.port, s.tls, s.topic_prefix, s.discovery_prefix) == (
        "broker.lan", 1883, False, "opennvr", "homeassistant")
    s = MqttSettings.from_config({**TOKENISH, "broker_url": "mqtts://b.lan"})
    assert s.port == 8883 and s.tls


@pytest.mark.parametrize(("config", "error"), [
    ({"broker_url": "http://b"}, "mqtt://"),
    ({"broker_url": "mqtt://169.254.169.254"}, "metadata"),
    ({"broker_url": "mqtt://b", "topic_prefix": "a/#"}, "topic_prefix"),
    ({"broker_url": "mqtt://b"}, "API token"),                 # discovery needs one
    ({"broker_url": "mqtt://b:99999", "api_token_id": 1}, "port"),
])
def test_settings_refusals(config, error):
    with pytest.raises(ValueError, match=error):
        MqttSettings.from_config(config)


def test_plain_mqtt_needs_no_token():
    assert MqttSettings.from_config({"broker_url": "b", "ha_discovery": False}).host == "b"


# ── discovery payloads ──────────────────────────────────────────────────

TOPICS = hd.Topics("opennvr", "homeassistant", "0755a940-1ff5-4861-ac08-1f57bb29a180")
CAM = {"kind": "camera", "id": 1}
ZONE = {"kind": "zone", "id": 7, "camera_id": 1, "name": "Driveway"}


def _d(key, platform, name, device=CAM, **extra):
    return {"key": key, "platform": platform, "name": name, "device": device,
            "enabled_default": True, **extra}


DESCS = [
    _d("camera.1.motion", "binary_sensor", "Motion", device_class="motion"),
    _d("camera.1.count.person", "sensor", "person count", state_class="measurement"),
    _d("camera.1.detection", "switch", "Object detection",
       command={"type": "core_control", "control": "detection"}),
    _d("camera.1.ptz_preset", "select", "PTZ preset", options=["Home", "Gate"]),
    _d("camera.1.ptz_up", "button", "PTZ up"),
    _d("camera.1.detections", "event", "Detection", event_types=["person", "car"]),
    _d("camera.1.last_object", "image", "Last object"),
    _d("zone.7.occupancy.all", "binary_sensor", "All occupancy", ZONE),
    _d("site.cpu", "sensor", "CPU", {"kind": "site", "id": "site"}, unit="%",
       enabled_default=False),
]


def test_device_based_discovery():
    devices = hd.build_discovery(TOPICS, "0.1.5", DESCS, {"camera_1": "Front door"},
                                 site_mode=True)
    assert set(devices) == {"camera_1", "zone_7", "site"}
    cam = devices["camera_1"]
    assert cam["dev"]["name"] == "Front door" and cam["dev"]["via_device"].endswith("_site")
    assert cam["avty_t"] == "opennvr/0755a9401ff5/status"
    cmps = cam["cmps"]
    assert "opennvr_camera_1_last_object" not in cmps           # image: native only
    switch = cmps["opennvr_camera_1_detection"]
    assert switch["cmd_t"] == "opennvr/0755a9401ff5/camera.1.detection/set"
    assert switch["stat_t"].endswith("/camera.1.detection/state")
    assert cmps["opennvr_camera_1_ptz_preset"]["ops"] == ["Home", "Gate"]
    assert "stat_t" not in cmps["opennvr_camera_1_ptz_up"]
    event = cmps["opennvr_camera_1_detections"]
    assert event["evt_typ"] == ["person", "car"] and event["stat_t"].endswith("/event")
    assert devices["zone_7"]["dev"]["via_device"].endswith("_camera_1")
    assert devices["site"]["cmps"]["opennvr_site_cpu"]["en"] is False
    assert devices["site"]["cmps"]["opennvr_site_mode"]["p"] == "alarm_control_panel"
    assert TOPICS.device_config("camera_1") == (
        "homeassistant/device/opennvr_0755a9401ff5_camera_1/config")


def test_payload_mappings():
    assert hd.state_payload("binary_sensor", True) == "ON"
    assert hd.state_payload("switch", False) == "OFF"
    assert hd.state_payload("sensor", None) == "None"
    assert hd.state_payload("sensor", 3) == "3"
    assert hd.command_value("switch", "off") is False
    assert hd.command_value("number", "12.5") == 12.5 and hd.command_value("number", "4") == 4
    assert hd.command_value("button", "PRESS") is None
    with pytest.raises(ValueError):
        hd.command_value("switch", "maybe")
    with pytest.raises(ValueError):
        hd.command_value("sensor", "1")
    assert TOPICS.key_of_command("opennvr/0755a9401ff5/camera.1.detection/set") == (
        "camera.1.detection")
    assert TOPICS.key_of_command("opennvr/0755a9401ff5/a/b/set") is None
    assert TOPICS.key_of_command("other/0755a9401ff5/x/set") is None
    ce = hd.cloud_event("SITE", "camera.1.detections", {"type": "person",
                                                         "attributes": {"track_id": "9"}})
    assert ce["specversion"] == "1.0" and ce["data"] == {"event_type": "person",
                                                         "track_id": "9"}


# ── the bridge, end to end against a fake broker ────────────────────────


def _matches(filt: str, topic: str) -> bool:
    f, t = filt.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(f) == len(t)


class FakeBroker:
    """Records publishes, keeps retained topics (an empty payload clears one)
    and, like a broker, hands a subscriber the retained ones it matches."""

    def __init__(self):
        self.published: list[tuple[str, str, bool]] = []
        self.subscribed: list[str] = []
        self.retained: dict[str, str] = {}
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.will = None

    def client(self, settings, *, identifier=None, will=None):
        self.will = will
        broker = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def publish(self, topic, payload=None, qos=0, retain=False):
                broker.published.append((topic, payload, retain))
                if retain and payload:
                    broker.retained[topic] = payload
                elif retain:
                    broker.retained.pop(topic, None)

            async def subscribe(self, topic):
                broker.subscribed.append(topic)
                for name, payload in list(broker.retained.items()):
                    if _matches(topic, name):
                        broker.send(name, payload, retain=True)

            @property
            def messages(self):
                async def gen():
                    while True:
                        yield await broker.inbox.get()
                return gen()

        return _Client()

    def send(self, topic, payload, retain=False):
        self.inbox.put_nowait(types.SimpleNamespace(topic=topic, payload=payload.encode(),
                                                    retain=retain))

    def last(self, topic):
        return next((p for t, p, _ in reversed(self.published) if t == topic), None)


async def _until(predicate, timeout=5.0, *, what: str = "", show=None):
    """Poll until ``predicate`` holds, then say something useful if it
    never does.

    TWO FIXES, FOR TWO SEPARATE PROBLEMS.

    The timeout was 5 seconds, which is generous on an idle laptop and
    not on a loaded CI runner: this suite publishes through a real
    asyncio event loop and a background manager, and when the box is
    busy those tasks get scheduled late. It failed exactly once here,
    under two test suites running at the same time, and passed sixteen
    runs in a row otherwise — the signature of a deadline that is fine
    until the machine is not. A longer one costs NOTHING on a passing
    run, because the loop returns the moment the predicate holds; it
    only buys patience for the run that would otherwise fail for being
    hurried.

    And the message was ``"condition not met in time"``. Which
    condition, out of nine in this file, and what was the state when it
    gave up? Neither. Chasing that failure meant re-running the suite to
    find out which line it was, then adding prints — twenty minutes to
    learn something the assertion could have said. ``what`` names the
    wait; ``show`` renders whatever the reader needs to see, and is
    where "it never arrived" becomes "it arrived on a topic you did not
    expect".
    """
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.02)

    detail = ""
    if show is not None:
        try:
            detail = f"\n  observed: {show()!r}"
        except Exception as exc:  # noqa: BLE001 — diagnosing, not asserting
            detail = f"\n  observed: <could not render: {exc!r}>"
    raise AssertionError(
        f"waited {timeout:g}s for {what or 'a condition'} and it never "
        f"happened.{detail}")


@pytest.fixture
def bridge_env(env, monkeypatch):  # noqa: F811
    import services.live_state as ls_mod
    import services.mqtt_settings as ms
    from services import entity_state_publisher as pub, event_bus_service as ebs

    monkeypatch.setattr(ebs, "_event_bus_instance", ebs.EventBus())
    monkeypatch.setattr(ls_mod, "_instance", ls_mod.LiveState())
    pub._forget()
    monkeypatch.setattr(pub, "_lock", None)
    broker = FakeBroker()
    monkeypatch.setattr(ms, "client", broker.client)
    monkeypatch.setattr(hd, "RECHECK_S", 0.2)
    monkeypatch.setattr(hd, "CLEAR_COLLECT_S", 0.1)
    return env, broker


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """Broker names in these tests don't resolve; don't wait on DNS."""
    import services.mqtt_settings as ms

    async def resolved(host):
        return None

    monkeypatch.setattr(ms, "check_resolved", resolved)


def test_bridge_publishes_runs_commands_and_stops_on_revoke(bridge_env):
    env, broker = bridge_env
    minted = _mint(env, scopes=["settings.view", "cameras.view", "cameras.manage"],
                   camera_ids=[1])
    config = {"broker_url": "mqtt://broker.lan", "api_token_id": minted["id"]}

    async def scenario():
        from services.event_bus_service import publish_entity_state

        bridge = hd.MqttBridge(9, "ha-mqtt", config)
        task = asyncio.create_task(bridge.run())
        await _until(lambda: any(t.endswith("/status") and p == "online"
                                 for t, p, _ in broker.published))
        # Discovery, then the current states (resolved off the loop).
        await _until(lambda: any(t.endswith("/camera.1.detection/state")
                                 for t, _, _ in broker.published))
        topics = [t for t, _, _ in broker.published]
        # Discovery for the site and camera 1 only (the token's camera).
        assert any(t.endswith("_camera_1/config") for t in topics)
        assert not any(t.endswith("_camera_2/config") for t in topics)
        site = next(json.loads(p) for t, p, _ in broker.published if t.endswith("_site/config"))
        # No settings.manage: site mode is shown, read-only (a sensor).
        assert site["cmps"]["opennvr_site_mode_state"]["p"] == "sensor"
        assert broker.will.topic.endswith("/status") and broker.will.payload == "offline"
        prefix = site["avty_t"].rsplit("/", 1)[0]
        assert broker.last(f"{prefix}/camera.1.detection/state") == "ON"
        assert f"{prefix}/+/set" in broker.subscribed

        # A push reaches the state topic.
        await publish_entity_state(key="camera.1.detection", camera_id=1,
                                   required_scope="cameras.manage",
                                   payload={"key": "camera.1.detection", "state": False,
                                            "attributes": {}})
        await _until(lambda: broker.last(f"{prefix}/camera.1.detection/state") == "OFF",
                     what="the detection switch to publish OFF",
                     show=lambda: broker.last(f"{prefix}/camera.1.detection/state"))
        # An event reaches the event topic as a CloudEvent.
        await publish_entity_state(key="camera.1.detections", camera_id=1,
                                   required_scope="cameras.view",
                                   payload={"key": "camera.1.detections",
                                            "event": {"type": "person", "attributes": {}}})
        await _until(lambda: broker.last(f"{prefix}/camera.1.detections/event") is not None,
                     what="a detection event on the camera's event topic",
                     show=lambda: sorted({t for t, _p, _r in broker.published}))
        ce = json.loads(broker.last(f"{prefix}/camera.1.detections/event"))
        assert ce["specversion"] == "1.0" and ce["data"]["event_type"] == "person"

        # A command runs as the token.
        broker.send(f"{prefix}/camera.1.detection/set", "OFF")
        s = env.Session()
        try:
            await _until(lambda: s.get(env.models.Camera, 1).detection_enabled is False
                         or s.expire_all())
        finally:
            s.close()
        # Home Assistant came back: discovery again.
        before = len([t for t in broker.published if t[0].endswith("/config")])
        broker.send("homeassistant/status", "online")
        await _until(lambda: len([t for t in broker.published
                                  if t[0].endswith("/config")]) > before)
        # A camera the token can't see is refused (nothing changes).
        broker.send(f"{prefix}/camera.2.detection/set", "OFF")
        # A RETAINED command is never run (it would run again at every connect).
        broker.send(f"{prefix}/camera.1.detection/set", "ON", retain=True)
        await asyncio.sleep(0.2)
        s = env.Session()
        try:
            assert s.get(env.models.Camera, 1).detection_enabled is False
        finally:
            s.close()

        # Revoked: the session ends and the site goes offline.
        env.client.delete(f"/api/v1/api-tokens/{minted['id']}", headers=env.jwt("admin"))
        await _until(lambda: broker.last(site["avty_t"]) == "offline",
                     what="the site availability topic to go offline",
                     show=lambda: broker.last(site["avty_t"]))
        await bridge.stop()
        await asyncio.wait_for(task, 5)
        return prefix

    asyncio.run(scenario())
    s = env.Session()
    try:
        assert s.get(env.models.Camera, 2).detection_enabled is not False
        rows = s.query(env.models.AuditLog).filter_by(action="entity.command").all()
        details = [json.loads(r.details) if isinstance(r.details, str) else r.details
                   for r in rows]
        assert rows and all(d.get("actor") == "mqtt:ha-mqtt" for d in details)
        assert all(d["key"] == "camera.1.detection" for d in details)   # not camera 2
    finally:
        s.close()


def test_manager_keeps_unchanged_bridges_and_clears_removed_ones(bridge_env):
    env, broker = bridge_env
    minted = _mint(env, scopes=["settings.view", "cameras.view"], camera_ids=[1])
    s = env.Session()
    try:
        row = env.models.Integration(
            name="ha-mqtt", type=env.models.IntegrationType.MQTT, enabled=True,
            config={"broker_url": "mqtt://broker.lan", "api_token_id": minted["id"]})
        s.add(row)
        s.commit()
        iid = row.id
    finally:
        s.close()

    def configs():
        return {t: p for t, p, _ in broker.published if t.endswith("/config")}

    async def scenario():
        manager = hd._Manager()
        await manager.reload()
        await _until(lambda: any(p == "online" for t, p, _ in broker.published
                                 if t.endswith("/status")))
        await _until(lambda: configs(), what="a discovery config to be published",
                     show=lambda: sorted(configs()))
        first = manager._bridges[iid][0]
        # Nothing changed: the same bridge keeps running (no offline blip).
        await manager.reload()
        assert manager._bridges[iid][0] is first
        assert broker.last(next(t for t, _, _ in broker.published
                                if t.endswith("/status"))) == "online"
        # Deleted: its devices are removed from Home Assistant.
        # The running bridge reads the DB from worker threads, and on this
        # StaticPool every session shares ONE sqlite connection: a thread's
        # close() is a ROLLBACK that can undo our DELETE between its flush
        # and commit (seen on CI: the row survived, reload kept the bridge).
        # Production sessions have their own connections; retry until the
        # delete sticks.
        for _ in range(20):
            s = env.Session()
            try:
                row = s.get(env.models.Integration, iid)
                if row is None:
                    break
                s.delete(row)
                s.commit()
            finally:
                s.close()
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the integration row could not be deleted")
        await manager.reload()
        assert iid not in manager._bridges
        # Every discovery config, state and the status: cleared on the broker.
        assert not any(k.startswith(("homeassistant/", "opennvr/")) for k in broker.retained)
        assert configs() and all(p is None for p in configs().values())

    asyncio.run(scenario())


def test_deleting_clears_even_when_the_bridge_is_not_connected(bridge_env):
    """Revoke the token first (the bridge idles), then delete: still cleared."""
    env, broker = bridge_env
    broker.retained.update({
        "homeassistant/device/opennvr_SITE_camera_1/config": "{}",
        "homeassistant/device/other_nvr_camera_1/config": "{}",      # not ours: kept
        "opennvr/SITE/camera.1.detection/state": "ON",
        "opennvr/SITE/status": "offline"})
    settings = MqttSettings.from_config(TOKENISH)

    async def scenario():
        import services.site_settings as ss

        real = ss.get_site_id
        ss.get_site_id = lambda db: "SITE"
        try:
            return await hd.clear_published(settings)
        finally:
            ss.get_site_id = real

    assert asyncio.run(scenario()) == 3
    assert list(broker.retained) == ["homeassistant/device/other_nvr_camera_1/config"]


def test_reloads_do_not_overlap(bridge_env, monkeypatch):
    env, broker = bridge_env
    started: list[int] = []

    def wanted():
        return [(1, "m", {"broker_url": "mqtt://b", "api_token_id": 1})]

    monkeypatch.setattr(hd, "_wanted_integrations", wanted)

    async def run(self):
        started.append(self.integration_id)
        await self._stop.wait()

    monkeypatch.setattr(hd.MqttBridge, "run", run)

    async def scenario():
        manager = hd._Manager()
        await asyncio.gather(manager.reload(), manager.reload(), manager.reload())
        await asyncio.sleep(0.05)
        assert len(manager._bridges) == 1 and started == [1]
        await manager.stop()

    asyncio.run(scenario())


def _snap(descs, held=("cameras.view",)):
    return {"site_id": "0755a940-1ff5-4861-ac08-1f57bb29a180", "descriptors": descs,
            "names": {}, "held": list(held), "cameras": None, "event_types": None,
            "site_mode": {"mode": "disarmed"}}


def test_vanished_components_and_devices_are_removed(bridge_env, monkeypatch):
    env, broker = bridge_env
    from services import entity_state_publisher as pub

    async def warm():
        return None

    monkeypatch.setattr(pub, "warm", warm)
    monkeypatch.setattr(pub, "current_states", lambda: {})
    cam2 = {"kind": "camera", "id": 2}
    first = DESCS + [_d("camera.2.motion", "binary_sensor", "Motion", cam2)]
    second = [d for d in DESCS if d["key"] != "camera.1.detection"]

    async def scenario():
        bridge = hd.MqttBridge(1, "m", TOKENISH)
        mq = broker.client(None)
        await bridge._publish_all(mq, TOPICS, _snap(first), "1")
        broker.published.clear()
        await bridge._publish_all(mq, TOPICS, _snap(second), "1")

    asyncio.run(scenario())
    got = {t: p for t, p, _ in broker.published}
    cam1 = json.loads(got[TOPICS.device_config("camera_1")])
    # The switch is removed as a SWITCH (its old platform), not a sensor.
    assert cam1["cmps"]["opennvr_camera_1_detection"] == {"p": "switch"}
    assert got[TOPICS.device_config("camera_2")] is None             # the whole device
    for key in ("camera.1.detection", "camera.2.motion"):            # no stale values
        assert got[TOPICS.state(key)] is None and got[TOPICS.attributes(key)] is None
    assert TOPICS.state("camera.1.motion") not in got


def test_site_mode_is_a_sensor_without_settings_manage():
    view = hd.build_discovery(TOPICS, "1", [], {}, site_mode=True, site_mode_control=False)
    comp = view["site"]["cmps"]["opennvr_site_mode_state"]
    assert comp["p"] == "sensor" and "cmd_t" not in comp
    assert "opennvr_site_mode" not in view["site"]["cmps"]


def test_free_text_app_ids_stay_one_topic_level():
    assert hd.topic_key("app.x+y/#.count") == "app.x_y__.count"
    assert TOPICS.state("app.a+b.count") == "opennvr/0755a9401ff5/app.a_b.count/state"
    bridge = hd.MqttBridge(1, "m", TOKENISH)
    bridge._platform = {"app.a+b.gate": "switch"}
    bridge._by_topic = {hd.topic_key(k): k for k in bridge._platform}
    assert bridge._by_topic[TOPICS.key_of_command(TOPICS.command("app.a+b.gate"))] == \
        "app.a+b.gate"


def test_commands_are_rate_limited_and_never_end_the_session(monkeypatch):
    monkeypatch.setattr(hd, "COMMAND_LIMIT", 2)
    ran: list[str] = []

    async def scenario():
        bridge = hd.MqttBridge(1, "m", TOKENISH)

        async def run_command(key, payload):
            ran.append(key)
            raise RuntimeError("database gone")        # anything at all

        bridge._run_command = run_command
        worker = asyncio.create_task(bridge._command_worker())
        for i in range(5):
            bridge._commands.put_nowait((f"k{i}", "ON"))
        await asyncio.sleep(0.1)
        assert not worker.done()                        # the error didn't end it
        worker.cancel()

    asyncio.run(scenario())
    assert ran == ["k0", "k1"]


def test_bad_payloads_cost_no_database(monkeypatch):
    import core.database as cdb

    def no_db():
        raise AssertionError("touched the database")

    monkeypatch.setattr(cdb, "SessionLocal", no_db)
    bridge = hd.MqttBridge(1, "m", TOKENISH)
    bridge._platform = {"camera.1.detection": "switch"}

    async def scenario():
        with pytest.raises(ValueError):
            await bridge._run_command("camera.1.detection", "MAYBE")
        with pytest.raises(ValueError):
            await bridge._run_command(hd.SITE_MODE_KEY, "PANIC")
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            await bridge._run_command("camera.9.detection", "ON")

    asyncio.run(scenario())


def test_the_command_path_rechecks_the_camera(bridge_env):
    """Even a key the bridge knows is refused for a camera outside the
    token's allow-list: run_command checks, not only the catalogue."""
    env, broker = bridge_env
    minted = _mint(env, scopes=["cameras.view", "cameras.manage"], camera_ids=[1])
    bridge = hd.MqttBridge(1, "m", {"broker_url": "mqtt://b", "api_token_id": minted["id"]})
    bridge._platform = {"camera.2.detection": "switch"}
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        asyncio.run(bridge._run_command("camera.2.detection", "OFF"))
    assert err.value.status_code in (403, 404)
    s = env.Session()
    try:
        assert s.get(env.models.Camera, 2).detection_enabled is not False
    finally:
        s.close()


def test_address_restricted_tokens_cannot_be_bound(bridge_env):
    env, _ = bridge_env
    minted = _mint(env, allowed_cidrs=["192.168.1.10/32"])
    bridge = hd.MqttBridge(1, "m", {"broker_url": "mqtt://b", "api_token_id": minted["id"]})
    assert bridge._token_alive() is False


def test_booleans_and_failures_are_read_strictly():
    assert MqttSettings.from_config({**TOKENISH, "tls_insecure": "false"}).tls_insecure is False
    assert MqttSettings.from_config({**TOKENISH, "ha_discovery": "false"}).ha_discovery is False
    with pytest.raises(ValueError, match="true or false"):
        MqttSettings.from_config({**TOKENISH, "tls_insecure": "perhaps"})
    from services.mqtt_settings import failure

    assert failure(OSError("[Errno 111] Connection refused")) == "the connection was refused"
    assert failure(Exception("CONNACK: Not authorized")).startswith("the broker refused")
    assert failure(Exception("[Errno 13] 10.0.0.5:22 odd")) == "the broker could not be reached"


def test_a_name_resolving_to_metadata_is_refused(monkeypatch):
    import importlib

    ms = importlib.reload(importlib.import_module("services.mqtt_settings"))

    async def scenario():
        loop = asyncio.get_running_loop()

        async def fake(host, port, **kw):
            return [(2, 1, 6, "", ("169.254.169.254", 0))]

        monkeypatch.setattr(loop, "getaddrinfo", fake)
        with pytest.raises(ValueError, match="metadata"):
            await ms.check_resolved("evil.example")

    asyncio.run(scenario())


def test_restart_in_place_keeps_discovery():
    a = hd.MqttBridge(1, "x", TOKENISH)
    assert not hd._moved(a, hd.MqttBridge(1, "renamed", {**TOKENISH, "username": "u"}))
    assert hd._moved(a, hd.MqttBridge(1, "x", {**TOKENISH, "discovery_prefix": "ha"}))
    assert hd._moved(a, hd.MqttBridge(1, "x", {**TOKENISH, "api_token_id": 2}))


def test_integration_api_validates_and_tests_mqtt(env, monkeypatch):  # noqa: F811
    import importlib

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import services.mqtt_settings as ms

    broker = FakeBroker()
    monkeypatch.setattr(ms, "client", broker.client)
    reloads = []
    monkeypatch.setattr(importlib.import_module("routers.integrations"), "_reload_mqtt",
                        lambda: reloads.append(1))
    app = FastAPI()
    app.include_router(importlib.import_module("routers.integrations").router, prefix="/api/v1")
    from core.database import get_db

    app.dependency_overrides[get_db] = lambda: (yield env.Session())
    from core.auth import get_current_superuser

    app.dependency_overrides[get_current_superuser] = lambda: None
    client = TestClient(app)
    bad = client.post("/api/v1/integrations", json={
        "name": "m", "type": "mqtt", "enabled": True,
        "config": {"broker_url": "mqtt://b", "api_token_id": 999}})
    assert bad.status_code == 422 and "api_token_id" in bad.text
    fenced = _mint(env, name="fenced", allowed_cidrs=["10.0.0.0/8"])
    bad = client.post("/api/v1/integrations", json={
        "name": "m", "type": "mqtt", "enabled": True,
        "config": {"broker_url": "mqtt://b", "api_token_id": fenced["id"]}})
    assert bad.status_code == 422 and "addresses" in bad.text
    token = _mint(env)
    ok = client.post("/api/v1/integrations", json={
        "name": "m", "type": "mqtt", "enabled": True,
        "config": {"broker_url": "mqtt://b", "api_token_id": token["id"]}})
    assert ok.status_code == 200 and reloads == [1]
    tested = client.post(f"/api/v1/integrations/{ok.json()['id']}/test")
    assert tested.status_code == 200 and broker.published[0][0] == "opennvr/test"


def test_alerts_ride_the_live_bridge_instead_of_a_connection_each(bridge_env, monkeypatch):
    """Alert delivery to an MQTT integration used to connect, publish and
    disconnect per alert — thousands a day on a busy site — while the
    discovery bridge held a session to the same broker. It rides the
    bridge when one is connected, and falls back to a one-off connection
    only when none is."""
    env, broker = bridge_env
    minted = _mint(env, scopes=["settings.view", "cameras.view"], camera_ids=[1])
    config = {"broker_url": "mqtt://broker.lan", "api_token_id": minted["id"]}

    async def scenario():
        manager = hd._Manager()
        bridge = hd.MqttBridge(7, "ha-mqtt", config)
        # No bridge connected yet: the caller must fall back to a one-off.
        assert manager.offer(7, "alerts", "{}") is False
        task = asyncio.create_task(bridge.run())
        manager._bridges[7] = (bridge, task)
        await _until(lambda: bridge.state == "connected", what="the bridge to connect")
        assert manager.offer(7, "alerts", json.dumps({"subject": "s"})) is True
        await _until(lambda: any(t == "opennvr/alerts" for t, _, _ in broker.published),
                     what="the alert to be published on the bridge's session")
        await bridge.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
