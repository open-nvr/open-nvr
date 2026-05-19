# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Audit store unit tests — emit / read / filter / corruption tolerance."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kai_c.audit import AuditEventType, AuditStore, new_correlation_id


@pytest.fixture
def audit(tmp_path: Path) -> AuditStore:
    return AuditStore(path=str(tmp_path / "audit.jsonl"))


def test_emit_writes_jsonl_line(audit):
    event = audit.emit(
        AuditEventType.ADAPTER_REGISTERED,
        adapter="yolov8",
        adapter_version="1.0.0",
    )
    assert event["type"] == "adapter.registered"
    assert event["adapter"] == "yolov8"
    assert event["correlation_id"]
    contents = Path(audit.path).read_text().strip().splitlines()
    assert len(contents) == 1
    parsed = json.loads(contents[0])
    assert parsed["adapter"] == "yolov8"
    assert parsed["adapter_version"] == "1.0.0"
    assert "ts" in parsed


def test_emit_mints_correlation_id_when_absent(audit):
    event = audit.emit(AuditEventType.INFERENCE_COMPLETED, adapter="x", camera_id="cam-1")
    assert len(event["correlation_id"]) == 32  # hex uuid


def test_emit_preserves_supplied_correlation_id(audit):
    cid = new_correlation_id()
    event = audit.emit(AuditEventType.INFERENCE_COMPLETED, correlation_id=cid, adapter="x")
    assert event["correlation_id"] == cid


def test_filter_by_adapter(audit):
    audit.emit(AuditEventType.ADAPTER_REGISTERED, adapter="a")
    audit.emit(AuditEventType.ADAPTER_REGISTERED, adapter="b")
    audit.emit(AuditEventType.INFERENCE_COMPLETED, adapter="a", camera_id="cam-1")
    rows = audit.filter(adapter="a")
    assert len(rows) == 2
    assert all(r["adapter"] == "a" for r in rows)


def test_filter_by_event_type(audit):
    audit.emit(AuditEventType.ADAPTER_REGISTERED, adapter="a")
    audit.emit(AuditEventType.INFERENCE_COMPLETED, adapter="a")
    audit.emit(AuditEventType.INFERENCE_FAILED, adapter="a")
    rows = audit.filter(event_type=AuditEventType.INFERENCE_COMPLETED)
    assert len(rows) == 1
    assert rows[0]["type"] == "inference.completed"


def test_filter_by_camera_id_and_since(audit):
    audit.emit(AuditEventType.INFERENCE_COMPLETED, adapter="a", camera_id="cam-1", ts="2020-01-01T00:00:00Z")
    audit.emit(AuditEventType.INFERENCE_COMPLETED, adapter="a", camera_id="cam-2", ts="2026-01-01T00:00:00Z")
    rows = audit.filter(camera_id="cam-2")
    assert len(rows) == 1


def test_filter_limit(audit):
    for i in range(10):
        audit.emit(AuditEventType.INFERENCE_COMPLETED, adapter="a", camera_id=f"cam-{i}")
    rows = audit.filter(limit=3)
    assert len(rows) == 3
    # Newest first → last three cams
    assert [r["camera_id"] for r in rows] == ["cam-7", "cam-8", "cam-9"]


def test_corrupt_lines_are_skipped(audit):
    audit.emit(AuditEventType.ADAPTER_REGISTERED, adapter="a")
    Path(audit.path).write_text(
        Path(audit.path).read_text() + "not-json-at-all\n" + json.dumps({"type": "x"}) + "\n"
    )
    rows = audit.read_all()
    # Original + the new valid one; corrupt line skipped.
    assert len(rows) == 2


def test_read_all_missing_file_returns_empty(tmp_path):
    audit = AuditStore(path=str(tmp_path / "missing.jsonl"))
    assert audit.read_all() == []
