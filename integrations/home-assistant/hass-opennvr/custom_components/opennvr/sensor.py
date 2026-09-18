"""Sensors the server describes: counts, health, alerts, last plate, ..."""

from __future__ import annotations

from typing import Any

from pyopennvr import EntityDescriptor

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import OpenNVRConfigEntry
from .descriptor import OpenNVRDescriptorEntity, async_setup_platform_entities, enum_or_none

PARALLEL_UPDATES = 0

#: HA refuses longer states.
MAX_STATE_LENGTH = 255


async def async_setup_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    entry.async_on_unload(async_setup_platform_entities(
        entry.runtime_data.coordinator, "sensor", OpenNVRSensor, async_add_entities))


class OpenNVRSensor(OpenNVRDescriptorEntity, SensorEntity):
    def _apply(self, desc: EntityDescriptor) -> None:
        super()._apply(desc)
        self._attr_device_class = enum_or_none(SensorDeviceClass, desc.device_class)
        self._attr_state_class = enum_or_none(SensorStateClass, desc.state_class)
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_options = (
            [str(o) for o in desc.options]
            if self._attr_device_class is SensorDeviceClass.ENUM
            and isinstance(desc.options, list) else None)

    @property
    def native_value(self) -> Any:
        """The server's value, shaped the way HA insists for this kind of
        sensor. A value that does not fit reads as unknown rather than
        raising (which would drop the update, and others with it)."""
        state = self.raw_state
        if state is None:
            return None
        dc = self._attr_device_class
        if dc is SensorDeviceClass.TIMESTAMP:
            when = dt_util.parse_datetime(str(state))
            if when is not None and when.tzinfo is None:
                when = when.replace(tzinfo=dt_util.UTC)
            return when
        if dc is SensorDeviceClass.DATE:
            return dt_util.parse_date(str(state))
        if dc is SensorDeviceClass.ENUM:
            state = str(state)
            return state if not self._attr_options or state in self._attr_options else None
        if self._attr_state_class or self._attr_native_unit_of_measurement or dc is not None:
            if isinstance(state, bool):
                return None
            if isinstance(state, (int, float)):
                return state
            try:
                return float(state)
            except (TypeError, ValueError):
                return None
        if isinstance(state, (dict, list)):
            return None  # structure belongs in attributes, not in a state
        return str(state)[:MAX_STATE_LENGTH]
