# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""RFC-0002 gap 8 — agent contract parity.

The flagship app becomes visible to its own platform: it serves the
same ``/manifest`` and ``/state`` contract routes every SDK app serves,
and self-registers with the App Catalog on boot. Pinned here:

* the two routes exist, answer without auth (same trust level as
  ``/health`` — the catalog's probe carries no OpenNVR bearer), and
  return the contracted shapes;
* the manifest is the SDK ``AppManifest`` shape with the agent's fixed
  identity (id ``camera-agent``);
* registration is best-effort and never raises — unwired, unreachable,
  and rejected all mean "runs exactly as before";
* registration POSTs the same body shape the SDK posts
  (``{url, manifest}`` to ``/api/v1/apps/register``).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from camera_agent import (
    AppConfig,
    CameraAgentRuntime,
    agent_manifest,
    build_app,
)
from context import CameraSpec


def _cfg(**kw) -> AppConfig:
    return AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg",
                            role="front door")],
        **kw,
    )


def test_manifest_is_the_sdk_shape_with_fixed_identity():
    m = agent_manifest()
    assert m["id"] == "camera-agent"
    assert m["category"] == "assistant"
    # The registry upserts by manifest id — a drifting id would register
    # a SECOND app instead of updating the first.
    assert set(m) >= {"id", "name", "version", "category", "summary",
                      "requires_tasks", "params", "emits"}
    assert m["requires_tasks"] == []


def test_contract_routes_exist_and_are_open():
    runtime = CameraAgentRuntime(_cfg(auth_mode="opennvr",
                                      opennvr_api_url="http://core:8000"))
    app = build_app(runtime)
    with TestClient(app) as client:
        man = client.get("/manifest")
        assert man.status_code == 200
        assert man.json()["id"] == "camera-agent"
        state = client.get("/state")
        assert state.status_code == 200
        body = state.json()
        assert body["cameras"] == ["cam1"]
        assert set(body["skills"]) == {"enabled", "total"}
        assert "llm_error" in body and "vision_error" in body


def test_state_never_500s_when_skills_derivation_breaks():
    runtime = CameraAgentRuntime(_cfg())

    def boom():
        raise RuntimeError("panel bug")
    runtime.skills_payload = boom  # type: ignore[assignment]
    body = runtime.contract_state()
    assert body["skills"] == {"enabled": 0, "total": 0}


def test_registration_posts_the_sdk_body_shape(monkeypatch):
    runtime = CameraAgentRuntime(_cfg(
        opennvr_api_url="http://core:8000",
        opennvr_api_key="sekrit",
        agent_public_url="https://agent.lan:9100",
    ))
    seen = {}

    class _Resp:
        status_code = 200
        text = "ok"

    class _Client:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None, headers=None):
            seen.update(url=url, json=json, headers=headers)
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert asyncio.run(runtime.register_with_app_catalog()) is True
    assert seen["url"] == "http://core:8000/api/v1/apps/register"
    assert seen["json"]["url"] == "https://agent.lan:9100"
    assert seen["json"]["manifest"]["id"] == "camera-agent"
    # Both header shapes, like the SDK: one key, either credential kind.
    assert seen["headers"]["X-Internal-Api-Key"] == "sekrit"
    assert seen["headers"]["Authorization"] == "Bearer sekrit"


@pytest.mark.parametrize("failure", ["transport", "rejected"])
def test_registration_failure_is_false_never_raise(monkeypatch, failure):
    runtime = CameraAgentRuntime(_cfg(opennvr_api_url="http://core:8000"))

    class _Resp:
        status_code = 403
        text = "no"

    class _Client:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None, headers=None):
            if failure == "transport":
                raise httpx.ConnectError("down")
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert asyncio.run(runtime.register_with_app_catalog()) is False


def test_registration_unwired_is_a_clean_no():
    runtime = CameraAgentRuntime(_cfg())      # no opennvr_api_url
    assert asyncio.run(runtime.register_with_app_catalog()) is False


def test_default_registration_url_scheme_follows_tls(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "agent-host")
    seen = {}

    class _Resp:
        status_code = 200
        text = "ok"

    class _Client:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None, headers=None):
            seen.update(json=json)
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    rt = CameraAgentRuntime(_cfg(opennvr_api_url="http://core:8000"))
    asyncio.run(rt.register_with_app_catalog())
    assert seen["json"]["url"] == "http://agent-host:9100"

    rt = CameraAgentRuntime(_cfg(opennvr_api_url="http://core:8000",
                                 tls_certfile="/certs/a.pem",
                                 tls_keyfile="/certs/a.key"))
    asyncio.run(rt.register_with_app_catalog())
    assert seen["json"]["url"] == "https://agent-host:9100"
