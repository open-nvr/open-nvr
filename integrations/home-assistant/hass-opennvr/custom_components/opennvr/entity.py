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
                      model=raw.get("model") or "Camera",
                      via_device=site_identifier(coordinator))


def device_info_for(coordinator: OpenNVRCoordinator, device: dict[str, Any]) -> DeviceInfo:
    """The device a descriptor's ``device`` names."""
    kind, dev_id = device.get("kind"), device.get("id")
    site_id = coordinator.data.info.site_id
    if kind == "camera" and dev_id is not None:
        return camera_device_info(coordinator, int(dev_id))
    if kind == "zone" and dev_id is not None:
        cam_id = device.get("camera_id")
        return DeviceInfo(
            identifiers={(DOMAIN, f"{site_id}:zone:{dev_id}")},
            name=str(device.get("name") or f"Zone {dev_id}"),
            manufacturer="OpenNVR", model="Zone",
            via_device=(camera_identifier(coordinator, int(cam_id)) if cam_id is not None
                        else site_identifier(coordinator)))
    if kind == "app" and dev_id is not None:
        return DeviceInfo(identifiers={(DOMAIN, f"{site_id}:app:{dev_id}")},
                          name=str(device.get("name") or dev_id),
                          manufacturer="OpenNVR", model="App",
                          via_device=site_identifier(coordinator))
    return site_device_info(coordinator)


class OpenNVREntity(CoordinatorEntity[OpenNVRCoordinator]):
    """Base of every OpenNVR entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: OpenNVRCoordinator, key: str,
                 device: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self.key = key
        self._attr_unique_id = f"{coordinator.data.info.site_id}:{key}"
        self._attr_device_info = device_info_for(coordinator, device)
