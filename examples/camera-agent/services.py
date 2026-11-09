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

Pipecat's service APIs evolve between minor versions. This file is
written against the version pinned in ``pyproject.toml`` (1.x: the
``pipecat.services.*_service`` base classes, the universal
``LLMContext``). If you bump Pipecat and see import errors at boot,
check the new base classes and adjust the imports below — the body of
each ``run_*`` method should still be portable since it's just an
``async def`` calling our adapter clients.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Any, AsyncGenerator

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
        LLMUpdateSettingsFrame,
        TextFrame,
        TranscriptionFrame,
        TTSAudioRawFrame,
        TTSStartedFrame,
        TTSStoppedFrame,
        TTSTextFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
        InterruptionFrame,
        OutputTransportMessageFrame,
        OutputTransportMessageUrgentFrame,
    )
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.services.llm_service import LLMService
    from pipecat.services.stt_service import SegmentedSTTService, STTService
    from pipecat.services.tts_service import TTSService
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.frames.frames import LLMContextFrame
    from pipecat.utils.time import time_now_iso8601
except Exception:  # pragma: no cover
    # Tests don't import this module directly; they stub Pipecat in
    # sys.modules before importing camera_agent. Falling back to bare
    # ``object`` lets ``ast.parse`` succeed for the static checks too.
    Frame = object  # type: ignore
    LLMFullResponseEndFrame = object  # type: ignore
    LLMFullResponseStartFrame = object  # type: ignore
    LLMTextFrame = object  # type: ignore
    LLMUpdateSettingsFrame = object  # type: ignore
    TextFrame = object  # type: ignore
    TranscriptionFrame = object  # type: ignore
    TTSAudioRawFrame = object  # type: ignore
    TTSStartedFrame = object  # type: ignore
    TTSStoppedFrame = object  # type: ignore
    TTSTextFrame = object  # type: ignore
    UserStartedSpeakingFrame = object  # type: ignore
    UserStoppedSpeakingFrame = object  # type: ignore
    InterruptionFrame = object  # type: ignore
    OutputTransportMessageFrame = object  # type: ignore
    OutputTransportMessageUrgentFrame = object  # type: ignore
    FrameProcessor = object  # type: ignore
    LLMService = object  # type: ignore
    STTService = object  # type: ignore
    SegmentedSTTService = object  # type: ignore
    TTSService = object  # type: ignore
    LLMContext = object  # type: ignore
    LLMContextFrame = object  # type: ignore

    def time_now_iso8601() -> str:  # type: ignore
        import datetime as _dt
        return _dt.datetime.now(_dt.timezone.utc).isoformat()


from adapter_clients import OllamaClient, PiperClient, WhisperClient

logger = logging.getLogger(__name__)


# ── STT: Whisper via OpenNVR adapter ───────────────────────────────


