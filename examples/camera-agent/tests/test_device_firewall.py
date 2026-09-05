# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenNVR device-firewall delegation (Settings → Firewall): the agent asks
the server whether the calling browser's device token is approved, before
the bearer check. Enforcement off ⇒ open; on ⇒ only approved devices."""
from __future__ import annotations

import asyncio
import logging

from fastapi.testclient import TestClient

import camera_agent as ca
from adapter_clients import OpennvrAuthClient
from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec


# ── unit: OpennvrAuthClient.device_allowed ─────────────────────────


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
    assert asyncio.run(c.device_allowed("whatever")) is True


def test_no_token_denied_when_enforced():
    seen: dict = {}
    c = _auth_with_status(seen, {"enforcement_active": True, "status": "unknown"})
    assert asyncio.run(c.device_allowed(None)) is False
    # No token → no x-device-token header sent.
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
    # Cached under the SHORT failure TTL, not the normal one: after 11s the
    # next call re-probes (and here succeeds with a denial).
    fake_now[0] += 11.0
    seen: dict = {}
    c._client = lambda: _status_client(seen, {"enforcement_active": True,
                                              "status": "pending"})
    assert asyncio.run(c.device_allowed("dev-1")) is False
    assert seen["calls"]  # re-probed


def test_status_cached_per_device_token():
    seen: dict = {}
    c = _auth_with_status(seen, {"enforcement_active": True, "status": "approved"})

    async def many():
        for _ in range(5):
            await c.device_allowed("dev-1")
    asyncio.run(many())
    assert len(seen["calls"]) == 1


# ── the HTTP gate + WS ─────────────────────────────────────────────


class _FakeAuth:
    def __init__(self, device_ok=True):
        self.device_ok = device_ok
        self.device_calls = 0
        self.device_tokens: list = []

    async def me(self, token):
        return ({"username": "v", "is_superuser": False, "role_name": "viewer"}
                if token == "tok-viewer" else None)

    async def visible_cameras(self, token, user=None):
        return None   # unrestricted — per-camera scope has its own tests

    async def device_allowed(self, device_token):
        self.device_calls += 1
        self.device_tokens.append(device_token)
        return self.device_ok

    async def aclose(self):
        pass


def _client(auth_mode="opennvr", device_ok=True):
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        auth_mode=auth_mode, opennvr_api_url="http://srv",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg",
                            role="front")],
    )
    rt = CameraAgentRuntime(cfg)
    rt.auth = _FakeAuth(device_ok=device_ok)
    return rt, TestClient(build_app(rt))


def test_blocked_device_gets_403_before_auth_probe():
    rt, c = _client(device_ok=False)
    r = c.get("/cameras", headers={"Authorization": "Bearer tok-viewer",
                                   "X-Device-Token": "dev-9"})
    assert r.status_code == 403
    assert r.json()["error"] == "device_not_approved"
    assert rt.auth.device_tokens == ["dev-9"]


def test_allowed_device_passes_through_to_bearer_check():
    rt, c = _client(device_ok=True)
    assert c.get("/cameras").status_code == 401          # no bearer
    assert c.get("/cameras", headers={"Authorization": "Bearer tok-viewer"}
                 ).status_code == 200


def test_cookie_fallback_carries_device_identity():
    rt, c = _client(device_ok=True)
    c.get("/cameras", headers={"Authorization": "Bearer tok-viewer"},
          cookies={"opennvr_device": "dev-cookie"})
    assert "dev-cookie" in rt.auth.device_tokens


def test_open_paths_skip_the_device_check():
    rt, c = _client(device_ok=False)
    assert c.get("/health").status_code == 200
    assert c.get("/demo").status_code == 200
    assert c.get("/agent").status_code == 200
    assert rt.auth.device_calls == 0


def test_none_mode_makes_no_device_calls():
    rt, c = _client(auth_mode="none")
    assert c.get("/cameras").status_code == 200
    assert rt.auth.device_calls == 0


def test_redaction_covers_device_token():
    f = ca._RedactTokensFilter()
    rec = logging.LogRecord("t", logging.INFO, "", 0,
                            "GET /ws?token=SECRET1&device_token=SECRET2 200",
                            None, None)
    f.filter(rec)
    assert "SECRET1" not in rec.msg and "SECRET2" not in rec.msg
    assert "device_token=[redacted]" in rec.msg
