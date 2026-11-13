"""The site's alarm panel (site mode) and the server update entity."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from pyopennvr import OpenNVRAuthError, OpenNVRRequestError, SiteMode
import pytest

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.components.update import UpdateEntityFeature
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import create_mock_config_entry, load_fixture, setup_mock_config_entry, system_info
from .conftest import FakeStream

ALARM = "alarm_control_panel.opennvr_site_mode"
UPDATE = "update.opennvr_server"


def _may_arm(mock_client: MagicMock) -> None:
    info = system_info()
    mock_client.get_system_info.return_value = system_info(caller={
        **info.caller, "scopes": [*info.caller["scopes"], "settings.manage"]})


async def _setup(hass: HomeAssistant):
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    return entry


async def test_alarm_panel_follows_site_mode(hass: HomeAssistant, mock_client: MagicMock,
                                            mock_stream: type[FakeStream]) -> None:
    _may_arm(mock_client)
    await _setup(hass)
    state = hass.states.get(ALARM)
    assert state.state == AlarmControlPanelState.ARMED_AWAY
    assert state.attributes["changed_by"] == "token:home-assistant"
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == (
        AlarmControlPanelEntityFeature.ARM_HOME | AlarmControlPanelEntityFeature.ARM_AWAY)
    [stream] = mock_stream.instances
    stream.on_frame({"v": 2, "seq": 1, "event_type": "site_mode",
                     "payload": {"mode": "disarmed", "changed_by": "admin"}})
    await hass.async_block_till_done()
    assert hass.states.get(ALARM).state == AlarmControlPanelState.DISARMED
    stream.on_frame({"v": 2, "seq": 2, "event_type": "site_mode",
                     "payload": {"mode": "armed_on_the_moon"}})
    await hass.async_block_till_done()
    assert hass.states.get(ALARM).state == "unknown"            # a newer server's mode


async def test_arming(hass: HomeAssistant, mock_client: MagicMock,
                      mock_stream: type[FakeStream]) -> None:
    _may_arm(mock_client)
    await _setup(hass)
    mock_client.set_site_mode.return_value = SiteMode.from_dict(
        {"mode": "armed_home", "changed_by": "token:home-assistant"})
    await hass.services.async_call("alarm_control_panel", "alarm_arm_home",
                                   {ATTR_ENTITY_ID: ALARM}, blocking=True)
    call = mock_client.set_site_mode.call_args
    assert call.args == ("armed_home",) and call.kwargs["correlation_id"]
    assert hass.states.get(ALARM).state == AlarmControlPanelState.ARMED_HOME
    mock_client.set_site_mode.side_effect = OpenNVRAuthError("lacks settings.manage", 403)
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call("alarm_control_panel", "alarm_disarm",
                                       {ATTR_ENTITY_ID: ALARM}, blocking=True)
    assert exc.value.translation_key == "not_permitted"


async def test_read_only_without_settings_manage(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    info = system_info()
    mock_client.get_system_info.return_value = system_info(caller={
        **info.caller, "scopes": ["settings.view", "cameras.view", "live.view"]})
    await _setup(hass)
    assert hass.states.get(ALARM).attributes[ATTR_SUPPORTED_FEATURES] == 0


async def test_no_panel_without_site_mode(hass: HomeAssistant, mock_client: MagicMock,
                                          mock_stream: type[FakeStream]) -> None:
    mock_client.get_system_info.return_value = system_info(
        features=["api_tokens", "ws_v2", "entities"])
    await _setup(hass)
    assert hass.states.get(ALARM) is None
    assert hass.states.get(UPDATE) is not None


@pytest.mark.parametrize(("latest", "state"), [(None, STATE_OFF), ("0.2.0", STATE_ON)])
async def test_update_entity(hass: HomeAssistant, mock_client: MagicMock,
                             mock_stream: type[FakeStream], latest, state) -> None:
    mock_client.get_system_info.return_value = system_info(latest_version=latest)
    await _setup(hass)
    update = hass.states.get(UPDATE)
    assert update.state == state
    assert update.attributes["installed_version"] == "0.1.5"
    assert update.attributes["latest_version"] == (latest or "0.1.5")
    assert update.attributes["release_url"].endswith("/releases")


def _can_install(mock_client: MagicMock, *, scopes: tuple[str, ...] = ("system.update",),
                 **status: Any) -> None:
    """A contract-1.2 server whose host found 0.2.1; the token holds *scopes*."""
    info = system_info()
    mock_client.get_system_info.return_value = system_info(
        features=[*info.features, "update_install"],
        caller={**info.caller, "scopes": [*info.caller["scopes"], *scopes]})
    mock_client.get_update_status.return_value = {**load_fixture("update_status"), **status}


async def test_install_asks_the_host(hass: HomeAssistant, mock_client: MagicMock,
                                     mock_stream: type[FakeStream]) -> None:
    _can_install(mock_client)
    entry = await _setup(hass)
    update = hass.states.get(UPDATE)
    assert update.state == STATE_ON and update.attributes["latest_version"] == "0.2.1"
    assert update.attributes["release_url"].endswith("/v0.2.1")
    assert update.attributes[ATTR_SUPPORTED_FEATURES] == (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS)
    assert update.attributes["in_progress"] is False
    mock_client.get_update_status.return_value = {
        **load_fixture("update_status"), "pending_request": {"id": "r1", "to": "0.2.1"}}
    await hass.services.async_call("update", "install", {ATTR_ENTITY_ID: UPDATE}, blocking=True)
    call = mock_client.request_update.call_args
    assert call.args == ("0.2.1",) and call.kwargs["correlation_id"]
    await hass.async_block_till_done()
    assert hass.states.get(UPDATE).attributes["in_progress"] is True
    # HA refuses a second install while one is in progress.
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("update", "install", {ATTR_ENTITY_ID: UPDATE},
                                       blocking=True)
    mock_client.get_update_status.return_value = load_fixture("update_status")
    await entry.runtime_data.coordinator.async_refresh()           # the runner took it
    await hass.async_block_till_done()
    assert hass.states.get(UPDATE).attributes["in_progress"] is False
    mock_client.request_update.side_effect = OpenNVRRequestError(
        "POST /system/update/request: already requested", 409, "already requested")
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call("update", "install", {ATTR_ENTITY_ID: UPDATE},
                                       blocking=True)
    assert exc.value.translation_key == "command_failed"


@pytest.mark.parametrize(("scopes", "status"), [
    ((), {}),                                                    # no system.update
    (("system.update",), {"policy": {"mode": "off"}}),           # updates turned off
    (("system.update",), {"available": None}),                   # nothing found
    (("system.update",), {"available": {"version": "0.2.1", "manual_steps": [{"id": "x"}]}}),
])
async def test_no_install_when_it_cannot_go_ahead(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream],
        scopes, status) -> None:
    _can_install(mock_client, scopes=scopes, **status)
    await _setup(hass)
    assert hass.states.get(UPDATE).attributes[ATTR_SUPPORTED_FEATURES] == UpdateEntityFeature.PROGRESS


async def test_update_status_unreadable_falls_back(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    _can_install(mock_client)
    mock_client.get_update_status.side_effect = OpenNVRAuthError("lacks settings.view", 403)
    await _setup(hass)
    update = hass.states.get(UPDATE)
    assert update.state == STATE_OFF and update.attributes[ATTR_SUPPORTED_FEATURES] == 0
