"""Numbers the server describes (app settings such as a dwell threshold).
``options`` carries ``{min, max, step}``."""

from __future__ import annotations

from pyopennvr import EntityDescriptor

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenNVRConfigEntry
from .descriptor import OpenNVRDescriptorEntity, async_setup_platform_entities, enum_or_none

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    entry.async_on_unload(async_setup_platform_entities(
        entry.runtime_data.coordinator, "number", OpenNVRNumber, async_add_entities))


class OpenNVRNumber(OpenNVRDescriptorEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def _apply(self, desc: EntityDescriptor) -> None:
        super()._apply(desc)
        self._attr_device_class = enum_or_none(NumberDeviceClass, desc.device_class)
        self._attr_native_unit_of_measurement = desc.unit
        opts = desc.options if isinstance(desc.options, dict) else {}
        for attr, key in (("_attr_native_min_value", "min"), ("_attr_native_max_value", "max"),
                          ("_attr_native_step", "step")):
            if isinstance(opts.get(key), (int, float)) and not isinstance(opts.get(key), bool):
                setattr(self, attr, float(opts[key]))

    @property
    def native_value(self) -> float | None:
        state = self.raw_state
        if isinstance(state, bool):
            return None
        try:
            return None if state is None else float(state)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        sent = int(value) if float(value).is_integer() else value
        await self.async_command(sent)
        self.set_local_state(sent)
