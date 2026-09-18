"""Config flow: user, cameras, zeroconf, reauth, reconfigure, options."""

from __future__ import annotations

from ipaddress import ip_address
from unittest.mock import MagicMock

from pyopennvr import (
    OpenNVRAuthError,
    OpenNVRConnectionError,
    OpenNVRNotFoundError,
    OpenNVRSSLError,
)
import pytest

from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.const import CONF_API_TOKEN, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from custom_components.opennvr.config_flow import normalise_url
from custom_components.opennvr.const import CONF_CAMERAS, CONF_MEDIA_TTL, DOMAIN

from . import (
    SITE_ID,
    TOKEN,
    URL,
    create_mock_config_entry,
    setup_mock_config_entry,
    system_info,
)
from .conftest import FakeStream

USER_INPUT = {CONF_URL: "nvr.local/", CONF_API_TOKEN: f" {TOKEN} ", CONF_VERIFY_SSL: False}


@pytest.mark.parametrize(("raw", "url"), [
    ("nvr.local", "https://nvr.local"),
    ("http://10.0.0.5:8080/", "http://10.0.0.5:8080"),
    ("https://nvr.local/api/v1/", "https://nvr.local"),
    ("https://example.org/nvr/", "https://example.org/nvr"),
    ("ftp://nvr.local", None),
    ("https://", None),
    ("https://nvr.local:99999", None),
])
def test_normalise_url(raw, url) -> None:
    assert normalise_url(raw) == url


