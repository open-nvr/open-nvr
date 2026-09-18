"""Entities the server describes (design §6.10), and their lifecycle.

Every platform except camera, alarm panel and update renders descriptors
generically: the server says what an entity is (platform, name, device
class, unit, options, command) and resolves its value; this module only maps
that onto Home Assistant. A new OpenNVR feature or AI app therefore shows up
without an integration release.

* Values come from the coordinator (REST refresh, snapshot) and are pushed
  per key (``entity_state``); each entity listens to its own key only.
* Commands are typed and go to ``POST /entities/{key}/command``; the server
  checks the token's scopes on every one.
* Anything unknown (a device class, entity category, platform) is ignored,
  never an error: an older integration keeps working against a newer server.
* ``async_setup_platform_entities`` adds entities as descriptors appear;
  ``async_prune`` removes entities and devices whose descriptors are gone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import logging
from typing import Any

from pyopennvr import EntityDescriptor, OpenNVRAuthError, OpenNVRError

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import OpenNVRCoordinator
from .entity import OpenNVREntity, camera_identifier, device_info_for, site_identifier

_LOGGER = logging.getLogger(__name__)


def enum_or_none(enum: type, value: Any) -> Any:
    """``enum(value)``, or None for a value this HA version doesn't know."""
    if value is None:
        return None
    try:
        return enum(value)
    except ValueError:
        _LOGGER.debug("Ignoring unknown %s %r", enum.__name__, value)
        return None


class OpenNVRDescriptorEntity(OpenNVREntity):
    """An entity rendered from a descriptor."""

    def __init__(self, coordinator: OpenNVRCoordinator, desc: EntityDescriptor) -> None:
        super().__init__(coordinator, desc.key, desc.device)
        self.descriptor = desc
        self._attr_entity_registry_enabled_default = desc.enabled_default
        self._apply(desc)

    def _apply(self, desc: EntityDescriptor) -> None:
        """Take the descriptor's presentation. Called again whenever the
        catalogue brings a changed descriptor (a label added to a camera,
        new enum options), so a live entity never keeps a stale one."""
        # The server names entities (in English): its translation keys can't
        # all be known to an integration that predates them.
        self._attr_name = desc.name
        self._attr_icon = desc.icon
        self._attr_entity_category = enum_or_none(EntityCategory, desc.entity_category)

    @callback
    def _handle_coordinator_update(self) -> None:
        desc = self.coordinator.descriptor(self.key)
        if desc is not None and desc is not self.descriptor:
            self.descriptor = desc
            self._apply(desc)
        super()._handle_coordinator_update()

    @property
    def resolved(self) -> dict[str, Any] | None:
        """``{"state", "attributes"}`` as the server last resolved it."""
        return self.coordinator.state_of(self.key)

    @property
    def raw_state(self) -> Any:
        value = self.resolved
        return None if value is None else value.get("state")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        value = self.resolved
        attrs = value.get("attributes") if value else None
        return dict(attrs) if attrs else None

    @property
    def available(self) -> bool:
        return super().available and self.key in self.coordinator.known_keys

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_key_listener(self.key, self._on_push))

    @callback
    def _on_push(self, update: dict[str, Any]) -> None:
        self.async_write_ha_state()

    async def async_command(self, value: Any = None, args: dict[str, Any] | None = None) -> None:
        """Run the descriptor's command on the server."""
        try:
            await self.coordinator.client.command_entity(
                self.key, value, args=args,
                correlation_id=self._context.id if self._context else None)
        except OpenNVRAuthError as err:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="not_permitted",
                                     translation_placeholders={"detail": str(err)}) from err
        except OpenNVRError as err:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="command_failed",
                                     translation_placeholders={"detail": str(err)}) from err

    @callback
    def set_local_state(self, state: Any) -> None:
        """Show a command's effect now; the server's next push confirms it."""
        value = dict(self.resolved or {"attributes": {}})
        value["state"] = state
        self.coordinator.async_set_state(self.key, value)
        self.async_write_ha_state()


