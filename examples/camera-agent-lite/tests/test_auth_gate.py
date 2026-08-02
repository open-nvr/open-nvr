# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""auth_mode="opennvr" (camera-agent parity): bearer gate, login/refresh
proxies, open page shell, /ws close codes. Lite is strictly conversational,
so ANY valid OpenNVR user passes everywhere — there is no tier map."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import camera_agent as ca
from camera_agent import Config

USERS = {
    "tok-viewer": {"username": "v", "is_superuser": False, "role_name": "viewer"},
    "tok-admin": {"username": "a", "is_superuser": False, "role_name": "admin"},
}


class FakeAuth:
    def __init__(self, device_ok=True):
        self.me_calls = 0
        self.device_ok = device_ok
        self.device_calls = 0
        self.device_tokens: list = []
        self.login_kwargs: dict = {}

    async def me(self, token):
        self.me_calls += 1
        return USERS.get(token)

    async def device_allowed(self, device_token):
        self.device_calls += 1
        self.device_tokens.append(device_token)
        return self.device_ok

    async def login(self, username, password, totp_code=None, **kw):
        self.login_kwargs = {"totp_code": totp_code, **kw}
        if (username, password) == ("admin", "pw"):
            return 200, {"access_token": "tok-admin", "refresh_token": "r1",
                         "device_token": "dev-new", "token_type": "bearer"}
        return 401, {"detail": "Incorrect username or password"}

    async def refresh(self, refresh_token):
        if refresh_token == "r1":
            return 200, {"access_token": "tok-admin", "refresh_token": "r2",
                         "token_type": "bearer"}
        return 401, {"detail": "Invalid or expired refresh token"}

    async def aclose(self):
        pass


def make_client(auth_mode="opennvr", device_ok=True):
    ca._cfg = Config(auth_mode=auth_mode, opennvr_api_url="http://srv")
    ca._auth = FakeAuth(device_ok=device_ok)
    # No lifespan (plain TestClient) → the heavy adapter warm-up never runs;
    # the middleware reads the globals at request time.
    return ca._auth, TestClient(ca.app)


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


# ── the gate ───────────────────────────────────────────────────────


def test_data_endpoints_401_without_token_shell_open():
    _auth, c = make_client()
    assert c.get("/api/status").status_code == 401
    assert c.post("/ask", json={"question": "hi"}).status_code == 401
    assert c.post("/voice", content=b"").status_code == 401
    assert c.get("/health").json() == {"status": "ok"}
    assert c.get("/demo").status_code == 200
    a = c.get("/agent").json()
    assert a["auth_mode"] == "opennvr"


def test_none_mode_stays_open():
    _auth, c = make_client(auth_mode="none")
    assert c.get("/api/status").status_code == 200
    assert c.post("/auth/login", json={"username": "x", "password": "y"}
                  ).status_code == 404
    assert _auth.me_calls == 0 and _auth.device_calls == 0


def test_any_valid_user_passes_everywhere():
    # Lite has no mutating endpoints → viewer tier suffices (no tier map).
    _auth, c = make_client()
    assert c.get("/api/status", headers=_h("tok-viewer")).status_code == 200
    r = c.post("/ask", json={"question": "hi"}, headers=_h("tok-viewer"))
    assert r.status_code == 200


def test_login_proxy_passthrough_and_device_forwarding():
    auth, c = make_client()
    r = c.post("/auth/login",
               json={"username": "admin", "password": "pw", "totp_code": "123456"},
               headers={"X-Device-Token": "dev-old", "User-Agent": "TestBrowser/1"})
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] == "tok-admin"
    assert body["refresh_token"] == "r1"
    assert body["device_token"] == "dev-new"
    # The browser's device identity + UA reached the proxy call.
    assert auth.login_kwargs["device_token"] == "dev-old"
    assert auth.login_kwargs["user_agent"] == "TestBrowser/1"
    assert auth.login_kwargs["totp_code"] == "123456"
    # Bad creds pass the server's status through.
    assert c.post("/auth/login", json={"username": "x", "password": "y"}
                  ).status_code == 401
    # Missing fields are a local 400.
    assert c.post("/auth/login", json={}).status_code == 400


def test_refresh_proxy_rotates():
    _auth, c = make_client()
    r = c.post("/auth/refresh", json={"refresh_token": "r1"})
    assert r.status_code == 200 and r.json()["refresh_token"] == "r2"
    assert c.post("/auth/refresh", json={}).status_code == 400


# ── /ws close codes (bypasses the HTTP middleware; gated in-handler) ─


def test_ws_closes_4401_without_token():
    _auth, c = make_client()
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/ws"):
            pass
    assert exc.value.code == 4401


def test_ws_closes_4403_for_blocked_device():
    _auth, c = make_client(device_ok=False)
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/ws?token=tok-viewer&device_token=dev-9"):
            pass
    assert exc.value.code == 4403
