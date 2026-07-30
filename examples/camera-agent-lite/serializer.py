# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Minimal raw-PCM WebSocket serializer for Pipecat (ported from the
camera-agent example — same wire protocol, so any client that speaks to
camera-agent's /ws also works here).

  * **Inbound** (browser → server): bare PCM int16 little-endian samples at
    16 kHz mono, captured with the Web Audio API and shipped as raw
    ``Int16Array`` chunks over the WebSocket.

  * **Outbound** (server → browser): bare PCM int16 little-endian samples at
    the TTS sample rate (22050 for the Piper amy voice), played back by
    feeding samples into an ``AudioBufferSourceNode``.

Frames that aren't audio (transcripts, system frames, control frames) are
dropped on the wire — the demo page doesn't render them. A production UI
would upgrade to ``ProtobufFrameSerializer`` + ``@pipecat-ai/client-js``.
"""
from __future__ import annotations

import logging
from typing import Optional

# Wrapped in try so the module remains importable in test environments
# that don't have Pipecat installed.
try:  # pragma: no cover — import-time only
    from pipecat.frames.frames import (
        Frame,
        InputAudioRawFrame,
        OutputAudioRawFrame,
        StartFrame,
        TTSAudioRawFrame,
    )
    from pipecat.serializers.base_serializer import (
        FrameSerializer,
        FrameSerializerType,
    )
except Exception:  # pragma: no cover
    Frame = object  # type: ignore
    InputAudioRawFrame = object  # type: ignore
    OutputAudioRawFrame = object  # type: ignore
    StartFrame = object  # type: ignore
    TTSAudioRawFrame = object  # type: ignore

    class FrameSerializer:  # type: ignore
        pass

    class FrameSerializerType:  # type: ignore
        BINARY = "binary"
        TEXT = "text"


logger = logging.getLogger(__name__)


class RawPcmSerializer(FrameSerializer):
    """Inbound: raw int16 PCM bytes → ``InputAudioRawFrame``.
    Outbound: ``TTSAudioRawFrame`` → raw int16 PCM bytes.
    Everything else is dropped (returns None).
    """

    def __init__(
        self,
        *,
        input_sample_rate: int = 16000,
        output_sample_rate: int = 22050,
        num_channels: int = 1,
    ) -> None:
        self._input_sample_rate = input_sample_rate
        self._output_sample_rate = output_sample_rate
        self._num_channels = num_channels

    @property
    def type(self):  # type: ignore[override]
        return FrameSerializerType.BINARY

    async def setup(self, frame: "StartFrame") -> None:  # type: ignore[override]
        # Honour the StartFrame's negotiated sample rates if Pipecat set
        # them. Field names pinned to pipecat-ai 0.0.5x — a future rename
        # falls back to the constructor defaults and audio still flows.
        rate_in = getattr(frame, "audio_in_sample_rate", None)
        rate_out = getattr(frame, "audio_out_sample_rate", None)
        if isinstance(rate_in, int) and rate_in > 0:
            self._input_sample_rate = rate_in
        if isinstance(rate_out, int) and rate_out > 0:
            self._output_sample_rate = rate_out

    async def serialize(self, frame: "Frame") -> Optional[bytes]:  # type: ignore[override]
        # Only audio gets serialised onto the wire.
        if isinstance(frame, (TTSAudioRawFrame, OutputAudioRawFrame)):
            audio = getattr(frame, "audio", None)
            if not isinstance(audio, (bytes, bytearray)) or not audio:
                return None
            return bytes(audio)
        return None

    async def deserialize(self, data) -> Optional["Frame"]:  # type: ignore[override]
        if not isinstance(data, (bytes, bytearray)) or not data:
            return None
        # int16 PCM is 2 bytes/sample — an odd-length payload can't be a
        # whole number of samples; drop rather than feed Silero VAD noise.
        if len(data) % 2 != 0:
            return None
        return InputAudioRawFrame(
            audio=bytes(data),
            sample_rate=self._input_sample_rate,
            num_channels=self._num_channels,
        )
