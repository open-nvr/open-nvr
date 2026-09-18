"""``opennvr/card_session``: a dashboard card's way into OpenNVR (design §7.8).

Card data does not route through this integration's code. A card asks Home
Assistant for a session and then talks to OpenNVR's own API (websocket v2,
WHEP, media) directly, with a credential that is:

* minted by OpenNVR from the integration's token, reading only;
* limited to the cameras this entry shows (or fewer, if the card asks);
* at most ten minutes long, and revoked with the integration's token.

When the browser can't reach OpenNVR (remote access through HA Cloud), the
card uses the passthrough view instead (``passthrough`` in the result),
which relays read-only GETs the server allows.
"""

from __future__ import annotations

from typing import Any

from pyopennvr import OpenNVRError
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .views import URL_BASE

SESSION_TTL_S = 600


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_card_session)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/card_session",
    vol.Optional("entry_id"): str,
    vol.Optional("camera_ids"): [vol.Coerce(int)],
})
@websocket_api.async_response
async def ws_card_session(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                          msg: dict[str, Any]) -> None:
    loaded = [e for e in hass.config_entries.async_entries(DOMAIN)
              if e.state is ConfigEntryState.LOADED]
    if "entry_id" in msg:
        loaded = [e for e in loaded if e.entry_id == msg["entry_id"]]
    if len(loaded) != 1:
        connection.send_error(msg["id"], "not_found",
                              "Name the OpenNVR site (entry_id)" if loaded else
                              "No such connected OpenNVR site")
        return
    entry = loaded[0]
    coordinator = entry.runtime_data.coordinator
    shown = set(coordinator.data.cameras)
    wanted = set(msg.get("camera_ids") or shown)
    cameras = sorted(wanted & shown)
    if not cameras:
        connection.send_error(msg["id"], "not_found", "None of those cameras is shown here")
        return
    try:
        session = await entry.runtime_data.client.open_session(camera_ids=cameras,
                                                               ttl_s=SESSION_TTL_S)
    except OpenNVRError as err:
        connection.send_error(msg["id"], "session_failed", str(err))
        return
    site = coordinator.data.info.site_id
    connection.send_result(msg["id"], {
        "token": session["token"],
        "expires_at": session.get("expires_at"),
        "scopes": session.get("scopes"),
        "camera_ids": session.get("camera_ids"),
        "site_id": site,
        "api_url": entry.data[CONF_URL],
        "passthrough": f"{URL_BASE}/{site}/passthrough",
    })
