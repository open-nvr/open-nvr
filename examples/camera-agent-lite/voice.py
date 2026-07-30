# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Streaming voice: thin Pipecat services over the lite adapter clients.

Ported from the camera-agent example's ``services.py`` (same architecture,
same hardening) with the backends swapped: whispercpp / llamacpp / pipertts
adapters instead of Whisper / Ollama / Piper, and the lite deterministic
router added as a fast-path in front of the LLM.

Pipecat owns frame pumping, Silero VAD, turn-taking, and pipeline
coordination; ``adapter_clients.py`` owns the wire format; these classes are
just the glue. Targets ``pipecat-ai >=0.0.55,<0.0.60`` — the same pin as
camera-agent, for the same serializer-API reasons.

Interruptions are DISABLED (camera-agent precedent): the raw-PCM demo client
can't send proper cancel frames, so barge-in would let echo/noise cancel
in-flight replies. The agent always finishes its answer, then listens again.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator

# Wrapped in ``try`` to keep the module importable in test environments that
# don't have Pipecat installed (tests exercise only the pure helpers).
try:  # pragma: no cover — import-time only
    from pipecat.frames.frames import (
        Frame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
        TranscriptionFrame,
        TTSAudioRawFrame,
        TTSStartedFrame,
        TTSStoppedFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.services.ai_services import (
        LLMService,
        SegmentedSTTService,
        TTSService,
    )
    from pipecat.processors.aggregators.openai_llm_context import (
        OpenAILLMContext,
        OpenAILLMContextFrame,
    )
    from pipecat.utils.time import time_now_iso8601

    PIPECAT_AVAILABLE = True
except Exception:  # pragma: no cover
    PIPECAT_AVAILABLE = False
    Frame = object  # type: ignore
    LLMFullResponseEndFrame = object  # type: ignore
    LLMFullResponseStartFrame = object  # type: ignore
    LLMTextFrame = object  # type: ignore
    TranscriptionFrame = object  # type: ignore
    TTSAudioRawFrame = object  # type: ignore
    TTSStartedFrame = object  # type: ignore
    TTSStoppedFrame = object  # type: ignore
    UserStoppedSpeakingFrame = object  # type: ignore
    LLMService = object  # type: ignore
    SegmentedSTTService = object  # type: ignore
    TTSService = object  # type: ignore
    OpenAILLMContext = object  # type: ignore
    OpenAILLMContextFrame = object  # type: ignore

    def time_now_iso8601() -> str:  # type: ignore
        import datetime as _dt
        return _dt.datetime.now(_dt.timezone.utc).isoformat()


logger = logging.getLogger(__name__)


# ── STT: whisper.cpp adapter ───────────────────────────────────────


class LiteWhisperSTT(SegmentedSTTService):
    """VAD-buffered utterances → whispercpp adapter → one
    ``TranscriptionFrame`` per utterance. Carries camera-agent's field-tested
    hardening: force-stop on long silence/speech, auto-gain for quiet mics,
    a noise gate, and a hallucination filter."""

    def __init__(self, *, client, sample_rate: int = 16000) -> None:
        super().__init__(sample_rate=sample_rate)
        self._client = client
        self._trailing_silence_secs = 0.0
        self._speaking_secs = 0.0
        # RMS below which audio counts as trailing silence for force-stop:
        # well below normal speech (rms ~300-2000) but above true silence.
        self._silence_rms_threshold = 200
        self._force_stop_after_silence_secs = 2.0
        self._force_stop_after_speech_secs = 14.0

    async def _handle_user_started_speaking(self, frame):  # type: ignore[override]
        self._trailing_silence_secs = 0.0
        self._speaking_secs = 0.0
        await super()._handle_user_started_speaking(frame)

    async def _handle_user_stopped_speaking(self, frame):  # type: ignore[override]
        self._trailing_silence_secs = 0.0
        self._speaking_secs = 0.0
        await super()._handle_user_stopped_speaking(frame)

    async def process_audio_frame(self, frame, direction):  # type: ignore[override]
        await super().process_audio_frame(frame, direction)

        if not getattr(self, "_user_speaking", False):
            self._trailing_silence_secs = 0.0
            self._speaking_secs = 0.0
            return

        audio = getattr(frame, "audio", b"") or b""
        sample_rate = int(getattr(frame, "sample_rate", self.sample_rate) or self.sample_rate)
        channels = int(getattr(frame, "num_channels", 1) or 1)
        if not audio or sample_rate <= 0:
            return

        seconds = len(audio) / float(sample_rate * channels * 2)
        self._speaking_secs += seconds
        try:
            import audioop

            rms = audioop.rms(audio, 2)
        except Exception:
            rms = self._silence_rms_threshold + 1

        if rms < self._silence_rms_threshold:
            self._trailing_silence_secs += seconds
        else:
            self._trailing_silence_secs = 0.0

        if (
            self._trailing_silence_secs >= self._force_stop_after_silence_secs
            or self._speaking_secs >= self._force_stop_after_speech_secs
        ):
            logger.info(
                "STT forcing utterance end: trailing_silence=%.2fs speech=%.2fs rms=%d",
                self._trailing_silence_secs, self._speaking_secs, rms,
            )
            stop_frame = UserStoppedSpeakingFrame()
            await self._handle_user_stopped_speaking(stop_frame)
            await self.push_frame(stop_frame, direction)

    # Whisper hallucinates short tokens on near-silence. Keep this list
    # SMALL — only noise tokens, not legitimate one-word commands.
    _HALLUCINATION_TOKENS: frozenset[str] = frozenset({
        "you", "you.", "you!", "you?",
        "thank you", "thank you.", "thank you!",
        "thanks", "thanks.",
        ".", "..", "...",
        "uh", "um", "hmm", "hm",
    })

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        if not audio:
            return
        # Auto-gain + noise gate (camera-agent's fixes for wildly inconsistent
        # browser mic levels). ``audio`` is a WAV blob — skip the RIFF header
        # when measuring/scaling samples.
        try:
            import audioop
            head, pcm = (audio[:44], audio[44:]) if audio[:4] == b"RIFF" else (b"", audio)
            peak = audioop.max(pcm, 2)
            target = 28000
            if peak < 500:
                logger.info("STT: %d bytes, peak=%d -> noise gate drop", len(audio), peak)
                return
            if peak < target:
                gain = min(target / peak, 40.0)
                audio = head + audioop.mul(pcm, 2, gain)
        except Exception:
            logger.exception("STT auto-gain failed; sending original audio")
        try:
            result = await self._client.transcribe_wav(audio)
            text = (result.text or "").strip()
        except Exception:
            logger.exception("whispercpp adapter call failed; dropping utterance")
            return
        logger.info("STT transcript: %r", text)
        if not text or text.lower() in self._HALLUCINATION_TOKENS:
            return
        yield TranscriptionFrame(text, "", time_now_iso8601())


# ── LLM: llamacpp adapter, with routing fast-path + tool calling ───


class LiteLLM(LLMService):
    """Pipecat LLM contract → the lite brain's machinery.

    Per user turn:

      1. The deterministic router gets first look — a clearly-visual
         question skips the LLM and goes straight to ``look_at_camera``
         (the lite latency fast-path, same behaviour as the /ask route).
      2. Otherwise the llamacpp adapter runs the bounded tool-calling loop
         over the same fixed registry, with results fed back as
         ``role: tool`` messages.
      3. Final text streams out as ``LLMTextFrame`` for sentence-level TTS.
    """

    def __init__(
        self,
        *,
        client,                 # adapter_clients.LlmClient
        executor,               # tools.ToolExecutor
        router,                 # routing.IntentRouter
        tools: list[dict[str, Any]],
        temperature: float = 0.3,
        max_tokens: int = 200,
        max_tool_iterations: int = 4,
    ) -> None:
        super().__init__()
        self._client = client
        self._executor = executor
        self._router = router
        self._tools = list(tools)
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_tool_iterations = max_tool_iterations

    async def process_frame(self, frame: Frame, direction: Any) -> None:  # type: ignore[override]
        # Act on the context frame; forward everything else untouched so the
        # rest of the pipeline keeps receiving the frames it needs (without
        # the explicit push_frame the pipeline locks up — camera-agent
        # learned this the hard way).
        await super().process_frame(frame, direction)
        if isinstance(frame, OpenAILLMContextFrame):
            await self._handle_context(frame.context)
        else:
            await self.push_frame(frame, direction)

    async def _handle_context(self, context: OpenAILLMContext) -> None:
        messages = list(context.get_messages())
        await self.push_frame(LLMFullResponseStartFrame())
        try:
            # 1) deterministic fast-path on the newest user utterance
            user_text = next(
                (m.get("content", "") for m in reversed(messages)
                 if m.get("role") == "user" and isinstance(m.get("content"), str)),
                "",
            )
            decision = await self._router.route(user_text)
            if decision.route == "vision" and decision.camera_id:
                logger.info("voice route=vision camera=%s (fast-path)", decision.camera_id)
                result = await self._executor.execute("look_at_camera", {
                    "camera_id": decision.camera_id,
                    "question": decision.question,
                    "temporal": decision.requires_multiple_frames,
                })
                data = result.to_model_json()
                answer = (data.get("answer") or data.get("error")
                          or "I couldn't analyse that view.")
                await self.push_frame(LLMTextFrame(answer))
                context.add_message({"role": "assistant", "content": answer})
                return

            # 2) LLM tool-calling loop
            from services import (
                META_NUDGE_MESSAGE,
                NUDGE_MESSAGE,
                mentions_tools,
                sounds_unfinished,
            )

            tool_names = [
                (t.get("function") or {}).get("name", "") for t in self._tools
            ]
            nudged = False
            meta_nudged = False
            for iteration in range(self._max_tool_iterations + 2):
                response = await self._client.chat(
                    messages=messages,
                    tools=self._tools,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                )
                message = (response.get("choices") or [{}])[0].get("message") or {}
                tool_calls = message.get("tool_calls") or []
                content = (message.get("content") or "").strip()

                messages.append({
                    "role": "assistant",
                    "content": content,
                    **({} if not tool_calls else {"tool_calls": tool_calls}),
                })
                logger.info("voice LLM iter %d: content=%r tool_calls=%d",
                            iteration, content[:120], len(tool_calls))

                if not tool_calls:
                    # "Please wait while I check…" — speak it (natural for
                    # voice), then nudge once so it actually checks.
                    if content and not nudged and sounds_unfinished(content):
                        logger.info("voice nudging: promised action without a tool call")
                        nudged = True
                        await self.push_frame(LLMTextFrame(content))
                        messages.append({"role": "user", "content": NUDGE_MESSAGE})
                        continue
                    # "Call look_at_camera with camera_id…" — never SPEAK
                    # tool-speak; retry silently, then degrade gracefully.
                    if content and mentions_tools(content, tool_names):
                        if not meta_nudged:
                            logger.info("voice nudging: narrated tools instead of acting")
                            meta_nudged = True
                            messages.append({"role": "user", "content": META_NUDGE_MESSAGE})
                            continue
                        await self.push_frame(LLMTextFrame(
                            "Which camera should I look at?"))
                        return
                    if content:
                        await self.push_frame(LLMTextFrame(content))
                    return

                # Speak any partial reply the model emitted alongside tool
                # calls ("Let me check…") so the user knows it's alive.
                if content and not mentions_tools(content, tool_names):
                    await self.push_frame(LLMTextFrame(content))

                for call in tool_calls:
                    name, result_text = await self._invoke_tool(call)
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "name": name,
                        "content": result_text,
                    }
                    messages.append(tool_msg)
                    context.add_message(tool_msg)

            logger.warning("voice LLM tool loop exhausted after %d iterations",
                           self._max_tool_iterations)
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
            return name or "<unknown>", f"ERROR: tool '{name}' received malformed arguments."
        # ToolExecutor already rejects unknown tools + invalid args safely.
        result = await self._executor.execute(name, args)
        text = json.dumps(result.to_model_json())
        if len(text) > 1200:
            text = text[:1200] + " …(truncated)"
        return name, text


