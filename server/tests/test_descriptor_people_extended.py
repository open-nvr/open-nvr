# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The extended person kinds are an opt-in on top of the people opt-in."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.descriptor_enrichment import (
    EXTENDED_PERSON_KINDS,
    KIND_QUESTIONS,
    LABEL_KINDS,
    kinds_for,
    normalise_answer,
)


def test_default_keeps_the_two_question_ceiling():
    assert kinds_for("person") == tuple(LABEL_KINDS["person"])
    assert len(kinds_for("person")) == 2
    assert kinds_for("car") == tuple(LABEL_KINDS["car"])


def test_extended_adds_three_kinds_to_people_only():
    assert kinds_for("person", extended=True) == tuple(LABEL_KINDS["person"]) + EXTENDED_PERSON_KINDS
    assert kinds_for("car", extended=True) == tuple(LABEL_KINDS["car"])
    assert kinds_for("suitcase", extended=True) == ()


def test_every_extended_kind_can_be_normalised():
    for kind in EXTENDED_PERSON_KINDS:
        assert kind in KIND_QUESTIONS
    assert normalise_answer("gender", "It looks like a woman.") == "woman"
    assert normalise_answer("gender", "A young boy") == "child"
    assert normalise_answer("headwear", "The person is wearing a hard hat") == "helmet"
    assert normalise_answer("headwear", "Nothing on the head") == "nothing"
    assert normalise_answer("clothing_bottom", "Blue jeans") in ("blue", "denim")


def test_setting_defaults_off():
    from core.config import settings

    assert getattr(settings, "events_descriptor_people_extended", None) is False


def test_search_maps_gender_and_headwear_words_to_vqa():
    from services.search_intent import GENDER_WORDS, HEADWEAR_WORDS, KIND_SKILL

    assert "woman" in GENDER_WORDS and "helmet" in HEADWEAR_WORDS
    assert KIND_SKILL["gender"] == KIND_SKILL["headwear"] == KIND_SKILL["clothing_bottom"] == "vqa"
