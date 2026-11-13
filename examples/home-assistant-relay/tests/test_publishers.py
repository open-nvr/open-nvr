# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the MQTT + REST publishers. Both backends are mocked
so no broker / HA instance is needed for unit tests."""
from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock

import pytest

from ha_mapper import HaEntity
from publishers import MqttConfig, MqttPublisher, RestConfig, RestPublisher


# ── Common fixture ────────────────────────────────────────────────


def _entity(component: str = "binary_sensor") -> HaEntity:
    return HaEntity(
        component=component,
        object_id="opennvr_front_porch_doorbell_visitor",
        name="Front-porch Doorbell",
        device_class="occupancy",
        state="ON" if component == "binary_sensor" else "ABC-1234",
        attributes={"alert_id": "alrt_1", "severity": "high"},
        auto_off_seconds=30,
    )


# ── MQTT publisher ────────────────────────────────────────────────


class _FakeMqttInfo:
    """Stand-in for paho's MQTTMessageInfo."""

    def __init__(self, rc: int = 0) -> None:
        self.rc = rc


class _FakeMqttClient:
    def __init__(self, *args, **kwargs) -> None:
        self.init_args = args
        self.init_kwargs = kwargs
        self.published: list[dict] = []
        self.connected = False
        self.username: tuple | None = None
        self.on_connect = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.proxy: dict | None = None

    def username_pw_set(self, user, pw):  # noqa: D401
        self.username = (user, pw)

    def proxy_set(self, **kwargs):  # noqa: D401 — paho + PySocks
        self.proxy = kwargs

    def connect(self, host, port, keepalive):  # noqa: D401
        self.connected = True
        if self.on_connect is not None:
            self.on_connect(self, None, {}, 0, None)

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def publish(self, topic, value, qos=0, retain=False):
        self.published.append({
            "topic": topic, "value": value, "qos": qos, "retain": retain,
        })
        return _FakeMqttInfo(rc=0)


@pytest.fixture
def fake_paho(monkeypatch):
    """Install a fake paho.mqtt.client module that records publishes."""
    fake_client_module = types.ModuleType("paho.mqtt.client")
    # CallbackAPIVersion enum — paho v2 requires this. Stub the
    # value the publisher passes.
    fake_client_module.CallbackAPIVersion = types.SimpleNamespace(
        VERSION2="v2"
    )
    fake_client_module.Client = _FakeMqttClient

    fake_paho_module = types.ModuleType("paho")
    fake_paho_mqtt_module = types.ModuleType("paho.mqtt")
    fake_paho_mqtt_module.client = fake_client_module
    fake_paho_module.mqtt = fake_paho_mqtt_module

    monkeypatch.setitem(sys.modules, "paho", fake_paho_module)
    monkeypatch.setitem(sys.modules, "paho.mqtt", fake_paho_mqtt_module)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", fake_client_module)
    # A developer shell may carry its own proxy variables; the broker
    # path under test is the direct one unless a test says otherwise.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    fake_socks = types.ModuleType("socks")
    fake_socks.HTTP = "HTTP"
    monkeypatch.setitem(sys.modules, "socks", fake_socks)


@pytest.mark.asyncio
async def test_mqtt_goes_through_the_egress_proxy_when_the_platform_sets_one(fake_paho, monkeypatch):
    """On the internal apps network the broker is reachable only via the
    egress proxy: paho is pointed at it as an HTTP CONNECT tunnel."""
    pub = MqttPublisher(MqttConfig(host="192.168.1.20", port=1883))
    assert await pub.publish_state(_entity()) is True
    assert pub._client.proxy is None                                  # no proxy set → direct

    monkeypatch.setenv("HTTPS_PROXY", "http://egress-proxy:3128")
    monkeypatch.setenv("NO_PROXY", "opennvr-core,nats,nats-apps,egress-proxy")
    pub = MqttPublisher(MqttConfig(host="192.168.1.20", port=1883))
    assert await pub.publish_state(_entity()) is True
    assert pub._client.proxy == {"proxy_type": "HTTP", "proxy_addr": "egress-proxy", "proxy_port": 3128}
    assert pub._client.connected

    pub = MqttPublisher(MqttConfig(host="nats", port=1883))          # NO_PROXY host: direct
    assert await pub.publish_state(_entity()) is True
    assert pub._client.proxy is None


@pytest.mark.asyncio
async def test_mqtt_first_publish_emits_discovery(fake_paho):
    pub = MqttPublisher(MqttConfig(host="x", port=1883))
    ok = await pub.publish_state(_entity())
    assert ok is True
    # Three publishes: discovery, state, attributes.
    client = pub._client
    topics = [p["topic"] for p in client.published]
    assert any("homeassistant/binary_sensor/" in t and t.endswith("/config") for t in topics)
    assert any(t.endswith("/state") for t in topics)
    assert any(t.endswith("/attributes") for t in topics)


@pytest.mark.asyncio
async def test_mqtt_second_publish_skips_discovery(fake_paho):
    """Discovery is published once per entity per process lifetime —
    a chatty alert stream shouldn't republish the config every time."""
    pub = MqttPublisher(MqttConfig(host="x"))
    await pub.publish_state(_entity())
    pub._client.published.clear()
    await pub.publish_state(_entity())
    topics = [p["topic"] for p in pub._client.published]
    assert not any(t.endswith("/config") for t in topics)
    assert any(t.endswith("/state") for t in topics)


