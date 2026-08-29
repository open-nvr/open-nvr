# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""RFC-0002 Phase 1: the skills panel consumes the PLATFORM registry.

The rule under test is the fallback chain for platform-owned facts:
registry verdict (includes provider health) → KAI-C tasks_advertised
→ config-based, never greying anything out on "unknown". And the
on-ramp fields (suggested_adapters / suggested_apps) come from the
registry when it answered — so every consumer renders the same
guidance — else the private editorial maps.

Agent-LOCAL facts (advertised tools, config gating, enabled state)
must be untouched by anything the registry says.
"""
from __future__ import annotations

import asyncio

from adapter_clients import SkillsRegistryClient
from camera_agent import (
    _SKILL_SUGGESTED_ADAPTERS,
    AppConfig,
    CameraAgentRuntime,
)
from context import CameraSpec


def _runtime() -> CameraAgentRuntime:
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t",
        cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg",
                            role="front door")],
    )
    return CameraAgentRuntime(cfg)


class _StubRegistry:
    """Stands in for SkillsRegistryClient on the runtime."""

    def __init__(self, skills):
        self._skills = skills

    def availability_by_agent_skill(self):
        return SkillsRegistryClient.availability_by_agent_skill(self)

    def suggestions_by_agent_skill(self):
        return SkillsRegistryClient.suggestions_by_agent_skill(self)


def _entry(agent_skill, status, *, adapters=(), apps=()):
    return {
        "id": f"task-for-{agent_skill}-{status}", "agent_skill": agent_skill,
        "status": status,
        "suggested_adapters": list(adapters), "suggested_apps": list(apps),
    }


def _by_id(runtime):
    return {s["id"]: s for s in runtime.skills_payload()}


def test_registry_verdict_wins_over_tasks_advertised():
    rt = _runtime()
    # KAI-C never fetched (None = unknown) → old behavior said available.
    assert rt.kaic_capabilities.tasks_advertised is None
    # Registry answered: the 'count' skill's backing task is degraded
    # (registered adapter, failing health probe — the #344 shape).
    rt.skills_registry = _StubRegistry([_entry("count", "degraded")])
    assert _by_id(rt)["count"]["tasks_available"] is False
    # And a healthy verdict reads available even with KAI-C caps unknown.
    rt.skills_registry = _StubRegistry([_entry("count", "available")])
    assert _by_id(rt)["count"]["tasks_available"] is True


def test_any_available_backing_entry_wins():
    rt = _runtime()
    rt.skills_registry = _StubRegistry([
        _entry("count", "degraded"),
        _entry("count", "available"),
    ])
    assert _by_id(rt)["count"]["tasks_available"] is True


def test_unknown_registry_falls_back_to_private_derivation():
    rt = _runtime()
    rt.skills_registry = None            # unwired
    before = _by_id(rt)
    class _Unknown:
        def availability_by_agent_skill(self):
            return None
        def suggestions_by_agent_skill(self):
            return {}
    rt.skills_registry = _Unknown()      # wired but unreachable
    after = _by_id(rt)
    for sid in before:
        assert before[sid]["tasks_available"] == after[sid]["tasks_available"], (
            f"{sid}: an unreachable registry changed the panel")


def test_skill_absent_from_registry_uses_old_derivation():
    rt = _runtime()
    # Registry knows about 'count' only; 'see' keeps the old rule
    # (KAI-C unknown → not greyed out).
    rt.skills_registry = _StubRegistry([_entry("count", "available")])
    assert _by_id(rt)["see"]["tasks_available"] is True


def test_suggestions_come_from_registry_when_greyed_out():
    rt = _runtime()
    rt.skills_registry = _StubRegistry([
        _entry("faces", "missing-dependency",
               adapters=["insightface"], apps=["smart-doorbell"]),
    ])
    s = _by_id(rt)["faces"]
    assert s["tasks_available"] is False
    assert s["suggested_adapters"] == ["insightface"]
    assert s["suggested_apps"] == ["smart-doorbell"]


def test_suggestions_fall_back_to_editorial_maps():
    rt = _runtime()

    class _DegradedNoSuggestions:
        def availability_by_agent_skill(self):
            return {"faces": False}
        def suggestions_by_agent_skill(self):
            return {}
    rt.skills_registry = _DegradedNoSuggestions()
    s = _by_id(rt)["faces"]
    assert s["tasks_available"] is False
    assert s["suggested_adapters"] == _SKILL_SUGGESTED_ADAPTERS.get("faces", [])


def test_registry_never_touches_agent_local_state():
    rt = _runtime()
    baseline = _by_id(rt)
    rt.skills_registry = _StubRegistry(
        [_entry(sid, "available") for sid in baseline])
    after = _by_id(rt)
    for sid in baseline:
        for field in ("enabled", "available", "tools"):
            assert baseline[sid].get(field) == after[sid].get(field), (
                f"{sid}.{field}: platform registry leaked into agent-local "
                "state (it may only steer tasks_available + suggestions)")


def test_client_parses_and_negative_caches():
    calls = {"n": 0}

    class _Resp:
        def __init__(self, payload):
            self._p = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._p

    client = SkillsRegistryClient(base_url="http://core", api_key="k",
                                  ttl_seconds=60.0)

    class _Http:
        async def get(self, url, headers=None):
            calls["n"] += 1
            assert url.endswith("/api/v1/internal/camera-agent/skills")
            assert headers == {"X-Internal-Api-Key": "k"}
            return _Resp({"skills": [_entry("count", "available")]})
    client._http = _Http()

    got = asyncio.run(client.refresh())
    assert got and got[0]["agent_skill"] == "count"
    assert client.availability_by_agent_skill() == {"count": True}
    # TTL: a second refresh inside the window must not re-fetch.
    asyncio.run(client.refresh())
    assert calls["n"] == 1


def test_client_failure_is_unknown_not_crash():
    client = SkillsRegistryClient(base_url="http://core", api_key="k")

    class _Boom:
        async def get(self, url, headers=None):
            raise RuntimeError("down")
    client._http = _Boom()
    assert asyncio.run(client.refresh()) is None
    assert client.availability_by_agent_skill() is None
    assert client.suggestions_by_agent_skill() == {}
