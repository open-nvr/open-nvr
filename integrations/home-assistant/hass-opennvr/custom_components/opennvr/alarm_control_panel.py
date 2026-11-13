"""The site's arming state as an alarm panel (design §7.4, OpenNVR site mode).

OpenNVR's site mode (``disarmed | armed_home | armed_away``) decides whether
alerts fire their alarm actions. Reading it needs ``settings.view``; arming
needs ``settings.manage``, and without it the panel is read-only. No code:
Home Assistant users add one with their own automations if they want it.
"""

from __future__ import annotations

from pyopennvr import OpenNVRAuthError, OpenNVRError

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenNVRConfigEntry
from .const import DOMAIN
from .coordinator import OpenNVRCoordinator
from .entity import OpenNVREntity

PARALLEL_UPDATES = 0

#: The key (unique id suffix) of the site's alarm panel.
KEY = "site.alarm"
MANAGE_SCOPE = "settings.manage"


async def async_setup_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    if coordinator.data.info.has("site_mode") and coordinator.data.site_mode is not None:
        async_add_entities([OpenNVRSiteAlarm(coordinator)])


class OpenNVRSiteAlarm(OpenNVREntity, AlarmControlPanelEntity):
    _attr_translation_key = "site_mode"
    _attr_code_arm_required = False

    def __init__(self, coordinator: OpenNVRCoordinator) -> None:
        super().__init__(coordinator, KEY, {"kind": "site", "id": "site"})
        scopes = coordinator.data.info.scopes
        if scopes is None or MANAGE_SCOPE in scopes:
            self._attr_supported_features = (AlarmControlPanelEntityFeature.ARM_HOME
                                              | AlarmControlPanelEntityFeature.ARM_AWAY)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.site_mode is not None

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        mode = self.coordinator.data.site_mode
        if mode is None:
            return None
        try:
            return AlarmControlPanelState(mode.mode)
        except ValueError:
            return None  # a mode a newer server added

    @property
    def changed_by(self) -> str | None:
        mode = self.coordinator.data.site_mode
        return mode.changed_by if mode else None

    async def _set(self, mode: str) -> None:
        try:
            result = await self.coordinator.client.set_site_mode(
                mode, reason="Home Assistant",
                correlation_id=self._context.id if self._context else None)
        except OpenNVRAuthError as err:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="not_permitted",
                                     translation_placeholders={"detail": str(err)}) from err
        except OpenNVRError as err:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="command_failed",
                                     translation_placeholders={"detail": str(err)}) from err
        self.coordinator.async_set_site_mode(result)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._set("disarmed")

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._set("armed_home")

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._set("armed_away")
