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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PLATFORMS
from .coordinator import OpenNVRCoordinator
from .descriptor import async_prune, wanted_device_identifiers
from .entity import async_register_devices
from .llm import async_setup_llm_api
from .notifications import async_setup_notifications
from .services import async_setup_services
from .views import async_register_views
from .websocket_api import async_register_websocket

# Set up from config entries only; no YAML configuration.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class OpenNVRData:
    """Runtime data of one config entry."""

    client: OpenNVRClient
    coordinator: OpenNVRCoordinator


type OpenNVRConfigEntry = ConfigEntry[OpenNVRData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the OpenNVR integration: its actions exist once, for every site."""
    async_setup_services(hass)
    async_register_views(hass)
    async_register_websocket(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry) -> bool:
    """Connect to one OpenNVR site."""
    verify = entry.data.get(CONF_VERIFY_SSL, True)
    client = OpenNVRClient(entry.data[CONF_URL], entry.data[CONF_API_TOKEN],
                           async_get_clientsession(hass, verify), verify_ssl=verify)
    coordinator = OpenNVRCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = OpenNVRData(client=client, coordinator=coordinator)

    # The device tree (server > cameras > zones), parents first, before any
    # entity names its device; kept current on every refresh. Registered
    # before the platforms' listeners, so a new zone's device exists (with
    # its parent) by the time its entities are added.
    async_register_devices(hass, coordinator)
    entry.async_on_unload(coordinator.async_add_listener(
        lambda: async_register_devices(hass, coordinator)))

    async_setup_notifications(hass, entry)
    async_setup_llm_api(hass, entry)
    coordinator.async_start_stream()
    entry.async_on_unload(coordinator.async_stop_stream)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Entities and devices follow the catalogue: gone descriptors, deleted
    # zones and deselected cameras are removed (stale-devices).
    @callback
    def prune() -> None:
        async_prune(hass, coordinator)

    prune()
    entry.async_on_unload(coordinator.async_add_listener(prune))
    return True


async def async_remove_config_entry_device(hass: HomeAssistant, entry: OpenNVRConfigEntry,
                                           device: dr.DeviceEntry) -> bool:
    """Let the user delete a device OpenNVR no longer has."""
    return not device.identifiers & wanted_device_identifiers(entry.runtime_data.coordinator)


async def async_unload_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry) -> bool:
    """Unload a config entry; the events stream stops via async_on_unload."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
