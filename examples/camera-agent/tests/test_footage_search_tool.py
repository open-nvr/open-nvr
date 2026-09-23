# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""``search_footage`` — one source, and the honest answer when it is down.

This file used to be two, and most of what was in them is gone with the
thing they tested. The agent read footage-search's private SQLite index:
first as its only source, then as an outage fallback, with counters
recording which one answered so the question "does that index still earn
a second store?" could be settled by evidence.

It settled itself. footage-search 2.0.0 deleted the index, so nothing
writes that file any more — it is either absent or frozen at the moment
of the upgrade. Falling back to it would mean answering "was anyone at
the gate last night?" from a database that stopped being written in
August, during an outage, when nobody is positioned to notice the dates
are months old. A stale answer presented as a current one is worse than
no answer, so the fallback is gone and the tool says it cannot say.

What is left to test is that distinction, in all three directions:
nothing matched, could not look, and the caller is broken.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass

import pytest
from opennvr_app_sdk.client import TimelineAPI

from tools import CameraTools

#: The signature every fake below must satisfy. Imported from the SDK
#: rather than restated, so a parameter renamed or dropped there breaks
#: these tests instead of quietly widening them. A fake of the shape
#: ``def find(self, *a, **kw)`` is what let the agent ship a call to a
#: method that did not exist and read as working for a month.
_REAL_FIND = inspect.signature(TimelineAPI.find)


@dataclass
class _Cam:
    camera_id: str


class _FakeContext:
    def __init__(self, ids):
        self.cameras = [_Cam(i) for i in ids]

    def known_camera(self, cid):
        return any(c.camera_id == cid for c in self.cameras)


class _Timeline:
    """The canonical store. ``answer=None`` is the SDK's "could not
    reach the store", which is deliberately NOT an empty result."""

    def __init__(self, answer=None):
        self.answer = answer
        self.calls = []

    def find(self, *a, **kw):
        bound = _REAL_FIND.bind(self, *a, **kw)
        bound.apply_defaults()
        self.calls.append({k: v for k, v in list(bound.arguments.items())[1:]})
        return self.answer


def _tools(timeline=None, cameras=("cam-gate", "cam-dock")):
    return CameraTools(
        context=_FakeContext(cameras),
        caption_client=None, detection_client=None, recognition_client=None,
        timeline=timeline, resolve_camera=lambda cid: "7",
    )


ARGS = {"keywords": ["red", "truck"]}

_A_MATCH = {
    "total": 1,
    "results": [{
        "id": 41, "camera_id": 2, "started_at": "2026-09-22T14:03:00+00:00",
        "caption": "a red truck at a loading dock", "label": "truck",
        "plate_text": "KA01AB1234", "has_evidence": True,
    }],
}


# ── it answers ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_store_answers_and_the_hit_carries_what_a_frame_could_not():
    t = _tools(_Timeline(_A_MATCH))
    out = await t.search_footage(ARGS)

    assert "red truck at a loading dock" in out
    # The things the private index could never have carried, because
    # none of them exist on the bus when a frame is analyzed.
    assert "KA01AB1234" in out
    assert "photo kept" in out
    assert "#41" in out
    assert t.footage_search_sources == {"canonical": 1, "unanswerable": 0}


@pytest.mark.asyncio
async def test_the_query_is_sent_as_joined_search_text():
    """There is no `parse` flag to turn off: the app-facing route does
    no sentence parsing at all, which is the version of "one parser per
    query" that actually exists. This assertion used to name a
    parameter the client has never had, and passed because the fake
    accepted anything."""
    tl = _Timeline(_A_MATCH)
    await _tools(tl).search_footage(ARGS)

    assert tl.calls[0]["text"] == "red truck"
    assert "parse" not in tl.calls[0]


@pytest.mark.asyncio
async def test_an_empty_result_is_reported_as_nothing_matched():
    t = _tools(_Timeline({"total": 0, "results": []}))
    out = await t.search_footage({"keywords": ["zebra"]})

    assert "No recorded footage matched" in out
    assert t.footage_search_sources["canonical"] == 1


