# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the footage_index reader and the camera-agent
``search_footage`` tool — no NATS, no LLM, no live adapters."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

import pytest

from footage_index import FootageIndex
from tools import CameraTools


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