@pytest.mark.asyncio
async def test_mqtt_discovery_payload_carries_device_class(fake_paho):
    pub = MqttPublisher(MqttConfig(host="x"))
    await pub.publish_state(_entity())
    discovery = next(
        p for p in pub._client.published if p["topic"].endswith("/config")
    )
    body = json.loads(discovery["value"])
    assert body["device_class"] == "occupancy"
    assert body["unique_id"] == "opennvr_front_porch_doorbell_visitor"
    assert body["payload_on"] == "ON"
    assert body["payload_off"] == "OFF"
    # Device card grouping — every entity goes under one OpenNVR
    # device. The identifier embeds the discovery_prefix so two
    # OpenNVR instances against one HA / broker don't collide.
    assert body["device"]["identifiers"] == ["opennvr_homeassistant"]


@pytest.mark.asyncio
async def test_mqtt_publish_off_uses_off_payload(fake_paho):
    pub = MqttPublisher(MqttConfig(host="x"))
    await pub.publish_state(_entity())
    pub._client.published.clear()
    await pub.publish_off(_entity())
    state_publishes = [
        p for p in pub._client.published if p["topic"].endswith("/state")
    ]
    assert state_publishes
    assert state_publishes[-1]["value"] == "OFF"


@pytest.mark.asyncio
async def test_mqtt_sensor_publish_off_writes_empty_string(fake_paho):
    """For sensor entities (e.g. plate text), publish_off clears the
    value rather than writing 'OFF' — HA renders that as 'unknown'."""
    pub = MqttPublisher(MqttConfig(host="x"))
    sensor = _entity("sensor")
    await pub.publish_state(sensor)
    pub._client.published.clear()
    await pub.publish_off(sensor)
    state_publishes = [
        p for p in pub._client.published if p["topic"].endswith("/state")
    ]
    assert state_publishes[-1]["value"] == ""


@pytest.mark.asyncio
async def test_mqtt_aclose_stops_loop_and_disconnects(fake_paho):
    pub = MqttPublisher(MqttConfig(host="x"))
    await pub.publish_state(_entity())
    client = pub._client
    await pub.aclose()
    assert client.loop_stopped is True
    assert client.disconnected is True
    assert pub._client is None


@pytest.mark.asyncio
async def test_mqtt_aclose_is_idempotent(fake_paho):
    pub = MqttPublisher(MqttConfig(host="x"))
    await pub.aclose()  # never connected — must not raise
    await pub.aclose()  # second close — also safe


@pytest.mark.asyncio
async def test_mqtt_username_is_set_when_configured(fake_paho):
    pub = MqttPublisher(MqttConfig(host="x", username="u", password="p"))
    await pub.publish_state(_entity())
    assert pub._client.username == ("u", "p")


# ── REST publisher ────────────────────────────────────────────────


class _FakeHttpResponse:
    def __init__(self, status_code: int = 200, text: str = "ok") -> None:
        self.status_code = status_code
        self.text = text


class _FakeAsyncHttpxClient:
    def __init__(self, *args, **kwargs) -> None:
        self.init_kwargs = kwargs
        self.calls: list[dict] = []
        self._response = _FakeHttpResponse()

    async def post(self, path, json=None):  # noqa: D401, A002
        self.calls.append({"path": path, "json": json})
        return self._response

    async def aclose(self):
        pass


@pytest.fixture
def fake_httpx(monkeypatch):
    """Patch httpx.AsyncClient inside the publishers module."""
    import publishers
    monkeypatch.setattr(
        publishers.httpx, "AsyncClient",
        lambda *a, **k: _FakeAsyncHttpxClient(*a, **k),
    )


@pytest.mark.asyncio
async def test_rest_publish_state_posts_to_states_endpoint(fake_httpx):
    pub = RestPublisher(RestConfig(url="http://ha:8123", token="tok"))
    ok = await pub.publish_state(_entity())
    assert ok is True
    call = pub._client.calls[0]
    assert call["path"] == "/api/states/binary_sensor.opennvr_front_porch_doorbell_visitor"
    assert call["json"]["state"] == "ON"
    assert call["json"]["attributes"]["friendly_name"] == "Front-porch Doorbell"
    assert call["json"]["attributes"]["device_class"] == "occupancy"


@pytest.mark.asyncio
async def test_rest_publish_off_writes_off(fake_httpx):
    pub = RestPublisher(RestConfig(url="http://ha:8123", token="tok"))
    await pub.publish_off(_entity())
    call = pub._client.calls[-1]
    assert call["json"]["state"] == "OFF"


@pytest.mark.asyncio
async def test_rest_4xx_returns_false(fake_httpx):
    pub = RestPublisher(RestConfig(url="http://ha:8123", token="tok"))
    # Force the next response to be a 401.
    await pub.publish_state(_entity())  # init client
    pub._client._response = _FakeHttpResponse(status_code=401, text="bad token")
    ok = await pub.publish_state(_entity())
    assert ok is False


@pytest.mark.asyncio
async def test_rest_aclose_clears_client(fake_httpx):
    pub = RestPublisher(RestConfig(url="http://ha:8123", token="tok"))
    await pub.publish_state(_entity())
    await pub.aclose()
    assert pub._client is None


@pytest.mark.asyncio
async def test_rest_aclose_on_never_used_client(fake_httpx):
    pub = RestPublisher(RestConfig(url="http://ha:8123", token="tok"))
    await pub.aclose()  # never built the client
