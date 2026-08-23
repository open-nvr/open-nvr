# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
HTTP clients for the OpenNVR adapters the camera-agent uses.

Two posture choices baked into v0.1:

* The **streaming voice path** (Whisper STT, Ollama LLM, Piper TTS)
  talks to the adapters directly with bearer-token auth. KAI-C v0.1
  doesn't proxy streaming yet (its `/api/v1/infer` is JSON-only and
  blocking), so until the streaming proxy lands these three calls
  bypass the central audit chain. Each adapter still records the
  call in its own audit log.

* The **tool calls** (BLIP scene captions, YOLOv8 object detection,
  InsightFace recognition) flow THROUGH KAI-C with the
  X-Internal-Api-Key header — same shape as smart-doorbell / LPR /
  package-delivery. These are event-driven not streaming so the
  JSON-only proxy is fine.

This split is documented in the README's "Audit chain" section and
in config.example.yml so operators understand what's auditable and
what isn't until v0.2.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


CORRELATION_ID_HEADER = "X-Correlation-Id"


# ── KAI-C client (audit-chain tools) ───────────────────────────────


class _ReusableClientMixin:
    """One ``httpx.AsyncClient`` per service instance — avoids paying
    TCP + TLS setup for every adapter call inside a tool-heavy LLM
    turn. The client is lazily constructed because that lets the
    instance be created at config-load time and only spin up the
    underlying connection pool when the first call actually fires."""

    _timeout: float
    _http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


class KaicAdapterClient(_ReusableClientMixin):
    """JSON+base64 POST to KAI-C's ``/api/v1/infer/{adapter_name}``.

    Mirrors the wire shape used by smart-doorbell / LPR / package-
    delivery: ``frame_b64`` for image bytes plus any extra params
    the adapter expects on its top-level payload (task, threshold,
    etc.). The SDK body parser unwraps these into the service.
    """

    def __init__(
        self,
        *,
        kaic_url: str,
        api_key: str,
        adapter_name: str,
        timeout_seconds: float = 120.0,
        retries: int = 2,
        retry_backoff_s: float = 1.5,
    ) -> None:
        self._url = f"{kaic_url.rstrip('/')}/api/v1/infer/{adapter_name}"
        self._api_key = api_key
        self._timeout = timeout_seconds
        # Brief retry bridges the adapter cold-start window: right after
        # boot the model may still be loading / its weights still
        # downloading, so the first inference can 5xx or drop the
        # connection. Retrying a couple of times avoids surfacing that to
        # the user as "camera offline" until someone restarts the adapter.
        self._retries = max(0, retries)
        self._retry_backoff_s = retry_backoff_s

    async def infer(
        self,
        *,
        frame_jpeg: bytes,
        extra: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "X-Internal-Api-Key": self._api_key,
            "Content-Type": "application/json",
        }
        if correlation_id:
            headers[CORRELATION_ID_HEADER] = correlation_id
        body: dict[str, Any] = {
            "frame_b64": base64.b64encode(frame_jpeg).decode("ascii"),
        }
        if extra:
            body.update(extra)

        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = await self._client().post(self._url, json=body, headers=headers)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError as exc:
                # Surface KAI-C's response body in the error: a bare
                # "403 Forbidden" hides WHICH gate refused (permission
                # approval vs sovereignty vs auth), and the field cost of
                # that ambiguity was a multi-hour hunt. The detail string
                # travels into tools' degradation reason and the user's
                # answer.
                detail = ""
                resp_obj = getattr(exc, "response", None)
                if resp_obj is not None:
                    try:
                        detail = str((resp_obj.json() or {}).get("detail") or "")[:300]
                    except Exception:
                        detail = (resp_obj.text or "")[:300]
                if detail:
                    exc = httpx.HTTPStatusError(
                        f"{exc} — KAI-C detail: {detail}",
                        request=getattr(exc, "request", None),
                        response=resp_obj,
                    ) if isinstance(exc, httpx.HTTPStatusError) else exc
                last_exc = exc
                if attempt < self._retries:
                    logger.warning(
                        "KAI-C infer attempt %d/%d failed (%s); retrying in %.1fs "
                        "(adapter may still be warming up)",
                        attempt + 1, self._retries + 1, exc, self._retry_backoff_s,
                    )
                    await asyncio.sleep(self._retry_backoff_s)
        assert last_exc is not None
        raise last_exc