# ── and says so when it cannot ───────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unreachable_store_is_not_reported_as_nothing_matched():
    """The distinction the whole tool is built around. Telling an
    operator that no red truck came past, when the truth is that nobody
    was able to look, is the worst answer available here."""
    t = _tools(_Timeline(None))
    out = await t.search_footage(ARGS)

    assert "cannot say whether anything matched" in out
    assert "No recorded footage matched" not in out
    assert t.footage_search_sources == {"canonical": 0, "unanswerable": 1}


@pytest.mark.asyncio
async def test_no_store_configured_at_all_is_unanswerable():
    """A deployment with no core connection. Used to be the case the
    local index existed for; there is nothing to fall back to now, and
    the tool is not advertised to the LLM at all in this state."""
    t = _tools(timeline=None)
    out = await t.search_footage(ARGS)

    assert "isn't available" in out
    assert t.footage_search_sources == {"canonical": 0, "unanswerable": 1}


@pytest.mark.asyncio
async def test_a_programming_error_is_not_reported_as_an_outage():
    """The defect this whole change exists to stop repeating.

    A bare ``except Exception`` around the call put "the store is
    unreachable" and "this code is wrong" on the same branch, so a
    method that did not exist read as a month of healthy fallbacks.
    Anything that is not the documented ``None`` must be loud — and
    must move no counter, because a wrong counter is evidence somebody
    will make a decision on.
    """
    class _Broken:
        def find(self, *a, **kw):
            raise AttributeError("find")

    t = _tools(_Broken())
    with pytest.raises(AttributeError):
        await t.search_footage(ARGS)

    assert t.footage_search_sources == {"canonical": 0, "unanswerable": 0}


def test_an_unreachable_store_arrives_as_none_not_as_an_exception():
    """``find`` must SWALLOW transport failures and return ``None``.

    The tool no longer wraps the call, so this is load-bearing: if
    ``find`` ever starts raising when core is down, an outage stops
    being reported and starts crashing the tool. Asserted against a
    real ``TimelineAPI`` over a transport that fails, rather than
    against a fake that would only restate the assumption.
    """
    import httpx

    from opennvr_app_sdk.client import _Http

    def _refuse(request):
        raise httpx.ConnectError("connection refused")

    class _NoCreds:
        def headers(self):
            return {"X-Internal-Api-Key": "oak_test"}

    http = _Http.__new__(_Http)
    http.base = "http://core.invalid"
    http.creds = _NoCreds()
    http.timeout = 1.0
    http._client = httpx.Client(transport=httpx.MockTransport(_refuse))

    assert TimelineAPI(http).find("red truck") is None


# ── arguments ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_rejected_query_counts_as_nothing():
    """Bad arguments never reached a store, so they must not move a
    counter that is read as "how often could we answer?"."""
    t = _tools(_Timeline(_A_MATCH))

    assert "ERROR" in await t.search_footage({})
    assert "ERROR" in await t.search_footage({"keywords": []})
    assert "ERROR" in await t.search_footage({"keywords": ["x"],
                                              "camera_id": "cam-nope"})
    assert "ERROR" in await t.search_footage({"keywords": ["x"],
                                              "within_minutes": "soon"})
    assert t.footage_search_sources == {"canonical": 0, "unanswerable": 0}


@pytest.mark.asyncio
async def test_a_known_camera_is_resolved_to_a_server_side_id():
    """The agent's handles are operator-chosen names; the platform
    scopes by id. An unresolvable camera is refused rather than sent as
    a filter the store cannot read."""
    tl = _Timeline(_A_MATCH)
    await _tools(tl).search_footage({"keywords": ["red"],
                                     "camera_id": "cam-gate"})
    assert tl.calls[0]["camera"] == [7]


@pytest.mark.asyncio
async def test_any_camera_sends_no_filter():
    tl = _Timeline(_A_MATCH)
    await _tools(tl).search_footage({"keywords": ["red"],
                                     "camera_id": "__any__"})
    assert tl.calls[0]["camera"] is None


@pytest.mark.asyncio
async def test_the_counts_accumulate_across_calls():
    tl = _Timeline(_A_MATCH)
    t = _tools(tl)
    await t.search_footage(ARGS)
    await t.search_footage(ARGS)
    tl.answer = None
    await t.search_footage(ARGS)

    assert t.footage_search_sources == {"canonical": 2, "unanswerable": 1}
