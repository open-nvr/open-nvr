# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Typed views of OpenNVR API payloads.

Tolerant readers (design §6.11): unknown fields are kept in ``raw`` and
otherwise ignored, missing optional fields default, so a newer server with
additive changes never breaks an older client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


#: Descriptor platforms this library knows how to model. Unknown ones are
#: skipped (and reported), never an error.
KNOWN_PLATFORMS = frozenset({"sensor", "binary_sensor", "switch", "select", "button",
                             "number", "event", "image"})


def _get(d: dict, key: str, default: Any = None) -> Any:
    value = d.get(key, default)
    return default if value is None and default is not None else value


@dataclass(frozen=True)
class SystemInfo:
    site_id: str
    name: str
    version: str
    contract_version: str
    features: tuple[str, ...]
    recording_pause_enabled: bool
    uptime_s: int
    latest_version: str | None
    #: Who asked (contract 1.1, ``caller_info``): for a token its name,
    #: effective scopes, cameras and expiry. Empty from older servers.
    caller: dict = field(compare=False, default_factory=dict)
    #: The server's clock (contract 1.1, ``network_info``); None from older servers.
    server_time: str | None = None
    #: ``{"webrtc_ice_hosts": bool|None, "rtsps_exposed": bool}``; empty from
    #: older servers.
    network: dict = field(compare=False, default_factory=dict)
    raw: dict = field(repr=False, compare=False, default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> SystemInfo:
        return cls(site_id=str(d["site_id"]), name=str(_get(d, "name", "OpenNVR")),
                   version=str(_get(d, "version", "unknown")),
                   contract_version=str(d["contract_version"]),
                   features=tuple(_get(d, "features", [])),
                   recording_pause_enabled=bool(_get(d, "recording_pause_enabled", False)),
                   uptime_s=int(_get(d, "uptime_s", 0)),
                   latest_version=d.get("latest_version"),
                   caller=dict(d.get("caller") or {}), server_time=d.get("server_time"),
                   network=dict(d.get("network") or {}), raw=d)

    def has(self, feature: str) -> bool:
        return feature in self.features

    @property
    def scopes(self) -> frozenset[str] | None:
        """The calling token's effective scopes; None if the server doesn't
        say (older than contract 1.1) or the caller is not a token."""
        if self.caller.get("kind") != "token" or "scopes" not in self.caller:
            return None
        return frozenset(self.caller["scopes"])

    @property
    def token_expires_at(self) -> str | None:
        return self.caller.get("expires_at") if self.caller.get("kind") == "token" else None


@dataclass(frozen=True)
class Camera:
    id: int
    name: str
    is_active: bool
    detection_enabled: bool
    recording_state: str | None
    raw: dict = field(repr=False, compare=False, default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> Camera:
        return cls(id=int(d["id"]), name=str(d.get("name") or f"Camera {d['id']}"),
                   is_active=bool(d.get("is_active", True)),
                   detection_enabled=d.get("detection_enabled") is not False,
                   recording_state=d.get("recording_state"), raw=d)


@dataclass(frozen=True)
class StreamInfo:
    camera_id: int
    stream_name: str
    token: str
    webrtc_url: str | None
    rtsps_url: str | None
    raw: dict = field(repr=False, compare=False, default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> StreamInfo:
        urls = d.get("urls") or {}
        return cls(camera_id=int(d["camera_id"]), stream_name=str(d["stream_name"]),
                   token=str(d["token"]), webrtc_url=urls.get("webrtc"),
                   rtsps_url=urls.get("rtsps"), raw=d)


@dataclass(frozen=True)
class Zone:
    id: int
    camera_id: int
    name: str
    polygon: tuple[tuple[float, float], ...]
    labels: tuple[str, ...] | None

    @classmethod
    def from_dict(cls, d: dict) -> Zone:
        labels = d.get("labels")
        return cls(id=int(d["id"]), camera_id=int(d["camera_id"]), name=str(d["name"]),
                   polygon=tuple((float(x), float(y)) for x, y in d.get("polygon") or []),
                   labels=tuple(labels) if labels else None)


@dataclass(frozen=True)
class EntityDescriptor:
    """One server-described entity (design §6.10)."""

    key: str
    platform: str
    name: str
    device: dict
    required_scope: str
    origin: str
    enabled_default: bool
    camera_id: int | None = None
    translation_key: str | None = None
    device_class: str | None = None
    unit: str | None = None
    state_class: str | None = None
    entity_category: str | None = None
    icon: str | None = None
    options: Any = None
    event_types: tuple[str, ...] | None = None
    command: dict | None = None
    raw: dict = field(repr=False, compare=False, default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> EntityDescriptor:
        ev = d.get("event_types")
        return cls(key=str(d["key"]), platform=str(d["platform"]), name=str(d["name"]),
                   device=dict(d.get("device") or {}),
                   required_scope=str(d.get("required_scope", "")),
                   origin=str(d.get("origin", "core")),
                   enabled_default=bool(d.get("enabled_default", True)),
                   camera_id=d.get("camera_id"), translation_key=d.get("translation_key"),
                   device_class=d.get("device_class"), unit=d.get("unit"),
                   state_class=d.get("state_class"), entity_category=d.get("entity_category"),
                   icon=d.get("icon"), options=d.get("options"),
                   event_types=tuple(ev) if ev else None, command=d.get("command"), raw=d)


@dataclass(frozen=True)
class EntityCatalog:
    etag: str
    descriptors: tuple[EntityDescriptor, ...]
    #: Descriptors whose platform this client does not know; kept so the
    #: integration can list them in diagnostics instead of failing.
    skipped: tuple[dict, ...] = ()

    @classmethod
    def from_dict(cls, d: dict) -> EntityCatalog:
        """``GET /entities``: descriptors of platforms this client knows, the
        rest in ``skipped``."""
        known, skipped = [], []
        for e in d.get("entities", []):
            (known if e.get("platform") in KNOWN_PLATFORMS else skipped).append(e)
        return cls(etag=str(d.get("etag", "")),
                   descriptors=tuple(EntityDescriptor.from_dict(e) for e in known),
                   skipped=tuple(skipped))


@dataclass(frozen=True)
class SignedMedia:
    url: str
    expires_at: str


@dataclass(frozen=True)
class SiteMode:
    mode: str
    changed_at: str | None
    changed_by: str | None
    modes: tuple[str, ...]

    @classmethod
    def from_dict(cls, d: dict) -> SiteMode:
        return cls(mode=str(d["mode"]), changed_at=d.get("changed_at"),
                   changed_by=d.get("changed_by"),
                   modes=tuple(d.get("modes") or ("disarmed", "armed_home", "armed_away")))