class OpenNvrWhisperSTT(SegmentedSTTService):
    """Bridges Pipecat's STTService contract to the Whisper adapter.

    Uses ``SegmentedSTTService`` (not the streaming ``STTService``) so
    Pipecat VAD-buffers a whole utterance and hands ``run_stt`` one
    WAV-wrapped ``bytes`` blob per utterance. The streaming base would
    instead fire ``run_stt`` on every ~0.25s audio chunk, feeding
    Whisper fragments and yielding empty/garbled transcripts. Our
    adapter is non-streaming, so one ``TranscriptionFrame`` per
    utterance is exactly the right shape.

    Where the utterance ends is no longer this service's problem: the
    user aggregator's turn strategies (Silero VAD to start, Smart Turn
    v3 to stop — see ``build_pipeline_task``) decide, and the
    ``max_duration_secs`` of the turn analyzer bounds a runaway
    utterance. The pre-1.x force-stop timers that lived here are gone.

    Since pipecat 1.10 the base is queue-based: ``run_stt`` runs in a
    background task while audio and other frames keep flowing through
    the pipeline, so a two-second Whisper call on CPU no longer stalls
    the turn machinery behind it; and each segment is padded with
    ``trailing_silence_secs`` of silence so the model hears the end of
    speech (``stt_trailing_silence_secs`` in config).
    """

    def __init__(
        self,
        *,
        client: WhisperClient,
        sample_rate: int = 16000,
        trailing_silence_secs: float = 0.5,
    ) -> None:
        super().__init__(sample_rate=sample_rate, trailing_silence_secs=trailing_silence_secs)
        self._client = client

    # Whisper hallucinates short tokens when fed near-silence or very short
    # noise bursts. Drop these so they don't loop the LLM indefinitely.
    # Keep this list SMALL — only tokens Whisper emits on silence/noise,
    # NOT legitimate one-word commands ('yes', 'no', 'ok', 'bye', etc.).
    _HALLUCINATION_TOKENS: frozenset[str] = frozenset({
        "you", "you.", "you!", "you?",
        "thank you", "thank you.", "thank you!",
        "thanks", "thanks.",
        ".", "..", "...",
        "uh", "um", "hmm", "hm",
    })

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        if not audio:
            logger.info("STT run_stt: received 0 bytes (no audio captured)")
            return
        # Auto-gain: browser/OS mic levels are wildly inconsistent, and a
        # faint capture makes Whisper return an empty transcript. Measure the
        # peak and, if the signal is quiet, amplify the PCM toward a target
        # level before sending.
        #
        # Noise-gate: drop truly silent bursts (peak < 500 = below background
        # hiss level) before paying the Whisper round-trip cost. Keep this
        # threshold LOW — real speech with a quiet mic can peak as low as
        # 800–1500. The hallucination filter below handles the case where
        # Whisper still fires on marginal audio.
        # ``audio`` is a WAV blob, so we skip the 44-byte RIFF header when
        # measuring/scaling the samples.
        try:
            import audioop
            head, pcm = (audio[:44], audio[44:]) if audio[:4] == b"RIFF" else (b"", audio)
            peak = audioop.max(pcm, 2)
            # Target 28000 (≈86% of int16 max) — loud enough for Whisper's
            # internal VAD to detect speech. Cap at 40x so a genuinely silent
            # burst (pure hiss, peak ~100) isn't amplified to clipping.
            target = 28000
            if peak < 500:
                logger.info(
                    "STT run_stt: %d bytes, peak_amp=%d -> noise gate drop (below 500)",
                    len(audio), peak,
                )
                return
            if peak < target:
                gain = min(target / peak, 40.0)
                pcm = audioop.mul(pcm, 2, gain)
                audio = head + pcm
                logger.info(
                    "STT run_stt: %d bytes, peak_amp=%d -> amplified %.1fx (peak_after=%d)",
                    len(audio), peak, gain, min(int(peak * gain), 32767),
                )
            else:
                logger.info(
                    "STT run_stt: %d bytes, peak_amp=%d (no boost)",
                    len(audio), peak,
                )
        except Exception:
            logger.exception("STT auto-gain failed; sending original audio")
        try:
            text = await self._client.transcribe(audio)
        except Exception:
            logger.exception("Whisper adapter call failed; emitting empty transcript")
            return
        text = (text or "").strip()
        logger.info("STT transcript: %r", text)
        if not text:
            return
        # Hallucination filter: Whisper emits known noise tokens ('You',
        # 'Thank you', etc.) when fed short or near-silent audio bursts.
        # Drop these so they don't trigger the LLM tool-calling loop.
        if text.lower() in self._HALLUCINATION_TOKENS:
            logger.info("STT transcript: %r -> hallucination filter drop", text)
            return
        yield TranscriptionFrame(text, "", time_now_iso8601())


# ── LLM: Ollama via OpenNVR adapter, with tool calling ─────────────


