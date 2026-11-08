# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The backup LLM: when the OpenAI-compatible brain (the NPU adapter) is
away — connection refused, a timeout, a 503 while its model loads — the
turn is answered by a fallback Ollama, slower, with one spoken notice per
episode; the primary is tried again every LLM_PROBE_INTERVAL_S and taken
back when it answers. Without a fallback configured, the turn fails with
the named URL exactly as before."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec


def _runtime(fallback: bool = True) -> CameraAgentRuntime:
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t", text_mode=True,
        synthetic_detection=True, llm_provider="openai", llm_base_url="http://caption-adapter:9006",
        llm_model="gemma-4-e2b-npu",
        llm_fallback_url="http://host.docker.internal:11434" if fallback else None,
        cameras=[CameraSpec(camera_id="front_door", frame_url="synth:people=0", role="the door")],
    )
    return CameraAgentRuntime(cfg)


class _Dead:
    def __init__(self, exc):
        self.exc, self.calls = exc, 0
    async def chat(self, **kw):
        self.calls += 1
        raise self.exc
    async def aclose(self):
        return None


class _Answers:
    def __init__(self, text):
        self.text, self.calls = text, 0
    async def chat(self, **kw):
        self.calls += 1
        return {"message": {"role": "assistant", "content": self.text, "tool_calls": []}}
    async def aclose(self):
        return None


def _refused():
    return httpx.ConnectError("All connection attempts failed")


def _status(code):
    req = httpx.Request("POST", "http://caption-adapter:9006/v1/chat/completions")
    return httpx.HTTPStatusError(str(code), request=req, response=httpx.Response(code, request=req, json={"error": {"code": "model_not_loaded"}}))


def test_runtime_builds_a_fallback_only_for_an_openai_brain_with_a_url():
    assert _runtime().llm_fallback is not None
    assert _runtime(fallback=False).llm_fallback is None


@pytest.mark.parametrize("exc", [_refused(), _status(503), httpx.ReadTimeout("slow")])
def test_an_outage_on_the_brain_is_answered_by_the_backup_with_one_notice(exc):
    rt = _runtime()
    rt.ollama, rt.llm_fallback = _Dead(exc), _Answers("You're welcome.")
    out = asyncio.run(ca._run_conversation_turn(rt, [], "thanks, that's all"))
    assert out == "I'm on my slower backup model for now. You're welcome."
    assert rt.llm_degraded and rt.llm_fallback.calls == 1
    # the next turn goes straight to the backup (no probe yet) and says nothing more about it
    out2 = asyncio.run(ca._run_conversation_turn(rt, [], "thanks, that's all"))
    assert out2 == "You're welcome." and rt.ollama.calls == 1 and rt.llm_fallback.calls == 2


def test_the_brain_is_probed_again_and_taken_back_when_it_answers():
    rt = _runtime()
    rt.ollama, rt.llm_fallback = _Dead(_refused()), _Answers("backup")
    asyncio.run(ca._run_conversation_turn(rt, [], "thanks, that's all"))
    assert rt.llm_degraded
    rt._llm_probe_at -= ca.LLM_PROBE_INTERVAL_S + 1          # a minute has passed
    rt.ollama = _Answers("primary")                           # and the adapter is back
    out = asyncio.run(ca._run_conversation_turn(rt, [], "thanks, that's all"))
    assert out == "primary" and not rt.llm_degraded and rt.llm_degraded_since is None


def test_a_bad_request_is_not_an_outage_and_without_a_backup_the_turn_fails_as_before():
    rt = _runtime()
    rt.ollama, rt.llm_fallback = _Dead(_status(400)), _Answers("backup")
    with pytest.raises(ca.LLMTurnError):
        asyncio.run(ca._run_conversation_turn(rt, [], "thanks, that's all"))
    assert not rt.llm_degraded and rt.llm_fallback.calls == 0
    rt2 = _runtime(fallback=False)
    rt2.ollama = _Dead(_refused())
    with pytest.raises(ca.LLMTurnError) as e:
        asyncio.run(ca._run_conversation_turn(rt2, [], "thanks, that's all"))
    assert "caption-adapter:9006" in str(e.value)


def test_health_reports_the_backup_episode():
    rt = _runtime()
    rt.ollama, rt.llm_fallback = _Dead(_refused()), _Answers("backup")
    asyncio.run(ca._run_conversation_turn(rt, [], "thanks, that's all"))
    client = TestClient(build_app(rt))
    body = client.get("/health").json()
    assert body.get("llm_degraded") is True
