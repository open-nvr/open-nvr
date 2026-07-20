# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
CameraDriver abstraction — the vendor-neutral seam for reading and (later)
writing camera device settings.

Design guarantees baked into this base class:

* **No IP change, no factory reset — ever.** There is deliberately no
  ``set_network``/``set_ip``/``factory_reset`` method anywhere in this
  hierarchy. The safety property is structural: you cannot call what does
  not exist. (Losing the credential + moving the IP is how cameras get
  bricked out of reach; OpenNVR will not do it.)
* **"Unsupported" is data, not an exception.** Read methods return a result
  object with ``supported=False`` when the device/driver can't do it, so the
  UI can gray out cleanly. Only auth/transport failures raise.

Phase 0 implements identity, capability, network (read), and storage (read).
Later phases add imaging/encoder/time/osd/motion/events/users as overrides on
the concrete drivers; the base provides ``supported=False`` defaults so each
phase is additive.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Result dataclasses (all JSON-serializable via to_dict)
# ---------------------------------------------------------------------------


@dataclass
class DeviceInfo:
    manufacturer: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    serial_number: str | None = None
    hardware_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NetworkInfo:
    supported: bool = False
    mac_address: str | None = None
    ip_address: str | None = None
    subnet_mask: str | None = None
    gateway: str | None = None
    dns_primary: str | None = None
    dns_secondary: str | None = None
    dhcp: bool | None = None
    mtu: int | None = None
    source: str | None = None  # "onvif" | "isapi" — where the data came from

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StorageSlot:
    name: str | None = None
    status: str | None = None
    capacity_mb: int | None = None
    free_mb: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StorageInfo:
    supported: bool = False
    present: bool = False  # a card/disk is actually inserted
    slots: list[StorageSlot] = field(default_factory=list)
    # Network storage: which mount types the device supports and what is mounted
    nas_supported: bool = False
    nas_mount_types: list[str] = field(default_factory=list)
    nas_mounts: list[dict[str, Any]] = field(default_factory=list)
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "present": self.present,
            "slots": [s.to_dict() for s in self.slots],
            "nas_supported": self.nas_supported,
            "nas_mount_types": self.nas_mount_types,
            "nas_mounts": self.nas_mounts,
            "source": self.source,
        }


@dataclass
class ImagingInfo:
    supported: bool = False
    # current values: brightness/contrast/saturation/sharpness (int),
    # ir_cut_filter/wdr/backlight (str mode)
    settings: dict[str, Any] = field(default_factory=dict)
    # per-field constraints: {brightness:{min,max}}, {ir_cut_filter:{options:[...]}}
    ranges: dict[str, Any] = field(default_factory=dict)
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EncoderInfo:
    supported: bool = False
    # one entry per video-encoder configuration (main/sub); each carries its
    # current values (token, encoding, width, height, fps, bitrate, gov_length)
    # and an "options" block (resolutions[], fps_range, gov_range, encodings).
    configs: list[dict[str, Any]] = field(default_factory=list)
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"supported": self.supported, "configs": self.configs, "source": self.source}


@dataclass
class TimeInfo:
    supported: bool = True
    mode: str | None = None  # "manual" | "ntp"
    utc_datetime: str | None = None  # ISO 8601, no tz suffix
    local_datetime: str | None = None
    timezone: str | None = None  # POSIX TZ string (e.g. "CST-8:00:00")
    daylight_savings: bool | None = None
    ntp_from_dhcp: bool | None = None
    ntp_server: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OsdInfo:
    supported: bool = False
    datetime_enabled: bool | None = None
    channel_name_enabled: bool | None = None
    # Slot 1 kept as flat fields for backward compatibility; ``slots`` carries
    # every text overlay the device exposes: {id, enabled, text, x, y}.
    text_enabled: bool | None = None
    text: str | None = None
    slots: list[dict[str, Any]] = field(default_factory=list)
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MotionInfo:
    supported: bool = False
    enabled: bool | None = None
    sensitivity: int | None = None
    sensitivity_max: int = 100
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ServiceEntry:
    """One network service the camera exposes (UPnP, SNMP, FTP, DDNS, …).

    ``writable`` is False for services whose config is too complex to drive
    safely from here (QoS, event HTTP hosts) — those are shown read-only.
    """

    key: str
    label: str
    supported: bool = False
    enabled: bool | None = None
    writable: bool = False
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ServicesInfo:
    supported: bool = False
    services: list[ServiceEntry] = field(default_factory=list)
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "services": [s.to_dict() for s in self.services],
            "source": self.source,
        }


