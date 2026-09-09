# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""resolve_total: exact totals, and the one case that must not guess."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.pagination import resolve_total  # noqa: E402


def _counter(value):
    """A count_fn that records whether it was called."""
    calls = []

    def fn():
        calls.append(1)
        return value

    fn.calls = calls
    return fn


def test_a_short_first_page_is_the_whole_answer():
    c = _counter(999)
    assert resolve_total(page_len=7, skip=0, limit=25, count_fn=c) == 7
    assert c.calls == [], "a short page already proves the total; do not count"


def test_a_short_later_page_adds_the_offset():
    c = _counter(999)
    assert resolve_total(page_len=5, skip=50, limit=25, count_fn=c) == 55
    assert c.calls == []


def test_a_full_page_must_count():
    c = _counter(312)
    assert resolve_total(page_len=25, skip=0, limit=25, count_fn=c) == 312
    assert c.calls == [1], "a full page proves nothing about what follows"


def test_an_empty_page_past_the_end_counts_rather_than_guessing():
    """The trap. skip=100 with no rows proves total <= 100, not == 100 —
    guessing would report 'of 100' for a five-row table."""
    c = _counter(5)
    assert resolve_total(page_len=0, skip=100, limit=25, count_fn=c) == 5
    assert c.calls == [1]


def test_an_empty_first_page_is_genuinely_empty():
    c = _counter(0)
    assert resolve_total(page_len=0, skip=0, limit=25, count_fn=c) == 0
    assert c.calls == []


@pytest.mark.parametrize("limit", [1, 25, 200, 500])
def test_a_full_page_always_counts_whatever_the_limit(limit):
    c = _counter(1000)
    assert resolve_total(limit, 0, limit, c) == 1000
    assert c.calls == [1]
