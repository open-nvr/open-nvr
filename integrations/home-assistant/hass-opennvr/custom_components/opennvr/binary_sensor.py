"""Binary sensors the server describes: occupancy, motion, online, problems."""

from __future__ import annotations

from pyopennvr import EntityDescriptor

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenNVRConfigEntry
from .descriptor import OpenNVRDescriptorEntity, async_setup_platform_entities, enum_or_none

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    entry.async_on_unload(async_setup_platform_entities(
        entry.runtime_data.coordinator, "binary_sensor", OpenNVRBinarySensor,
        async_add_entities))


class OpenNVRBinarySensor(OpenNVRDescriptorEntity, BinarySensorEntity):
    def _apply(self, desc: EntityDescriptor) -> None:
        super()._apply(desc)
        self._attr_device_class = enum_or_none(BinarySensorDeviceClass, desc.device_class)

    @property
    def is_on(self) -> bool | None:
        state = self.raw_state
        return None if state is None else bool(state)
