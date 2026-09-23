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


def test_manifest_declares_the_external_ui(monkeypatch):
    # The agent is a full application: the catalog shows "Open app" to its
    # own /demo, never an embedded /ui. {host} = wherever the operator is
    # browsing from, on the port the compose overlay publishes.
    m = agent_manifest(_cfg(tls_certfile="/certs/a.pem", tls_keyfile="/certs/a.key"))
    assert m["has_ui"] is True and m["ui_mode"] == "external"
    assert m["ui_url"] == "https://{host}:9100/demo"
    assert agent_manifest(_cfg())["ui_url"] == "http://{host}:9100/demo"
    # an operator-set public URL wins (LAN hostname, reverse proxy)
    m = agent_manifest(_cfg(agent_public_url="https://agent.lan/"))
    assert m["ui_url"] == "https://agent.lan/demo"
    # a runtime's /manifest serves the same thing
    rt = CameraAgentRuntime(_cfg(tls_certfile="/certs/a.pem", tls_keyfile="/certs/a.key"))
    from fastapi.testclient import TestClient
    body = TestClient(build_app(rt)).get("/manifest").json()
    assert body["ui_url"] == "https://{host}:9100/demo"


def test_public_url_is_not_the_contract_url(monkeypatch):
    # agent_public_url is for the operator's browser (deep links, "Open
    # app"); it is never what core is told to probe — a LAN name would
    # fail core's single-label URL policy and the container id would not
    # resolve. Only agent_contract_url or the hostname go to the registry.
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "camera-agent")
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
    rt = CameraAgentRuntime(_cfg(opennvr_api_url="http://core:8000",
                                 agent_public_url="https://agent.lan:9100",
                                 tls_certfile="/certs/a.pem", tls_keyfile="/certs/a.key"))
    asyncio.run(rt.register_with_app_catalog())
    assert seen["json"]["url"] == "https://camera-agent:9100"
    assert seen["json"]["manifest"]["ui_url"] == "https://agent.lan:9100/demo"


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


def test_state_reports_whether_footage_search_could_be_answered():
    """These counters were added to decide, by evidence rather than
    opinion, whether the private SQLite index still earned a second
    store. `index_fallback` was the reading the decision turned on.

    The decision resolved without the month of data: footage-search
    2.0.0 deleted the index, so nothing writes that file and a fallback
    to it would serve whatever was true on the day of the upgrade. The
    counter went with it.

    What remains is still worth exposing. `unanswerable` climbing means
    operators are asking about the past during outages and being told
    nothing — which is the honest answer, but a number worth watching,
    because it is the cost this change accepted.
    """
    runtime = CameraAgentRuntime(_cfg())
    body = runtime.contract_state()

    assert "footage_search" in body
    assert set(body["footage_search"]) == {"canonical", "unanswerable"}

    runtime.tools.footage_search_sources["canonical"] += 3
    runtime.tools.footage_search_sources["unanswerable"] += 1
    after = runtime.contract_state()
    assert after["footage_search"]["canonical"] == 3
    assert after["footage_search"]["unanswerable"] == 1
    # A copy, not the live dict: /state is a public door and a reader
    # must not be able to reach in and reset the evidence.
    after["footage_search"]["canonical"] = 999
    assert runtime.tools.footage_search_sources["canonical"] == 3


def test_state_survives_an_agent_with_no_tools_yet():
    """/state must never 500 the probe — including before the toolbox
    exists, which is the shape the skills-derivation test above guards
    for a different field."""
    runtime = CameraAgentRuntime(_cfg())
    runtime.tools = None  # type: ignore[assignment]
    assert runtime.contract_state()["footage_search"] == {}


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
        agent_contract_url="https://agent.lan:9100",
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
