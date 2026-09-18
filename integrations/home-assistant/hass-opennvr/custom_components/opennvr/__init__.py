"""The OpenNVR integration.

One config entry per OpenNVR site (keyed on the server's ``site_id``). The
entry holds a pyopennvr client and a coordinator fed by a REST refresh plus
the events websocket; platforms render what the server describes
(docs/design/home-assistant-integration.md §7).
"""

from __future__ import annotations

from dataclasses import dataclass

from pyopennvr import OpenNVRClient

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_TOKEN, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PLATFORMS
from .coordinator import OpenNVRCoordinator
from .entity import site_device_info

# Set up from config entries only; no YAML configuration.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class OpenNVRData:
    """Runtime data of one config entry."""

    client: OpenNVRClient
    coordinator: OpenNVRCoordinator


type OpenNVRConfigEntry = ConfigEntry[OpenNVRData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the OpenNVR integration."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry) -> bool:
    """Connect to one OpenNVR site."""
    verify = entry.data.get(CONF_VERIFY_SSL, True)
    client = OpenNVRClient(entry.data[CONF_URL], entry.data[CONF_API_TOKEN],
                           async_get_clientsession(hass, verify), verify_ssl=verify)
    coordinator = OpenNVRCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = OpenNVRData(client=client, coordinator=coordinator)

    # The server's own device, so cameras can name it as via_device before
    # any of its entities exist.
    dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
                                           **site_device_info(coordinator))

    coordinator.async_start_stream()
    entry.async_on_unload(coordinator.async_stop_stream)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry) -> bool:
    """Unload a config entry; the events stream stops via async_on_unload."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
