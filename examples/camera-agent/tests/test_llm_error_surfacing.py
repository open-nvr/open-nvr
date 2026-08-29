# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Issue #344's real lesson: the agent's brain (Ollama) crash-looped for
three days while the health dot glowed green and every question returned a
bare "assistant failed" — and when the model was missing, Ollama's own 404
body LITERALLY contained the fix ("model 'x' not found, try pulling it
first") and the agent threw it away.

These pin the surfacing that replaces that:

* an LLM connect failure → 502 whose error NAMES the URL, and /health
  carries the same text as ``llm_error``;
* an LLM HTTP failure → the response BODY (the diagnosis) is kept;
* a successful turn CLEARS ``llm_error`` — green means answering again;
* non-LLM turn failures keep the old generic 502 (no behaviour change);
* the think=False retry reads the error body first: a missing model is
  not a think rejection, and must not burn the retry on a doomed request;
* the demo's header dot treats llm_error as red-with-reason, above the
  amber vision_error.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec


def _runtime():
    cfg = AppConfig(
        kaic_url="http://k", kaic_api_key="x", system_prompt="t", text_mode=True,
        synthetic_detection=True,
        cameras=[CameraSpec(camera_id="front_door", frame_url="synth:people=2",
                            role="door")],
    )
    return CameraAgentRuntime(cfg)


class _DeadOllama:
    """The LLM leg of issue #344: connection refused, every time."""
    async def chat(self, **kw):
        raise httpx.ConnectError("All connection attempts failed")

    async def aclose(self):
        return None


class _Http404Ollama:
    """Ollama answering but the model is missing — its body IS the fix."""
    def __init__(self, body: str):
        self._body = body

    async def chat(self, **kw):
        req = httpx.Request("POST", "http://host.docker.internal:11434/api/chat")
        resp = httpx.Response(404, request=req, text=self._body)
        raise httpx.HTTPStatusError("404", request=req, response=resp)

    async def aclose(self):
        return None


class _AnsweringOllama:
    async def chat(self, **kw):
        return {"message": {"content": "All quiet.", "tool_calls": []}}

    async def aclose(self):
        return None


# ── connect failure: the 502 explains, /health carries it ───────────────

def test_unreachable_llm_names_the_url_in_the_502():
    rt = _runtime()
    rt.ollama = _DeadOllama()
    client = TestClient(build_app(rt))
    r = client.post("/ask", json={"text": "anyone at the door?"})
    assert r.status_code == 502
    err = r.json()["error"]
    assert "unreachable" in err
    assert rt.cfg.ollama_url in err           # the address the AGENT dials
    assert err != "assistant failed"          # the old, useless message


def test_llm_failure_reaches_health_as_llm_error():
    rt = _runtime()
    rt.ollama = _DeadOllama()
    client = TestClient(build_app(rt))
    client.post("/ask", json={"text": "hello"})
    h = client.get("/health").json()
    assert h["llm_error"] is not None
    assert "unreachable" in h["llm_error"]


# ── HTTP failure: the body is the diagnosis, keep it ────────────────────

def test_http_error_keeps_ollamas_own_diagnosis():
    rt = _runtime()
    body = json.dumps({"error": "model \"qwen2.5:3b\" not found, "
                                "try pulling it first"})
    rt.ollama = _Http404Ollama(body)
    client = TestClient(build_app(rt))
    r = client.post("/ask", json={"text": "hello"})
    assert r.status_code == 502
    assert "try pulling it first" in r.json()["error"]
    assert "404" in r.json()["error"]
    h = client.get("/health").json()
    assert "try pulling it first" in h["llm_error"]


def test_http_error_body_is_capped():
    # An error page can be arbitrarily long; llm_error must not become one.
    rt = _runtime()
    rt.ollama = _Http404Ollama("x" * 10_000)
    client = TestClient(build_app(rt))
    r = client.post("/ask", json={"text": "hello"})
    assert len(r.json()["error"]) < 500


# ── recovery: success clears the flag ───────────────────────────────────

def test_a_successful_turn_clears_llm_error():
    rt = _runtime()
    rt.ollama = _DeadOllama()
    client = TestClient(build_app(rt))
    client.post("/ask", json={"text": "hello"})
    assert client.get("/health").json()["llm_error"] is not None
    rt.ollama = _AnsweringOllama()            # Ollama came back
    r = client.post("/ask", json={"text": "hello again"})
    assert r.status_code == 200
    assert client.get("/health").json()["llm_error"] is None


def test_healthy_boot_reports_no_llm_error():
    rt = _runtime()
    h = TestClient(build_app(rt)).get("/health").json()
    assert h["llm_error"] is None


# ── non-LLM failures keep the old generic message ───────────────────────

def test_non_llm_failures_still_say_assistant_failed(monkeypatch):
    rt = _runtime()
    rt.ollama = _AnsweringOllama()

    async def _boom(*a, **k):
        raise RuntimeError("something unrelated")
    monkeypatch.setattr(ca, "_run_conversation_turn", _boom)
    client = TestClient(build_app(rt))
    r = client.post("/ask", json={"text": "hello"})
    assert r.status_code == 502
    assert r.json()["error"] == "assistant failed"
    # And it must NOT masquerade as an LLM problem.
    assert client.get("/health").json()["llm_error"] is None


# ── the think retry reads the body before burning its one retry ─────────

def _ollama_client_with(status: int, body: str, calls: list):
    from adapter_clients import OllamaClient
    c = OllamaClient(url="http://x:11434", token="", model="m", think=False)

    class _Resp:
        status_code = status
        text = body

        def raise_for_status(self):
            if status >= 400:
                req = httpx.Request("POST", "http://x:11434/api/chat")
                raise httpx.HTTPStatusError(
                    str(status), request=req,
                    response=httpx.Response(status, request=req, text=body))

        def json(self):
            return {"message": {"content": "ok"}}

    class _C:
        async def post(self, url, **kw):
            # Copy: the client pops "think" from this dict IN PLACE for the
            # retry, and a stored reference would retroactively lose the key.
            calls.append(dict(kw.get("json", {})))
            return _Resp()

    c._client = lambda: _C()
    return c


def test_missing_model_is_not_mistaken_for_a_think_rejection():
    """Issue #344's box: a 404 'model not found' fired the think retry —
    one extra doomed request and a misleading log line."""
    calls: list = []
    c = _ollama_client_with(
        404, '{"error":"model \\"qwen2.5:3b\\" not found, try pulling it first"}',
        calls)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(c.chat(messages=[{"role": "user", "content": "hi"}]))
    assert len(calls) == 1, "a missing model must not burn the think retry"


def test_a_real_think_rejection_still_retries_without_it():
    calls: list = []
    c = _ollama_client_with(400, '{"error":"this model does not support think"}',
                            calls)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(c.chat(messages=[{"role": "user", "content": "hi"}]))
    assert len(calls) == 2, "a think rejection keeps its retry"
    assert "think" in calls[0] and "think" not in calls[1]


# ── demo: the header dot puts the brain above the eye ───────────────────

_HTML = (Path(__file__).resolve().parents[1] / "demo" / "index.html").read_text()


def test_demo_health_dot_shows_llm_error_red_and_first():
    fn = _HTML[_HTML.index("async function pollHealth"):]
    fn = fn[:fn.index("// ── History card")]
    assert "h.llm_error" in fn
    # Red (down), not amber: with the brain gone EVERY question fails.
    llm = fn[fn.index("h.llm_error"):]
    assert 'className="hdot down"' in llm[:200]
    # And it outranks vision_error: checked first.
    assert fn.index("h.llm_error") < fn.index("h.vision_error")
    assert "LLM failing:" in fn
