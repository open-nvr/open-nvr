"""Event entities the server describes: detections (per camera and zone),
alerts, doorbell presses, app events. Each pushed ``entity_state`` carrying
an ``event`` fires one; event entities have no resolved state."""

from __future__ import annotations

import logging
from typing import Any

from pyopennvr import EntityDescriptor

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenNVRConfigEntry
from .descriptor import OpenNVRDescriptorEntity, async_setup_platform_entities, enum_or_none

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    entry.async_on_unload(async_setup_platform_entities(
        entry.runtime_data.coordinator, "event", OpenNVREvent, async_add_entities))


class OpenNVREvent(OpenNVRDescriptorEntity, EventEntity):
    def _apply(self, desc: EntityDescriptor) -> None:
        super()._apply(desc)
        self._attr_device_class = enum_or_none(EventDeviceClass, desc.device_class)
        self._attr_event_types = list(desc.event_types or [])

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return None  # the event's own attributes ride on the event

    @callback
    def _on_push(self, update: dict[str, Any]) -> None:
        event = update.get("event")
        if not isinstance(event, dict):
            return
        kind = str(event.get("type") or "")
        if kind not in self._attr_event_types:
            # A type the descriptor does not list (yet): once the
            # catalogue brings it, _apply updates the entity's types.
            _LOGGER.debug("%s: event type %r not announced, dropped", self.key, kind)
            return
        attrs = event.get("attributes")
        self._trigger_event(kind, attrs if isinstance(attrs, dict) else None)
        self.async_write_ha_state()
