# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenNVR device-firewall delegation (Settings → Firewall): unit tests for
OpennvrAuthClient.device_allowed plus the HTTP-gate 403 behaviour."""
from __future__ import annotations

import asyncio

from adapter_clients import OpennvrAuthClient

from tests.test_auth_gate import make_client, _h


# ── unit: device_allowed ───────────────────────────────────────────


class _StatusResp:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _status_client(seen, body):
    class _C:
        async def get(self, url, headers=None):
            seen.setdefault("calls", []).append((url, dict(headers or {})))
            return _StatusResp(body)
    return _C()


def _auth_with_status(seen, body):
    c = OpennvrAuthClient(base_url="http://srv")
    c._client = lambda: _status_client(seen, body)
    return c


def test_approved_device_allowed():
    seen: dict = {}
    c = _auth_with_status(seen, {"enforcement_active": True, "status": "approved"})
    assert asyncio.run(c.device_allowed("dev-1")) is True
    url, headers = seen["calls"][0]
    assert url.endswith("/api/v1/device-firewall/status")
    assert headers["x-device-token"] == "dev-1"


def test_pending_device_denied_when_enforced():
    c = _auth_with_status({}, {"enforcement_active": True, "status": "pending"})
    assert asyncio.run(c.device_allowed("dev-1")) is False


def test_enforcement_off_allows_anything():
    c = _auth_with_status({}, {"enforcement_active": False, "status": "unknown"})
    assert asyncio.run(c.device_allowed(None)) is True


def test_no_token_denied_when_enforced():
    seen: dict = {}
    c = _auth_with_status(seen, {"enforcement_active": True, "status": "unknown"})
    assert asyncio.run(c.device_allowed(None)) is False
    assert "x-device-token" not in seen["calls"][0][1]


def test_server_unreachable_fails_open_with_short_window(monkeypatch):
    c = OpennvrAuthClient(base_url="http://srv")

    class _Boom:
        async def get(self, url, headers=None):
            raise OSError("connection refused")
    c._client = lambda: _Boom()

    fake_now = [1000.0]
    monkeypatch.setattr("adapter_clients.time.monotonic", lambda: fake_now[0])
    assert asyncio.run(c.device_allowed("dev-1")) is True   # fail-open
    fake_now[0] += 11.0                                      # past the 10s window
    seen: dict = {}
    c._client = lambda: _status_client(seen, {"enforcement_active": True,
                                              "status": "pending"})
    assert asyncio.run(c.device_allowed("dev-1")) is False   # re-probed → denied
    assert seen["calls"]


def test_status_cached_per_device_token():
    seen: dict = {}
    c = _auth_with_status(seen, {"enforcement_active": True, "status": "approved"})

    async def many():
        for _ in range(5):
            await c.device_allowed("dev-1")
    asyncio.run(many())
    assert len(seen["calls"]) == 1


# ── the HTTP gate (uses the fixtures from test_auth_gate) ──────────


def test_blocked_device_gets_403_before_auth():
    auth, c = make_client(device_ok=False)
    r = c.get("/api/status", headers={**_h("tok-viewer"),
                                      "X-Device-Token": "dev-9"})
    assert r.status_code == 403
    assert r.json()["error"] == "device_not_approved"
    assert auth.device_tokens == ["dev-9"]
    # The bearer was never even consulted — device check comes first.
    assert auth.me_calls == 0


def test_open_paths_skip_the_device_check():
    auth, c = make_client(device_ok=False)
    assert c.get("/health").status_code == 200
    assert c.get("/demo").status_code == 200
    assert c.get("/agent").status_code == 200
    assert auth.device_calls == 0
