# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The MQTT integration's settings and connection (HA-401).

An ``Integration`` row of type ``mqtt`` holds, in ``config``:

* ``broker_url``: ``mqtt://host[:1883]`` or ``mqtts://host[:8883]`` (TLS);
* ``username`` / ``password``: optional broker credentials;
* ``topic_prefix``: OpenNVR's own topics, default ``opennvr``;
* ``ha_discovery``: publish Home Assistant MQTT discovery (default on);
* ``discovery_prefix``: Home Assistant's, default ``homeassistant``;
* ``api_token_id``: the API token whose scopes and cameras bound what is
  published and which ``.../set`` commands run (required for discovery:
  MQTT has no user of its own);
* ``tls_insecure``: accept a broker certificate that doesn't verify.
"""

from __future__ import annotations

import ipaddress
import re
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

_TOPIC_PART = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class MqttSettings:
    host: str
    port: int
    tls: bool
    username: str | None
    password: str | None
    topic_prefix: str
    ha_discovery: bool
    discovery_prefix: str
    api_token_id: int | None
    tls_insecure: bool

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> MqttSettings:
        """Parsed and checked; ValueError with an operator-facing reason."""
        c = dict(config or {})
        url = str(c.get("broker_url") or "").strip()
        if "://" not in url:
            url = f"mqtt://{url}"
        parts = urlsplit(url)
        if parts.scheme not in ("mqtt", "mqtts"):
            raise ValueError("broker_url must start with mqtt:// or mqtts://")
        host = (parts.hostname or "").strip()
        if not host:
            raise ValueError("broker_url has no host")
        try:
            port = parts.port or (8883 if parts.scheme == "mqtts" else 1883)
        except ValueError as exc:
            raise ValueError("broker_url has an invalid port") from exc
        _refuse_metadata(host)
        prefix = str(c.get("topic_prefix") or "opennvr").strip("/")
        discovery = str(c.get("discovery_prefix") or "homeassistant").strip("/")
        for name, value in (("topic_prefix", prefix), ("discovery_prefix", discovery)):
            if not all(_TOPIC_PART.fullmatch(p) for p in value.split("/")):
                raise ValueError(f"{name} may use letters, digits, _ and - only")
        token_id = c.get("api_token_id")
        if token_id is not None and (isinstance(token_id, bool) or not str(token_id).isdigit()):
            raise ValueError("api_token_id must be a token id")
        ha = _bool(c.get("ha_discovery", True), "ha_discovery")
        if ha and token_id is None:
            raise ValueError("Home Assistant discovery needs an API token (api_token_id): "
                             "it decides which cameras and entities are published")
        return cls(host=host, port=int(port), tls=parts.scheme == "mqtts",
                   username=c.get("username") or None, password=c.get("password") or None,
                   topic_prefix=prefix, ha_discovery=ha, discovery_prefix=discovery,
                   api_token_id=int(token_id) if token_id is not None else None,
                   tls_insecure=_bool(c.get("tls_insecure", False), "tls_insecure"))


def _bool(value: Any, name: str) -> bool:
    """A JSON boolean, or its usual spellings; the string "false" is false."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off", "", "none"):
        return False
    raise ValueError(f"{name} must be true or false")


def _refuse_metadata(host: str) -> None:
    from core.config import _METADATA_ADDRESSES

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return  # a name, not an address
    if addr in _METADATA_ADDRESSES:
        raise ValueError("Refusing to connect to the cloud metadata address")


async def check_resolved(host: str) -> None:
    """Refuse a broker NAME that resolves to the cloud metadata address
    (``from_config`` only sees literal addresses)."""
    import asyncio
    import socket

    from core.config import _METADATA_ADDRESSES

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None,
                                                             type=socket.SOCK_STREAM)
    except OSError:
        return  # unresolvable: the connection fails on its own
    for info in infos:
        try:
            addr = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            continue
        if addr in _METADATA_ADDRESSES:
            raise ValueError("Refusing to connect to the cloud metadata address")


def failure(exc: BaseException) -> str:
    """Why a connection failed, in words that don't echo the socket error
    (which would make the Test button a port scanner's oracle)."""
    text = str(exc).lower()
    for needle, words in (
            ("not authorized", "the broker refused the username or password"),
            ("bad user name or password", "the broker refused the username or password"),
            ("certificate", "the broker's TLS certificate did not verify"),
            ("ssl", "the TLS handshake failed"),
            ("refused", "the connection was refused"),
            ("timed out", "the connection timed out"),
            ("name or service not known", "the broker's name did not resolve"),
            ("nodename nor servname", "the broker's name did not resolve"),
            ("temporary failure in name resolution", "the broker's name did not resolve")):
        if needle in text:
            return words
    return "the broker could not be reached"


def client(settings: MqttSettings, *, identifier: str | None = None, will=None):
    """An ``aiomqtt.Client`` (use as ``async with``)."""
    import aiomqtt

    tls_context = None
    if settings.tls:
        tls_context = ssl.create_default_context()
        if settings.tls_insecure:
            tls_context.check_hostname = False
            tls_context.verify_mode = ssl.CERT_NONE
    return aiomqtt.Client(settings.host, settings.port, username=settings.username,
                          password=settings.password, identifier=identifier, will=will,
                          tls_context=tls_context, timeout=10, keepalive=30)


async def test_connection(config: dict[str, Any]) -> dict[str, Any]:
    """Connect, publish one retained-free message, disconnect."""
    try:
        settings = MqttSettings.from_config({**config, "ha_discovery": False})
    except ValueError as exc:
        return {"success": False, "message": str(exc)}
    try:
        await check_resolved(settings.host)
        async with client(settings) as c:
            await c.publish(f"{settings.topic_prefix}/test", "OpenNVR MQTT test", qos=1)
    except ValueError as exc:
        return {"success": False, "message": str(exc)}
    except Exception as exc:  # reported to the operator, classified
        return {"success": False, "message": f"Could not reach the broker: {failure(exc)}"}
    return {"success": True,
            "message": f"Connected to {settings.host}:{settings.port} and published "
                       f"to {settings.topic_prefix}/test"}


async def publish_once(config: dict[str, Any], suffix: str, payload: dict[str, Any]) -> dict:
    """Connect, publish JSON to ``<topic_prefix>/<suffix>``, disconnect: how
    alerts reach an MQTT integration (they are rare; no standing connection)."""
    import json

    try:
        settings = MqttSettings.from_config({**config, "ha_discovery": False})
        await check_resolved(settings.host)
        async with client(settings) as c:
            await c.publish(f"{settings.topic_prefix}/{suffix}",
                            json.dumps(payload, default=str), qos=1)
    except ValueError as exc:
        return {"success": False, "message": str(exc)}
    except Exception as exc:  # reported per integration by the caller
        return {"success": False, "message": failure(exc)}
    return {"success": True, "message": "published"}
