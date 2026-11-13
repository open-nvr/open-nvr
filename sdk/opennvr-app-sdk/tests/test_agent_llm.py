# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""opennvr_app_sdk.agent.llm: the model clients the AI step runs on."""
from __future__ import annotations

import json

import httpx
import pytest
from opennvr_app_sdk.agent import (
    ChatResult,
    LLMClient,
    LLMHTTPError,
    LLMOutputError,
    LLMTimeoutError,
    LLMUnreachableError,
    OllamaChat,
    OpenAIChat,
)

SCHEMA = {"type": "object", "properties": {"decision": {"enum": ["yes", "no"]}},
          "required": ["decision"]}
MESSAGES = [{"role": "user", "content": "hi"}]


class Recorder:
    """An httpx transport that records requests and answers from a script."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        status, body = r
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def ollama_reply(content="", tool_calls=None, **extra):
    msg = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return 200, {"model": "qwen3:1.7b", "message": msg, **extra}


# ── Ollama ──────────────────────────────────────────────────────────


async def test_ollama_sends_the_native_body():
    rec = Recorder(ollama_reply("ok"))
    llm = OllamaChat("http://ollama:11434/", model="qwen3:1.7b", num_ctx=8192,
                     num_thread=2, think=False, http=rec.client())
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    await llm.chat(messages=MESSAGES, tools=tools, format=SCHEMA,
                   temperature=0.1, max_tokens=64)
    req = rec.requests[0]
    assert str(req.url) == "http://ollama:11434/api/chat"
    assert "authorization" not in req.headers, "native Ollama needs no auth"
    body = rec.body()
    assert body["model"] == "qwen3:1.7b" and body["messages"] == MESSAGES
    assert body["stream"] is False and body["keep_alive"] == -1
    assert body["options"] == {"temperature": 0.1, "num_predict": 64,
                               "num_ctx": 8192, "num_thread": 2}
    assert body["think"] is False and body["tools"] == tools
    assert body["format"] == SCHEMA


async def test_ollama_leaves_out_what_was_not_asked_for():
    rec = Recorder(ollama_reply("ok"))
    llm = OllamaChat("http://ollama:11434", model="m", token="secret", http=rec.client())
    await llm.chat(messages=MESSAGES)
    body = rec.body()
    for key in ("think", "tools", "format"):
        assert key not in body
    assert "num_thread" not in body["options"]
    assert rec.requests[0].headers["authorization"] == "Bearer secret"


async def test_ollama_accepts_a_full_api_chat_url():
    rec = Recorder(ollama_reply("ok"))
    llm = OllamaChat("http://h:11434/api/chat", model="m", http=rec.client())
    await llm.chat(messages=MESSAGES)
    assert str(rec.requests[0].url) == "http://h:11434/api/chat"


async def test_ollama_normalises_the_reply():
    rec = Recorder(ollama_reply(None, tool_calls=[
        {"function": {"name": "describe_camera", "arguments": {"camera_id": "gate"}}},
        {"function": {"name": "detect_objects", "arguments": "not json"}},
    ], prompt_eval_count=812, eval_count=31))
    llm = OllamaChat("http://o", model="m", http=rec.client())
    result = await llm.chat(messages=MESSAGES)
    assert result.content == "" and result.message["content"] == ""
    first, second = result.tool_calls
    assert (first.name, first.arguments) == ("describe_camera", {"camera_id": "gate"})
    assert second.arguments is None and second.raw_arguments == "not json"
    assert (result.prompt_tokens, result.completion_tokens) == (812, 31)
    assert result.model == "qwen3:1.7b" and result.ms >= 0


async def test_ollama_retries_without_think_when_think_is_rejected():
    rec = Recorder((400, "unknown field: think"), ollama_reply("ok"))
    llm = OllamaChat("http://o", model="m", think=False, http=rec.client())
    result = await llm.chat(messages=MESSAGES)
    assert result.content == "ok"
    assert "think" in rec.body(0) and "think" not in rec.body(1)


async def test_ollama_does_not_retry_a_missing_model():
    """The camera agent's issue #344: a 404 for a model never pulled burned
    the retry and hid the one line that named the fix."""
    rec = Recorder((404, '{"error":"model \\"qwen9\\" not found, try pulling it first"}'))
    llm = OllamaChat("http://ollama:11434", model="qwen9", think=False, http=rec.client())
    with pytest.raises(LLMHTTPError) as err:
        await llm.chat(messages=MESSAGES)
    assert len(rec.requests) == 1
    assert err.value.status == 404
    assert "try pulling it first" in str(err.value)
    assert "http://ollama:11434/api/chat" in str(err.value)


async def test_transport_failures_name_the_url():
    rec = Recorder(httpx.ConnectError("refused"))
    llm = OllamaChat("http://ollama:11434", model="m", http=rec.client())
    with pytest.raises(LLMUnreachableError, match="ollama:11434"):
        await llm.chat(messages=MESSAGES)

    rec = Recorder(httpx.ReadTimeout("slow"))
    llm = OllamaChat("http://ollama:11434", model="m", timeout_seconds=7, http=rec.client())
    with pytest.raises(LLMTimeoutError, match="7s"):
        await llm.chat(messages=MESSAGES)


# ── OpenAI-compatible ───────────────────────────────────────────────


@pytest.mark.parametrize("base,url", [
    ("http://vllm:8000", "http://vllm:8000/v1/chat/completions"),
    ("http://vllm:8000/v1/", "http://vllm:8000/v1/chat/completions"),
    ("http://h/v1/chat/completions", "http://h/v1/chat/completions"),
])
def test_openai_url_forms(base, url):
    assert OpenAIChat(base, model="m").url == url


async def test_openai_body_and_reply():
    rec = Recorder((200, {"model": "llama3.2:3b", "choices": [{"message": {
        "role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "recent_events", "arguments": '{"minutes": 15}'}}]}}],
        "usage": {"prompt_tokens": 500, "completion_tokens": 12}}))
    llm = OpenAIChat("http://h/v1", model="llama3.2:3b", api_key="k", http=rec.client())
    tools = [{"type": "function", "function": {"name": "recent_events", "parameters": {}}}]
    result = await llm.chat(messages=MESSAGES, tools=tools, format=SCHEMA, max_tokens=99)
    body = rec.body()
    assert body["tool_choice"] == "auto" and body["max_tokens"] == 99
    assert body["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "response", "schema": SCHEMA, "strict": True}}
    assert rec.requests[0].headers["authorization"] == "Bearer k"
    (call,) = result.tool_calls
    assert (call.id, call.name, call.arguments) == ("c1", "recent_events", {"minutes": 15})
    assert result.content == "" and (result.prompt_tokens, result.completion_tokens) == (500, 12)


async def test_openai_http_error_carries_the_body():
    rec = Recorder((401, "invalid api key"))
    llm = OpenAIChat("http://h", model="m", http=rec.client())
    with pytest.raises(LLMHTTPError, match="401: invalid api key"):
        await llm.chat(messages=MESSAGES)


# ── ChatResult.json() and the protocol ──────────────────────────────


def test_json_reads_constrained_output_and_drops_a_think_block():
    r = ChatResult(content='<think>hmm, a van</think>\n{"decision": "yes"}')
    assert r.json() == {"decision": "yes"}


def test_json_refuses_prose():
    with pytest.raises(LLMOutputError, match="did not return JSON"):
        ChatResult(content="It is probably a delivery.", model="m").json()


def test_both_clients_satisfy_the_protocol():
    assert isinstance(OllamaChat("http://o", model="m"), LLMClient)
    assert isinstance(OpenAIChat("http://o", model="m"), LLMClient)


async def test_a_client_closes_only_what_it_opened():
    own = OllamaChat("http://o", model="m")
    own._client()
    await own.aclose()
    assert own._http is None

    shared = httpx.AsyncClient()
    llm = OllamaChat("http://o", model="m", http=shared)
    async with llm:
        pass
    assert not shared.is_closed, "an injected client is the caller's to close"
    await shared.aclose()
