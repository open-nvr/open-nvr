"""Repairs: raised when their condition holds, cleared when it's gone."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from pyopennvr import OpenNVRAuthError, OpenNVRNotFoundError, StreamInfo
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from homeassistant.components.camera import async_get_stream_source
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_API_TOKEN, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.opennvr.const import DOMAIN

from . import TOKEN, URL, create_mock_config_entry, setup_mock_config_entry, system_info
from .conftest import FakeStream


def _issues(hass: HomeAssistant, entry) -> set[str]:
    suffix = f"_{entry.entry_id}"
    return {i.removesuffix(suffix) for (domain, i) in ir.async_get(hass).issues
            if domain == DOMAIN and i.endswith(suffix)}


def _now_info(**overrides):
    now = dt_util.utcnow()
    return system_info(server_time=now.isoformat(), **overrides)


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(31)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_healthy_site_raises_only_ssl(hass: HomeAssistant, mock_client: MagicMock,
                                            mock_stream: type[FakeStream]) -> None:
    mock_client.get_system_info.return_value = _now_info()
    entry = create_mock_config_entry()           # verify_ssl off
    await setup_mock_config_entry(hass, entry)
    assert _issues(hass, entry) == {"ssl_unverified"}
    verified = create_mock_config_entry(unique_id="other", data={
        CONF_URL: "https://b", CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True})
    mock_client.get_system_info.return_value = _now_info(site_id="other")
    await setup_mock_config_entry(hass, verified)
    assert _issues(hass, verified) == set()


async def test_token_expiring_and_its_fix_flow(
        hass: HomeAssistant, hass_client: ClientSessionGenerator, mock_client: MagicMock,
        mock_stream: type[FakeStream]) -> None:
    info = system_info()
    soon = (dt_util.utcnow() + timedelta(days=3)).isoformat()
    mock_client.get_system_info.return_value = _now_info(
        caller={**info.caller, "expires_at": soon})
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    assert "token_expiring" in _issues(hass, entry)

    assert await async_setup_component(hass, "repairs", {})
    client = await hass_client()
    resp = await client.post("/api/repairs/issues/fix",
                             json={"handler": DOMAIN,
                                   "issue_id": f"token_expiring_{entry.entry_id}"})
    flow = await resp.json()
    assert flow["step_id"] == "confirm"
    resp = await client.post(f"/api/repairs/issues/fix/{flow['flow_id']}", json={})
    assert (await resp.json())["type"] == "create_entry"
    await hass.async_block_till_done()
    [reauth] = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert reauth["context"]["source"] == SOURCE_REAUTH


async def test_expiry_far_away_is_fine(hass: HomeAssistant, mock_client: MagicMock,
                                       mock_stream: type[FakeStream]) -> None:
    info = system_info()
    later = (dt_util.utcnow() + timedelta(days=30)).isoformat()
    mock_client.get_system_info.return_value = _now_info(
        caller={**info.caller, "expires_at": later})
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    assert "token_expiring" not in _issues(hass, entry)


async def test_revoked_raises_then_clears(hass: HomeAssistant, mock_client: MagicMock,
                                          mock_stream: type[FakeStream],
                                          freezer: FrozenDateTimeFactory) -> None:
    good = _now_info()
    mock_client.get_system_info.return_value = good
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    entry.runtime_data.coordinator.async_add_listener(lambda: None)
    mock_client.get_cameras.side_effect = OpenNVRAuthError("revoked", 401)
    await _tick(hass, freezer)
    assert "token_revoked" in _issues(hass, entry)
    mock_client.get_cameras.side_effect = None
    mock_client.get_system_info.return_value = _now_info()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert "token_revoked" not in _issues(hass, entry)


async def test_revoked_on_the_socket(hass: HomeAssistant, mock_client: MagicMock,
                                     mock_stream: type[FakeStream]) -> None:
    mock_client.get_system_info.return_value = _now_info()
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    mock_stream.instances[0].on_state("auth_failed")
    await hass.async_block_till_done()
    assert "token_revoked" in _issues(hass, entry)


async def test_refused_address_is_a_firewall_issue_not_reauth(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    mock_client.get_system_info.side_effect = OpenNVRAuthError(
        "This API token may not be used from this address", 403, "token_address")
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert "firewall_blocked" in _issues(hass, entry)
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


@pytest.mark.parametrize(("contract", "kind"), [("2.0.0", "integration_too_old"),
                                                ("0.9.0", "server_too_old")])
async def test_contract_mismatch(hass: HomeAssistant, mock_client: MagicMock,
                                 mock_stream: type[FakeStream], contract, kind) -> None:
    mock_client.get_system_info.return_value = _now_info(contract_version=contract)
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert kind in _issues(hass, entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{kind}_{entry.entry_id}")
    assert issue.translation_placeholders["version"] == contract


async def test_server_without_system_info(hass: HomeAssistant, mock_client: MagicMock,
                                          mock_stream: type[FakeStream]) -> None:
    mock_client.get_system_info.side_effect = OpenNVRNotFoundError("404")
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "server_too_old" in _issues(hass, entry)


async def test_clock_skew(hass: HomeAssistant, mock_client: MagicMock,
                          mock_stream: type[FakeStream],
                          freezer: FrozenDateTimeFactory) -> None:
    skewed = (dt_util.utcnow() + timedelta(minutes=5)).isoformat()
    mock_client.get_system_info.return_value = system_info(server_time=skewed)
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"clock_skew_{entry.entry_id}")
    assert 290 <= int(issue.translation_placeholders["seconds"]) <= 310
    entry.runtime_data.coordinator.async_add_listener(lambda: None)
    freezer.tick(31)
    mock_client.get_system_info.return_value = system_info(
        server_time=dt_util.utcnow().isoformat())
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert "clock_skew" not in _issues(hass, entry)


async def test_webrtc_hosts_unset(hass: HomeAssistant, mock_client: MagicMock,
                                  mock_stream: type[FakeStream]) -> None:
    mock_client.get_system_info.return_value = _now_info(
        network={"webrtc_ice_hosts": False, "rtsps_exposed": False})
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    assert "webrtc_hosts_unset" in _issues(hass, entry)


async def test_rtsp_not_exposed_only_when_asked(hass: HomeAssistant, mock_client: MagicMock,
                                                mock_stream: type[FakeStream]) -> None:
    mock_client.get_system_info.return_value = _now_info()
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    assert "rtsp_not_exposed" not in _issues(hass, entry)
    mock_client.get_stream_info.return_value = StreamInfo.from_dict({
        "camera_id": 1, "stream_name": "cam-1", "token": "t",
        "urls": {"webrtc": f"{URL}/webrtc/cam-1/whep", "rtsps": "rtsps://127.0.0.1:8322/cam-1"}})
    assert await async_get_stream_source(hass, "camera.front_door") is None
    assert "rtsp_not_exposed" in _issues(hass, entry)
    mock_client.get_stream_info.return_value = StreamInfo.from_dict({
        "camera_id": 1, "stream_name": "cam-1", "token": "t",
        "urls": {"webrtc": f"{URL}/webrtc/cam-1/whep", "rtsps": "rtsps://10.0.0.2:8322/cam-1"}})
    assert await async_get_stream_source(hass, "camera.front_door")
    assert "rtsp_not_exposed" not in _issues(hass, entry)


async def test_mqtt_discovery_alongside_warns(hass: HomeAssistant, mock_client: MagicMock,
                                              mock_stream: type[FakeStream]) -> None:
    mock_client.get_system_info.return_value = _now_info(mqtt_discovery=True)
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    assert "mqtt_duplicate" in _issues(hass, entry)
