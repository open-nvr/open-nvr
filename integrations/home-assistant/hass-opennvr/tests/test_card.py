"""Dashboard cards: ``opennvr/card_session`` and the read-only passthrough."""

from __future__ import annotations

from unittest.mock import MagicMock

from pyopennvr import OpenNVRAuthError
import pytest
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from homeassistant.core import HomeAssistant

from custom_components.opennvr.const import CONF_CAMERAS
from custom_components.opennvr.views import passthrough_allowed

from . import SITE_ID, URL, create_mock_config_entry, setup_mock_config_entry
from .conftest import FakeStream

SESSION = {"token": "onvr_sess1234_abcdefghijklmnopqrstuvwxyz0123456789",
           "expires_at": "2026-09-19T10:10:00+00:00", "scopes": ["cameras.view", "live.view"],
           "camera_ids": [1, 3]}


async def _setup(hass: HomeAssistant, **kwargs):
    entry = create_mock_config_entry(**kwargs)
    await setup_mock_config_entry(hass, entry)
    return entry


async def test_card_session(hass: HomeAssistant, hass_ws_client: WebSocketGenerator,
                            mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    await _setup(hass)
    mock_client.open_session.return_value = SESSION
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "opennvr/card_session"})
    msg = await ws.receive_json()
    assert msg["success"]
    result = msg["result"]
    assert result["token"] == SESSION["token"] and result["api_url"] == URL
    assert result["passthrough"] == f"/api/opennvr/{SITE_ID}/passthrough"
    assert mock_client.open_session.call_args.kwargs == {"camera_ids": [1, 3], "ttl_s": 600}
    # Asking for fewer, or for one this entry doesn't show.
    await ws.send_json_auto_id({"type": "opennvr/card_session", "camera_ids": [3, 99]})
    assert (await ws.receive_json())["success"]
    assert mock_client.open_session.call_args.kwargs["camera_ids"] == [3]
    await ws.send_json_auto_id({"type": "opennvr/card_session", "camera_ids": [99]})
    assert (await ws.receive_json())["error"]["code"] == "not_found"
    mock_client.open_session.side_effect = OpenNVRAuthError("no", 403)
    await ws.send_json_auto_id({"type": "opennvr/card_session"})
    assert (await ws.receive_json())["error"]["code"] == "session_failed"


async def test_card_session_keeps_to_the_entrys_cameras(
        hass: HomeAssistant, hass_ws_client: WebSocketGenerator, mock_client: MagicMock,
        mock_stream: type[FakeStream]) -> None:
    await _setup(hass, options={CONF_CAMERAS: [3]})
    mock_client.open_session.return_value = SESSION
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "opennvr/card_session", "camera_ids": [1, 3]})
    assert (await ws.receive_json())["success"]
    assert mock_client.open_session.call_args.kwargs["camera_ids"] == [3]


@pytest.mark.parametrize(("path", "ok"), [
    ("/api/v1/cameras/", True), ("/api/v1/cameras/3/stats", True), ("/api/v1/search", True),
    ("/api/v1/searching", False), ("/api/v1/api-tokens", False), ("/api/v1/users/", False),
])
def test_passthrough_allowed(path: str, ok: bool) -> None:
    allow = ("/api/v1/cameras/", "/api/v1/search", "/api/v1/system/info")
    assert passthrough_allowed(path, allow) is ok


async def test_passthrough(hass: HomeAssistant, hass_client: ClientSessionGenerator,
                           hass_client_no_auth: ClientSessionGenerator,
                           mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    await _setup(hass)
    mock_client.request.return_value = {"results": [], "semantic": False}
    client = await hass_client()
    resp = await client.get(f"/api/opennvr/{SITE_ID}/passthrough/api/v1/search?q=car&limit=5")
    assert resp.status == 200 and await resp.json() == {"results": [], "semantic": False}
    call = mock_client.request.call_args
    assert call.args == ("GET", "/search") and call.kwargs["params"] == {"q": "car", "limit": "5"}
    for bad in ("api/v1/api-tokens", "api/v1/users/", "api/v1/cameras/../api-tokens",
                "api/v1/cameras/%2e%2e/api-tokens", "etc/passwd"):
        mock_client.request.reset_mock()
        resp = await client.get(f"/api/opennvr/{SITE_ID}/passthrough/{bad}")
        assert resp.status == 404, bad
        assert not mock_client.request.called
    assert (await client.post(f"/api/opennvr/{SITE_ID}/passthrough/api/v1/search")).status == 405
    anon = await hass_client_no_auth()
    assert (await anon.get(
        f"/api/opennvr/{SITE_ID}/passthrough/api/v1/search")).status == 401
