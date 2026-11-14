"""Voice search: what speech does to text, undone; the transcribe route."""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_voice_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from services import search_voice  # noqa: E402
from services.search_voice import spoken_to_text, transcript_from  # noqa: E402


@pytest.mark.parametrize("spoken, typed", [
    ("show me the white car at cam twenty three", "the white car at cam 23"),
    ("find me a person in a blue shirt yesterday please", "a person in a blue shirt yesterday"),
    ("plate k a zero one a b one two three four", "plate KA01AB1234"),
    ("number plate kay a zero one a bee one two three four", "plate KA01AB1234"),
    ("car with plate m h double zero one two a b", "car with plate MH0012AB"),
    ("three people together", "three people together"),
    ("two men looking at a laptop", "two men looking at a laptop"),
    ("camera one hundred and five", "camera 105"),
    ("a truck on camera seven", "a truck on camera 7"),
    ("red car", "red car"),
    ("", ""),
])
def test_speech_is_read_back_as_the_typed_query(spoken, typed):
    assert spoken_to_text(spoken).text == typed


def test_the_changes_are_reported_so_nothing_is_silent():
    q = spoken_to_text("show me the white car at cam twenty three with plate k a zero one a b one two three four")
    assert q.text == "the white car at cam 23 with plate KA01AB1234"
    assert q.changes == ["dropped “show me”", "“twenty three” → 23",
                         "“k a zero one a b one two three four” → KA01AB1234"]
    assert spoken_to_text("red car").changes == []


def test_short_letter_runs_are_words_not_plates():
    # "a" is an article, "i" a pronoun; a run must be long and mixed.
    assert spoken_to_text("a man in a car").text == "a man in a car"
    assert spoken_to_text("i see a bee").text == "i see a bee"
    assert spoken_to_text("a b c d e").text == "a b c d e"          # letters only: not a plate


def test_transcript_from_reads_the_shapes_adapters_answer():
    assert transcript_from({"result": {"text": " hello "}}) == "hello"
    assert transcript_from({"result": {"transcript": "hi"}}) == "hi"
    assert transcript_from({"text": "flat"}) == "flat"
    assert transcript_from({"result": {"segments": [{"text": "a"}, {"text": "b"}]}}) == "a b"
    assert transcript_from({"result": {}}) == "" and transcript_from(None) == ""


@pytest.mark.asyncio
async def test_voice_status_says_whether_a_spoken_search_can_be_heard(monkeypatch):
    """The search page shows the microphone only when an adapter advertises
    speech to text; before this the page learnt it after the operator had
    spoken (503 no-stt)."""
    async def none():
        return None

    async def whisper():
        return "whisper"

    monkeypatch.setattr(search_voice, "resolve_stt_adapter", none)
    assert await search_voice.voice_status() == {"available": False, "adapter": None}
    monkeypatch.setattr(search_voice, "resolve_stt_adapter", whisper)
    assert await search_voice.voice_status() == {"available": True, "adapter": "whisper"}
