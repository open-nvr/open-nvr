"""Selects the server describes: PTZ presets, app choices."""

from __future__ import annotations

from pyopennvr import EntityDescriptor

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenNVRConfigEntry
from .coordinator import OpenNVRCoordinator
from .descriptor import OpenNVRDescriptorEntity, async_setup_platform_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    entry.async_on_unload(async_setup_platform_entities(
        entry.runtime_data.coordinator, "select", OpenNVRSelect, async_add_entities))


class OpenNVRSelect(OpenNVRDescriptorEntity, SelectEntity):
    def __init__(self, coordinator: OpenNVRCoordinator, desc: EntityDescriptor) -> None:
        super().__init__(coordinator, desc)
        self._attr_options = [str(o) for o in desc.options or []]

    @property
    def options(self) -> list[str]:
        # The catalogue is re-read when it changes (a preset added), and the
        # descriptor object replaced; read the current one.
        desc = self.coordinator.descriptor(self.key) or self.descriptor
        return [str(o) for o in desc.options or []]

    @property
    def current_option(self) -> str | None:
        state = self.raw_state
        return str(state) if state is not None and str(state) in self.options else None

    async def async_select_option(self, option: str) -> None:
        await self.async_command(option)
        self.set_local_state(option)