@dataclass
class SecurityInfo:
    """Camera-side security posture.

    ``ip_filter`` is the lockout-capable one: in ``allow`` mode the camera
    refuses every source not on the list, so the list MUST contain the address
    OpenNVR reaches it from. See ``feature.md``.
    """

    supported: bool = False
    ip_filter_supported: bool = False
    ip_filter_enabled: bool | None = None
    ip_filter_mode: str | None = None  # "allow" | "deny"
    ip_filter_entries: list[str] = field(default_factory=list)
    ip_filter_max: int | None = None
    # The source address the camera actually sees us as — the address that must
    # be on the allowlist. Computed from the live socket, never guessed.
    server_ip: str | None = None
    illegal_login_lock: bool | None = None
    cloud_supported: bool = False
    cloud_enabled: bool | None = None
    cloud_host: str | None = None
    # Read-only: disabling the HTTP port would cut OpenNVR off from the camera,
    # so protocol toggles are reported but deliberately not writable here.
    protocols: list[dict[str, Any]] = field(default_factory=list)
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SmartDetector:
    """One analytics detector (line crossing, intrusion, face, tamper…).

    ``configured`` reports whether the detector actually has a line/region
    drawn on the device. Several detectors happily report ``enabled=true``
    while doing nothing because no region was ever defined — the UI must be
    able to say so rather than implying the feature is live. ``None`` means
    "not applicable" (e.g. face detection needs no region).
    """

    key: str
    label: str
    supported: bool = False
    enabled: bool | None = None
    sensitivity: int | None = None
    sensitivity_max: int = 100
    configured: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SmartInfo:
    supported: bool = False
    detectors: list[SmartDetector] = field(default_factory=list)
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "detectors": [d.to_dict() for d in self.detectors],
            "source": self.source,
        }


@dataclass
class UsersInfo:
    supported: bool = False
    # each: {id, name, level, is_current}  (is_current = the account OpenNVR uses)
    users: list[dict[str, Any]] = field(default_factory=list)
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Capabilities:
    driver_name: str = "onvif"
    supported_areas: dict[str, bool] = field(default_factory=dict)
    onvif_endpoints: dict[str, str] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# The canonical list of capability areas the UI knows how to render. A driver
# reports which of these it can actually drive on a given device.
CAPABILITY_AREAS = (
    "info",
    "imaging",
    "encoder",
    "time",
    "osd",
    "motion",
    "smart",  # analytics: line crossing, intrusion, face, tamper
    "security",  # camera-side IP filter / cloud / login lock
    "services",  # UPnP / SNMP / FTP / DDNS / QoS / event hosts
    "maintenance",  # reboot / config backup (only where a driver implements them)
    "events",
    "network",  # read-only display
    "storage",  # read-only display
    "ptz",
    "users",
    "audio",
)


# ---------------------------------------------------------------------------
# Base driver
# ---------------------------------------------------------------------------


