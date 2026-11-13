# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The descriptor questions as one call (RFC-0004).

The cost story this guards: a vehicle visit was three captioner calls
(the sentence, then colour, then type) and a person visit up to six, all
on the same frame and the same model. With the flag on it is one, the
answers land in the same rows through the same vocabulary, and the
descriptor task does not ask again. A model that answers in prose keeps
the prose as its caption and costs nothing extra: the questions wait for
the per-question path. Off, nothing here runs.
"""

from __future__ import annotations

import asyncio
import os
import secrets

from cryptography.fernet import Fernet

# Same settings bootstrap as the sibling enrichment tests; before the imports.
os.environ.setdefault("DATABASE_URL", "sqlite:///./_structured_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402

from services import structured_enrichment as se  # noqa: E402
from services.descriptor_enrichment import VQA_TASK  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── the prompt ────────────────────────────────────────────────────


def test_attributes_prompt_has_the_kinds_and_no_caption():
    prompt = se.build_attributes_prompt(["colour", "vehicle_type"])
    assert "caption" not in prompt.lower()
    assert prompt.rstrip().endswith('{"colour": "...", "vehicle_type": "..."}')
    assert "main vehicle" in prompt


# ── the answer ────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    '{"caption": "A white van", "colour": "white"}',
    'Here you go:\n```json\n{"caption": "A white van", "colour": "white"}\n```\nHope that helps.',
    'Sure. {"Caption": "A white van", "COLOUR": "white"} ',
])
def test_parse_answer_finds_the_object_in_json_fenced_or_prose(text):
    assert se.parse_answer(text) == {"caption": "A white van", "colour": "white"}


@pytest.mark.parametrize("text", ["", "A white van parked by the gate.", "[1, 2]", "{not json}"])
def test_parse_answer_is_none_without_an_object(text):
    assert se.parse_answer(text) is None


def test_interpret_json_gives_normalised_claims():
    before = dict(se.STATS)
    out = se.interpret('{"caption": "A white van at the gate.", "colour": "White", '
                       '"vehicle_type": "It is a van", "clothing_top": "unknown"}',
                       ["colour", "vehicle_type", "clothing_top"], "litertlm")
    assert out.json_ok and out.caption == "A white van at the gate."
    assert {(c.kind, c.value) for c in out.claims} == {("colour", "white"), ("vehicle_type", "van")}
    assert all(c.source_task == VQA_TASK and c.source_adapter == "litertlm"
               and c.confidence is None for c in out.claims)
    assert se.STATS["json_ok"] == before["json_ok"] + 1
    assert se.STATS["claims"] == before["claims"] + 2


def test_interpret_prose_keeps_the_text_as_caption_and_no_claims():
    before = dict(se.STATS)
    out = se.interpret("A white van at the gate.", ["colour"], "litertlm")
    assert not out.json_ok and out.caption == "A white van at the gate." and out.claims == []
    assert se.STATS["json_failed"] == before["json_failed"] + 1


def test_interpret_drops_a_word_outside_the_vocabulary():
    out = se.interpret('{"caption": "x", "colour": "turquoise-ish"}', ["colour"], "a")
    assert out.json_ok and out.claims == []