class KaicCapabilitiesClient(_ReusableClientMixin):
    """Cached view of KAI-C's aggregated capabilities — which task strings
    (``tasks_advertised``) the registered adapters currently provide.

    ``GET {kaic_url}/api/v1/ai/capabilities`` with the same
    ``X-Internal-Api-Key`` the infer path uses (KAI-C's dev-mode bypass
    means an empty key is fine on loopback deployments). Results are
    cached for ``ttl_seconds`` (default 60) so the demo UI's skills
    polling never hammers KAI-C.

    This is ADVISORY display data for the skills panel: ``refresh()``
    never raises, and an unreachable KAI-C (or a fetch that hasn't
    happened yet) leaves :attr:`tasks_advertised` as ``None`` =
    "unknown" — callers fall back to config-based availability instead
    of greying anything out. Failures are negative-cached for the same
    TTL so a down KAI-C costs at most one short timeout per minute.
    """

    def __init__(
        self,
        *,
        kaic_url: str,
        api_key: str = "",
        ttl_seconds: float = 60.0,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._url = f"{kaic_url.rstrip('/')}/api/v1/ai/capabilities"
        self._api_key = api_key
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._fetched_at: float | None = None
        self._tasks: set[str] | None = None   # None = unknown / unreachable
        # adapter name -> declared permissions.gpu (contract §6). Advisory
        # like the task set (None = unknown); drives the Hardware panel's
        # "running on GPU / CPU" honesty line.
        self._gpu: dict[str, bool] | None = None

    @property
    def tasks_advertised(self) -> set[str] | None:
        """Last-known union of adapter task strings (``None`` = unknown)."""
        return self._tasks

    @property
    def gpu_adapters(self) -> dict[str, bool] | None:
        """Last-known ``adapter name -> permissions.gpu`` (``None`` = unknown)."""
        return self._gpu

    async def refresh(self) -> set[str] | None:
        """Fetch (at most once per TTL) and return the advertised-task set.

        Never raises — on any error the cached value becomes ``None``
        ("unknown") until the next TTL window.
        """
        now = time.monotonic()
        if self._fetched_at is not None and now - self._fetched_at < self._ttl:
            return self._tasks
        # Stamp BEFORE the call so an unreachable KAI-C is also rate-limited
        # to one attempt per TTL (negative caching).
        self._fetched_at = now
        try:
            headers = {}
            if self._api_key:
                headers["X-Internal-Api-Key"] = self._api_key
            resp = await self._client().get(self._url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            tasks: set[str] = set()
            gpu: dict[str, bool] = {}
            adapters = data.get("adapters") or {}
            for name, cap in adapters.items():
                for task in (cap or {}).get("tasks_advertised") or []:
                    tasks.add(str(task))
                perms = (cap or {}).get("permissions") or {}
                gpu[str(name)] = bool(perms.get("gpu"))
            self._tasks = tasks
            self._gpu = gpu
        except Exception as exc:
            logger.debug(
                "KAI-C capabilities fetch failed (%s); skills fall back to "
                "config-based availability", exc,
            )
            self._tasks = None
            self._gpu = None
        return self._tasks


class AppRegistryClient(_ReusableClientMixin):
    """Cached, read-only view of the OpenNVR app registry — the installed
    catalog apps and their live state — for the agent's app door.

    Two calls, both ``GET`` against the server's ``/api/v1/apps`` routes
    with the same ``X-Internal-Api-Key`` the infer path uses:

    * :meth:`list_apps` → ``GET {base}/api/v1/apps`` — the catalog rows
      (id / name / category / enabled / manifest / …).
    * :meth:`app_status` → ``GET {base}/api/v1/apps/{id}/status`` — the
      app's proxied ``/health`` + ``/state``.

    This is the READ half of "every catalog app is a conversational
    skill": the agent discovers container apps it can't import and
    *relays* their state. It never enables / disables / configures an
    app — those stay operator actions (the agent guides).

    ADVISORY, like :class:`KaicCapabilitiesClient`: both methods NEVER
    raise. Results (and failures) are cached for ``ttl_seconds``
    (default 60) so a down / unset registry costs at most one short
    timeout per TTL. On any error the cached value is ``None`` = unknown
    / unreachable, and the tools surface a graceful "couldn't reach the
    app registry" message instead of crashing.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        ttl_seconds: float = 60.0,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        # list_apps cache (list, or None = unknown / unreachable).
        self._apps: list[dict[str, Any]] | None = None
        self._apps_fetched_at: float | None = None
        # Per-app status cache: app_id -> (fetched_at, value|None).
        self._status: dict[str, tuple[float, dict[str, Any] | None]] = {}

    @property
    def apps_cached(self) -> list[dict[str, Any]] | None:
        """Last-known installed-apps list without triggering a fetch
        (``None`` = unknown / never fetched / unreachable). Lets the
        synchronous skills panel surface app entries from whatever the
        most recent :meth:`list_apps` refresh saw."""
        return self._apps

    def _headers(self) -> dict[str, str]:
        return {"X-Internal-Api-Key": self._api_key} if self._api_key else {}

    async def list_apps(self) -> list[dict[str, Any]] | None:
        """The installed apps (``GET /api/v1/apps``), cached per TTL.

        Returns the catalog list, or ``None`` when the registry is
        unreachable / errored (negative-cached for the TTL). Never
        raises."""
        now = time.monotonic()
        if (
            self._apps_fetched_at is not None
            and now - self._apps_fetched_at < self._ttl
        ):
            return self._apps
        # Stamp BEFORE the call so an unreachable registry is rate-limited
        # to one attempt per TTL (negative caching), same as the caps client.
        self._apps_fetched_at = now
        try:
            resp = await self._client().get(
                f"{self._base}/api/v1/apps", headers=self._headers()
            )
            resp.raise_for_status()
            data = resp.json()
            self._apps = list(data) if isinstance(data, list) else None
        except Exception as exc:
            logger.debug(
                "app registry list_apps failed (%s); app skills fall back to "
                "unavailable", exc,
            )
            self._apps = None
        return self._apps

    async def app_status(self, app_id: str) -> dict[str, Any] | None:
        """One app's live status (``GET /api/v1/apps/{id}/status``),
        cached per TTL per app.

        Returns the proxied ``{"health": …, "state": …}`` dict, or
        ``None`` when the registry is unreachable / errored. Never
        raises."""
        now = time.monotonic()
        cached = self._status.get(app_id)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        value: dict[str, Any] | None
        try:
            resp = await self._client().get(
                f"{self._base}/api/v1/apps/{app_id}/status",
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            value = data if isinstance(data, dict) else None
        except Exception as exc:
            logger.debug(
                "app registry status for %r failed (%s); tool reports the "
                "registry is unreachable", app_id, exc,
            )
            value = None
        self._status[app_id] = (now, value)
        return value


class SyntheticDetectionClient:
    """Demo detector: instead of calling KAI-C/YOLOv8, it reads the ground-truth
    scene a ``SyntheticFrameSource`` embedded in the frame and returns matching
    detections. Lets the whole agent run with no cameras/adapters so the demo is
    deterministic and recordable. Clearly a DEMO path — not real inference."""

    async def infer(
        self,
        *,
        frame_jpeg: bytes,
        extra: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        from frame_sources import synth_detections_from_frame
        return {"result": {"detections": synth_detections_from_frame(frame_jpeg)}}

    async def aclose(self) -> None:  # parity with KaicAdapterClient
        return None


# ── Adapter-direct clients (streaming voice path) ──────────────────


class WhisperClient(_ReusableClientMixin):
    """Direct call to the Whisper adapter's ``/infer`` endpoint with
    a base64-encoded audio chunk. Returns the transcribed text.

    The Whisper adapter accepts WAV / Opus / MP3 / FLAC. We POST raw
    bytes and let the adapter handle format detection."""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._url = url.rstrip("/") + "/infer"
        self._token = token
        self._timeout = timeout_seconds

    async def transcribe(self, audio_bytes: bytes) -> str:
        # Only send Authorization when a token is configured. Native
        # Ollama needs no auth and ``ollama_token`` is empty; an empty
        # ``Bearer `` value (trailing space, no token) is rejected by
        # httpx as an illegal header (LocalProtocolError) before the
        # request is even sent.
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        # FLAT body — the deployed standalone SDK whisper-adapter parses
        # ``audio_b64`` and the params (task/language/vad_filter) at the
        # TOP LEVEL of the JSON object (opennvr_adapter_sdk AUDIO body
        # shape). The legacy combined ai-adapter took a {task, input:{...}}
        # envelope; nesting under ``input`` here makes the SDK parser fail
        # to find ``audio_b64`` and return 400 "JSON body must include
        # 'audio_b64'".
        body = {
            "task": "audio_transcription",
            "audio_b64": base64.b64encode(audio_bytes).decode("ascii"),
            "language": "en",
            # vad_filter OFF: the agent already VAD-segments each
            # utterance (Pipecat Silero + the force-stop logic in
            # services.py) before it reaches here, so the clip is
            # ALREADY trimmed speech. Running Whisper's own Silero VAD
            # again was re-classifying the short, mic-quiet clips as
            # non-speech and discarding the whole utterance ("returned
            # no segments"). The hallucination-token filter in
            # services.py handles the 'You'/'Thank you' noise case.
            "vad_filter": False,
        }
        resp = await self._client().post(self._url, json=body, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        # Log full diagnostic info so we can see no_speech_prob per segment.
        segments = result.get("segments") or []
        if segments:
            seg_info = [(s.get("text", ""), s.get("no_speech_prob")) for s in segments]
            logger.info("Whisper segments: %r", seg_info)
        else:
            logger.info("Whisper returned no segments (vad_filter likely removed all audio)")
        text = (
            result.get("text")
            or result.get("transcript")
            or result.get("transcription")
            or ""
        )
        text = str(text).strip()
        # no_speech_prob gate: Whisper flags hallucinated noise ('You',
        # 'Thank you', …) with a high no_speech_prob. Real speech sits low
        # (~0.1); noise sits high (~0.75+). When EVERY segment is above the
        # gate, treat the whole clip as non-speech and drop it. This is more
        # robust than matching a fixed token list because it also catches
        # hallucinations we didn't enumerate. Segments with no probability
        # reported are treated as speech (fail-open) so we never silently
        # eat a real transcript.
        if text and segments:
            probs = [
                s.get("no_speech_prob")
                for s in segments
                if isinstance(s.get("no_speech_prob"), (int, float))
            ]
            if probs and all(p > 0.6 for p in probs):
                logger.info(
                    "Whisper transcript %r dropped: all segments non-speech "
                    "(no_speech_prob=%r)",
                    text, probs,
                )
                return ""
        return text


class OllamaClient(_ReusableClientMixin):
    """Direct call to the Ollama adapter's ``/infer`` endpoint. Uses
    the OpenAI-style tool-calling shape that landed in S5-prereq —
    sends ``messages`` + ``tools`` and reads back either
    ``message.content`` or ``message.tool_calls``."""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        model: str,
        # First CPU inference cold-loads the model + processes a tool-heavy
        # prompt; keep this comfortably above the adapter-side timeout so the
        # first turn completes instead of being cut off.
        timeout_seconds: float = 300.0,
        # Limited-hardware knobs. num_thread caps CPU cores Ollama uses (None =
        # all cores; set e.g. 2 to keep the rest of the machine responsive).
        # num_ctx sizes the context window: 4096 holds the full tool prompt;
        # lower it (e.g. 2048, when enabled_tools keeps the prompt short) to
        # save RAM and speed up prefill on weak boxes.
        num_thread: int | None = None,
        num_ctx: int = 4096,
        # Reasoning toggle for thinking models (Qwen3). None = don't send the
        # field (non-thinking models). False = disable thinking so the model
        # spends its token budget on the ANSWER, not a hidden <think> block
        # (which otherwise leaves message.content empty → "Sorry…").
        think: bool | None = None,
        # How long Ollama keeps the model resident after a request. -1 =
        # forever (default) so the model never unloads between turns and no
        # turn pays a cold reload + full re-prefill. Sent on EVERY request so
        # residency holds no matter how the Ollama server was launched (a
        # host-run `ollama serve` without OLLAMA_KEEP_ALIVE in its env still
        # stays warm). On a very RAM-tight box, set a duration like "5m".
        keep_alive: str | float = -1,
    ) -> None:
        # Talk to Ollama's NATIVE chat API. The deployment points
        # ``ollama_url`` at the raw ollama runtime (http://ollama:11434),
        # which exposes /api/chat — NOT the SDK /infer envelope endpoint.
        # The response/tool-call consumer (_run_conversation_turn /
        # _invoke_tool) already expects the native shape: top-level
        # ``message`` with ``content`` + ``tool_calls[].function.arguments``.
        self._url = url.rstrip("/") + "/api/chat"
        self._token = token
        self._model = model
        self._timeout = timeout_seconds
        self._num_thread = num_thread
        self._num_ctx = num_ctx
        self._think = think
        self._keep_alive = keep_alive

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.4,
        max_tokens: int = 256,
    ) -> dict[str, Any]:
        # Only send Authorization when a token is configured. Native
        # Ollama needs no auth and ``ollama_token`` is empty; an empty
        # ``Bearer `` value (trailing space, no token) is rejected by
        # httpx as an illegal header (LocalProtocolError) before the
        # request is even sent.
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        # Native Ollama /api/chat body. ``stream: false`` so we get one
        # complete JSON object back (the response carries ``message`` with
        # ``content`` and ``tool_calls`` at the top level). temperature /
        # max_tokens live under ``options`` (num_predict) per the native
        # API — they are NOT top-level chat fields.
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            # Keep the model resident (see __init__): belt-and-suspenders with
            # the server's OLLAMA_KEEP_ALIVE, and the ONLY thing that keeps a
            # host-run Ollama warm when that env var isn't set.
            "keep_alive": self._keep_alive,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                # num_ctx MUST exceed the full system+tools+history prompt.
                # Ollama's default is 2048, but the tool-calling prompt runs
                # ~2200 tokens, so it was being silently TRUNCATED every call
                # (logged as "truncating input prompt"). Truncation (a) drops
                # context and (b) shifts the prefix window so the prewarmed
                # KV cache no longer matches — forcing a full ~2k-token CPU
                # re-prefill (~110s) on EVERY call instead of reusing cache.
                # 4096 holds the whole prompt so the prefix stays stable and
                # the prewarm + iter0→iter1 cache reuse actually kick in.
                "num_ctx": self._num_ctx,
            },
        }
        if self._num_thread:
            # Cap CPU cores so the LLM doesn't peg a limited machine.
            body["options"]["num_thread"] = self._num_thread
        if self._think is not None:
            # Top-level (NOT under options) for Ollama's thinking models.
            body["think"] = self._think
        if tools:
            # Native /api/chat decides tool use automatically; there is no
            # ``tool_choice`` field (it would be ignored). Just advertise
            # the tools.
            body["tools"] = tools
        resp = await self._client().post(self._url, json=body, headers=headers)
        if "think" in body and getattr(resp, "status_code", 200) >= 400:
            # Some Ollama versions / non-thinking models reject the ``think``
            # field. Retry once without it rather than failing the turn.
            logger.info("ollama: request rejected with think=%s; retrying without it", body["think"])
            body.pop("think", None)
            resp = await self._client().post(self._url, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()


class OpenAILLMClient(_ReusableClientMixin):
    """Cloud/hybrid LLM brain via any OpenAI-compatible chat API — OpenAI,
    Groq, Together, OpenRouter, or a local OpenAI-API server (vLLM, llama.cpp,
    LM Studio, Ollama's /v1). Used when ``llm_provider: openai`` so the agent
    gets stronger, lower-latency tool-calling without loading a local LLM
    (issue #82). Normalises the response to the same ``{"message": {...}}``
    shape the turn loop expects."""

    def __init__(self, *, base_url: str, api_key: str | None, model: str,
                 timeout_seconds: float = 120.0) -> None:
        url = base_url.rstrip("/")
        if url.endswith("/chat/completions"):
            self._url = url
        elif url.endswith("/v1"):
            self._url = url + "/chat/completions"
        else:
            self._url = url + "/v1/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.4,
        max_tokens: int = 256,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body: dict[str, Any] = {
            "model": self._model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        resp = await self._client().post(self._url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        message = (choices[0].get("message") if choices else None) or {
            "role": "assistant", "content": ""
        }
        # OpenAI omits content when only tool_calls are returned; normalise to "".
        if message.get("content") is None:
            message["content"] = ""
        return {"message": message}


class PiperClient(_ReusableClientMixin):
    """Direct call to the Piper adapter's ``/infer`` endpoint. Returns
    raw audio bytes (WAV format from Piper by default)."""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._url = url.rstrip("/") + "/infer"
        self._token = token
        self._timeout = timeout_seconds

    async def synthesize(self, text: str) -> bytes:
        # Only send Authorization when a token is configured. Native
        # Ollama needs no auth and ``ollama_token`` is empty; an empty
        # ``Bearer `` value (trailing space, no token) is rejected by
        # httpx as an illegal header (LocalProtocolError) before the
        # request is even sent.
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        # task name varies across adapter generations — legacy used
        # ``speech_synthesis``; the contract §5.4 spec is ``text_to_speech``.
        # Send the modern name; legacy operators upgrade the adapter.
        #
        # ``inline: true`` asks the SDK Piper service to base64-encode
        # the generated WAV into ``result.audio_b64`` alongside the
        # default ``result.audio_uri``. We don't have a shared
        # filesystem mount to dereference the opennvr://audio/...
        # URI, so the inline body is the only way we get the audio
        # bytes back over plain HTTP.
        # FLAT body — the deployed standalone SDK piper-adapter reads
        # ``text`` (and the ``inline`` flag) at the TOP LEVEL of the JSON
        # body, not nested under an ``input`` envelope. The adapter task is
        # ``speech_synthesis`` (not ``text_to_speech``); ``inline: true``
        # asks it to return the WAV bytes base64-encoded since we have no
        # shared audio mount. Nesting under ``input`` made the service see
        # no top-level ``text`` and return 400 "Field 'text' is required".
        body = {
            "task": "speech_synthesis",
            "text": text,
            "inline": True,
        }
        client = self._client()
        resp = await client.post(self._url, json=body, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        # Combined /infer returns SpeechSynthesisResponse at the top level;
        # per-adapter shapes nest it under "result". Accept both.
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        # Two response shapes in flight: inline base64 audio for
        # streaming-friendly clients, and a server-side URI for
        # bandwidth-conscious deployments. Prefer inline; fall back
        # to fetching the URI if the adapter only emitted one.
        audio_b64 = result.get("audio_b64") or result.get("audio")
        if audio_b64:
            return base64.b64decode(audio_b64)
        audio_uri = result.get("audio_uri") or result.get("uri")
        if audio_uri:
            # Adapter-controlled URI — could point anywhere. Two
            # safety layers:
            #
            #  1. Resolve relative URIs (e.g. ``/audio/abc.wav``)
            #     against the adapter base URL so httpx has an
            #     absolute target. Without this, an empty scheme /
            #     netloc would short-circuit the same-origin check
            #     AND then crash the GET because the shared client
            #     has no ``base_url`` set.
            #  2. Only forward the bearer token when the resolved
            #     URI is same-origin with the adapter we trust.
            #     An attacker-tampered response pointing at
            #     evil.example.com MUST NOT leak the token.
            from urllib.parse import urljoin, urlparse
            resolved_uri = urljoin(self._url, audio_uri)
            adapter_origin = urlparse(self._url)
            uri_origin = urlparse(resolved_uri)
            same_origin = (
                uri_origin.scheme == adapter_origin.scheme
                and uri_origin.netloc.lower() == adapter_origin.netloc.lower()
            )
            secondary_headers: dict[str, str] = {}
            if same_origin:
                secondary_headers["Authorization"] = headers["Authorization"]
            try:
                audio_resp = await client.get(resolved_uri, headers=secondary_headers)
                audio_resp.raise_for_status()
                return audio_resp.content
            except Exception:
                logger.exception(
                    "Piper adapter returned audio_uri %s (resolved %s) but fetch failed",
                    audio_uri, resolved_uri,
                )
                return b""
        # Common cause: adapter received the inline=true flag but
        # the underlying audio_uri couldn't be resolved (Piper's
        # _read_audio_inline swallows resolve_audio_uri failures and
        # falls back to None, so we end up here with neither field
        # set). Check the adapter's logs for "could not resolve
        # audio_uri" or "could not read audio file" entries.
        logger.warning(
            "Piper adapter response contained no audio_b64 nor audio_uri; "
            "if inline=true was sent, check adapter logs for resolve "
            "or read failures"
        )
        return b""


class OpennvrAuthClient(_ReusableClientMixin):
    """The agent's auth delegation to the main OpenNVR server — the agent
    never mints or stores credentials of its own (no second user table,
    revocation and MFA stay the server's job, and a future Android app
    speaks the exact same bearer contract).

    Three calls against ``{base}/api/v1/auth``:

    * :meth:`login`   → ``POST /login-json`` (username/password [+ TOTP]) —
      passthrough of the server's token pair, so the demo page and any
      mobile client get access **and refresh** tokens from one origin.
    * :meth:`refresh` → ``POST /refresh`` — new pair from a refresh token.
    * :meth:`me`      → ``GET /me`` with the presented bearer token —
      the validation path, cached per token for ``ttl_seconds`` so a
      page full of polling widgets costs ~one upstream call a minute,
      not one per request. 401 → None (cached briefly too, so a bad
      token can't hammer the server through the agent).
    """

    def __init__(self, *, base_url: str, ttl_seconds: float = 60.0,
                 timeout_seconds: float = 5.0) -> None:
        self._root = base_url.rstrip("/")
        self._base = f"{self._root}/api/v1/auth"
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        # token -> (checked_at_monotonic, user_payload_or_None)
        self._cache: dict[str, tuple[float, dict | None]] = {}
        self._cache_max = 256   # bound: distinct tokens seen per TTL window
        # device_token -> (checked_at_monotonic, allowed, negative_ttl_used)
        self._device_cache: dict[str, tuple[float, bool, float]] = {}
        self._device_fail_ttl = 10.0   # short: enforcement resumes fast after a blip

    async def login(self, username: str, password: str,
                    totp_code: str | None = None, *,
                    device_token: str | None = None,
                    user_agent: str | None = None) -> tuple[int, dict]:
        """Proxy a login. Returns (status_code, response_json) verbatim —
        the caller relays both, so setup-required / MFA / bad-credential
        semantics stay exactly the server's.

        ``device_token``/``user_agent`` are forwarded as headers so the
        server's device firewall re-recognises this browser instead of
        minting a fresh device row per login, and the admin's device list
        shows the real browser UA rather than python-httpx."""
        body: dict = {"username": username, "password": password}
        if totp_code:
            # OpenNVR's UserLogin schema names the MFA field ``code`` (see
            # server/schemas.py). Sending ``totp_code`` was silently dropped by
            # Pydantic, so the server saw no code → "Invalid or missing MFA code".
            body["code"] = totp_code
        headers: dict = {}
        if device_token:
            headers["x-device-token"] = device_token
        if user_agent:
            headers["user-agent"] = user_agent
        try:
            resp = await self._client().post(f"{self._base}/login-json", json=body,
                                             headers=headers)
            try:
                data = resp.json()
            except Exception:
                data = {"detail": resp.text[:200]}
            return resp.status_code, data
        except Exception as exc:
            logger.warning("auth: login proxy failed: %s", exc)
            return 502, {"detail": "OpenNVR server unreachable"}

    async def refresh(self, refresh_token: str) -> tuple[int, dict]:
        try:
            resp = await self._client().post(
                f"{self._base}/refresh", json={"refresh_token": refresh_token})
            try:
                data = resp.json()
            except Exception:
                data = {"detail": resp.text[:200]}
            return resp.status_code, data
        except Exception as exc:
            logger.warning("auth: refresh proxy failed: %s", exc)
            return 502, {"detail": "OpenNVR server unreachable"}

    async def me(self, token: str) -> dict | None:
        """Validate a bearer token → the server's user payload, or None.
        Cached per token (positive AND negative) for the TTL."""
        now = time.monotonic()
        hit = self._cache.get(token)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        user: dict | None = None
        try:
            resp = await self._client().get(
                f"{self._base}/me",
                headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 200:
                user = resp.json()
        except Exception as exc:
            logger.warning("auth: /me validation failed: %s", exc)
            # Unreachable server → treat as invalid but DON'T cache long:
            # drop through with user=None; the short negative cache below
            # limits retry pressure while letting recovery be quick.
        if len(self._cache) >= self._cache_max:
            self._cache.clear()   # crude but bounded; refills within a TTL
        self._cache[token] = (now, user)
        return user

    async def device_allowed(self, device_token: str | None) -> bool:
        """True iff the server's device firewall permits this browser.

        Calls the open endpoint ``GET /api/v1/device-firewall/status``
        forwarding the browser's device token (minted at login, relayed by
        the demo page as ``X-Device-Token``). Allowed when enforcement is
        off, or when this device's status is ``approved`` — exact parity
        with the server's own middleware semantics. A caller with no token
        while enforcement is on is denied; the recovery path is
        ``/auth/login`` (open), which enrolls the device and returns a
        fresh token.

        FAIL-OPEN on transport errors, deliberately: the server treats
        enforcement-state read errors as "off" too, and when the server is
        down ``me()`` fails as well, so the bearer gate still 401s — the
        firewall check failing open never admits an unauthenticated caller.
        Failure results are cached for only ``_device_fail_ttl`` seconds so
        enforcement resumes quickly after a server blip."""
        key = device_token or ""
        now = time.monotonic()
        hit = self._device_cache.get(key)
        if hit is not None and now - hit[0] < hit[2]:
            return hit[1]
        allowed = True
        ttl = self._ttl
        try:
            headers = {"x-device-token": device_token} if device_token else {}
            resp = await self._client().get(
                f"{self._root}/api/v1/device-firewall/status", headers=headers)
            data = resp.json() if resp.status_code == 200 else {}
            if data.get("enforcement_active"):
                allowed = data.get("status") == "approved"
        except Exception as exc:
            logger.warning("auth: device-firewall status check failed "
                           "(failing open): %s", exc)
            ttl = self._device_fail_ttl
        if len(self._device_cache) >= self._cache_max:
            self._device_cache.clear()
        self._device_cache[key] = (now, allowed, ttl)
        return allowed


class OpennvrRecordingsClient(_ReusableClientMixin):
    """User-token pass-through to the main server's playback API — the
    agent's Recorded row NEVER uses the service key for recorded video
    (same governance line as app actions): every call carries the
    CALLER's bearer token, so camera permissions and audit stay per-user.

    Three thin forwards against ``{base}/api/v1/recordings``:
    * :meth:`playback_cameras` → which server cameras have recordings
      (used to resolve a server camera id → its MediaMTX path).
    * :meth:`playback_list`    → the segment list for one path.
    * :meth:`playback_url`     → a direct player URL for one segment —
      the mobile-safe pattern (a plain URL ExoPlayer / <video> can
      stream with no auth headers).
    """

    def __init__(self, *, base_url: str, timeout_seconds: float = 10.0) -> None:
        self._root = base_url.rstrip("/")
        self._base = f"{self._root}/api/v1/recordings"
        self._timeout = timeout_seconds
        # server camera id -> (resolved_at_monotonic, mediamtx path)
        self._paths: dict[int, tuple[float, str]] = {}
        self._path_ttl = 300.0

    async def _get(self, token: str, path: str, params: dict) -> tuple[int, dict]:
        try:
            resp = await self._client().get(
                f"{self._base}{path}", params=params,
                headers={"Authorization": f"Bearer {token}"})
            try:
                data = resp.json()
            except Exception:
                data = {"error": resp.text[:200]}
            return resp.status_code, data
        except Exception as exc:
            logger.warning("recordings: %s proxy failed: %s", path, exc)
            return 502, {"error": "OpenNVR server unreachable"}

    async def resolve_path(self, token: str, opennvr_camera_id: int) -> tuple[int, str | None]:
        """Server camera id → MediaMTX path (cached ~5 min). The server
        owns the naming convention — never guess it here."""
        hit = self._paths.get(opennvr_camera_id)
        if hit is not None and time.monotonic() - hit[0] < self._path_ttl:
            return 200, hit[1]
        status, data = await self._get(token, "/playback/cameras", {})
        if status != 200:
            return status, None
        for cam in data.get("cameras") or []:
            try:
                cid = int(cam.get("camera_id"))
            except (TypeError, ValueError):
                continue
            path = str(cam.get("path") or "")
            if path:
                self._paths[cid] = (time.monotonic(), path)
        hit = self._paths.get(opennvr_camera_id)
        return 200, (hit[1] if hit else None)

    async def playback_list(self, token: str, path: str) -> tuple[int, dict]:
        return await self._get(token, "/playback/list", {"path": path})

    async def playback_url(self, token: str, path: str, start: str,
                           duration: float) -> tuple[int, dict]:
        return await self._get(token, "/playback/url",
                               {"path": path, "start": start,
                                "duration": str(duration)})

    async def stream_info(self, token: str, opennvr_camera_id: int) -> tuple[int, dict]:
        """LIVE stream info from the main server: the WHEP URL + a
        short-lived camera-scoped MediaMTX token (the same call the
        OpenNVR Live view makes — that's why ITS streams are smooth)."""
        try:
            resp = await self._client().get(
                f"{self._root}/api/v1/streams/{opennvr_camera_id}/info",
                headers={"Authorization": f"Bearer {token}"})
            try:
                data = resp.json()
            except Exception:
                data = {"error": resp.text[:200]}
            return resp.status_code, data
        except Exception as exc:
            logger.warning("streams: info proxy failed: %s", exc)
            return 502, {"error": "OpenNVR server unreachable"}
