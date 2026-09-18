"""The OpenNVR server version as an update entity.

``latest_version`` comes from ``/system/info``, which knows it only when the
operator opted in to the update check (``UPDATE_CHECK``; off by default so an
offline site never calls out). Without it the entity shows the installed
version and never claims an update. Installing is done on the server, not
from Home Assistant.
"""

from __future__ import annotations

from homeassistant.components.update import UpdateEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenNVRConfigEntry
from .coordinator import OpenNVRCoordinator
from .entity import OpenNVREntity

PARALLEL_UPDATES = 0

KEY = "site.update"
RELEASES_URL = "https://github.com/open-nvr/open-nvr/releases"


async def async_setup_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([OpenNVRUpdate(entry.runtime_data.coordinator)])


class OpenNVRUpdate(OpenNVREntity, UpdateEntity):
    _attr_translation_key = "server"
    _attr_title = "OpenNVR"
    _attr_release_url = RELEASES_URL

    def __init__(self, coordinator: OpenNVRCoordinator) -> None:
        super().__init__(coordinator, KEY, {"kind": "site", "id": "site"})

    @property
    def installed_version(self) -> str | None:
        return self.coordinator.data.info.version

    @property
    def latest_version(self) -> str | None:
        info = self.coordinator.data.info
        return info.latest_version or info.version
