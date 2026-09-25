"""Search knows what a question needs, and whether this box has it."""
from __future__ import annotations

import os
import secrets
import sys
import types as _types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_intent_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from services.search_intent import (  # noqa: E402
    STATE_AVAILABLE, STATE_MISSING, STATE_UNASSIGNED, BoxAbilities, resolve_needs,
)
from services.search_query import parse_query  # noqa: E402

NOW = datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc)


def _p(q, people=None):
    return parse_query(q, cameras={}, people=people, now=NOW)


# ── the parser: names and plate questions ─────────────────────────────


def test_a_known_name_becomes_the_face_filter_not_a_caption_word():
    parsed = _p("did you see varun today", people=["varun-singh", "priya-n"])
    assert parsed.attrs == [("face_id", "varun-singh")]
    assert parsed.text == "", "the name must not survive as free text"
    assert parsed.matched["person"] == "varun"
    assert parsed.from_ == datetime(2026, 9, 25, tzinfo=timezone.utc)


def test_a_full_name_and_a_first_name_both_land():
    assert _p("was varun singh here", people=["varun-singh"]).attrs == [("face_id", "varun-singh")]
    assert _p("varun", people=["varun-singh"]).attrs == [("face_id", "varun-singh")]


def test_a_shared_first_name_does_not_pick_one_of_two_people():
    parsed = _p("varun today", people=["varun-singh", "varun-mehta"])
    assert parsed.attrs == []
    assert parsed.text == "varun"


def test_an_unknown_name_stays_free_text():
    parsed = _p("did you see ravi", people=["varun-singh"])
    assert parsed.attrs == [] and parsed.text == "ravi"


def test_asking_for_the_plate_number_wants_visits_with_a_read():
    parsed = _p("what is the plate number of the white car in the last 2 hours")
    assert parsed.labels == ["car"]
    assert parsed.text == "white"
    assert parsed.plate == ""
    assert parsed.wants_plate is True
    assert parsed.matched["plate"] == "with a plate read"


def test_a_given_plate_is_a_plate_filter_not_a_wish():
    parsed = _p("plate ka01ab1234")
    assert parsed.plate == "KA01AB1234" and parsed.wants_plate is False


def test_as_dict_carries_the_new_fields():
    d = _p("plate number of the van", people=[]).as_dict()
    assert d["wants_plate"] is True and d["attrs"] == []


# ── needs: what the words ask of the skills ───────────────────────────


def _box(offered=(), claimed=(), captions=False):
    return BoxAbilities(offered_kinds=set(offered), claimed_kinds=set(claimed),
                        captions=captions)


def test_a_red_shirt_needs_clothing_colour_and_says_when_the_box_cannot():
    needs = resolve_needs(words=["red", "shirt"], labels=["person"],
                          box=_box(offered=["colour", "vehicle_type"], captions=True))
    assert [(n.word, n.kind, n.skill, n.state) for n in needs] == [
        ("red", "clothing_top", "vqa", STATE_MISSING)]
    assert needs[0].fallback == "captions"


def test_a_skill_offered_but_never_assigned_is_its_own_state():
    needs = resolve_needs(words=["red"], labels=["car"],
                          box=_box(offered=["colour"], claimed=["plate"]))
    assert needs[0].state == STATE_UNASSIGNED
    needs = resolve_needs(words=["red"], labels=["car"],
                          box=_box(offered=["colour"], claimed=["colour"]))
    assert needs[0].state == STATE_AVAILABLE


def test_a_colour_with_no_class_lists_both_readings():
    kinds = {n.kind for n in resolve_needs(words=["blue"], labels=[], box=_box())}
    assert kinds == {"colour", "clothing_top"}


def test_clothing_cue_words_make_a_colour_about_a_person():
    kinds = {n.kind for n in resolve_needs(words=["blue", "jacket"], labels=[], box=_box())}
    assert kinds == {"clothing_top"}


def test_names_and_plates_are_needs_too():
    needs = resolve_needs(words=[], labels=[], attrs=[("face_id", "varun-singh")],
                          wants_plate=True,
                          box=_box(offered=["face_id", "plate"], claimed=["face_id", "plate"]))
    by_kind = {n.kind: n for n in needs}
    assert by_kind["face_id"].skill == "face_recognition"
    assert by_kind["face_id"].state == STATE_AVAILABLE
    assert by_kind["plate"].skill == "license_plate_recognition"


def test_plain_class_words_need_nothing():
    assert resolve_needs(words=[], labels=["car"], box=_box()) == []