async def _user(hass: HomeAssistant, user_input=USER_INPUT):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_user_flow_all_cameras(hass: HomeAssistant, mock_client: MagicMock,
                                     mock_stream: type[FakeStream]) -> None:
    result = await _user(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "cameras"
    # The fixture token holds every recommended scope.
    assert result["description_placeholders"]["missing_scopes"] == "-"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CAMERAS: ["1", "3"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "OpenNVR"
    assert result["data"] == {CONF_URL: URL, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: False}
    assert result["options"] == {}          # all = including cameras added later
    assert result["result"].unique_id == SITE_ID


async def test_user_flow_some_cameras(hass: HomeAssistant, mock_client: MagicMock,
                                      mock_stream: type[FakeStream]) -> None:
    result = await _user(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"],
                                                            {CONF_CAMERAS: []})
    assert result["errors"] == {"base": "no_cameras"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"],
                                                            {CONF_CAMERAS: ["3"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_CAMERAS: [3]}


async def test_token_without_cameras_skips_the_step(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    mock_client.get_cameras.return_value = []
    result = await _user(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["options"] == {}


async def test_missing_recommended_scopes_are_named(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    info = system_info()
    caller = {**info.caller, "scopes": ["settings.view", "cameras.view", "alerts.view"]}
    mock_client.get_system_info.return_value = system_info(caller=caller)
    result = await _user(hass)
    assert result["step_id"] == "cameras"
    assert result["description_placeholders"]["missing_scopes"] == "live.view, recordings.view"


@pytest.mark.parametrize(("error", "key"), [
    (OpenNVRConnectionError("down"), "cannot_connect"),
    (OpenNVRSSLError("self-signed"), "ssl_error"),
    (OpenNVRAuthError("nope", 401), "invalid_auth"),
    (OpenNVRAuthError("scope", 403), "missing_scopes"),
    (OpenNVRNotFoundError("404"), "not_opennvr"),
    (KeyError("site_id"), "not_opennvr"),
])
async def test_user_flow_errors_then_recovers(hass: HomeAssistant, mock_client: MagicMock,
                                              mock_stream: type[FakeStream],
                                              error, key) -> None:
    good = mock_client.get_system_info.return_value
    mock_client.get_system_info.side_effect = error
    result = await _user(hass)
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": key}
    if key == "missing_scopes":
        assert result["description_placeholders"]["scopes"] == "settings.view"
    mock_client.get_system_info.side_effect = None
    mock_client.get_system_info.return_value = good
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["step_id"] == "cameras"


async def test_user_flow_validation(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await _user(hass, {**USER_INPUT, CONF_URL: "ftp://x"})
    assert result["errors"] == {CONF_URL: "invalid_url"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_API_TOKEN: "eyJhbGciOi.jwt"})
    assert result["errors"] == {"base": "token_format"}
    assert not mock_client.get_system_info.called
    mock_client.get_system_info.return_value = system_info(contract_version="2.0.0")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "unsupported_version"}
    assert result["description_placeholders"]["version"] == "2.0.0"
    info = system_info()
    mock_client.get_system_info.return_value = system_info(
        caller={**info.caller, "scopes": ["settings.view"]})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "missing_scopes"}
    assert result["description_placeholders"]["scopes"] == "cameras.view"


async def test_user_flow_already_configured(hass: HomeAssistant, mock_client: MagicMock) -> None:
    create_mock_config_entry().add_to_hass(hass)
    result = await _user(hass)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


def _zeroconf(ip="192.168.1.20", port=443, https="1") -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=ip_address(ip), ip_addresses=[ip_address(ip)], port=port,
        hostname="opennvr.local.", type="_opennvr._tcp.local.",
        name="OpenNVR._opennvr._tcp.local.",
        properties={"txtvers": "1", "path": "/api/v1", "https": https, "port": str(port),
                    "version": "0.1.5"})


async def test_zeroconf_flow(hass: HomeAssistant, mock_client: MagicMock,
                             mock_stream: type[FakeStream]) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_zeroconf(port=8443))
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"]["url"] == "https://192.168.1.20:8443"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: False})
    assert result["step_id"] == "cameras"
    result = await hass.config_entries.flow.async_configure(result["flow_id"],
                                                            {CONF_CAMERAS: ["1", "3"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == "https://192.168.1.20:8443"
    assert result["result"].unique_id == SITE_ID


async def test_zeroconf_never_moves_a_configured_site(
        hass: HomeAssistant, mock_client: MagicMock) -> None:
    entry = create_mock_config_entry(data={CONF_URL: "https://192.168.1.20",
                                           CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: False})
    entry.add_to_hass(hass)
    # Same URL: dropped before any token is asked for.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_zeroconf())
    assert result["type"] is FlowResultType.ABORT
    # Another address that turns out to be the same site: aborted, and the
    # entry keeps its URL (an announcement is not authenticated).
    hass.config_entries.async_update_entry(entry, data={**entry.data,
                                                        CONF_URL: "https://old.example"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=_zeroconf(ip="192.168.1.99"))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: False})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert entry.data[CONF_URL] == "https://old.example"


async def test_zeroconf_bad_port(hass: HomeAssistant) -> None:
    info = _zeroconf()
    info.properties["port"] = "http"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "not_opennvr"


async def test_reauth(hass: HomeAssistant, mock_client: MagicMock,
                      mock_stream: type[FakeStream]) -> None:
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    mock_client.get_system_info.side_effect = OpenNVRAuthError("nope", 401)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "onvr_newtoken_x"})
    assert result["errors"] == {"base": "invalid_auth"}
    mock_client.get_system_info.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "onvr_newtoken_x"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_TOKEN] == "onvr_newtoken_x"


async def test_reauth_against_another_site_is_refused(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    result = await entry.start_reauth_flow(hass)
    mock_client.get_system_info.return_value = system_info(site_id="someone-else")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "onvr_newtoken_x"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "wrong_site"
    assert entry.data[CONF_API_TOKEN] == TOKEN


async def test_reconfigure(hass: HomeAssistant, mock_client: MagicMock,
                           mock_stream: type[FakeStream]) -> None:
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "https://10.0.0.9", CONF_VERIFY_SSL: True})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_URL: "https://10.0.0.9", CONF_API_TOKEN: TOKEN,
                          CONF_VERIFY_SSL: True}


async def test_reconfigure_to_another_site_is_refused(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    mock_client.get_system_info.return_value = system_info(site_id="someone-else")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "https://10.0.0.9", CONF_VERIFY_SSL: True})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "wrong_site"
    assert entry.data[CONF_URL] == URL


async def test_options(hass: HomeAssistant, mock_client: MagicMock,
                       mock_stream: type[FakeStream]) -> None:
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CAMERAS: [], CONF_MEDIA_TTL: 12})
    assert result["errors"] == {"base": "no_cameras"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CAMERAS: ["1"], CONF_MEDIA_TTL: 12})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {CONF_CAMERAS: [1], CONF_MEDIA_TTL: 12}
    # Reloaded with the new selection.
    assert set(entry.runtime_data.coordinator.data.cameras) == {1}


async def test_options_need_a_loaded_entry(hass: HomeAssistant) -> None:
    entry = create_mock_config_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "not_loaded"
