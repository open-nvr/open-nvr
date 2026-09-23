# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the footage_index reader and the camera-agent
``search_footage`` tool — no NATS, no LLM, no live adapters."""
from __future__ import annotations

import inspect
import sqlite3
import time
from dataclasses import dataclass

import pytest
from opennvr_app_sdk.client import TimelineAPI

from footage_index import FootageIndex
from tools import CameraTools

#: Imported from the SDK rather than restated, so a parameter renamed
#: or dropped there breaks these tests instead of quietly widening them.
_REAL_FIND = inspect.signature(TimelineAPI.find)


# ── Minimal fakes ──────────────────────────────────────────────────


@dataclass
class _Cam:
    camera_id: str


class _FakeContext:
    def __init__(self, ids):
        self.cameras = [_Cam(i) for i in ids]

    def known_camera(self, cid):
        return any(c.camera_id == cid for c in self.cameras)


def _build_index(path, rows):
    """Create a footage-search-shaped SQLite DB with the given rows."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE keyframes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "camera_id TEXT, ts REAL, correlation_id TEXT, adapter TEXT, "
        "labels TEXT, caption TEXT)"
    )
    conn.executemany(
        "INSERT INTO keyframes (camera_id, ts, correlation_id, adapter, labels, caption) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def _tools(footage_index):
    return CameraTools(
        context=_FakeContext(["cam-dock", "cam-gate"]),
        caption_client=None, detection_client=None, recognition_client=None,
        footage_index=footage_index,
    )


# ── footage_index reader ───────────────────────────────────────────


def test_index_unavailable_when_file_missing(tmp_path):
    idx = FootageIndex(str(tmp_path / "nope.sqlite3"))
    assert idx.available is False
    assert idx.search(keywords=["truck"]) == []


def test_index_matches_label_and_caption(tmp_path):
    db = str(tmp_path / "idx.sqlite3")
    now = time.time()
    _build_index(db, [
        ("cam-dock", now - 600, "A", "blip", "truck person",
         "a red truck near a loading dock"),
        ("cam-dock", now - 300, "B", "blip", "car", "a blue car"),
    ])
    idx = FootageIndex(db)
    assert idx.available
    hits = idx.search(keywords=["red", "truck"])
    assert len(hits) == 1
    assert "red truck" in hits[0].caption


def test_index_time_and_camera_filters(tmp_path):
    db = str(tmp_path / "idx.sqlite3")
    now = time.time()
    _build_index(db, [
        ("cam-dock", now - 60, "A", "yolov8", "truck", ""),
        ("cam-dock", now - 7200, "B", "yolov8", "truck", ""),   # 2h ago
        ("cam-gate", now - 60, "C", "yolov8", "truck", ""),
    ])
    idx = FootageIndex(db)
    # last 30 min on cam-dock → only row A
    hits = idx.search(keywords=["truck"], within_minutes=30, camera_id="cam-dock")
    assert [h.correlation_id for h in hits] == ["A"]


# ── search_footage tool ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_reports_when_index_unavailable(tmp_path):
    tools = _tools(FootageIndex(str(tmp_path / "missing.sqlite3")))
    out = await tools.search_footage({"keywords": ["truck"]})
    assert "isn't available" in out


@pytest.mark.asyncio
async def test_tool_returns_matches(tmp_path):
    db = str(tmp_path / "idx.sqlite3")
    now = time.time()
    _build_index(db, [
        ("cam-dock", now - 120, "A", "blip", "truck", "a red truck at the dock"),
    ])
    tools = _tools(FootageIndex(db))
    out = await tools.search_footage({"keywords": ["red", "truck"], "camera_id": "cam-dock"})
    assert "red truck" in out
    assert "cam-dock" in out


@pytest.mark.asyncio
async def test_tool_rejects_unknown_camera(tmp_path):
    db = str(tmp_path / "idx.sqlite3")
    _build_index(db, [])
    tools = _tools(FootageIndex(db))
    out = await tools.search_footage({"keywords": ["truck"], "camera_id": "cam-x"})
    assert "unknown camera_id" in out


@pytest.mark.asyncio
async def test_tool_requires_keywords(tmp_path):
    db = str(tmp_path / "idx.sqlite3")
    _build_index(db, [])
    tools = _tools(FootageIndex(db))
    out = await tools.search_footage({"keywords": []})
    assert "ERROR" in out


# ── Lazy (re-)open ─────────────────────────────────────────────────
#
# On a stock install the agent and the footage-search indexer start
# together, and the agent probes BEFORE the indexer has created its
# schema. A one-shot probe at boot left search_footage "unavailable"
# forever until an agent restart; the reader now re-tries on every call.


def test_index_created_after_boot_lights_up_without_restart(tmp_path):
    db = str(tmp_path / "late.sqlite3")
    idx = FootageIndex(db)                    # agent boots first
    assert idx.available is False
    _build_index(db, [                         # indexer catches up later
        ("cam-dock", time.time() - 60, "A", "tier0", "truck", ""),
    ])
    assert idx.available is True, "reader must re-try, not remember failure"
    hits = idx.search(keywords=["truck"])
    assert len(hits) == 1 and hits[0].camera_id == "cam-dock"


def test_file_present_but_schema_not_yet_is_not_yet(tmp_path):
    """The DB file can exist before the indexer has created its table
    (sqlite creates the file on connect). Treat that as not-yet, then
    succeed once the schema lands."""
    db = str(tmp_path / "empty.sqlite3")
    sqlite3.connect(db).close()               # zero-byte file, no schema
    idx = FootageIndex(db)
    assert idx.available is False
    assert idx.search(keywords=["truck"]) == []
    _build_index(db, [
        ("cam-gate", time.time() - 30, "B", "tier0", "person", ""),
    ])
    assert idx.available is True
    assert len(idx.search(keywords=["person"])) == 1


# ── the canonical store is now the source; the index is the fallback ──
#
# search_footage used to read ONLY the footage-search app's private
# SQLite index. It now asks the platform's canonical store first
# (timeline.find — the operator Search page's own query), because that
# store is one row per VISIT rather than per analyzed frame, is scoped by
# the same predicate everything else uses, and carries the evidence
# photo, the plate and what each skill claimed. The index remains as a
# fallback for a box that cannot reach core.


class _FakeTimeline:
    """Stands in for the SDK's TimelineAPI. ``answer`` of None is the
    SDK's "could not reach the store" — deliberately NOT the same as an
    empty result list."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def find(self, *a, **kw):
        # Bound against the REAL signature. Without this line the fake
        # accepts calls no client can take, which is how
        # ``find("", text=..., parse=False)`` — a duplicate argument and
        # a parameter that has never existed — passed here for a month
        # while every live query fell through to the SQLite index.
        bound = _REAL_FIND.bind(self, *a, **kw)
        bound.apply_defaults()
        self.calls.append({k: v for k, v in list(bound.arguments.items())[1:]})
        return self.answer


def _tools_with(timeline=None, footage_index=None):
    return CameraTools(
        context=_FakeContext(["cam-dock", "cam-gate"]),
        caption_client=None, detection_client=None, recognition_client=None,
        footage_index=footage_index, timeline=timeline,
    )


@pytest.mark.asyncio
async def test_search_uses_the_canonical_store_when_it_is_available():
    tl = _FakeTimeline({
        "total": 1,
        "results": [{
            "id": 41, "camera_id": 2, "started_at": "2026-09-22T14:03:00+00:00",
            "caption": "a red truck at a loading dock", "label": "truck",
            "plate_text": "KA01AB1234", "has_evidence": True,
        }],
    })
    out = await _tools_with(timeline=tl).search_footage(
        {"keywords": ["red", "truck"]})
    assert "red truck at a loading dock" in out
    # The things the private index could never carry.
    assert "KA01AB1234" in out
    assert "photo kept" in out
    assert "#41" in out
    # And the keywords went out as the search text, joined. There is no
    # "parse" flag to turn off: the app-facing route does no sentence
    # parsing at all, which is the version of "one parser per query"
    # that actually exists. This assertion used to name a parameter the
    # client has never had, and passed because the fake accepted it.
    assert tl.calls[0]["text"] == "red truck"
    assert "parse" not in tl.calls[0]


@pytest.mark.asyncio
async def test_an_empty_result_is_reported_as_nothing_matched():
    tl = _FakeTimeline({"total": 0, "results": []})
    out = await _tools_with(timeline=tl).search_footage({"keywords": ["zebra"]})
    assert "No recorded footage matched" in out


@pytest.mark.asyncio
async def test_unreachable_store_is_not_reported_as_nothing_matched(tmp_path):
    """The distinction the events client goes out of its way to preserve:
    "nothing came" and "I couldn't check" are different answers, and in a
    security product conflating them is the dangerous direction."""
    tools = _tools_with(timeline=_FakeTimeline(None))
    out = await tools.search_footage({"keywords": ["red", "truck"]})
    assert "No recorded footage matched" not in out
    assert "cannot say" in out.lower()


@pytest.mark.asyncio
async def test_it_falls_back_to_the_local_index_when_core_is_unreachable(tmp_path):
    db = tmp_path / "index.sqlite3"
    _build_index(db, [("cam-dock", time.time() - 120, "corr-1", "blip",
                       "truck", "a red truck at the dock")])
    tools = _tools_with(timeline=_FakeTimeline(None),
                        footage_index=FootageIndex(str(db)))
    out = await tools.search_footage({"keywords": ["red", "truck"]})
    assert "red truck at the dock" in out


@pytest.mark.asyncio
async def test_the_index_is_not_consulted_when_the_store_answered(tmp_path):
    """Belt and braces: an answer from the canonical store must not be
    silently topped up from, or replaced by, the legacy index."""
    db = tmp_path / "index.sqlite3"
    _build_index(db, [("cam-dock", time.time() - 120, "corr-1", "blip",
                       "truck", "INDEX ROW THAT MUST NOT APPEAR")])
    tl = _FakeTimeline({"total": 0, "results": []})
    tools = _tools_with(timeline=tl, footage_index=FootageIndex(str(db)))
    out = await tools.search_footage({"keywords": ["truck"]})
    assert "INDEX ROW" not in out
    assert "No recorded footage matched" in out
