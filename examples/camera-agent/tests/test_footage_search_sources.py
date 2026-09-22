# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which store actually answered ``search_footage``.

The private SQLite index stopped being the source of footage search and
became its outage fallback. Whether it still earns a second store — with
its own 30-day retention, one row per analyzed frame, no camera scoping,
no evidence photo — turns on a question nobody could answer: has it
served a single query since the repoint?

So the tool counts. These tests pin the counting, because a counter that
is wrong is worse than no counter: it would be the evidence a deletion
decision gets made on.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from tools import CameraTools


@dataclass
class _Cam:
    camera_id: str


class _FakeContext:
    def __init__(self, ids):
        self.cameras = [_Cam(i) for i in ids]

    def known_camera(self, cid):
        return any(c.camera_id == cid for c in self.cameras)


@dataclass
class _Hit:
    ts: float
    camera_id: str
    caption: str
    labels: tuple


class _Index:
    """The private SQLite index, reduced to what the tool asks of it."""

    def __init__(self, hits=(), available=True, explode=False):
        self.available = available
        self._hits = list(hits)
        self._explode = explode
        self.calls = 0

    def search(self, **kw):
        self.calls += 1
        if self._explode:
            raise RuntimeError("index is corrupt")
        return list(self._hits)


class _Timeline:
    """The canonical store. ``answer=None`` is 'core unreachable', which
    is deliberately NOT the same as 'nothing matched'."""

    def __init__(self, answer=None, explode=False):
        self._answer = answer
        self._explode = explode
        self.calls = 0

    def find(self, *a, **kw):
        self.calls += 1
        if self._explode:
            raise RuntimeError("connection refused")
        return self._answer


def _tools(*, timeline=None, footage_index=None):
    return CameraTools(
        context=_FakeContext(["cam-gate"]),
        caption_client=None, detection_client=None, recognition_client=None,
        footage_index=footage_index, timeline=timeline,
    )


ARGS = {"keywords": ["red", "truck"]}

_A_MATCH = {"results": [{"id": 7, "started_at": "2026-09-22T10:00:00Z",
                         "camera_id": "cam-gate", "caption": "a red truck"}],
            "total": 1}


@pytest.mark.asyncio
async def test_the_canonical_store_answering_is_counted_as_canonical():
    t = _tools(timeline=_Timeline(answer=_A_MATCH),
               footage_index=_Index(hits=[_Hit(0, "cam-gate", "x", ())]))
    out = await t.search_footage(ARGS)

    assert "red truck" in out
    assert t.footage_search_sources == {
        "canonical": 1, "index_fallback": 0, "unanswerable": 0}


@pytest.mark.asyncio
async def test_the_canonical_store_finding_nothing_is_still_canonical():
    """'Nothing matched' is an ANSWER. Counting it as a fallback would
    inflate the case for keeping the index with queries it never saw."""
    index = _Index(hits=[_Hit(0, "cam-gate", "x", ())])
    t = _tools(timeline=_Timeline(answer={"results": [], "total": 0}),
               footage_index=index)
    out = await t.search_footage(ARGS)

    assert "No recorded footage matched" in out
    assert t.footage_search_sources["canonical"] == 1
    assert t.footage_search_sources["index_fallback"] == 0
    assert index.calls == 0, "the index must not be consulted after an answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("timeline", [
    _Timeline(answer=None),        # reachable, returned nothing usable
    _Timeline(explode=True),       # could not be reached at all
])
async def test_the_index_answering_is_counted_as_a_fallback(timeline):
    """The case the whole counter exists for. Both shapes of 'core did
    not answer' have to land here, or a real outage goes unrecorded."""
    index = _Index(hits=[_Hit(0, "cam-gate", "a red truck", ())])
    t = _tools(timeline=timeline, footage_index=index)
    out = await t.search_footage(ARGS)

    assert "red truck" in out
    assert index.calls == 1
    assert t.footage_search_sources["index_fallback"] == 1
    assert t.footage_search_sources["canonical"] == 0


@pytest.mark.asyncio
async def test_the_index_finding_nothing_still_counts_as_a_fallback():
    """It served the query. That it found nothing is a result, and the
    question being measured is 'was the index used', not 'did it hit'."""
    t = _tools(timeline=_Timeline(answer=None), footage_index=_Index(hits=[]))
    out = await t.search_footage(ARGS)

    assert "No recorded footage matched" in out
    assert t.footage_search_sources["index_fallback"] == 1


@pytest.mark.asyncio
async def test_no_store_at_all_is_unanswerable_not_a_fallback():
    t = _tools(timeline=None, footage_index=None)
    out = await t.search_footage(ARGS)

    assert "isn't available" in out
    assert t.footage_search_sources == {
        "canonical": 0, "index_fallback": 0, "unanswerable": 1}


@pytest.mark.asyncio
async def test_core_down_with_no_index_is_unanswerable():
    """The outcome deleting the index would convert every fallback into,
    which is exactly what the decision is trading away."""
    t = _tools(timeline=_Timeline(explode=True), footage_index=None)
    out = await t.search_footage(ARGS)

    assert "cannot say whether anything matched" in out
    assert t.footage_search_sources["unanswerable"] == 1
    assert t.footage_search_sources["index_fallback"] == 0


@pytest.mark.asyncio
async def test_a_broken_index_is_unanswerable_not_a_fallback():
    """An index that raises did not serve the query. Counting it as a
    fallback would argue for keeping a store that is not working."""
    t = _tools(timeline=_Timeline(answer=None),
               footage_index=_Index(explode=True))
    out = await t.search_footage(ARGS)

    assert "failed" in out.lower()
    assert t.footage_search_sources["unanswerable"] == 1
    assert t.footage_search_sources["index_fallback"] == 0


@pytest.mark.asyncio
async def test_a_rejected_query_counts_as_nothing():
    """Bad arguments never reached a store, so they must not move any
    counter — least of all the one a deletion rests on."""
    t = _tools(timeline=_Timeline(answer=_A_MATCH))
    assert "ERROR" in await t.search_footage({"keywords": []})
    # A list that only LOOKS usable until the strip empties it — a
    # different early return from the one above, and its own chance to
    # move a counter it has no business moving.
    assert "ERROR" in await t.search_footage({"keywords": ["   "]})
    assert "ERROR" in await t.search_footage({"keywords": ["x"],
                                              "within_minutes": "soon"})
    assert "ERROR" in await t.search_footage({"keywords": ["x"],
                                              "camera_id": "cam-nope"})
    assert t.footage_search_sources == {
        "canonical": 0, "index_fallback": 0, "unanswerable": 0}


@pytest.mark.asyncio
async def test_the_counts_accumulate_across_calls():
    index = _Index(hits=[_Hit(0, "cam-gate", "a red truck", ())])
    t = _tools(timeline=_Timeline(answer=None), footage_index=index)
    for _ in range(3):
        await t.search_footage(ARGS)
    assert t.footage_search_sources["index_fallback"] == 3
