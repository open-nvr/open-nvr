"""The image installs what the lock says — and the pipecat we build on.

`publish-images` went red on main the day pipecat 1.12 appeared: the
Dockerfile installed a RANGE, the lock (what the tests run against)
pinned 1.8.1, and the image's punkt download had no nltk to run in. And
even when it built, the hand-kept list shipped numpy 2 / opencv 5 /
fastapi 0.141 against a lock that said 1.26 / 4.11 / 0.115. The lock is
the one source; the Dockerfile exports it.
"""
from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def test_the_image_installs_from_the_lock_and_pyproject_agrees_with_it():
    """The Dockerfile carries no package list of its own: it exports
    uv.lock and installs that. pyproject.toml pins pipecat with == and
    the lock resolves to the same version."""
    docker = (HERE / "Dockerfile").read_text()
    assert "uv export --locked" in docker and "uv.lock" in docker, (
        "--locked: a lock that no longer matches pyproject.toml fails the build, "
        "instead of being baked into the image (--frozen would not check)")
    assert "--require-hashes" in docker, "the lock's hashes are enforced at install"
    assert re.search(r"pipecat-ai(\[[^\]]*\])?\s*[=<>~!]", docker) is None, (
        "no hand-kept pipecat pin or range in the Dockerfile: the lock is the one source")
    m = re.search(r'pipecat-ai\[silero,websocket\]==([0-9][0-9.]*)"', (HERE / "pyproject.toml").read_text())
    assert m, "pyproject.toml must pin pipecat with == (never a range)"
    lock = re.search(r'name = "pipecat-ai"\nversion = "([^"]+)"', (HERE / "uv.lock").read_text())
    assert lock and lock.group(1) == m.group(1), (m.group(1), lock and lock.group(1))


def test_the_image_needs_no_download_at_runtime():
    """No nltk / punkt step: since pipecat 1.11 the sentence splitter is
    self-contained, and the VAD and Smart Turn weights are in the wheel."""
    docker = (HERE / "Dockerfile").read_text()
    assert "nltk" not in docker.lower().replace("no nltk", "")
    lock = (HERE / "uv.lock").read_text()
    assert 'name = "nltk"' not in lock
    from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator  # noqa: F401
    import pipecat
    import importlib.resources as res
    data = res.files("pipecat") / "audio"
    assert (data / "vad" / "data" / "silero_vad.onnx").is_file()
    assert any(p.name.startswith("smart-turn-v3") for p in
               (data / "turn" / "smart_turn" / "data").iterdir())


def test_whisper_hears_the_end_of_the_utterance():
    """pipecat 1.10+ pads each segment before run_stt; the knob is ours."""
    from services import OpenNvrWhisperSTT

    class _Client:
        pass

    stt = OpenNvrWhisperSTT(client=_Client(), sample_rate=16000, trailing_silence_secs=0.25)
    stt._sample_rate = 16000                       # the pipeline's StartFrame sets it
    assert len(stt._trailing_silence()) == 16000 * 0.25 * 2, "16 kHz int16 mono"
    off = OpenNvrWhisperSTT(client=_Client(), sample_rate=16000, trailing_silence_secs=0)
    off._sample_rate = 16000
    assert off._trailing_silence() == b""
