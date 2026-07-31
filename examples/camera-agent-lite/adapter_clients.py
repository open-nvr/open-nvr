# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""HTTP clients for the four OpenNVR AI adapters the lite agent consumes.

Every model runtime lives in its own adapter container (built from the
ai-adapter repo) and speaks the standard adapter contract: ``POST /infer``
with a JSON envelope, ``GET /health``. This module is the only place that
knows those wire shapes:

* ``LlmClient``  → llamacpp adapter  (Qwen2.5-3B via llama.cpp, tool calling)
* ``SttClient``  → whispercpp adapter (utterance WAV → transcript)
* ``TtsClient``  → pipertts adapter   (text → spoken WAV, base64)
* ``VlmClient``  → smolvlm adapter    (frame + question → answer)
"""
from __future__ import annotations

import base64
import io
import time
import wave
from dataclasses import dataclass
from typing import Optional

import httpx
import numpy as np

import logging

logger = logging.getLogger(__name__)


class AdapterError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


class AdapterClient:
    """POSTs the contract ``/infer`` envelope and reads back ``result``."""

    def __init__(self, base_url: str, token: str = "", timeout_s: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def infer(self, body: dict) -> dict:
        try:
            resp = await self._client.post(
                f"{self.base_url}/infer", json=body, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise AdapterError(f"adapter unreachable at {self.base_url}: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            raise AdapterError(f"adapter HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200 or data.get("status") == "error":
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise AdapterError(
                err.get("message") or f"adapter HTTP {resp.status_code}",
                transient=bool(err.get("transient", resp.status_code >= 500)),
            )
        return data.get("result", {}) or {}

    async def health(self) -> bool:
        try:
            resp = await self._client.get(f"{self.base_url}/health", timeout=2.0)
            if resp.status_code != 200:
                return False
            status = str(resp.json().get("status", "")).lower()
            return status in ("ok", "degraded")
        except Exception:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()


# ── LLM (llamacpp adapter) ─────────────────────────────────────────


class LlmClient:
    """Chat with tool calling. Returns the OpenAI-shaped dict the agent's
    tool loop expects."""

    def __init__(self, base_url: str, token: str = "", timeout_s: float = 120.0) -> None:
        self._c = AdapterClient(base_url, token, timeout_s)

    async def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        **params,
    ) -> dict:
        body: dict = {
            "messages": messages,
            "max_tokens": params.get("max_tokens", 256),
            "temperature": params.get("temperature", 0.3),
            "top_p": params.get("top_p", 0.9),
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = params.get("tool_choice", "auto")
        result = await self._c.infer(body)
        message: dict = {"role": "assistant", "content": result.get("text", "")}
        if result.get("tool_calls"):
            message["tool_calls"] = result["tool_calls"]
        return {
            "choices": [{"message": message, "finish_reason": result.get("finish_reason")}],
            "usage": result.get("usage", {}),
        }

    async def health(self) -> bool:
        return await self._c.health()

    async def aclose(self) -> None:
        await self._c.aclose()


# ── STT (whispercpp adapter) ───────────────────────────────────────


@dataclass
class SttResult:
    text: str


class SttClient:
    def __init__(self, base_url: str, token: str = "", language: str = "en",
                 timeout_s: float = 60.0) -> None:
        self._c = AdapterClient(base_url, token, timeout_s)
        self._language = language

    async def transcribe(self, pcm: np.ndarray, sample_rate: int = 16000) -> SttResult:
        """Transcribe a complete utterance (float32 mono)."""
        if pcm is None or len(pcm) == 0:
            return SttResult(text="")
        return await self.transcribe_wav(_float32_to_wav(pcm, sample_rate))

    async def transcribe_wav(self, wav: bytes) -> SttResult:
        """Transcribe a complete utterance already packaged as a WAV blob
        (the shape Pipecat's SegmentedSTTService hands to run_stt)."""
        if not wav:
            return SttResult(text="")
        body = {
            "audio_b64": base64.b64encode(wav).decode("ascii"),
            "language": self._language,
        }
        result = await self._c.infer(body)
        return SttResult(text=(result.get("transcript") or "").strip())

    async def health(self) -> bool:
        return await self._c.health()

    async def aclose(self) -> None:
        await self._c.aclose()


def _float32_to_wav(pcm: np.ndarray, sample_rate: int) -> bytes:
    pcm16 = (np.clip(pcm, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


# ── TTS (pipertts adapter) ─────────────────────────────────────────


class TtsClient:
    def __init__(self, base_url: str, token: str = "", timeout_s: float = 60.0) -> None:
        self._c = AdapterClient(base_url, token, timeout_s)

    async def synthesize_b64(self, text: str) -> Optional[str]:
        """Synthesize speech; returns a base64 WAV or None on failure (the
        dashboard degrades to text-only, it never errors the whole turn)."""
        if not text.strip():
            return None
        try:
            result = await self._c.infer({"text": text, "inline": True})
            return result.get("audio_b64")
        except AdapterError as exc:
            logger.warning("TTS failed: %s", exc)
            return None

    async def health(self) -> bool:
        return await self._c.health()

    async def aclose(self) -> None:
        await self._c.aclose()


# ── VLM (smolvlm adapter) ──────────────────────────────────────────


@dataclass
class VlmResult:
    text: str
    inference_ms: float = 0.0
    error: Optional[str] = None


class VlmClient:
    def __init__(self, base_url: str, token: str = "", timeout_s: float = 95.0) -> None:
        self._c = AdapterClient(base_url, token, timeout_s)

    async def analyse_image(self, jpeg: bytes, question: str) -> VlmResult:
        if not jpeg:
            return VlmResult(text="", error="empty frame")
        body = {"frame_b64": base64.b64encode(jpeg).decode("ascii"), "question": question}
        started = time.monotonic()
        try:
            result = await self._c.infer(body)
        except AdapterError as exc:
            return VlmResult(text="", error=str(exc))
        return VlmResult(
            text=(result.get("text") or result.get("caption") or "").strip(),
            inference_ms=(time.monotonic() - started) * 1000.0,
        )

    async def analyse_frames(self, jpegs: list[bytes], question: str) -> VlmResult:
        # The SmolVLM adapter is single-image; use the newest frame (last).
        if not jpegs:
            return VlmResult(text="", error="no frames")
        return await self.analyse_image(jpegs[-1], question)

    async def health(self) -> bool:
        return await self._c.health()

    async def aclose(self) -> None:
        await self._c.aclose()


# ── OpenNVR auth delegation (auth_mode="opennvr") ──────────────────
# Self-contained copy of camera-agent's OpennvrAuthClient (examples are
# deliberately standalone — same reasoning as serializer.py). The agent
# never mints or stores credentials of its own: login/refresh are thin
# proxies to the main OpenNVR server and ``me()`` validates presented
# bearer tokens against it. ``device_allowed()`` additionally honours
# OpenNVR's device firewall (Settings → Firewall).


class _ReusableClientMixin:
    """One ``httpx.AsyncClient`` per service instance, lazily constructed."""

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


class OpennvrAuthClient(_ReusableClientMixin):
    """Auth + device-firewall delegation to the main OpenNVR server.

    * :meth:`login`   → ``POST /api/v1/auth/login-json`` (passthrough).
    * :meth:`refresh` → ``POST /api/v1/auth/refresh`` (passthrough).
    * :meth:`me`      → ``GET /api/v1/auth/me`` — validates a bearer token,
      cached per token (positive AND negative) for ``ttl_seconds``.
    * :meth:`device_allowed` → ``GET /api/v1/device-firewall/status`` —
      honours the server's device firewall.
    """

    def __init__(self, *, base_url: str, ttl_seconds: float = 60.0,
                 timeout_seconds: float = 5.0) -> None:
        self._root = base_url.rstrip("/")
        self._base = f"{self._root}/api/v1/auth"
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        # token -> (checked_at_monotonic, user_payload_or_None)
        self._cache: dict[str, tuple[float, Optional[dict]]] = {}
        self._cache_max = 256
        # device_token -> (checked_at_monotonic, allowed, ttl_used)
        self._device_cache: dict[str, tuple[float, bool, float]] = {}
        self._device_fail_ttl = 10.0   # short: enforcement resumes fast

    async def login(self, username: str, password: str,
                    totp_code: Optional[str] = None, *,
                    device_token: Optional[str] = None,
                    user_agent: Optional[str] = None) -> tuple[int, dict]:
        """Proxy a login; (status, body) pass through verbatim. The browser's
        device token + UA are forwarded so the server's device firewall
        re-recognises the browser instead of minting a row per login."""
        body: dict = {"username": username, "password": password}
        if totp_code:
            # OpenNVR's UserLogin schema names the MFA field ``code``.
            body["code"] = totp_code
        headers: dict = {}
        if device_token:
            headers["x-device-token"] = device_token
        if user_agent:
            headers["user-agent"] = user_agent
        try:
            resp = await self._client().post(f"{self._base}/login-json",
                                             json=body, headers=headers)
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

    async def me(self, token: str) -> Optional[dict]:
        """Validate a bearer token → the server's user payload, or None."""
        now = time.monotonic()
        hit = self._cache.get(token)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        user: Optional[dict] = None
        try:
            resp = await self._client().get(
                f"{self._base}/me",
                headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 200:
                user = resp.json()
        except Exception as exc:
            logger.warning("auth: /me validation failed: %s", exc)
        if len(self._cache) >= self._cache_max:
            self._cache.clear()
        self._cache[token] = (now, user)
        return user

    async def device_allowed(self, device_token: Optional[str]) -> bool:
        """True iff OpenNVR's device firewall permits this browser.

        Allowed when enforcement is off, or this device is ``approved``.
        No token while enforcement is on ⇒ denied (recovery: /auth/login is
        open; logging in enrolls the device and returns a fresh token).
        FAIL-OPEN on transport errors — the server itself treats
        enforcement-state read errors as off, and with the server down
        ``me()`` fails too, so the bearer gate still returns 401."""
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
            logger.warning("auth: device-firewall check failed (failing open): %s",
                           exc)
            ttl = self._device_fail_ttl
        if len(self._device_cache) >= self._cache_max:
            self._device_cache.clear()
        self._device_cache[key] = (now, allowed, ttl)
        return allowed
