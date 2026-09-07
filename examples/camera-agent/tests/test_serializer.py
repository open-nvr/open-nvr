# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RawPcmSerializer.

These tests exercise the wire shape directly — they don't boot
Pipecat or a WebSocket. The goal is to pin the contract: int16 PCM
in both directions, audio-only, non-audio frames dropped.
"""
from __future__ import annotations

import importlib

import pytest


def _pipecat_available() -> bool:
    try:
        importlib.import_module("pipecat.frames.frames")
        return True
    except Exception:
        return False


# The serializer module is only meaningfully testable when Pipecat
# is on the venv (the test env that runs `uv sync --extra dev`
# installs it via the camera-agent pyproject). Skip in environments
# where it isn't — the module-level fallback keeps imports working.
pytestmark = pytest.mark.skipif(
    not _pipecat_available(),
    reason="Pipecat not installed in this venv",
)


@pytest.mark.asyncio
async def test_deserialize_bytes_to_input_audio_frame():
    from pipecat.frames.frames import InputAudioRawFrame
    from serializer import RawPcmSerializer

    s = RawPcmSerializer(input_sample_rate=16000, num_channels=1)
    pcm = b"\x00\x01\x02\x03"  # 2 samples × 2 bytes
    frame = await s.deserialize(pcm)
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.audio == pcm
    assert frame.sample_rate == 16000
    assert frame.num_channels == 1


@pytest.mark.asyncio
async def test_deserialize_empty_bytes_returns_none():
    from serializer import RawPcmSerializer

    s = RawPcmSerializer()
    assert await s.deserialize(b"") is None
    # Non-bytes (e.g. str from a misconfigured client) also drops
    # rather than crashing.
    assert await s.deserialize("not bytes") is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_deserialize_odd_length_drops():
    """int16 PCM = 2 bytes/sample. Odd-length payloads can't be a
    whole number of samples and would feed garbage to Silero VAD.
    Drop rather than emit."""
    from serializer import RawPcmSerializer

    s = RawPcmSerializer()
    # 3 bytes = 1.5 samples → drop.
    assert await s.deserialize(b"\x00\x01\x02") is None
    # 4 bytes = 2 samples → accepted.
    out = await s.deserialize(b"\x00\x01\x02\x03")
    assert out is not None


@pytest.mark.asyncio
async def test_serialize_tts_audio_frame_emits_bytes():
    from pipecat.frames.frames import TTSAudioRawFrame
    from serializer import RawPcmSerializer

    s = RawPcmSerializer()
    pcm = b"\x10\x11\x12\x13\x14\x15"
    frame = TTSAudioRawFrame(audio=pcm, sample_rate=22050, num_channels=1)
    out = await s.serialize(frame)
    assert out == pcm


@pytest.mark.asyncio
async def test_serialize_non_audio_frame_drops_to_none():
    """LLM text, system frames, control frames must not bleed onto
    the demo wire — the browser doesn't parse them. A production UI
    would extend this with a sidecar JSON channel."""
    from pipecat.frames.frames import LLMTextFrame
    from serializer import RawPcmSerializer

    s = RawPcmSerializer()
    out = await s.serialize(LLMTextFrame("hello"))
    assert out is None


@pytest.mark.asyncio
async def test_setup_honours_start_frame_sample_rates():
    """When Pipecat's StartFrame carries sample-rate negotiation,
    the serializer picks those up so it stays in sync with whatever
    the transport ended up plumbing."""
    from serializer import RawPcmSerializer

    s = RawPcmSerializer(input_sample_rate=16000, output_sample_rate=22050)

    # Construct a stand-in for StartFrame instead of the real class.
    # The actual Pipecat ``StartFrame`` constructor has churned hard
    # across releases — 0.0.5x makes ``clock`` and ``task_manager``
    # required positional args (no way to fabricate one in a unit
    # test without dragging in the whole runtime), while 0.0.10x
    # makes everything optional but also rearranges other fields.
    # ``serializer.setup()`` only reads ``audio_in_sample_rate`` /
    # ``audio_out_sample_rate`` via ``getattr`` — pinning to a real
    # StartFrame just to satisfy a duck-typed attribute lookup is
    # the kind of test fragility that makes Pipecat bumps painful.
    # A bare object with the two attributes exercises the same code
    # path and survives any upstream rearrangement.
    class _FakeStartFrame:
        audio_in_sample_rate = 8000
        audio_out_sample_rate = 24000

    await s.setup(_FakeStartFrame())  # type: ignore[arg-type]
    assert s._input_sample_rate == 8000
    assert s._output_sample_rate == 24000


@pytest.mark.asyncio
async def test_serializer_is_a_pipecat_frame_serializer():
    """1.x: FrameSerializer is a BaseObject with a task manager and
    events; the serializer must construct through it (no ``type``
    property any more — binary is implied by returning bytes)."""
    from pipecat.serializers.base_serializer import FrameSerializer
    from serializer import RawPcmSerializer

    s = RawPcmSerializer()
    assert isinstance(s, FrameSerializer)
    assert s.name.startswith("RawPcmSerializer")