# ── TTS: pipertts adapter ──────────────────────────────────────────


class LitePiperTTS(TTSService):
    """One ``TTSAudioRawFrame`` per LLM sentence; Pipecat slices it into
    transport-sized chunks downstream."""

    def __init__(self, *, client, sample_rate: int = 22050) -> None:
        super().__init__(sample_rate=sample_rate)
        self._client = client

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        text = (text or "").strip()
        if not text:
            return
        yield TTSStartedFrame()
        try:
            audio_b64 = await self._client.synthesize_b64(text)
        except Exception:
            logger.exception("pipertts adapter synthesise failed")
            yield TTSStoppedFrame()
            return
        if not audio_b64:
            yield TTSStoppedFrame()
            return
        import base64

        pcm = strip_wav_header(base64.b64decode(audio_b64))
        yield TTSAudioRawFrame(audio=pcm, sample_rate=self._sample_rate, num_channels=1)
        yield TTSStoppedFrame()


def strip_wav_header(audio: bytes) -> bytes:
    """If ``audio`` is a RIFF WAV, return just the PCM data chunk.
    Falls back to the input untouched for non-WAV or malformed data."""
    if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return audio
    pos = 12
    while pos + 8 <= len(audio):
        chunk_id = audio[pos:pos + 4]
        chunk_size = int.from_bytes(audio[pos + 4:pos + 8], "little")
        if chunk_id == b"data":
            start = pos + 8
            return audio[start:start + chunk_size]
        pos += 8 + chunk_size
    return audio


