"""The OpenNVR server version as an update entity.

On a server with ``update_install`` (contract 1.2) ``latest_version`` is the
update the host's updater found and verified (``GET /system/update``), and
Install asks the host to apply it: the runner there backs up, updates, checks
and rolls back on its own within about a minute. Install is offered only when
the token has ``system.update`` (an admin must tick it; the Home Assistant
preset leaves it out), updates are not turned off, and the update needs no
manual steps. While the request waits or the update runs, the entity shows it
in progress.

Older servers: ``latest_version`` comes from ``/system/info``, which knows it
only when the operator opted in to the update check (``UPDATE_CHECK``; off by
default so an offline site never calls out). Without it the entity shows the
installed version and never claims an update; installing is done on the
server.
"""

from __future__ import annotations

from typing import Any

from pyopennvr import OpenNVRAuthError, OpenNVRError

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenNVRConfigEntry
from .const import DOMAIN
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

    def __init__(self, coordinator: OpenNVRCoordinator) -> None:
        super().__init__(coordinator, KEY, {"kind": "site", "id": "site"})

    @property
    def _available_update(self) -> dict[str, Any] | None:
        status = self.coordinator.data.update
        return (status or {}).get("available")

    @property
    def installed_version(self) -> str | None:
        return self.coordinator.data.info.version

    @property
    def latest_version(self) -> str | None:
        info = self.coordinator.data.info
        found = self._available_update
        if found:
            return found["version"]
        return info.latest_version or info.version

    @property
    def release_url(self) -> str | None:
        found = self._available_update
        return (found or {}).get("notes_url") or RELEASES_URL

    @property
    def supported_features(self) -> UpdateEntityFeature:
        status = self.coordinator.data.update
        scopes = self.coordinator.data.info.scopes
        found = self._available_update
        if status is None:
            return UpdateEntityFeature(0)
        # PROGRESS: a run from the app or the auto policy shows here too.
        if (found is None or scopes is None or "system.update" not in scopes
                or status.get("policy", {}).get("mode") == "off" or found.get("manual_steps")):
            return UpdateEntityFeature.PROGRESS
        return UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS

    @property
    def in_progress(self) -> bool:
        status = self.coordinator.data.update or {}
        return bool((status.get("run") or {}).get("in_progress") or status.get("pending_request"))

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        # The host always backs up before it updates; ``backup`` changes nothing.
        found = self._available_update
        target = version or (found or {}).get("version")
        if not target:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="command_failed",
                                     translation_placeholders={"detail": "no update was found"})
        try:
            await self.coordinator.client.request_update(
                target, correlation_id=self._context.id if self._context else None)
        except OpenNVRAuthError as err:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="not_permitted",
                                     translation_placeholders={"detail": str(err)}) from err
        except OpenNVRError as err:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="command_failed",
                                     translation_placeholders={"detail": str(err)}) from err
        # Now, not debounced: the request shows as in progress at once.
        await self.coordinator.async_refresh()
