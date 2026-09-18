"""Setup, unload, and the coordinator's REST + push behaviour."""

from __future__ import annotations

from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from pyopennvr import OpenNVRAuthError, OpenNVRConnectionError
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.opennvr.const import CONF_CAMERAS, DOMAIN, signal_frame

from . import SITE_ID, create_mock_config_entry, setup_mock_config_entry, system_info
from .conftest import FakeStream


async def test_setup_and_unload(hass: HomeAssistant, mock_client: MagicMock,
                                mock_stream: type[FakeStream]) -> None:
    entry = create_mock_config_entry()
    assert await setup_mock_config_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    data = entry.runtime_data.coordinator.data
    assert data.info.site_id == SITE_ID and set(data.cameras) == {1, 3}
    assert data.states["camera.1.detection"]["state"] is True
    assert data.site_mode.mode == "armed_away"
    site = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, SITE_ID), entry.entry_id)
    assert site is not None and site.manufacturer == "OpenNVR"

    [stream] = mock_stream.instances
    assert "entity_state" in stream.types and "tracks" not in stream.types
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED and stream.stopped


@pytest.mark.parametrize(("error", "state"), [
    (OpenNVRConnectionError("down"), ConfigEntryState.SETUP_RETRY),
    (OpenNVRAuthError("revoked", 401), ConfigEntryState.SETUP_ERROR),
])
async def test_setup_failures(hass: HomeAssistant, mock_client: MagicMock,
                              mock_stream: type[FakeStream], error, state) -> None:
    mock_client.get_system_info.side_effect = error
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    assert entry.state is state
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert bool(flows) == isinstance(error, OpenNVRAuthError)
    if flows:
        assert flows[0]["context"]["source"] == SOURCE_REAUTH


async def test_newer_contract_major_is_refused(hass: HomeAssistant, mock_client: MagicMock,
                                               mock_stream: type[FakeStream]) -> None:
    mock_client.get_system_info.return_value = system_info(contract_version="2.0.0")
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "2.0.0" in (entry.reason or "")


