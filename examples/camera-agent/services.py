# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Thin Pipecat services that wrap the OpenNVR adapter HTTP clients.

Pipecat ships built-in services for Whisper / Ollama / Piper, but
those talk to the upstreams directly and would bypass the OpenNVR
adapter contract entirely — no audit log entry on the adapter side,
no consistency with how the rest of the gallery talks to the same
models. These three wrappers route every call through the adapter's
``/infer`` endpoint instead, so the adapter's own audit log records
every utterance.

The wrappers stay deliberately thin: Pipecat owns the frame pumping,
VAD, turn-taking, and pipeline coordination; the OpenNVR adapter
clients in ``adapter_clients.py`` own the wire format. These classes
are just the glue.

Pipecat's service APIs evolve between minor versions. This file
targets ``pipecat-ai >=0.0.55,<1.0``. If you upgrade Pipecat and see
import errors at boot, check the new service base classes in
``pipecat.services`` and adjust the imports below — the body of each
``run_*`` method should still be portable since it's just an
``async def`` calling our adapter clients.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator

# Pipecat frame + service imports.
#
# Wrapped in ``try`` to keep the module importable in test environments
# that don't have Pipecat installed (the tests stub these out). The
# real boot path imports the module after ``pipecat-ai`` is on the
# venv path, so the stubs only matter to unit tests.
try:  # pragma: no cover — import-time only
    from pipecat.frames.frames import (
        Frame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
        TextFrame,
        TranscriptionFrame,
        TTSAudioRawFrame,
        TTSStartedFrame,
        TTSStoppedFrame,
    )
    from pipecat.services.ai_services import LLMService, SegmentedSTTService, TTSService
    from pipecat.processors.aggregators.openai_llm_context import (
        OpenAILLMContext,
        OpenAILLMContextFrame,
    )
    from pipecat.utils.time import time_now_iso8601
except Exception:  # pragma: no cover
    # Tests don't import this module directly; they stub Pipecat in
    # sys.modules before importing camera_agent. Falling back to bare
    # ``object`` lets ``ast.parse`` succeed for the static checks too.
    Frame = object  # type: ignore
    LLMFullResponseEndFrame = object  # type: ignore
    LLMFullResponseStartFrame = object  # type: ignore
    LLMTextFrame = object  # type: ignore
    TextFrame = object  # type: ignore
    TranscriptionFrame = object  # type: ignore
    TTSAudioRawFrame = object  # type: ignore
    TTSStartedFrame = object  # type: ignore
    TTSStoppedFrame = object  # type: ignore
    LLMService = object  # type: ignore
    SegmentedSTTService = object  # type: ignore
    TTSService = object  # type: ignore
    OpenAILLMContext = object  # type: ignore
    OpenAILLMContextFrame = object  # type: ignore

    def time_now_iso8601() -> str:  # type: ignore
        import datetime as _dt
        return _dt.datetime.now(_dt.timezone.utc).isoformat()


from adapter_clients import OllamaClient, PiperClient, WhisperClient

logger = logging.getLogger(__name__)


# ── STT: Whisper via OpenNVR adapter ───────────────────────────────


class OpenNvrWhisperSTT(SegmentedSTTService):
    """Bridges Pipecat's SegmentedSTTService contract to the Whisper adapter.

    SegmentedSTTService is the right base for a NON-streaming model: it
    uses the pipeline VAD to buffer a whole utterance (between
    UserStarted/StoppedSpeaking) and calls ``run_stt`` ONCE per utterance
    with the complete audio as a WAV blob. (The plain ``STTService`` base
    calls ``run_stt`` on every ~0.26s audio chunk, which Whisper can't
    transcribe — that produced empty transcripts.) We return one
    ``TranscriptionFrame`` per utterance.
    """

    def __init__(
        self,
        *,
        client: WhisperClient,
        sample_rate: int = 16000,
    ) -> None:
        super().__init__(sample_rate=sample_rate)
        self._client = client

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        if not audio:
            return
        try:
            text = await self._client.transcribe(audio)
        except Exception:
            logger.exception("Whisper adapter call failed; emitting empty transcript")
            return
        text = (text or "").strip()
        if not text:
            return
        yield TranscriptionFrame(text, "", time_now_iso8601())


# ── LLM: Ollama via OpenNVR adapter, with tool calling ─────────────


