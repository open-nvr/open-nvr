"""The descriptor-driven platforms, and entities/devices following the catalogue."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from pyopennvr import EntityCatalog, OpenNVRAuthError, SignedMedia
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.opennvr.const import CONF_CAMERAS, DOMAIN

from . import SITE_ID, URL, create_mock_config_entry, setup_mock_config_entry
from .conftest import FakeStream

CAM1 = {"kind": "camera", "id": 1}


def _d(key: str, platform: str, name: str, device=None, **extra: Any) -> dict[str, Any]:
    device = device or CAM1
    base = {"key": key, "platform": platform, "name": name, "device": device,
            "required_scope": "cameras.view", "origin": "core", "enabled_default": True,
            "descriptor_version": 1}
    if device.get("kind") == "camera":
        base["camera_id"] = device["id"]
    elif device.get("kind") == "zone":
        base["camera_id"] = device["camera_id"]
    return {**base, **extra}


ZONE = {"kind": "zone", "id": 7, "camera_id": 1, "name": "Driveway"}
APP = {"kind": "app", "id": "abandoned-object", "name": "Abandoned object"}

CATALOG = [
    _d("camera.1.count.person", "sensor", "person count", state_class="measurement"),
    _d("site.alerts_highest_severity", "sensor", "Highest open alert severity",
       {"kind": "site", "id": "site"}, device_class="enum",
       options=["none", "low", "medium", "high", "critical"]),
    _d("site.cpu", "sensor", "CPU", {"kind": "site", "id": "site"}, unit="%",
       entity_category="diagnostic", enabled_default=False, device_class="not-a-class"),
    _d("camera.1.motion", "binary_sensor", "Motion", device_class="motion"),
    _d("zone.7.occupancy.person", "binary_sensor", "person occupancy", ZONE,
       device_class="occupancy"),
    _d("camera.1.detection", "switch", "Object detection",
       command={"type": "core_control", "control": "detection"}),
    _d("camera.1.ptz_preset", "select", "PTZ preset", options=["Home", "Gate"],
       command={"type": "core_control", "control": "ptz_preset"}),
    _d("camera.1.ptz_up", "button", "PTZ up",
       command={"type": "core_control", "control": "ptz_move", "args": {"direction": "up"}}),
    _d("app.abandoned-object.dwell", "number", "Dwell threshold", APP, unit="s",
       options={"min": 10, "max": 600, "step": 5},
       command={"type": "app_action", "action": "set_dwell"}),
    _d("camera.1.detections", "event", "Detection", event_types=["person", "car"]),
    _d("camera.1.last_object", "image", "Last object"),
    _d("x.future", "lock", "From the future", {"kind": "site", "id": "site"}),
]
STATES = {
    "camera.1.count.person": {"state": 2, "attributes": {}},
    "site.alerts_highest_severity": {"state": "high", "attributes": {}},
    "site.cpu": {"state": 12.5, "attributes": {}},
    "camera.1.motion": {"state": True, "attributes": {}},
    "zone.7.occupancy.person": {"state": False, "attributes": {}},
    "camera.1.detection": {"state": True, "attributes": {}},
    "camera.1.ptz_preset": {"state": "Home", "attributes": {}},
    "app.abandoned-object.dwell": {"state": 60, "attributes": {}},
    "camera.1.last_object": {"state": "2026-09-18T10:00:00+00:00",
                             "attributes": {"event_id": 42, "label": "car",
                                            "image": "evidence"}},
}


@pytest.fixture
def catalog_client(mock_client: MagicMock) -> MagicMock:
    mock_client.get_entities.return_value = EntityCatalog.from_dict(
        {"etag": "e1", "entities": CATALOG})
    mock_client.get_entity_states.return_value = {k: dict(v) for k, v in STATES.items()}
    return mock_client


async def _setup(hass: HomeAssistant, **kwargs):
    entry = create_mock_config_entry(**kwargs)
    await setup_mock_config_entry(hass, entry)
    return entry


def _id(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        _platform(key), DOMAIN, f"{SITE_ID}:{key}")


def _platform(key: str) -> str:
    return next(d["platform"] for d in CATALOG if d["key"] == key)


async def test_every_platform_renders(hass: HomeAssistant, catalog_client: MagicMock,
                                      mock_stream: type[FakeStream]) -> None:
    await _setup(hass)
    assert hass.states.get(_id(hass, "camera.1.count.person")).state == "2"
    severity = hass.states.get(_id(hass, "site.alerts_highest_severity"))
    assert severity.state == "high" and severity.attributes["options"][0] == "none"
    assert hass.states.get(_id(hass, "camera.1.motion")).state == STATE_ON
    assert hass.states.get(_id(hass, "zone.7.occupancy.person")).state == STATE_OFF
    assert hass.states.get(_id(hass, "camera.1.detection")).state == STATE_ON
    assert hass.states.get(_id(hass, "camera.1.ptz_preset")).state == "Home"
    assert hass.states.get(_id(hass, "camera.1.ptz_up")) is not None
    dwell = hass.states.get(_id(hass, "app.abandoned-object.dwell"))
    assert dwell.state == "60.0" and dwell.attributes["max"] == 600
    assert hass.states.get(_id(hass, "camera.1.detections")).state == STATE_UNKNOWN
    assert hass.states.get(_id(hass, "camera.1.last_object")).state.startswith("2026-09-18")
    # Unknown platforms are skipped, never an error.
    assert er.async_get(hass).async_get_entity_id("lock", DOMAIN, f"{SITE_ID}:x.future") is None
    # Presentation from the descriptor; unknown device class ignored.
    cpu = er.async_get(hass).async_get(_id(hass, "site.cpu"))
    assert cpu.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert cpu.entity_category == "diagnostic" and cpu.original_device_class is None


async def test_devices(hass: HomeAssistant, catalog_client: MagicMock,
                       mock_stream: type[FakeStream], caplog: pytest.LogCaptureFixture) -> None:
    entry = await _setup(hass)
    # HA 2026.9 deprecates DeviceInfo.via_device; parents are linked by id.
    assert "deprecated" not in caplog.text
    reg = dr.async_get(hass)
    site = reg.async_get_device_by_identifier((DOMAIN, SITE_ID), entry.entry_id)
    cam = reg.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}:camera:1"), entry.entry_id)
    zone = reg.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}:zone:7"), entry.entry_id)
    app = reg.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}:app:abandoned-object"),
                                             entry.entry_id)
    assert cam.name == "Front door" and cam.via_device_id == site.id
    assert zone.name == "Driveway" and zone.via_device_id == cam.id
    assert app.via_device_id == site.id
    assert er.async_get(hass).async_get(_id(hass, "zone.7.occupancy.person")).device_id == zone.id


async def test_pushed_state(hass: HomeAssistant, catalog_client: MagicMock,
                            mock_stream: type[FakeStream]) -> None:
    await _setup(hass)
    [stream] = mock_stream.instances
    stream.on_frame({"v": 2, "seq": 1, "event_type": "entity_state",
                     "payload": {"key": "camera.1.count.person", "state": 5,
                                 "attributes": {"active": 1}}})
    await hass.async_block_till_done()
    state = hass.states.get(_id(hass, "camera.1.count.person"))
    assert state.state == "5" and state.attributes["active"] == 1
    # An enum value the descriptor didn't announce reads as unknown.
    stream.on_frame({"v": 2, "seq": 2, "event_type": "entity_state",
                     "payload": {"key": "site.alerts_highest_severity", "state": "apocalyptic"}})
    await hass.async_block_till_done()
    assert hass.states.get(_id(hass, "site.alerts_highest_severity")).state == STATE_UNKNOWN


async def test_events_fire(hass: HomeAssistant, catalog_client: MagicMock,
                           mock_stream: type[FakeStream]) -> None:
    await _setup(hass)
    [stream] = mock_stream.instances
    entity_id = _id(hass, "camera.1.detections")
    stream.on_frame({"v": 2, "seq": 1, "event_type": "entity_state", "payload": {
        "key": "camera.1.detections",
        "event": {"type": "person", "attributes": {"track_id": "9", "zones": [7]}}}})
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.attributes["event_type"] == "person"
    assert state.attributes["track_id"] == "9" and state.attributes["zones"] == [7]
    fired = state.state
    stream.on_frame({"v": 2, "seq": 2, "event_type": "entity_state", "payload": {
        "key": "camera.1.detections", "event": {"type": "unicorn"}}})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == fired          # unannounced type dropped


async def test_commands(hass: HomeAssistant, catalog_client: MagicMock,
                        mock_stream: type[FakeStream]) -> None:
    await _setup(hass)
    cmd = catalog_client.command_entity

    await hass.services.async_call("switch", "turn_off",
                                   {ATTR_ENTITY_ID: _id(hass, "camera.1.detection")},
                                   blocking=True)
    assert cmd.call_args.args == ("camera.1.detection", False)
    assert cmd.call_args.kwargs["correlation_id"]
    assert hass.states.get(_id(hass, "camera.1.detection")).state == STATE_OFF

    await hass.services.async_call("select", "select_option",
                                   {ATTR_ENTITY_ID: _id(hass, "camera.1.ptz_preset"),
                                    "option": "Gate"}, blocking=True)
    assert cmd.call_args.args == ("camera.1.ptz_preset", "Gate")

    await hass.services.async_call("button", "press",
                                   {ATTR_ENTITY_ID: _id(hass, "camera.1.ptz_up")},
                                   blocking=True)
    assert cmd.call_args.args == ("camera.1.ptz_up", None)

    await hass.services.async_call("number", "set_value",
                                   {ATTR_ENTITY_ID: _id(hass, "app.abandoned-object.dwell"),
                                    "value": 90}, blocking=True)
    assert cmd.call_args.args == ("app.abandoned-object.dwell", 90)

    cmd.side_effect = OpenNVRAuthError("lacks ptz.control", 403)
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call("button", "press",
                                       {ATTR_ENTITY_ID: _id(hass, "camera.1.ptz_up")},
                                       blocking=True)
    assert exc.value.translation_key == "not_permitted"


async def test_image(hass: HomeAssistant, catalog_client: MagicMock,
                     mock_stream: type[FakeStream],
                     aioclient_mock: AiohttpClientMocker) -> None:
    await _setup(hass)
    catalog_client.sign_media.return_value = SignedMedia(
        url=f"{URL}/api/v1/media/s/m1.abc", expires_at="2026-09-18T10:02:00+00:00")
    aioclient_mock.get(f"{URL}/api/v1/media/s/m1.abc", content=b"\xff\xd8car",
                       headers={"Content-Type": "image/jpeg"})
    from homeassistant.components.image import async_get_image

    image = await async_get_image(hass, _id(hass, "camera.1.last_object"))
    assert image.content == b"\xff\xd8car"
    catalog_client.sign_media.assert_called_with("event", id=42, name="evidence", ttl_s=120)
    await async_get_image(hass, _id(hass, "camera.1.last_object"))
    assert aioclient_mock.call_count == 1                        # same event: cached


async def test_catalogue_changes_add_and_remove(
        hass: HomeAssistant, catalog_client: MagicMock, mock_stream: type[FakeStream],
        freezer: FrozenDateTimeFactory) -> None:
    entry = await _setup(hass)
    reg = dr.async_get(hass)
    assert reg.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}:zone:7"), entry.entry_id)
    # The zone was deleted and a label added.
    newer = [d for d in CATALOG if not d["key"].startswith("zone.7")] + [
        _d("camera.1.count.car", "sensor", "car count")]
    catalog_client.get_entities.return_value = EntityCatalog.from_dict(
        {"etag": "e2", "entities": newer})
    [stream] = mock_stream.instances
    stream.on_frame({"v": 2, "seq": 1, "event_type": "descriptors_changed",
                     "payload": {"etag": "e2"}})
    freezer.tick(15)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{SITE_ID}:camera.1.count.car")
    zone_uid = f"{SITE_ID}:zone.7.occupancy.person"
    # Gone from the catalogue: kept (unavailable) for a few refreshes first.
    assert ent_reg.async_get_entity_id("binary_sensor", DOMAIN, zone_uid)
    entry.runtime_data.coordinator.async_add_listener(lambda: None)
    for _ in range(3):
        freezer.tick(31)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert ent_reg.async_get_entity_id("binary_sensor", DOMAIN, zone_uid) is None
    assert reg.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}:zone:7"),
                                              entry.entry_id) is None


async def test_deselected_camera_is_cleaned_up(
        hass: HomeAssistant, catalog_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    entry = await _setup(hass)
    assert _id(hass, "camera.1.motion")
    hass.config_entries.async_update_entry(entry, options={CONF_CAMERAS: [3]})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _id(hass, "camera.1.motion") is None
    assert _id(hass, "site.cpu") is not None
    assert dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{SITE_ID}:camera:1"), entry.entry_id) is None


async def test_outage_keeps_entities(hass: HomeAssistant, catalog_client: MagicMock,
                                     mock_stream: type[FakeStream],
                                     freezer: FrozenDateTimeFactory) -> None:
    from pyopennvr import OpenNVRConnectionError

    await _setup(hass)
    catalog_client.get_cameras.side_effect = OpenNVRConnectionError("down")
    freezer.tick(31)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get(_id(hass, "camera.1.motion"))
    assert state.state == "unavailable"
    assert _id(hass, "camera.1.motion") is not None


async def test_user_may_remove_a_stale_device(
        hass: HomeAssistant, catalog_client: MagicMock, mock_stream: type[FakeStream],
        hass_ws_client) -> None:
    from pytest_homeassistant_custom_component.common import async_setup_component

    entry = await _setup(hass)
    assert await async_setup_component(hass, "config", {})
    reg = dr.async_get(hass)
    live = reg.async_get_device_by_identifier((DOMAIN, f"{SITE_ID}:camera:1"), entry.entry_id)
    stale = reg.async_get_or_create(config_entry_id=entry.entry_id,
                                    identifiers={(DOMAIN, f"{SITE_ID}:camera:99")})
    client = await hass_ws_client(hass)
    for device, ok in ((live, False), (stale, True)):
        await client.send_json_auto_id({"type": "config/device_registry/remove_config_entry",
                                        "config_entry_id": entry.entry_id,
                                        "device_id": device.id})
        assert (await client.receive_json())["success"] is ok



async def test_a_briefly_missing_descriptor_keeps_its_entity(
        hass: HomeAssistant, catalog_client: MagicMock, mock_stream: type[FakeStream],
        freezer: FrozenDateTimeFactory) -> None:
    """After an OpenNVR restart, PTZ presets appear only once the camera
    answered: the select must come back as the SAME entity (id, name, area)."""
    entry = await _setup(hass)
    entry.runtime_data.coordinator.async_add_listener(lambda: None)
    ent_reg = er.async_get(hass)
    uid = f"{SITE_ID}:camera.1.ptz_preset"
    entity_id = ent_reg.async_get_entity_id("select", DOMAIN, uid)
    ent_reg.async_update_entity(entity_id, name="Gate camera preset")
    without = [d for d in CATALOG if d["key"] != "camera.1.ptz_preset"]
    catalog_client.get_entities.return_value = EntityCatalog.from_dict(
        {"etag": "e2", "entities": without})
    freezer.tick(31)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    catalog_client.get_entities.return_value = EntityCatalog.from_dict(
        {"etag": "e3", "entities": CATALOG})
    freezer.tick(31)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    entry_now = ent_reg.async_get(entity_id)
    assert entry_now is not None and entry_now.name == "Gate camera preset"


async def test_a_pruned_descriptor_that_returns_gets_its_entity_back(
        hass: HomeAssistant, catalog_client: MagicMock, mock_stream: type[FakeStream],
        freezer: FrozenDateTimeFactory) -> None:
    """Gone for longer than the grace period, the entity is pruned; when the
    descriptor then reappears (a camera back online after a long outage)
    the entity must be created again, not wait for a restart of HA."""
    entry = await _setup(hass)
    entry.runtime_data.coordinator.async_add_listener(lambda: None)
    ent_reg = er.async_get(hass)
    uid = f"{SITE_ID}:camera.1.ptz_preset"
    assert ent_reg.async_get_entity_id("select", DOMAIN, uid)
    without = [d for d in CATALOG if d["key"] != "camera.1.ptz_preset"]
    catalog_client.get_entities.return_value = EntityCatalog.from_dict(
        {"etag": "e2", "entities": without})
    for _ in range(3):
        freezer.tick(31)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert ent_reg.async_get_entity_id("select", DOMAIN, uid) is None      # pruned
    catalog_client.get_entities.return_value = EntityCatalog.from_dict(
        {"etag": "e3", "entities": CATALOG})
    freezer.tick(31)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    entity_id = ent_reg.async_get_entity_id("select", DOMAIN, uid)
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "Home"


async def test_catalogue_changes_reach_live_entities(
        hass: HomeAssistant, catalog_client: MagicMock, mock_stream: type[FakeStream],
        freezer: FrozenDateTimeFactory) -> None:
    entry = await _setup(hass)
    entry.runtime_data.coordinator.async_add_listener(lambda: None)
    [stream] = mock_stream.instances
    newer = [({**d, "event_types": ["person", "car", "bicycle"]}
              if d["key"] == "camera.1.detections" else d) for d in CATALOG]
    catalog_client.get_entities.return_value = EntityCatalog.from_dict(
        {"etag": "e2", "entities": newer})
    freezer.tick(31)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    stream.on_frame({"v": 2, "seq": 1, "event_type": "entity_state", "payload": {
        "key": "camera.1.detections", "event": {"type": "bicycle"}}})
    await hass.async_block_till_done()
    state = hass.states.get(_id(hass, "camera.1.detections"))
    assert state.attributes["event_type"] == "bicycle"
    assert "bicycle" in state.attributes["event_types"]


async def test_sensor_values_fit_their_kind(hass: HomeAssistant, catalog_client: MagicMock,
                                            mock_stream: type[FakeStream]) -> None:
    extra = [
        _d("app.x.last_seen", "sensor", "Last seen", {"kind": "site", "id": "site"},
           device_class="timestamp"),
        _d("app.x.dwell", "sensor", "Dwell", {"kind": "site", "id": "site"},
           state_class="measurement", unit="s"),
        _d("app.x.note", "sensor", "Note", {"kind": "site", "id": "site"}),
    ]
    catalog_client.get_entities.return_value = EntityCatalog.from_dict(
        {"etag": "e1", "entities": CATALOG + extra})
    catalog_client.get_entity_states.return_value = {
        **{k: dict(v) for k, v in STATES.items()},
        "app.x.last_seen": {"state": "2026-09-18T10:00:00", "attributes": {}},
        "app.x.dwell": {"state": "not a number", "attributes": {}},
        "app.x.note": {"state": "x" * 400, "attributes": {}},
    }
    await _setup(hass)
    ent = er.async_get(hass)
    get = lambda key: hass.states.get(ent.async_get_entity_id(  # noqa: E731
        "sensor", DOMAIN, f"{SITE_ID}:{key}")).state
    assert get("app.x.last_seen") == "2026-09-18T10:00:00+00:00"
    assert get("app.x.dwell") == STATE_UNKNOWN
    assert len(get("app.x.note")) == 255
    # And pushing a bad value keeps the stream (and other entities) alive.
    [stream] = mock_stream.instances
    stream.on_frame({"v": 2, "seq": 1, "event_type": "entity_state",
                     "payload": {"key": "app.x.dwell", "state": {"nested": 1}}})
    stream.on_frame({"v": 2, "seq": 2, "event_type": "entity_state",
                     "payload": {"key": "camera.1.count.person", "state": 9}})
    await hass.async_block_till_done()
    assert hass.states.get(_id(hass, "camera.1.count.person")).state == "9"