async def test_a_different_server_at_the_url_is_refused(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    entry = create_mock_config_entry(unique_id="another-site")
    await setup_mock_config_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not mock_stream.instances


async def test_camera_selection_filters_descriptors(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    entry = create_mock_config_entry(options={CONF_CAMERAS: [3]})
    await setup_mock_config_entry(hass, entry)
    coordinator = entry.runtime_data.coordinator
    assert set(coordinator.data.cameras) == {3} and set(coordinator.data.all_cameras) == {1, 3}
    assert {d.camera_id for d in coordinator.descriptors()} == {3}


async def test_pushed_state_wakes_only_that_key(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    coordinator = entry.runtime_data.coordinator
    [stream] = mock_stream.instances
    got, others, everyone = [], [], []
    remove = coordinator.async_add_key_listener("camera.1.count.person", got.append)
    coordinator.async_add_key_listener("camera.1.detection", others.append)
    coordinator.async_add_listener(lambda: everyone.append(1))

    stream.on_frame({"v": 2, "seq": 5, "event_type": "entity_state",
                     "payload": {"key": "camera.1.count.person", "state": 3,
                                 "attributes": {"zone": "x"}}})
    assert got == [{"state": 3, "attributes": {"zone": "x"}}] and not others and not everyone
    assert coordinator.state_of("camera.1.count.person")["state"] == 3

    load = {"key": "camera.1.count.person",
            "event": {"type": "person", "attributes": {"track_id": "9"}}}
    stream.on_frame({"v": 2, "seq": 6, "event_type": "entity_state", "payload": load})
    assert got[-1] == {"event": load["event"]}
    assert coordinator.state_of("camera.1.count.person")["state"] == 3   # events aren't state

    remove()
    stream.on_frame({"v": 2, "seq": 7, "event_type": "entity_state",
                     "payload": {"key": "camera.1.count.person", "state": 0}})
    assert len(got) == 2


async def test_snapshot_and_site_mode_update_everyone(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    coordinator = entry.runtime_data.coordinator
    [stream] = mock_stream.instances
    calls = []
    coordinator.async_add_listener(lambda: calls.append(1))
    stream.on_frame({"v": 2, "seq": 1, "event_type": "state_snapshot", "resync": True,
                     "cameras": [], "site_mode": {"mode": "disarmed"},
                     "entity_states": {"camera.1.detection": {"state": False,
                                                              "attributes": {}}}})
    assert coordinator.data.states["camera.1.detection"]["state"] is False
    assert coordinator.data.states["camera.1.count.person"]["state"] == 1   # merged, kept
    assert coordinator.data.site_mode.mode == "disarmed"
    assert coordinator.data.site_mode.modes == ("disarmed", "armed_home", "armed_away")
    stream.on_frame({"v": 2, "seq": 2, "event_type": "site_mode",
                     "payload": {"mode": "armed_home", "changed_by": "token:x"}})
    assert coordinator.data.site_mode.mode == "armed_home" and len(calls) == 2


async def test_descriptors_changed_rereads_the_catalogue(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream],
        freezer: FrozenDateTimeFactory) -> None:
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    coordinator = entry.runtime_data.coordinator
    coordinator.async_add_listener(lambda: None)   # polls only while something listens
    [stream] = mock_stream.instances
    etag = coordinator.data.catalog.etag
    mock_client.get_entities.reset_mock()
    stream.on_frame({"v": 2, "seq": 3, "event_type": "descriptors_changed",
                     "payload": {"etag": etag}})
    await hass.async_block_till_done()
    assert not mock_client.get_entities.called          # same catalogue
    stream.on_frame({"v": 2, "seq": 4, "event_type": "descriptors_changed",
                     "payload": {"etag": "new"}})
    freezer.tick(15)   # past the refresh debouncer
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    mock_client.get_entities.assert_called_with(etag=etag)


async def test_lagged_rereads_everything(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream],
        freezer: FrozenDateTimeFactory) -> None:
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    coordinator = entry.runtime_data.coordinator
    coordinator.async_add_listener(lambda: None)
    [stream] = mock_stream.instances
    mock_client.get_entity_states.reset_mock()
    stream.on_frame({"v": 2, "event_type": "lagged", "dropped": 31})
    freezer.tick(15)   # past the refresh debouncer, short of the 30 s poll
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert mock_client.get_entity_states.called


async def test_catalogue_304_keeps_the_old_one(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream],
        freezer: FrozenDateTimeFactory) -> None:
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    coordinator = entry.runtime_data.coordinator
    coordinator.async_add_listener(lambda: None)   # polls only while something listens
    before = coordinator.data.catalog
    mock_client.get_entities.return_value = None
    freezer.tick(31)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.data.catalog is before and coordinator.last_update_success


async def test_other_frames_are_forwarded(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    [stream] = mock_stream.instances
    seen = []
    async_dispatcher_connect(hass, signal_frame(entry.entry_id), seen.append)
    for kind in ("heartbeat", "subscribed", "app_alert", "media_ready"):
        stream.on_frame({"v": 2, "seq": 9, "event_type": kind, "payload": {}})
    await hass.async_block_till_done()
    assert [f["event_type"] for f in seen] == ["app_alert", "media_ready"]
    assert "heartbeat" not in [f["event_type"] for f in
                               entry.runtime_data.coordinator.recent_frames]


async def test_revoked_while_connected_starts_reauth(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    [stream] = mock_stream.instances
    stream.on_state("auth_failed")
    await hass.async_block_till_done()
    [flow] = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["context"]["source"] == SOURCE_REAUTH


async def test_refresh_auth_failure_starts_reauth_and_outage_marks_unavailable(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream],
        freezer: FrozenDateTimeFactory) -> None:
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    coordinator = entry.runtime_data.coordinator
    coordinator.async_add_listener(lambda: None)   # polls only while something listens
    mock_client.get_cameras.side_effect = OpenNVRConnectionError("down")
    freezer.tick(31)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    mock_client.get_cameras.side_effect = OpenNVRAuthError("scope removed", 403)
    freezer.tick(31)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    [flow] = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["context"]["source"] == SOURCE_REAUTH
