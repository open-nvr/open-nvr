# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""Model clients: one ``chat()`` over Ollama and OpenAI-compatible APIs.

Lifted from the camera agent (``examples/camera-agent/adapter_clients.py``,
``OllamaClient`` / ``OpenAILLMClient``) so core's workflow engine and,
later, the agent share one implementation. What changed on the way:

* ``format`` — a JSON schema the reply must follow (Ollama's ``format``,
  OpenAI's ``response_format``). An AI step's decision uses it so the
  model can only answer with one of the author's options.
* A :class:`ChatResult` instead of the raw body: content, tool calls with
  parsed arguments, the assistant message to append to history, and
  token counts and time for the trace.
* Failures raise :class:`LLMError` subclasses whose message is the
  diagnosis, URL included — "model 'x' not found, try pulling it first"
  must reach the operator, not a log (camera agent issue #344).

``httpx`` is the only dependency.
"""
from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_ERROR_BODY_CHARS = 300


# ── Errors ──────────────────────────────────────────────────────────


class LLMError(RuntimeError):
    """A model call failed; ``str(err)`` is a message worth showing."""


class LLMUnreachableError(LLMError):
    """Nothing answered at the model's URL (not running, wrong address)."""


class LLMTimeoutError(LLMError):
    """The model did not answer within the client's timeout."""


class LLMHTTPError(LLMError):
    """The model answered with an HTTP error; the body is in the message."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class LLMOutputError(LLMError):
    """The reply was not the JSON the call asked for."""


# ── Results ─────────────────────────────────────────────────────────


@dataclass
class ToolCall:
    """One tool call the model asked for.

    ``arguments`` is the parsed object, or ``None`` when the model sent
    something that is not a JSON object; ``raw_arguments`` is what it
    sent either way, so a caller can trace exactly what was asked.
    """

    name: str
    arguments: dict[str, Any] | None
    raw_arguments: Any = None
    id: str = ""


@dataclass
class ChatResult:
    """One model reply, normalised across runtimes.

    ``message`` is the assistant message as the runtime returned it —
    append it to the history before the tool results that answer it.
    """

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    message: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    ms: int = 0

    def json(self) -> Any:
        """The content parsed as JSON — for a call made with ``format``.

        A ``<think>`` block a reasoning model left in the content is
        dropped first. Raises :class:`LLMOutputError` if what remains is
        not JSON.
        """
        text = _THINK_RE.sub("", self.content or "").strip()
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMOutputError(
                f"model {self.model!r} did not return JSON: {text[:120]!r}") from exc


@runtime_checkable
class LLMClient(Protocol):
    """What the AI step needs from a model, whichever runtime serves it."""

    async def chat(self, *, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None,
                   format: dict[str, Any] | None = None,
                   temperature: float = 0.2,
                   max_tokens: int = 256) -> ChatResult:
        """One chat turn. ``format`` is a JSON schema the reply must follow.

        A user message may carry ``"images"``: base64-encoded JPEG or PNG
        strings, Ollama's native form. Clients for other runtimes convert
        it to theirs."""
        ...  # pragma: no cover - protocol


# ── Shared plumbing ─────────────────────────────────────────────────


class _HTTPClient:
    """One ``httpx.AsyncClient`` per model client, made on first use, so
    a client can be built at config time and a tool-heavy loop does not
    pay connection setup on every call. A client passed in is the
    caller's to close."""

    def __init__(self, timeout_seconds: float, http: httpx.AsyncClient | None) -> None:
        self._timeout = timeout_seconds
        self._http = http
        self._owns_http = http is None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    async def aclose(self) -> None:
        """Close the connection pool this client opened."""
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _post(self, url: str, body: dict[str, Any],
                    headers: dict[str, str]) -> httpx.Response:
        try:
            return await self._client().post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"LLM at {url} timed out after {self._timeout:g}s "
                f"({exc.__class__.__name__})") from exc
        except httpx.TransportError as exc:
            raise LLMUnreachableError(
                f"LLM unreachable at {url} ({exc.__class__.__name__}) — is the "
                f"runtime (e.g. Ollama) running and reachable from here?") from exc


def _http_error(url: str, resp: httpx.Response) -> LLMHTTPError:
    body = ""
    with contextlib.suppress(Exception):  # pragma: no cover - defensive
        body = (resp.text or "").strip()[:_ERROR_BODY_CHARS]
    return LLMHTTPError(f"LLM at {url} answered HTTP {resp.status_code}: "
                        f"{body or 'no detail'}", status=resp.status_code)


def _tool_calls(raw: list[dict[str, Any]] | None) -> list[ToolCall]:
    calls = []
    for i, call in enumerate(raw or []):
        func = call.get("function") or {}
        args_raw = func.get("arguments")
        args: dict[str, Any] | None
        if isinstance(args_raw, dict):
            args = dict(args_raw)
        elif isinstance(args_raw, str):
            try:
                parsed = json.loads(args_raw) if args_raw.strip() else {}
            except (json.JSONDecodeError, ValueError):
                parsed = None
            args = parsed if isinstance(parsed, dict) else None
        elif args_raw is None:
            args = {}
        else:
            args = None
        calls.append(ToolCall(name=str(func.get("name") or "").strip(), arguments=args,
                              raw_arguments=args_raw, id=str(call.get("id") or f"call-{i}")))
    return calls


# ── Ollama ──────────────────────────────────────────────────────────


class OllamaChat(_HTTPClient):
    """A model served by Ollama's native chat API (``/api/chat``).

    The knobs are the ones the camera agent needed on small boxes:
    ``num_ctx`` must hold the whole system + tools + history prompt (a
    truncated prompt also defeats Ollama's prefix cache), ``num_thread``
    caps the cores it takes, ``keep_alive=-1`` keeps the model resident so
    no turn pays a cold load, and ``think`` switches a reasoning model's
    thinking off (``False``) or on (``True``); ``None`` sends nothing,
    for models without the switch. Note that some builds ignore
    ``think: false`` — qwen3:4b as Ollama shipped it in October 2026 kept
    reasoning in its answer (camera-agent MODELS_AND_LATENCY.md).
    """

    def __init__(self, base_url: str, *, model: str, token: str | None = None,
                 timeout_seconds: float = 300.0, num_ctx: int = 4096,
                 num_thread: int | None = None, think: bool | None = None,
                 keep_alive: str | float = -1,
                 http: httpx.AsyncClient | None = None) -> None:
        super().__init__(timeout_seconds, http)
        url = base_url.rstrip("/")
        self.url = url if url.endswith("/api/chat") else url + "/api/chat"
        self.model = model
        self._token = token
        self._num_ctx = num_ctx
        self._num_thread = num_thread
        self._think = think
        self._keep_alive = keep_alive

    async def chat(self, *, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None,
                   format: dict[str, Any] | None = None,
                   temperature: float = 0.2,
                   max_tokens: int = 256) -> ChatResult:
        """One chat turn; see :class:`LLMClient`."""
        # An empty "Bearer " is an illegal header; native Ollama needs none.
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        options: dict[str, Any] = {"temperature": temperature, "num_predict": max_tokens,
                                   "num_ctx": self._num_ctx}
        if self._num_thread:
            options["num_thread"] = self._num_thread
        body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False,
                                "keep_alive": self._keep_alive, "options": options}
        if self._think is not None:
            body["think"] = self._think          # top level, not under options
        if tools:
            body["tools"] = tools                # no tool_choice in the native API
        if format is not None:
            body["format"] = format

        t0 = time.perf_counter()
        resp = await self._post(self.url, body, headers)
        if "think" in body and resp.status_code >= 400:
            # Some builds and non-thinking models reject ``think``: retry once
            # without it — but only if that is what the error is about. A
            # missing model ("not found") would fail the same way again.
            text = ""
            with contextlib.suppress(Exception):  # pragma: no cover - defensive
                text = (resp.text or "").lower()
            if not ("not found" in text and "think" not in text):
                body.pop("think")
                resp = await self._post(self.url, body, headers)
        if resp.status_code >= 400:
            raise _http_error(self.url, resp)
        data = resp.json()
        message = data.get("message") or {"role": "assistant", "content": ""}
        if message.get("content") is None:
            message["content"] = ""
        return ChatResult(
            content=message.get("content") or "",
            tool_calls=_tool_calls(message.get("tool_calls")),
            message=message, model=str(data.get("model") or self.model),
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
            ms=int((time.perf_counter() - t0) * 1000),
        )


