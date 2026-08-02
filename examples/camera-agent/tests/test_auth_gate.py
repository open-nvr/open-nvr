# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""auth_mode="opennvr": bearer gate, permission tiers, login/refresh
proxies, token-validation caching, and the viewer toolset. Default
("none") stays wide open — the rest of the suite is that regression."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec

USERS = {
    "tok-viewer": {"username": "v", "is_superuser": False, "role_name": "viewer"},
    "tok-op": {"username": "o", "is_superuser": False, "role_name": "operator"},
    "tok-admin": {"username": "a", "is_superuser": False, "role_name": "admin"},
    "tok-super": {"username": "s", "is_superuser": True, "role_name": "viewer"},
}


class _FakeAuth:
    def __init__(self):
        self.me_calls = 0
        self.device_calls = 0
        self.device_ok = True   # device firewall allows by default

    async def me(self, token):
        self.me_calls += 1
        return USERS.get(token)

    async def device_allowed(self, device_token):
        self.device_calls += 1
        return self.device_ok

    async def login(self, username, password, totp_code=None, **kw):
        if (username, password) == ("admin", "pw"):
            return 200, {"access_token": "tok-admin", "refresh_token": "r1",
                         "token_type": "bearer"}
        return 401, {"detail": "Incorrect username or password"}

    async def refresh(self, refresh_token):
        if refresh_token == "r1":
            return 200, {"access_token": "tok-admin", "refresh_token": "r2",
                         "token_type": "bearer"}
        return 401, {"detail": "Invalid or expired refresh token"}

    async def aclose(self):
        pass


def _client(auth_mode="opennvr"):
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        auth_mode=auth_mode, opennvr_api_url="http://srv",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front")],
    )
    rt = CameraAgentRuntime(cfg)
    rt.auth = _FakeAuth()
    return rt, TestClient(build_app(rt))


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


# ── the gate ───────────────────────────────────────────────────────────


def test_data_endpoints_401_without_token_page_shell_open():
    _, c = _client()
    assert c.get("/cameras").status_code == 401
    assert c.get("/monitors").status_code == 401
    assert c.post("/ask", json={"text": "x"}).status_code == 401
    # the shell that RENDERS the login stays reachable
    assert c.get("/health").status_code == 200
    assert c.get("/demo").status_code == 200
    assert c.get("/demo/camera/cam1").status_code == 200
    assert c.get("/agent").json()["auth_mode"] == "opennvr"


def test_none_mode_stays_open():
    _, c = _client(auth_mode="none")
    assert c.get("/cameras").status_code == 200
    assert c.post("/auth/login", json={"username": "a", "password": "b"}).status_code == 404


# ── tiers ──────────────────────────────────────────────────────────────


def test_viewer_can_look_but_not_touch():
    _, c = _client()
    assert c.get("/cameras", headers=_h("tok-viewer")).status_code == 200
    assert c.get("/monitors", headers=_h("tok-viewer")).status_code == 200
    r = c.post("/monitors", headers=_h("tok-viewer"),
               json={"kind": "notify", "target": "car", "camera_ids": ["cam1"]})
    assert r.status_code == 403 and "operator" in r.json()["error"]
    assert c.post("/skills/see/disable", headers=_h("tok-viewer")).status_code == 403


def test_operator_arms_but_cannot_govern():
    _, c = _client()
    r = c.post("/monitors", headers=_h("tok-op"),
               json={"kind": "notify", "target": "car", "camera_ids": ["cam1"]})
    assert r.status_code == 202
    assert c.post("/alarms", headers=_h("tok-op"),
                  json={"name": "A", "target": "person", "camera_ids": ["cam1"]}
                  ).status_code == 202
    r = c.post("/skills/see/disable", headers=_h("tok-op"))
    assert r.status_code == 403 and "admin" in r.json()["error"]


def test_admin_and_superuser_govern():
    _, c = _client()
    assert c.post("/skills/see/disable", headers=_h("tok-admin")).status_code == 200
    assert c.post("/skills/restore", headers=_h("tok-super")).status_code == 200


# ── login / refresh proxies ────────────────────────────────────────────


def test_login_proxy_passthrough():
    _, c = _client()
    ok = c.post("/auth/login", json={"username": "admin", "password": "pw"})
    assert ok.status_code == 200 and ok.json()["access_token"] == "tok-admin"
    assert ok.json()["refresh_token"] == "r1"          # mobile needs the pair
    bad = c.post("/auth/login", json={"username": "admin", "password": "no"})
    assert bad.status_code == 401
    assert c.post("/auth/login", json={}).status_code == 400
    ref = c.post("/auth/refresh", json={"refresh_token": "r1"})
    assert ref.status_code == 200 and ref.json()["refresh_token"] == "r2"


# ── viewer toolset (chat can't arm anything) ───────────────────────────


def test_viewer_chat_toolset_has_no_mutating_verbs(monkeypatch):
    rt, c = _client()
    seen = {}

    async def fake_turn(runtime, history, text, *, tool_definitions=None, **kw):
        seen["tools"] = {t["function"]["name"] for t in (tool_definitions or [])}
        return "ok"

    monkeypatch.setattr(ca, "_run_conversation_turn", fake_turn)
    assert c.post("/ask", json={"text": "arm an alarm"},
                  headers=_h("tok-viewer")).status_code == 200
    assert seen["tools"], "viewer turn ran with an empty toolset"
    forbidden = {"create_alarm", "stop_alarm", "create_monitor", "stop_monitor",
                 "create_report", "stop_report", "create_background_task",
                 "enroll_face", "forget_face"}
    assert not (seen["tools"] & forbidden), seen["tools"] & forbidden
    # operators get the full set
    assert c.post("/ask", json={"text": "x"}, headers=_h("tok-op")).status_code == 200
    assert "create_alarm" in seen["tools"]


# ── validation cache (real client, stubbed transport) ──────────────────


def test_me_validation_is_cached_per_token():
    from adapter_clients import OpennvrAuthClient

    client = OpennvrAuthClient(base_url="http://srv")
    calls = {"n": 0}

    class _Resp:
        status_code = 200
        def json(self):
            return {"username": "v", "role_name": "viewer", "is_superuser": False}

    class _Http:
        async def get(self, url, headers=None):
            calls["n"] += 1
            return _Resp()
        async def post(self, *a, **k):  # pragma: no cover
            raise AssertionError

    client._client = lambda: _Http()
    for _ in range(5):
        assert asyncio.run(client.me("tok"))["role_name"] == "viewer"
    assert calls["n"] == 1                     # 4 hits served from cache
