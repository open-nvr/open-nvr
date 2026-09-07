# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""The apps-bus failover, against the real nats-py client and a minimal
NATS server on loopback: an unresolvable apps bus first in the pool, the
platform bus second — the client must land on the second with the site
token from its URI, and ``connected_via_fallback`` must say so."""
from __future__ import annotations

import asyncio
import json

import pytest

from opennvr_app_sdk import credentials as creds_mod


class _MiniNats:
    """Enough of the NATS wire protocol to accept one client: INFO, then
    CONNECT + PING → +OK/PONG. Records the CONNECT options."""

    def __init__(self):
        self.connects: list[dict] = []
        self.server = None

    async def _handle(self, reader, writer):
        writer.write(b'INFO {"server_id":"mini","version":"2.10.0","proto":1,"max_payload":1048576,"auth_required":true}\r\n')
        await writer.drain()
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if not line:
                    break
                if line.startswith(b"CONNECT "):
                    self.connects.append(json.loads(line[8:].decode()))   # +OK only when verbose
                elif line.startswith(b"PING"):
                    writer.write(b"PONG\r\n")
                    await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_client_fails_over_to_the_platform_bus_with_the_site_token(monkeypatch):
    import nats

    mini = _MiniNats()
    port = await mini.start()
    key = "oak_my-app_" + "f" * 32
    monkeypatch.setenv("OPENNVR_APP_KEY", key)
    monkeypatch.setenv("OPENNVR_APP_BUS_URL", "nats://nats-apps.invalid:4222")   # never resolves
    kwargs = creds_mod.bus_connection(creds_mod.AppCredentials(), f"nats://127.0.0.1:{port}", "site-secret")
    assert kwargs["servers"][0].startswith("nats://my-app:oak_") and kwargs["dont_randomize"] is True

    nc = await nats.connect(**kwargs, connect_timeout=2.0, max_reconnect_attempts=3, reconnect_time_wait=0.1)
    try:
        assert nc.is_connected
        assert creds_mod.connected_via_fallback(nc, kwargs) is True
        assert mini.connects and mini.connects[0].get("auth_token") == "site-secret"
        assert "user" not in mini.connects[0] or mini.connects[0].get("user") in (None, "")
    finally:
        await nc.close()
    mini.server.close()


@pytest.mark.asyncio
async def test_client_prefers_the_apps_bus_when_it_answers(monkeypatch):
    import nats

    apps, platform = _MiniNats(), _MiniNats()
    apps_port, platform_port = await apps.start(), await platform.start()
    key = "oak_my-app_" + "e" * 32
    monkeypatch.setenv("OPENNVR_APP_KEY", key)
    monkeypatch.setenv("OPENNVR_APP_BUS_URL", f"nats://127.0.0.1:{apps_port}")
    kwargs = creds_mod.bus_connection(creds_mod.AppCredentials(), f"nats://127.0.0.1:{platform_port}", "site-secret")
    nc = await nats.connect(**kwargs, connect_timeout=2.0)
    try:
        assert creds_mod.connected_via_fallback(nc, kwargs) is False
        assert apps.connects[0].get("user") == "my-app" and apps.connects[0].get("pass") == key
        assert platform.connects == []
    finally:
        await nc.close()
    apps.server.close(); platform.server.close()
