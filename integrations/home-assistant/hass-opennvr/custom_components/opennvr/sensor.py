"""Sensors the server describes: counts, health, alerts, last plate, ..."""

from __future__ import annotations

from typing import Any

from pyopennvr import EntityDescriptor

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenNVRConfigEntry
from .coordinator import OpenNVRCoordinator
from .descriptor import OpenNVRDescriptorEntity, async_setup_platform_entities, enum_or_none

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    entry.async_on_unload(async_setup_platform_entities(
        entry.runtime_data.coordinator, "sensor", OpenNVRSensor, async_add_entities))


class OpenNVRSensor(OpenNVRDescriptorEntity, SensorEntity):
    def __init__(self, coordinator: OpenNVRCoordinator, desc: EntityDescriptor) -> None:
        super().__init__(coordinator, desc)
        self._attr_device_class = enum_or_none(SensorDeviceClass, desc.device_class)
        self._attr_state_class = enum_or_none(SensorStateClass, desc.state_class)
        self._attr_native_unit_of_measurement = desc.unit
        if self._attr_device_class is SensorDeviceClass.ENUM and isinstance(desc.options, list):
            self._attr_options = [str(o) for o in desc.options]

    @property
    def native_value(self) -> Any:
        state = self.raw_state
        if self._attr_device_class is SensorDeviceClass.ENUM and state is not None:
            state = str(state)
            if self._attr_options and state not in self._attr_options:
                return None  # a value this descriptor didn't announce
        return state
