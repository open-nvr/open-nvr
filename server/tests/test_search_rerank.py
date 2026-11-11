"""The bounded LLM re-rank: the prompt, the answer, the partition, and the
one call — against a fake chat, never the model."""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_rerank_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

import services.search_rerank as rr  # noqa: E402


def test_the_prompt_numbers_the_captions_and_bounds_their_length():
    p = rr.build_prompt("two men looking at a laptop", ["a man", "x" * 500, ""])
    assert p.startswith("Search: two men looking at a laptop")
    assert "1. a man" in p and "3. (no caption)" in p
    assert len(next(ln for ln in p.splitlines() if ln.startswith("2. "))) <= 3 + rr.MAX_CAPTION_CHARS


@pytest.mark.parametrize("text, n, expect", [
    ("2, 4", 5, [2, 4]),
    ("4 and 2 and 2", 5, [4, 2]),
    ("none", 5, []),
    ("None.", 5, []),
    ("Captions 1, 3 and 7 match", 5, [1, 3]),           # 7 is out of range
    ("", 5, None),
    ("I cannot tell", 5, None),
])
def test_the_answer_is_read_strictly(text, n, expect):
    assert rr.parse_answer(text, n) == expect


def test_apply_moves_the_yes_rows_to_the_front_in_their_fused_order():
    assert rr.apply(["a", "b", "c", "d"], [3, 1]) == ["a", "c", "b", "d"]
    assert rr.apply(["a", "b"], []) == ["a", "b"]
    assert rr.apply([], []) == []


def _fake_chat(answer: str, calls: list):
    async def _chat(url, payload, *, token, timeout_s):
        calls.append((url, payload))
        return {"choices": [{"message": {"role": "assistant", "content": answer}}]}
    return _chat


def test_rerank_asks_once_with_the_system_prompt_and_reports_what_it_kept(monkeypatch):
    calls: list = []
    monkeypatch.setattr(rr, "_chat", _fake_chat("2, 3", calls))
    yes, info = asyncio.run(rr.rerank("a bus", ["a car", "a bus", "a coach", "a bike"],
                                      base="http://caption-adapter:9006"))
    assert yes == [2, 3] and info["asked"] == 4 and info["kept"] == 2 and info["ms"] is not None
    assert info["answer"] == "2, 3"
    url, payload = calls[0]
    assert url == "http://caption-adapter:9006/v1/chat/completions"
    assert payload["messages"][0]["role"] == "system" and payload["max_tokens"] == 32
    assert "2. a bus" in payload["messages"][1]["content"]


def test_rerank_never_fails_a_search(monkeypatch):
    async def _boom(url, payload, *, token, timeout_s):
        raise RuntimeError("adapter away")
    monkeypatch.setattr(rr, "_chat", _boom)
    yes, info = asyncio.run(rr.rerank("q", ["a"], base="http://x"))
    assert yes is None and info["reason"] == "error"

    async def _slow(url, payload, *, token, timeout_s):
        await asyncio.sleep(0.3)
        return {}
    monkeypatch.setattr(rr, "_chat", _slow)
    yes, info = asyncio.run(rr.rerank("q", ["a"], base="http://x", timeout_s=0.05))
    assert yes is None and info["reason"] == "timeout"

    monkeypatch.setattr(rr, "_chat", _fake_chat("I cannot say", []))
    yes, info = asyncio.run(rr.rerank("q", ["a"], base="http://x"))
    assert yes is None and info["reason"] == "unreadable"

    async def _none():
        return None
    monkeypatch.setattr(rr, "resolve_chat_base", _none)
    yes, info = asyncio.run(rr.rerank("q", ["a"]))
    assert yes is None and info["reason"] == "no-model"
    assert asyncio.run(rr.rerank("", ["a"], base="http://x"))[1]["reason"] == "nothing-to-rank"


def test_the_chat_base_is_a_chat_adapter_else_the_captioner_once_probed(monkeypatch):
    import routers.skills as skills
    caps = {
        "litertlm": {"url": "http://caption-adapter:9006", "capabilities": {"tasks_advertised": ["scene_caption"]}},
        "moondream": {"url": "http://moondream:9004", "capabilities": {"tasks_advertised": ["scene_caption"]}},
    }

    async def _view():
        return None, caps
    monkeypatch.setattr(skills, "_kai_c_view", _view)
    probed: list[str] = []

    async def _probe(base, token):
        probed.append(base)
        return base.endswith("9006")
    monkeypatch.setattr(rr, "_probe", _probe)
    assert asyncio.run(rr.resolve_chat_base()) == "http://caption-adapter:9006"
    assert probed == ["http://caption-adapter:9006"], "sorted, the first that answers wins"
    caps["moondream"]["capabilities"]["tasks_advertised"] = ["chat"]
    probed.clear()
    assert asyncio.run(rr.resolve_chat_base()) == "http://caption-adapter:9006"
    assert probed[0] == "http://moondream:9004", "a chat adapter is tried before a captioner"
