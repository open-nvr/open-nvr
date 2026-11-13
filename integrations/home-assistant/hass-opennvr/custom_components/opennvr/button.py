"""Buttons the server describes: PTZ moves, acknowledge alerts, manual event,
app actions."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenNVRConfigEntry
from .descriptor import OpenNVRDescriptorEntity, async_setup_platform_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    entry.async_on_unload(async_setup_platform_entities(
        entry.runtime_data.coordinator, "button", OpenNVRButton, async_add_entities))


class OpenNVRButton(OpenNVRDescriptorEntity, ButtonEntity):
    async def async_press(self) -> None:
        await self.async_command()
