# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Streaming-voice glue: WAV header stripping and the raw-PCM serializer."""
import io
import wave

from serializer import RawPcmSerializer
from voice import PIPECAT_AVAILABLE, strip_wav_header


def make_wav(pcm: bytes, rate: int = 22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def test_pipecat_installed():
    assert PIPECAT_AVAILABLE


def test_strip_wav_header_roundtrip():
    pcm = b"\x01\x02" * 500
    assert strip_wav_header(make_wav(pcm)) == pcm


def test_strip_wav_header_passthrough_non_wav():
    raw = b"\x00\x01" * 100
    assert strip_wav_header(raw) == raw


async def test_serializer_outbound_is_audio_only():
    from pipecat.frames.frames import TextFrame, TTSAudioRawFrame

    s = RawPcmSerializer()
    audio = TTSAudioRawFrame(audio=b"\x01\x02\x03\x04", sample_rate=22050, num_channels=1)
    assert await s.serialize(audio) == b"\x01\x02\x03\x04"
    # Non-audio frames are dropped from the wire (raw-PCM protocol).
    assert await s.serialize(TextFrame("hello")) is None


async def test_serializer_inbound_pcm():
    from pipecat.frames.frames import InputAudioRawFrame

    s = RawPcmSerializer()
    frame = await s.deserialize(b"\x01\x02\x03\x04")
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.audio == b"\x01\x02\x03\x04"
    assert frame.sample_rate == 16000
    # Odd-length payloads can't be whole int16 samples -> dropped.
    assert await s.deserialize(b"\x01\x02\x03") is None