# ── OpenAI-compatible ───────────────────────────────────────────────


class OpenAIChat(_HTTPClient):
    """A model behind any OpenAI-compatible chat API: vLLM, llama.cpp's
    server, LM Studio, Ollama's ``/v1``, or a hosted provider.

    ``base_url`` may end in ``/v1``, in ``/chat/completions``, or be the
    bare server root. ``format`` is sent as a strict ``json_schema``
    ``response_format``; servers differ in how strictly they honour it,
    so check :meth:`ChatResult.json` either way.
    """

    def __init__(self, base_url: str, *, model: str, api_key: str | None = None,
                 timeout_seconds: float = 120.0,
                 http: httpx.AsyncClient | None = None) -> None:
        super().__init__(timeout_seconds, http)
        url = base_url.rstrip("/")
        if url.endswith("/chat/completions"):
            self.url = url
        elif url.endswith("/v1"):
            self.url = url + "/chat/completions"
        else:
            self.url = url + "/v1/chat/completions"
        self.model = model
        self._api_key = api_key

    async def chat(self, *, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None,
                   format: dict[str, Any] | None = None,
                   temperature: float = 0.2,
                   max_tokens: int = 256) -> ChatResult:
        """One chat turn; see :class:`LLMClient`."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body: dict[str, Any] = {"model": self.model, "messages": _openai_messages(messages),
                                "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if format is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "response", "schema": format, "strict": True}}

        t0 = time.perf_counter()
        resp = await self._post(self.url, body, headers)
        if resp.status_code >= 400:
            raise _http_error(self.url, resp)
        data = resp.json()
        choices = data.get("choices") or []
        message = (choices[0].get("message") if choices else None) or {
            "role": "assistant", "content": ""}
        if message.get("content") is None:     # tool-call-only replies omit it
            message["content"] = ""
        usage = data.get("usage") or {}
        return ChatResult(
            content=message.get("content") or "",
            tool_calls=_tool_calls(message.get("tool_calls")),
            message=message, model=str(data.get("model") or self.model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            ms=int((time.perf_counter() - t0) * 1000),
        )


def _openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ollama-style ``images`` on a message, as OpenAI content parts."""
    out = []
    for m in messages:
        images = m.get("images")
        if not images:
            out.append(m)
            continue
        parts: list[dict[str, Any]] = [{"type": "text", "text": m.get("content") or ""}]
        parts += [{"type": "image_url", "image_url": {"url": _data_url(b64)}} for b64 in images]
        out.append({k: v for k, v in m.items() if k != "images"} | {"content": parts})
    return out


def _data_url(b64: str) -> str:
    # JPEG and PNG are told apart by their first base64 characters.
    mime = "image/png" if b64.startswith("iVBOR") else "image/jpeg"
    return f"data:{mime};base64,{b64}"
