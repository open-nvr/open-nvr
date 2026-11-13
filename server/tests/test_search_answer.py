# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Search answers the question instead of only listing rows.

The danger in a summary is that it is believed. A list of ten rows is
obviously ten rows; a sentence saying "mostly on the Gate camera" is
read as a fact about the whole day. So every number here is counted from
rows already returned, the block says which scope it counted over, and
the tests below are mostly about what it must NOT say.

The one that earns the feature is ``undescribed``. A visit no skill ran
on carries no claims, so its silence about "red" is not evidence it was
not red. An operator reading a result list cannot see that distinction;
it is the difference between "no red van came by" and "nothing looked".
"""
from __future__ import annotations

import os
import secrets
import sys
import types as _types
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_answer_test.db")
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

from services.search_service import SearchHit, summarise_hits  # noqa: E402

_T0 = datetime(2026, 9, 22, 14, 0, tzinfo=UTC)


@dataclass
class _Event:
    id: int
    camera_id: int
    started_at: datetime
    plate_text: str | None = None
    evidence_path: str | None = None
    label: str = "car"


@dataclass
class _Hit:
    event: _Event
    claims: list = field(default_factory=list)


def _claim(kind, value, task="vqa"):
    return {"kind": kind, "value": value, "confidence": 0.9,
            "task": task, "adapter": "kai-c"}


def _hit(i, *, cam=1, mins=0, claims=(), plate=None, evidence=None):
    return _Hit(_Event(i, cam, _T0 + timedelta(minutes=mins),
                       plate_text=plate, evidence_path=evidence),
                list(claims))


# ── it counts what is there ──────────────────────────────────────────


def test_an_empty_page_gets_no_answer_at_all():
    """Zeroes read as a finding. 'Across 0 cameras, 0 described' is a
    sentence about nothing, and the empty state already has `relax` to
    say something useful."""
    assert summarise_hits([], total=0) == {}
    assert summarise_hits([], total=900) == {}


def test_cameras_are_counted_and_ordered_by_how_many_each_saw():
    hits = [_hit(1, cam=3), _hit(2, cam=3), _hit(3, cam=3), _hit(4, cam=7)]
    a = summarise_hits(hits, total=4, camera_names={3: "Gate", 7: "Drive"})

    assert a["cameras"] == [
        {"id": 3, "name": "Gate", "count": 3},
        {"id": 7, "name": "Drive", "count": 1},
    ]
    assert a["camera_count"] == 2


def test_the_time_span_is_the_first_and_last_of_what_matched():
    hits = [_hit(1, mins=51), _hit(2, mins=2), _hit(3, mins=20)]
    a = summarise_hits(hits, total=3)

    assert a["first_at"] == (_T0 + timedelta(minutes=2)).isoformat()
    assert a["last_at"] == (_T0 + timedelta(minutes=51)).isoformat()


def test_two_skills_agreeing_is_one_sighting_not_two():
    """Counting agreements as sightings would make the best-enriched
    visit look like a crowd, and 'red x2' from a single van is a lie
    about how much evidence there is."""
    hits = [_hit(1, claims=[_claim("colour", "red", task="vqa"),
                            _claim("colour", "red", task="image_captioning")])]
    a = summarise_hits(hits, total=1)

    assert a["claims"] == [{"kind": "colour", "value": "red", "count": 1}]


def test_two_skills_disagreeing_are_both_reported():
    """Neither is discarded. Which one is right is not search's call,
    and hiding the disagreement would present a guess as a fact."""
    hits = [_hit(1, claims=[_claim("colour", "red"), _claim("colour", "maroon")])]
    a = summarise_hits(hits, total=1)

    assert {c["value"] for c in a["claims"]} == {"red", "maroon"}


def test_plates_and_photos_are_counted():
    hits = [_hit(1, plate="MH12AB1234", evidence="ab/cd.jpg"),
            _hit(2, plate="MH12AB1234"),
            _hit(3, evidence="ef/gh.jpg")]
    a = summarise_hits(hits, total=3)

    assert a["plates"] == ["MH12AB1234"]      # distinct, not once per row
    assert a["plate_count"] == 1
    assert a["with_evidence"] == 2


# ── it says what it does NOT know ────────────────────────────────────


def test_visits_nobody_described_are_counted_as_such():
    """The whole point. Three of these five were never looked at by any
    skill, so 'no red van matched' would be an overstatement of what the
    store actually knows."""
    hits = [_hit(1, claims=[_claim("colour", "white")]),
            _hit(2, claims=[_claim("colour", "white")]),
            _hit(3), _hit(4), _hit(5)]
    a = summarise_hits(hits, total=5)

    assert a["undescribed"] == 3
    assert a["shown"] == 5


def test_a_fully_described_page_reports_nothing_undescribed():
    hits = [_hit(1, claims=[_claim("colour", "red")]),
            _hit(2, claims=[_claim("vehicle_type", "van")])]
    assert summarise_hits(hits, total=2)["undescribed"] == 0


def test_the_answer_says_it_counted_a_page_not_the_whole_match():
    """12,000 matches and ten loaded rows: quoting the ten as though
    they described the twelve thousand is the exact dishonesty this is
    meant to remove, so the scope is stated and both numbers are given.
    """
    a = summarise_hits([_hit(i) for i in range(10)], total=12000)

    assert a["scope"] == "page"
    assert a["shown"] == 10
    assert a["total"] == 12000


def test_claims_with_no_value_are_not_counted():
    """A descriptor row that failed to normalise has a kind and nothing
    to say. Counting it would invent a category with an empty label."""
    hits = [_hit(1, claims=[{"kind": "colour", "value": None},
                            {"kind": None, "value": "red"},
                            _claim("colour", "red")])]
    a = summarise_hits(hits, total=1)

    assert a["claims"] == [{"kind": "colour", "value": "red", "count": 1}]


def test_a_visit_with_no_timestamp_does_not_break_the_span():
    hits = [_hit(1, mins=5)]
    hits.append(_Hit(_Event(2, 1, None)))
    a = summarise_hits(hits, total=2)

    assert a["first_at"] == a["last_at"] == (_T0 + timedelta(minutes=5)).isoformat()
    assert a["shown"] == 2


def test_nothing_is_listed_past_the_top_few():
    """A summary that names thirty cameras is not a summary. The counts
    stay exact so the UI can say 'and 26 others' rather than pretending
    the list is complete."""
    hits = [_hit(i, cam=i) for i in range(30)]
    a = summarise_hits(hits, total=30)

    assert len(a["cameras"]) == 4
    assert a["camera_count"] == 30


# ── the route carries it ─────────────────────────────────────────────


def test_the_search_route_returns_the_answer_beside_the_results():
    import inspect

    from routers import search as search_router

    src = inspect.getsource(search_router)
    assert '"answer": summarise_hits(' in src, (
        "the /search response must carry the answer, or the UI has "
        "nothing to render and this service is dead code")
