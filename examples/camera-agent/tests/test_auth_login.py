# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenNVR-delegated login proxy: the MFA code must reach the server under the
field name its UserLogin schema expects (``code``). Sending ``totp_code`` was
silently dropped by Pydantic, so an MFA-enabled account always got
'Invalid or missing MFA code'."""
from __future__ import annotations

import asyncio

from adapter_clients import OpennvrAuthClient


class _Resp:
    status_code = 200

    def json(self):
        return {"access_token": "T", "refresh_token": "R"}


def _client_capturing(captured):
    class _C:
        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["body"] = json
            captured["headers"] = headers or {}
            return _Resp()
    return _C()


def test_login_sends_mfa_as_code():
    c = OpennvrAuthClient(base_url="http://srv")
    captured: dict = {}
    c._client = lambda: _client_capturing(captured)

    status, data = asyncio.run(c.login("admin", "pw", totp_code="339175"))

    assert status == 200
    assert captured["url"].endswith("/api/v1/auth/login-json")
    assert captured["body"]["code"] == "339175"      # the server's field name
    assert "totp_code" not in captured["body"]        # the old, dropped key
    assert captured["body"]["username"] == "admin"


def test_login_omits_code_without_mfa():
    c = OpennvrAuthClient(base_url="http://srv")
    captured: dict = {}
    c._client = lambda: _client_capturing(captured)

    asyncio.run(c.login("admin", "pw"))

    assert "code" not in captured["body"]
    assert "totp_code" not in captured["body"]


# ── full round trip through the agent's HTTP /auth/login → token → gate ──

from fastapi.testclient import TestClient  # noqa: E402

from camera_agent import AppConfig, CameraAgentRuntime, build_app  # noqa: E402
from context import CameraSpec  # noqa: E402


def _opennvr_runtime():
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        auth_mode="opennvr", opennvr_api_url="http://srv",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="r")],
    )
    return CameraAgentRuntime(cfg)


class _R:
    def __init__(self, status, body):
        self.status_code = status
        self._b = body

    def json(self):
        return self._b


def _stub_server(seen):
    """Mimics OpenNVR's /login-json exactly: MFA must arrive as ``code``.
    ``totp_code`` (the old bug) is ignored → the real 401 body."""
    class _Srv:
        async def post(self, url, json=None, headers=None):
            seen["body"] = json
            seen["headers"] = headers or {}
            if url.endswith("/login-json"):
                ok = (json.get("username") == "admin"
                      and json.get("password") == "pw"
                      and json.get("code") == "339175")
                if ok:
                    return _R(200, {"access_token": "AT", "refresh_token": "RT",
                                    "token_type": "bearer"})
                return _R(401, {"detail": {"error": "invalid_credentials",
                                           "message": "Invalid or missing MFA code",
                                           "remaining_attempts": 1}})
            return _R(404, {})
    return _Srv()


def test_login_round_trip_issues_token_and_authorizes_gate():
    rt = _opennvr_runtime()
    seen: dict = {}
    rt.auth._client = lambda: _stub_server(seen)

    async def fake_me(tok):
        return {"is_superuser": True, "role_name": "admin"} if tok == "AT" else None
    rt.auth.me = fake_me

    c = TestClient(build_app(rt))

    # 1) Browser posts the MFA as totp_code; the agent must forward it as code,
    #    the server accepts, and the token pair comes back.
    r = c.post("/auth/login",
               json={"username": "admin", "password": "pw", "totp_code": "339175"})
    assert r.status_code == 200, r.text
    assert seen["body"]["code"] == "339175" and "totp_code" not in seen["body"]
    token = r.json()["access_token"]
    assert token == "AT"

    # 2) That issued token now clears the auth gate on a protected endpoint —
    #    i.e. you are actually logged in, not just handed a string.
    assert c.get("/alarms").status_code == 401                     # no token → blocked
    g = c.get("/alarms", headers={"Authorization": f"Bearer {token}"})
    assert g.status_code == 200


def test_login_round_trip_surfaces_mfa_error_verbatim():
    rt = _opennvr_runtime()
    seen: dict = {}
    rt.auth._client = lambda: _stub_server(seen)
    c = TestClient(build_app(rt))

    # Missing MFA → the server's 401 passes through with the real message
    # (what the login form now renders instead of "[object Object]").
    r = c.post("/auth/login", json={"username": "admin", "password": "pw"})
    assert r.status_code == 401
    assert r.json()["detail"]["message"] == "Invalid or missing MFA code"