@callback
def async_setup_platform_entities(
    coordinator: OpenNVRCoordinator,
    platform: str,
    factory: Callable[[OpenNVRCoordinator, EntityDescriptor], OpenNVRDescriptorEntity],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> Callable[[], None]:
    """Add an entity for each descriptor of ``platform``, now and whenever
    new ones appear. Returns the listener's remover."""
    added: set[str] = set()

    @callback
    def add_new() -> None:
        new: list[OpenNVRDescriptorEntity] = []
        for desc in coordinator.descriptors():
            if desc.platform == platform and desc.key not in added:
                added.add(desc.key)
                new.append(factory(coordinator, desc))
        if new:
            async_add_entities(new)

    add_new()
    return coordinator.async_add_listener(add_new)


#: A key missing from this many consecutive refreshes is gone for good. A
#: descriptor can be briefly absent (after an OpenNVR restart, PTZ presets
#: appear only once the camera answered); removing it at once would lose the
#: user's entity id, name and area when it comes back a moment later.
PRUNE_AFTER_REFRESHES = 3


def expected_unique_ids(coordinator: OpenNVRCoordinator) -> set[str]:
    """Unique ids of every entity this entry should have now, including the
    hand-written ones exactly when their platform creates them."""
    data = coordinator.data
    site = data.info.site_id
    ids = {f"{site}:{key}" for key in coordinator.known_keys}
    scopes = data.info.scopes
    if scopes is None or "live.view" in scopes:              # camera.py
        ids |= {f"{site}:camera.{cid}" for cid in data.cameras}
    if data.info.has("site_mode") and data.site_mode is not None:   # alarm panel
        ids.add(f"{site}:site.alarm")
    ids.add(f"{site}:site.update")                           # update.py
    return ids


def _camera_of(coordinator: OpenNVRCoordinator, key: str) -> int | None:
    """The OpenNVR camera an entity key belongs to, if any."""
    for desc in coordinator.data.catalog.descriptors:
        if desc.key == key:
            return desc.camera_id
    parts = key.split(".")
    return int(parts[1]) if len(parts) >= 2 and parts[0] == "camera" and parts[1].isdigit() else None


def wanted_device_identifiers(coordinator: OpenNVRCoordinator) -> set[tuple[str, str]]:
    """Identifiers of every device this entry should have now."""
    ids = {site_identifier(coordinator)}
    ids |= {camera_identifier(coordinator, cid) for cid in coordinator.data.cameras}
    for desc in coordinator.descriptors():
        ids |= device_info_for(coordinator, desc.device).get("identifiers", set())
    return ids


@callback
def async_prune(hass: HomeAssistant, coordinator: OpenNVRCoordinator,
                keep_unique_ids: Iterable[str] | None = None) -> None:
    """Remove this entry's entities whose descriptor (or camera) is gone, and
    then the devices left without entities. A failed refresh keeps the
    previous data, so nothing vanishes during an outage."""
    if coordinator.data is None:
        return
    entry = coordinator.config_entry
    data = coordinator.data
    keep = set(keep_unique_ids if keep_unique_ids is not None
               else expected_unique_ids(coordinator))
    missing = coordinator.prune_missing
    ent_reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if ent.unique_id in keep:
            missing.pop(ent.unique_id, None)
            continue
        cam = _camera_of(coordinator, ent.unique_id.split(":", 1)[-1])
        # A camera the user deselected: its entities go now. Anything else
        # must stay missing for a few refreshes first.
        deselected = cam is not None and cam in data.all_cameras and cam not in data.cameras
        first = missing.setdefault(ent.unique_id, coordinator.refreshes)
        if deselected or coordinator.refreshes - first >= PRUNE_AFTER_REFRESHES - 1:
            missing.pop(ent.unique_id, None)
            ent_reg.async_remove(ent.entity_id)
    dev_reg = dr.async_get(hass)
    wanted = wanted_device_identifiers(coordinator)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        if device.identifiers & wanted:
            continue
        if er.async_entries_for_device(ent_reg, device.id, include_disabled_entities=True):
            continue
        dev_reg.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
