"""Base entity and devices for OpenNVR.

Devices (design §7.4): the server, one per camera (via the server), one per
zone (via its camera), one per app-level device (via the server). A
descriptor's ``device`` says which one an entity belongs to.

Unique ids are ``<site_id>:<descriptor key>``. Keys embed the camera or zone
id (``camera.3.motion``, ``zone.7.count.person``) and never a name, so they
survive renames.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OpenNVRCoordinator


def site_identifier(coordinator: OpenNVRCoordinator) -> tuple[str, str]:
    return (DOMAIN, coordinator.data.info.site_id)


def camera_identifier(coordinator: OpenNVRCoordinator, camera_id: int) -> tuple[str, str]:
    return (DOMAIN, f"{coordinator.data.info.site_id}:camera:{camera_id}")


def site_device_info(coordinator: OpenNVRCoordinator) -> DeviceInfo:
    info = coordinator.data.info
    name = info.name if info.name.lower().startswith("opennvr") else f"OpenNVR {info.name}"
    return DeviceInfo(identifiers={site_identifier(coordinator)}, name=name,
                      manufacturer="OpenNVR", model="OpenNVR server", sw_version=info.version,
                      configuration_url=coordinator.config_entry.data[CONF_URL])


def camera_device_info(coordinator: OpenNVRCoordinator, camera_id: int) -> DeviceInfo:
    cam = coordinator.data.all_cameras.get(camera_id)
    raw = cam.raw if cam else {}
    return DeviceInfo(identifiers={camera_identifier(coordinator, camera_id)},
                      name=cam.name if cam else f"Camera {camera_id}",
                      manufacturer=raw.get("manufacturer") or "OpenNVR",
                      model=raw.get("model") or "Camera")


def device_info_for(coordinator: OpenNVRCoordinator, device: dict[str, Any]) -> DeviceInfo:
    """The device a descriptor's ``device`` names. Its parent (via device)
    is linked by ``async_register_devices``, not here: HA 2026.9 takes the
    parent's registry id, which an entity doesn't know."""
    kind, dev_id = device.get("kind"), device.get("id")
    site_id = coordinator.data.info.site_id
    if kind == "camera" and dev_id is not None:
        return camera_device_info(coordinator, int(dev_id))
    if kind == "zone" and dev_id is not None:
        return DeviceInfo(identifiers={(DOMAIN, f"{site_id}:zone:{dev_id}")},
                          name=str(device.get("name") or f"Zone {dev_id}"),
                          manufacturer="OpenNVR", model="Zone")
    if kind == "app" and dev_id is not None:
        return DeviceInfo(identifiers={(DOMAIN, f"{site_id}:app:{dev_id}")},
                          name=str(device.get("name") or dev_id),
                          manufacturer="OpenNVR", model="App")
    return site_device_info(coordinator)


def parent_identifier(coordinator: OpenNVRCoordinator,
                      device: dict[str, Any]) -> tuple[str, str] | None:
    """The device a descriptor's device hangs off: a zone its camera, the
    rest the server; the server itself none."""
    kind = device.get("kind")
    if kind == "zone" and device.get("camera_id") is not None:
        return camera_identifier(coordinator, int(device["camera_id"]))
    if kind in ("camera", "zone", "app") and device.get("id") is not None:
        return site_identifier(coordinator)
    return None


@callback
def async_register_devices(hass: HomeAssistant, coordinator: OpenNVRCoordinator) -> None:
    """Create or update the device tree, parents first: the server, its
    cameras, then the zones and app devices the catalogue names. Runs before
    platforms add entities, and after every refresh (new zones, renames)."""
    reg = dr.async_get(hass)
    entry_id = coordinator.config_entry.entry_id
    ids: dict[tuple[str, str], str] = {}

    def create(info: DeviceInfo, parent: tuple[str, str] | None) -> None:
        via = ids.get(parent) if parent else None
        device = reg.async_get_or_create(config_entry_id=entry_id, **info,
                                         **({"via_device_id": via} if via else {}))
        for identifier in info["identifiers"]:
            ids[identifier] = device.id

    create(site_device_info(coordinator), None)
    for camera_id in coordinator.data.cameras:
        create(camera_device_info(coordinator, camera_id), site_identifier(coordinator))
    seen: set[tuple[str, str]] = set(ids)
    for desc in coordinator.descriptors():
        info = device_info_for(coordinator, desc.device)
        if info["identifiers"] & seen:
            continue
        seen |= info["identifiers"]
        create(info, parent_identifier(coordinator, desc.device))


class OpenNVREntity(CoordinatorEntity[OpenNVRCoordinator]):
    """Base of every OpenNVR entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: OpenNVRCoordinator, key: str,
                 device: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self.key = key
        self._attr_unique_id = f"{coordinator.data.info.site_id}:{key}"
        self._attr_device_info = device_info_for(coordinator, device)