class CameraDriver(ABC):
    """Vendor-neutral camera device driver.

    Concrete drivers (OnvifDriver, HikvisionIsapiDriver, …) are constructed by
    ``services.camera_drivers.registry.get_driver_for_camera`` with the
    camera's resolved HTTP control port and decrypted credentials.
    """

    driver_name: str = "base"

    def __init__(
        self,
        *,
        camera_id: int,
        ip: str,
        username: str | None,
        password: str | None,
        http_port: int = 80,
    ) -> None:
        self.camera_id = camera_id
        self.ip = ip
        self.username = username or ""
        self.password = password or ""
        self.http_port = http_port

    # --- identity / capability (required) ---

    @abstractmethod
    async def get_info(self) -> DeviceInfo:
        """Read manufacturer/model/firmware/serial. Raises on auth failure."""

    @abstractmethod
    async def get_capabilities(self) -> Capabilities:
        """Report which capability areas this device+driver supports."""

    # --- read-only device state (Phase 0) ---

    async def get_network(self) -> NetworkInfo:
        """Read the camera's network configuration (display-only)."""
        return NetworkInfo(supported=False)

    async def get_storage(self) -> StorageInfo:
        """Read SD/HDD storage status (display-only)."""
        return StorageInfo(supported=False)

    async def get_osd(self) -> OsdInfo:
        """Read on-screen-display overlays (date/time, channel name, text)."""
        return OsdInfo(supported=False)

    async def set_osd(self, patch: dict[str, Any]) -> OsdInfo:
        raise NotImplementedError("OSD not supported by this driver")

    async def get_motion(self) -> MotionInfo:
        """Read motion-detection state (enabled, sensitivity)."""
        return MotionInfo(supported=False)

    async def set_motion(self, patch: dict[str, Any]) -> MotionInfo:
        raise NotImplementedError("Motion config not supported by this driver")

    async def get_smart(self) -> SmartInfo:
        """Read analytics detectors (line crossing, intrusion, face, tamper)."""
        return SmartInfo(supported=False)

    async def set_smart(self, key: str, patch: dict[str, Any]) -> SmartInfo:
        raise NotImplementedError("Smart detection not supported by this driver")

    async def get_services(self) -> ServicesInfo:
        """Read the camera's network services (UPnP/SNMP/FTP/DDNS/…)."""
        return ServicesInfo(supported=False)

    async def set_service(self, key: str, enabled: bool) -> ServicesInfo:
        raise NotImplementedError("Service toggles not supported by this driver")

    async def get_snapshot(self) -> bytes:
        """Return a JPEG still from the camera."""
        raise NotImplementedError("Snapshot not supported by this driver")

    async def export_config(self, secret_key: str) -> bytes:
        """Return the camera's full configuration blob (backup).

        Vendors typically encrypt the blob with ``secret_key``; the same key is
        required to restore it, so it is the caller's to keep.
        """
        raise NotImplementedError("Config export not supported by this driver")

    async def get_security(self) -> SecurityInfo:
        """Read camera-side security posture (IP filter, cloud, lockout)."""
        return SecurityInfo(supported=False)

    async def set_ip_filter(
        self, *, enabled: bool, mode: str, entries: list[str]
    ) -> SecurityInfo:
        raise NotImplementedError("IP filtering not supported by this driver")

    async def set_cloud(self, enabled: bool) -> SecurityInfo:
        raise NotImplementedError("Cloud toggle not supported by this driver")

    async def subscribe_events(self):
        """Async generator of normalized camera events. Base is unsupported;
        the ``yield`` makes this an async generator so callers can iterate."""
        raise NotImplementedError("Event subscription not supported by this driver")
        yield {}  # pragma: no cover — unreachable, defines the generator type

    # --- on-camera accounts + system (gated, vendor-implemented) ---

    async def get_users(self) -> UsersInfo:
        """List on-camera user accounts."""
        return UsersInfo(supported=False)

    async def create_user(self, username: str, password: str, level: str) -> UsersInfo:
        raise NotImplementedError("User management not supported by this driver")

    async def delete_user(self, username: str) -> UsersInfo:
        raise NotImplementedError("User management not supported by this driver")

    async def reboot(self) -> dict[str, Any]:
        raise NotImplementedError("Reboot not supported by this driver")

    # NOTE: there is deliberately no set_network / set_ip / factory_reset method
    # anywhere in this hierarchy — losing the credential + moving the IP is how
    # cameras get bricked out of reach, so OpenNVR structurally cannot do it.
