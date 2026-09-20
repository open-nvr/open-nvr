"""Repairs (design §7.9): what the user must fix, raised and cleared here.

| issue | when | fix |
|---|---|---|
| ``token_revoked`` | the token is refused (revoked, expired, a scope removed) | reauth (fix flow) |
| ``token_expiring`` | the token expires within 7 days (by the server's clock) | reauth (fix flow) |
| ``firewall_blocked`` | the token may not be used from HA's address | the token's allowed addresses, in OpenNVR |
| ``server_too_old`` | the server predates the contract (or its major is older) | update OpenNVR |
| ``integration_too_old`` | the server speaks a newer contract major | update the integration |
| ``webrtc_hosts_unset`` | MediaMTX advertises no reachable WebRTC address | ``MEDIAMTX_WEBRTC_HOSTS`` |
| ``rtsp_not_exposed`` | HA asked for an RTSP stream OpenNVR does not publish | ``MEDIAMTX_EXTERNAL_RTSPS_URL`` (optional) |
| ``clock_skew`` | the clocks differ by more than a minute | NTP |
| ``ssl_unverified`` | certificate verification is off | a trusted certificate, then reconfigure |
| ``mqtt_duplicate`` | OpenNVR also publishes MQTT discovery | use one of the two |

Issue ids carry the entry id, so two OpenNVR sites keep separate issues.
Each is cleared as soon as the condition is gone.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pyopennvr import SystemInfo

from homeassistant.const import CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

DOCS = "https://github.com/open-nvr/open-nvr/blob/main/docs/HOME_ASSISTANT.md"
EXPIRY_WARNING = timedelta(days=7)
MAX_CLOCK_SKEW = timedelta(seconds=60)

#: Issues whose fix flow starts reauthentication (repairs.py).
REAUTH_FIXABLE = ("token_revoked", "token_expiring")
#: Issues about reaching the server at all; cleared once it answers normally.
CONNECTION_ISSUES = ("token_revoked", "firewall_blocked", "server_too_old",
                     "integration_too_old")

_SEVERITY = {
    "token_revoked": ir.IssueSeverity.ERROR,
    "firewall_blocked": ir.IssueSeverity.ERROR,
    "server_too_old": ir.IssueSeverity.ERROR,
    "integration_too_old": ir.IssueSeverity.ERROR,
    "token_expiring": ir.IssueSeverity.WARNING,
    "webrtc_hosts_unset": ir.IssueSeverity.WARNING,
    "rtsp_not_exposed": ir.IssueSeverity.WARNING,
    "clock_skew": ir.IssueSeverity.WARNING,
    "ssl_unverified": ir.IssueSeverity.WARNING,
    "mqtt_duplicate": ir.IssueSeverity.WARNING,
}
_LEARN_MORE = {
    "firewall_blocked": f"{DOCS}#tokens",
    "webrtc_hosts_unset": f"{DOCS}#mediamtx_webrtc_hosts-live-video-from-another-machine",
    "rtsp_not_exposed": f"{DOCS}#rtsps_bind_host-rtsps-for-home-assistants-stream-component-optional",
    "server_too_old": DOCS,
    "integration_too_old": DOCS,
}


def issue_id(kind: str, entry: ConfigEntry) -> str:
    return f"{kind}_{entry.entry_id}"


@callback
def async_raise(hass: HomeAssistant, entry: ConfigEntry, kind: str,
                **placeholders: Any) -> None:
    fixable = kind in REAUTH_FIXABLE
    ir.async_create_issue(
        hass, DOMAIN, issue_id(kind, entry), is_fixable=fixable, is_persistent=False,
        severity=_SEVERITY[kind], translation_key=kind,
        translation_placeholders={"name": entry.title,
                                  **{k: str(v) for k, v in placeholders.items()}},
        learn_more_url=_LEARN_MORE.get(kind),
        data={"entry_id": entry.entry_id} if fixable else None)


@callback
def async_clear(hass: HomeAssistant, entry: ConfigEntry, *kinds: str) -> None:
    for kind in kinds:
        ir.async_delete_issue(hass, DOMAIN, issue_id(kind, entry))


@callback
def async_check_site(hass: HomeAssistant, entry: ConfigEntry, info: SystemInfo) -> None:
    """After a good refresh: clear what is fixed, raise what is found.
    ``info`` was just read, so its ``server_time`` is (nearly) now."""
    async_clear(hass, entry, *CONNECTION_ISSUES)
    local_now = dt_util.utcnow()
    server_now = dt_util.parse_datetime(info.server_time) if info.server_time else None

    expires = dt_util.parse_datetime(info.token_expires_at or "")
    if expires is not None and expires - (server_now or local_now) < EXPIRY_WARNING:
        async_raise(hass, entry, "token_expiring",
                    expires=dt_util.as_local(expires).strftime("%Y-%m-%d %H:%M"))
    else:
        async_clear(hass, entry, "token_expiring")

    if server_now is not None and abs(server_now - local_now) > MAX_CLOCK_SKEW:
        async_raise(hass, entry, "clock_skew",
                    seconds=int(abs((server_now - local_now).total_seconds())))
    else:
        async_clear(hass, entry, "clock_skew")

    if info.network.get("webrtc_ice_hosts") is False:
        async_raise(hass, entry, "webrtc_hosts_unset")
    else:
        async_clear(hass, entry, "webrtc_hosts_unset")

    if entry.data.get(CONF_VERIFY_SSL, True):
        async_clear(hass, entry, "ssl_unverified")
    else:
        async_raise(hass, entry, "ssl_unverified")

    # OpenNVR's MQTT discovery publishes the same entities to HA's MQTT
    # integration: running both shows everything twice. Only a warning when
    # this Home Assistant has an MQTT integration to receive them.
    if info.raw.get("mqtt_discovery") is True and hass.config_entries.async_entries("mqtt"):
        async_raise(hass, entry, "mqtt_duplicate")
    else:
        async_clear(hass, entry, "mqtt_duplicate")
