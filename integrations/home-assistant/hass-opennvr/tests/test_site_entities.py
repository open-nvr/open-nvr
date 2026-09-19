"""The site's alarm panel (site mode) and the server update entity."""

from __future__ import annotations

from unittest.mock import MagicMock

from pyopennvr import OpenNVRAuthError, SiteMode
import pytest

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import create_mock_config_entry, setup_mock_config_entry, system_info
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
