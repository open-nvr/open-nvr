"""search_history says "not yet" when core is describing the window now.

Core's internal /events answers with `pending` when the question asked
for a claim the matching visits did not have; the SDK client keeps it
on `last_pending`; the tool turns it into a sentence. "No blue car" must
never be said about visits nobody has looked at (#583).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from context import CameraContext, CameraSpec
from tools import CameraTools


class _Events:
    def __init__(self, rows, pending=None):
        self.rows, self.last_pending = rows, pending
        self.calls = []

    async def search(self, **kw):
        self.calls.append(kw)
        return self.rows

    async def evidence(self, event_id):
        return None


def _tools(events):
    ctx = CameraContext(cameras=[CameraSpec(camera_id="cam1", frame_url="x", role="gate",
                                            opennvr_camera_id=1)])
    return CameraTools(context=ctx, detection_client=None, caption_client=None,
                       recognition_client=None, events_client=events)


def _visit(**over):
    base = dict(id=12, camera_id=1, label="car", score=0.9,
                started_at="2026-09-28T14:02:00+05:30", ended_at="2026-09-28T14:05:00+05:30",
                stationary=False, plate_text=None, has_evidence=False)
    base.update(over)
    return SimpleNamespace(**base)


def test_an_empty_window_being_described_is_not_yet_not_no():
    ev = _Events([], pending={"missing": 28, "requested": 28, "already_queued": 0,
                              "eta_s": 360.0})
    out = asyncio.run(_tools(ev).search_history({"label": "car", "attr": ["blue"]}))
    assert "28 visits in that window have not been described yet" in out
    assert "about 6 minutes" in out and "Ask me again" in out
    assert "not the same as there having been none" not in out, (
        "the generic caveat is replaced by the specific one")


def test_a_partial_answer_still_says_what_is_pending():
    ev = _Events([_visit()], pending={"missing": 1, "requested": 0, "already_queued": 1,
                                      "eta_s": 20.0})
    out = asyncio.run(_tools(ev).search_history({"label": "car", "attr": ["blue"]}))
    assert "I remember 1 car visit" in out
    assert "1 visit in that window has not been described yet" in out
    assert "about 20 seconds" in out


def test_nothing_pending_means_no_note():
    ev = _Events([_visit()], pending={"missing": 0, "requested": 0, "already_queued": 0})
    out = asyncio.run(_tools(ev).search_history({"label": "car"}))
    assert "not been described" not in out
    ev2 = _Events([_visit()])                       # an older SDK: no attribute at all
    del ev2.last_pending
    out2 = asyncio.run(_tools(ev2).search_history({"label": "car"}))
    assert "not been described" not in out2


def test_pending_on_the_rows_wins_over_the_clients_last_block():
    """A newer SDK returns the block on the rows; an older one only sets
    last_pending. The rows are per-call; the client attribute is shared."""
    class _Rows(list):
        pending = None

    rows = _Rows([_visit()])
    rows.pending = {"missing": 2, "requested": 2, "already_queued": 0, "eta_s": 10.0}
    ev = _Events(rows, pending={"missing": 99, "requested": 99, "already_queued": 0,
                                "eta_s": 999.0})
    out = asyncio.run(_tools(ev).search_history({"label": "car", "attr": ["blue"]}))
    assert "2 visits in that window" in out and "99" not in out