def history_and_question(messages: list[dict[str, Any]]) -> tuple[list[dict[str, str]], str]:
    """The agent's turn takes (history, the question): prior user and
    assistant TEXT turns, and the last user message. System and tool
    messages are the pipeline's business, not the turn's."""
    turns = [m for m in messages
             if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
    if not turns or turns[-1].get("role") != "user":
        return [{"role": m["role"], "content": m["content"]} for m in turns], ""
    history = [{"role": m["role"], "content": m["content"]} for m in turns[:-1]]
    return history, str(turns[-1]["content"]).strip()


class StreamEvents(FrameProcessor):
    """What the page needs to know besides the audio, as JSON messages on
    the same WebSocket: the transcript of what was heard, each sentence as
    it is spoken, when the user started and stopped, when the agent
    started and stopped, and an interruption (so the page drops the audio
    it has queued). Sits just before the transport's output; every frame
    passes through untouched."""

    _TEXT_TYPES: tuple = ()

    async def process_frame(self, frame: "Frame", direction: Any) -> None:  # type: ignore[override]
        await super().process_frame(frame, direction)
        message = None
        urgent = False
        if isinstance(frame, TranscriptionFrame):
            message = {"type": "transcript", "text": getattr(frame, "text", "")}
        elif isinstance(frame, TTSTextFrame):
            message = {"type": "reply", "text": getattr(frame, "text", "")}
        elif isinstance(frame, UserStartedSpeakingFrame):
            message = {"type": "user_started"}
        elif isinstance(frame, UserStoppedSpeakingFrame):
            message = {"type": "user_stopped"}
        elif isinstance(frame, TTSStartedFrame):
            message = {"type": "bot_started"}
        elif isinstance(frame, TTSStoppedFrame):
            message = {"type": "bot_stopped"}
        elif isinstance(frame, InterruptionFrame):
            message, urgent = {"type": "interrupted"}, True
        if message is not None:
            out = (OutputTransportMessageUrgentFrame if urgent else OutputTransportMessageFrame)(message=message)
            await self.push_frame(out, direction)
        await self.push_frame(frame, direction)


def with_clock_in_user_turn(messages: list[dict[str, Any]],
                            clock_line: Callable[[], str] | None) -> list[dict[str, Any]]:
    """The per-turn clock, in front of the LAST user message — the one
    message rendered after the system+tools prefix Ollama caches. A clock
    in the system prompt changed every minute and re-prefilled ~4k tokens
    on every voice turn. The context's own list is left alone; history
    keeps the bare words."""
    if not clock_line or not messages or messages[-1].get("role") != "user":
        return messages
    last = dict(messages[-1])
    last["content"] = f"[{clock_line()}]\n\n{last.get('content') or ''}"
    return messages[:-1] + [last]


class OpenNvrOllamaLLM(LLMService):
    """Bridges Pipecat's LLM contract to the Ollama adapter's
    OpenAI-style chat_completion task.

    Handles the full tool-calling loop inline:

      1. Pipecat hands us an ``LLMContextFrame`` carrying the
         universal ``LLMContext`` (OpenAI-style message dicts).
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
        clock_line: Callable[[], str] | None = None,
        turn_runner: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__()
        self._client = client
        self._clock_line = clock_line
        # The agent's own turn (router, forced grounding, the window fit,
        # the backup model, the reply guards), ``async (history, text) ->
        # reply``. With it the streaming path answers exactly as the
        # request path does; without it (tests with bare fakes) the inline
        # tool loop below runs.
        self._turn_runner = turn_runner
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
        if isinstance(frame, LLMContextFrame):
            await self._handle_context(frame.context)
        elif isinstance(frame, LLMUpdateSettingsFrame):
            pass  # the base consumed it (ours) or already forwarded it (another service's)
        else:
            await self.push_frame(frame, direction)

    async def _handle_context(self, context: LLMContext) -> None:
        # Snapshot the conversation messages; Pipecat's context
        # aggregator owns the canonical list.
        raw_messages = list(context.get_messages())
        messages = with_clock_in_user_turn(raw_messages, self._clock_line)
        logger.info("LLM _handle_context fired with %d messages", len(messages))

        # Emit the "LLM is thinking" bracket so downstream TTS knows
        # when to start / stop assembling its audio chunks.
        await self.push_frame(LLMFullResponseStartFrame())

        try:
            if self._turn_runner is not None:
                history, text = history_and_question(raw_messages)
                if text:
                    # The page never sees the TranscriptionFrame: the user
                    # aggregator upstream consumes it. So the transcript goes
                    # out from here, before the answer, and a turn_end after
                    # it — without them the page showed no user text and
                    # appended every answer to one bubble (2026-11-09).
                    await self.push_frame(OutputTransportMessageFrame(message={"type": "transcript", "text": text}))
                    out = await self._turn_runner(history, text)
                    reply, meta = (out if isinstance(out, tuple) else (out, {}))
                    if reply:
                        await self.push_frame(LLMTextFrame(str(reply)))
                    await self.push_frame(OutputTransportMessageFrame(message={"type": "turn_end"}))
                    if (meta or {}).get("end"):
                        # After the goodbye: the page leaves talk mode once
                        # the reply's audio has played.
                        await self.push_frame(OutputTransportMessageFrame(message={"type": "session_end"}))
                return
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
                    **({} if not tool_calls else {"tool_calls": tool_calls}),
                })

                logger.info(
                    "LLM iter %d: content=%r tool_calls=%d",
                    iteration, content[:120], len(tool_calls),
                )

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

    async def run_tts(self, text: str, context_id: str | None = None) -> AsyncGenerator[Frame, None]:
        text = (text or "").strip()
        if not text:
            return
        logger.info("TTS synthesizing: %r", text[:120])
        yield TTSStartedFrame(context_id=context_id)
        try:
            audio_bytes = await self._client.synthesize(text)
        except Exception:
            logger.exception("Piper adapter synthesise failed")
            yield TTSStoppedFrame(context_id=context_id)
            return
        logger.info("TTS got %d audio bytes from Piper", len(audio_bytes or b""))
        if not audio_bytes:
            yield TTSStoppedFrame(context_id=context_id)
            return
        # WAV from Piper carries a 44-byte RIFF header; strip it so
        # we ship raw PCM frames the transport layer can chunk
        # without re-parsing headers per chunk. Defensive: only strip
        # when the header actually matches.
        pcm = _strip_wav_header(audio_bytes)
        yield TTSAudioRawFrame(
            audio=pcm,
            sample_rate=self.sample_rate,
            num_channels=1,
            context_id=context_id,
        )
        yield TTSStoppedFrame(context_id=context_id)


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