# ── Pipeline assembly ──────────────────────────────────────────────


def build_pipeline_task(brain, cfg, transport, *, stt_client, tts_client) -> Any:
    """Construct one Pipecat pipeline per WebSocket conversation.
    Imported lazily by the /ws endpoint so the app boots without Pipecat
    in stripped-down environments."""
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.aggregators.llm_response import (
        LLMAssistantContextAggregator,
        LLMUserContextAggregator,
    )

    from services import SYSTEM_PROMPT

    stt = LiteWhisperSTT(client=stt_client)
    llm = LiteLLM(
        client=brain.llm,
        executor=brain.executor,
        router=brain.router,
        tools=brain.registry.openai_tools(),
        temperature=cfg.llm_temperature,
        max_tokens=cfg.llm_max_tokens,
    )
    tts = LitePiperTTS(client=tts_client)

    context = OpenAILLMContext(messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
    ])
    user_agg = LLMUserContextAggregator(context=context)
    assistant_agg = LLMAssistantContextAggregator(context=context)

    pipeline = Pipeline([
        transport.input(),
        stt,
        user_agg,
        llm,
        tts,
        transport.output(),
        assistant_agg,
    ])

    return PipelineTask(
        pipeline,
        params=PipelineParams(
            # Interruptions DISABLED — see module docstring.
            allow_interruptions=False,
            enable_metrics=True,
        ),
    )
