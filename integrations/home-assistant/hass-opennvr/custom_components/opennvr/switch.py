"""Switches the server describes: detection, recording (where allowed), app
toggles. Each flips through the descriptor's typed command."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenNVRConfigEntry
from .descriptor import OpenNVRDescriptorEntity, async_setup_platform_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    entry.async_on_unload(async_setup_platform_entities(
        entry.runtime_data.coordinator, "switch", OpenNVRSwitch, async_add_entities))


class OpenNVRSwitch(OpenNVRDescriptorEntity, SwitchEntity):
    @property
    def is_on(self) -> bool | None:
        state = self.raw_state
        return None if state is None else bool(state)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_command(True)
        self.set_local_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_command(False)
        self.set_local_state(False)