class OpenNvrOllamaLLM(LLMService):
    """Bridges Pipecat's LLM contract to the Ollama adapter's
    OpenAI-style chat_completion task.

    Handles the full tool-calling loop inline:

      1. Pipecat hands us an ``OpenAILLMContext`` carrying the
         conversation history.
      2. We POST ``messages`` + ``tools`` to the adapter.
      3. If the response carries ``tool_calls``, we invoke the
         registered handlers (via ``self._tool_handlers``), append
         the results as ``role: tool`` messages, and re-POST.
      4. Once the response is plain text, we stream it out as
         ``LLMTextFrame``s and let Pipecat's downstream aggregator
         pipe it into TTS.

    A guard caps the loop at ``max_tool_iterations`` so a confused
    model can't ping-pong indefinitely between tool calls.
    """

    def __init__(
        self,
        *,
        client: OllamaClient,
        tools: list[dict[str, Any]],
        tool_handlers: dict[str, Any],  # name → async callable(args: dict) -> str
        temperature: float = 0.4,
        max_tokens: int = 256,
        max_tool_iterations: int = 4,
    ) -> None:
        super().__init__()
        self._client = client
        self._tools = list(tools)
        self._tool_handlers = dict(tool_handlers)
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_tool_iterations = max_tool_iterations

    async def process_frame(self, frame: Frame, direction: Any) -> None:  # type: ignore[override]
        # We act on the context frame; every other frame is forwarded
        # untouched so the rest of the pipeline (TTS, transport, the
        # assistant aggregator) keeps receiving system / interruption
        # / audio frames it needs to function. Without the explicit
        # push_frame() below, neither AIService.process_frame nor
        # FrameProcessor.process_frame propagates anything — frames
        # are silently swallowed and the pipeline locks up after the
        # first non-context frame.
        await super().process_frame(frame, direction)
        if isinstance(frame, OpenAILLMContextFrame):
            await self._handle_context(frame.context)
        else:
            await self.push_frame(frame, direction)

    async def _handle_context(self, context: OpenAILLMContext) -> None:
        # Snapshot the conversation messages; Pipecat's context
        # aggregator owns the canonical list.
        messages = list(context.get_messages())

        # Emit the "LLM is thinking" bracket so downstream TTS knows
        # when to start / stop assembling its audio chunks.
        await self.push_frame(LLMFullResponseStartFrame())

        try:
            for iteration in range(self._max_tool_iterations):
                response = await self._client.chat(
                    messages=messages,
                    tools=self._tools,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                )
                message = response.get("message") or {}
                tool_calls = message.get("tool_calls") or []
                content = (message.get("content") or "").strip()

                # Append the assistant turn (with tool_calls if any)
                # so the next iteration has the history.
                messages.append({
                    "role": "assistant",
                    "content": content,
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                })

                if not tool_calls:
                    # Final assistant text — stream into the pipeline.
                    if content:
                        await self.push_frame(LLMTextFrame(content))
                    return

                # Models sometimes emit a partial natural-language
                # reply alongside the tool calls ("Let me check the
                # porch…" + a describe_camera invocation). Speak the
                # partial reply so the user knows the agent's still
                # there, then proceed to the tools. Without this the
                # partial content is silently dropped.
                if content:
                    await self.push_frame(LLMTextFrame(content))

                # Execute each tool call and append its result.
                for call in tool_calls:
                    name, result = await self._invoke_tool(call)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "name": name,
                        "content": result,
                    })
                    # Mirror the result onto the Pipecat context so
                    # transcript-style observers see what happened.
                    context.add_message({
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "name": name,
                        "content": result,
                    })

            # Loop exhausted without a final answer. Fail gracefully
            # rather than hang.
            logger.warning(
                "LLM tool-call loop exhausted after %d iterations",
                self._max_tool_iterations,
            )
            await self.push_frame(LLMTextFrame(
                "Sorry, I'm having trouble looking that up right now."
            ))
        finally:
            await self.push_frame(LLMFullResponseEndFrame())

    async def _invoke_tool(self, call: dict[str, Any]) -> tuple[str, str]:
        func = call.get("function") or {}
        name = str(func.get("name") or "").strip()
        args_raw = func.get("arguments") or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
        except json.JSONDecodeError:
            logger.warning(
                "tool %r received malformed JSON arguments: %r", name, args_raw
            )
            return name or "<unknown>", (
                f"ERROR: tool '{name}' received malformed arguments."
            )
        handler = self._tool_handlers.get(name)
        if handler is None:
            return name, f"ERROR: tool '{name}' is not registered."
        try:
            result = await handler(args)
        except Exception:
            logger.exception("Tool %s raised", name)
            return name, f"ERROR: tool '{name}' failed unexpectedly."
        # Truncate so a runaway tool can't blow the prompt budget.
        if isinstance(result, str) and len(result) > 1200:
            result = result[:1200] + " …(truncated)"
        return name, str(result)


# ── TTS: Piper via OpenNVR adapter ─────────────────────────────────


class OpenNvrPiperTTS(TTSService):
    """Bridges Pipecat's TTSService contract to the Piper adapter.

    Piper produces a complete WAV buffer per utterance (no
    inter-chunk streaming at the adapter), so this service emits one
    ``TTSAudioRawFrame`` per LLM sentence. Pipecat downstream slices
    it into transport-sized chunks itself.
    """

    def __init__(
        self,
        *,
        client: PiperClient,
        sample_rate: int = 22050,
    ) -> None:
        super().__init__(sample_rate=sample_rate)
        self._client = client

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        text = (text or "").strip()
        if not text:
            return
        yield TTSStartedFrame()
        try:
            audio_bytes = await self._client.synthesize(text)
        except Exception:
            logger.exception("Piper adapter synthesise failed")
            yield TTSStoppedFrame()
            return
        if not audio_bytes:
            yield TTSStoppedFrame()
            return
        # WAV from Piper carries a 44-byte RIFF header; strip it so
        # we ship raw PCM frames the transport layer can chunk
        # without re-parsing headers per chunk. Defensive: only strip
        # when the header actually matches.
        pcm = _strip_wav_header(audio_bytes)
        yield TTSAudioRawFrame(
            audio=pcm,
            sample_rate=self._sample_rate,
            num_channels=1,
        )
        yield TTSStoppedFrame()


def _strip_wav_header(audio: bytes) -> bytes:
    """If ``audio`` is a RIFF WAV, return just the PCM data chunk.
    Falls back to the input untouched for non-WAV or malformed data."""
    if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return audio
    # Walk chunks until we hit 'data'. WAV chunk header = 4-byte id +
    # 4-byte little-endian size.
    pos = 12
    while pos + 8 <= len(audio):
        chunk_id = audio[pos:pos + 4]
        chunk_size = int.from_bytes(audio[pos + 4:pos + 8], "little")
        if chunk_id == b"data":
            start = pos + 8
            return audio[start:start + chunk_size]
        pos += 8 + chunk_size
    return audio
