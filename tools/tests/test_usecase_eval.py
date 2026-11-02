# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scorer and the evidence pack, with no stack: alerts are dicts, the
snapshot fetcher is a stub."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location("usecase_eval", Path(__file__).resolve().parents[1] / "usecase_eval.py")
usecase_eval = importlib.util.module_from_spec(_spec)
sys.modules["usecase_eval"] = usecase_eval
_spec.loader.exec_module(usecase_eval)

START = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)


def alert(row_id: int, alert_type: str | None, title: str, at: float, images=()) -> dict:
    return {"id": row_id, "alert_id": f"alrt_{row_id}", "alert_type": alert_type, "title": title,
            "severity": "high", "fired_at": (START + timedelta(seconds=at)).isoformat(), "images": list(images)}


SPEC = {"use_case": "gate", "expect": [{"alert_type": "gate_left_open", "at_seconds": 10, "within_seconds": 20},
                                       {"alert_type": "gate_closed", "at_seconds": 40, "within_seconds": 20}],
        "no_alerts": ["gate_unknown"]}


def test_score_keeps_every_alert_with_its_offset_and_flags_false_positives():
    alerts = [alert(3, "gate_closed", "Gate closed at cam1", 45),          # newest first, as the inbox returns them
              alert(2, "gate_unknown", "Gate at cam1 cannot be judged", 20),
              alert(1, "gate_left_open", "Gate left open at cam1", 14, images=["snapshot"])]
    res = usecase_eval.score_clip(SPEC, alerts, START)
    assert (res["hits"], res["misses"], res["false_positives"]) == (2, 0, 1)
    assert res["delays_s"] == [4.0, 5.0]
    assert [a["offset_s"] for a in res["alerts"]] == [14.0, 20.0, 45.0]
    assert [a["false_positive"] for a in res["alerts"]] == [False, True, False]
    assert res["alerts"][0]["images"] == ["snapshot"] and res["alerts"][0]["id"] == 1


def test_report_has_an_index_a_page_per_use_case_and_the_snapshots(tmp_path):
    good = usecase_eval.score_clip(SPEC, [alert(1, "gate_left_open", "Gate left open at cam1", 14, ["snapshot"]),
                                          alert(3, "gate_closed", "Gate closed at cam1", 45)], START)
    good.update({"use_case": "gate", "clip": "gate_open_3min.mp4", "verdict": "PASS", "length_s": 70.0,
                 "started_at": START.isoformat(), "spec": SPEC})
    bad = usecase_eval.score_clip(SPEC, [alert(4, "gate_unknown", "Gate at cam1 cannot be judged", 20)], START)
    bad.update({"use_case": "gate", "clip": "gate_fog.mp4", "verdict": "FAIL", "length_s": 70.0,
                "started_at": START.isoformat(), "spec": SPEC})
    other = usecase_eval.score_clip({"use_case": "ppe", "expect": [{"alert_type": "ppe_missing"}]},
                                    [alert(5, "ppe_missing", "Person without helmet at cam1", 30, ["snapshot"])], START)
    other.update({"use_case": "ppe", "clip": "no_helmet.mp4", "verdict": "PASS", "length_s": 40.0,
                  "started_at": START.isoformat(), "spec": {"expect": [{"alert_type": "ppe_missing"}]}})
    fetched: list[tuple[int, str]] = []

    def fetch_image(row_id, name):
        fetched.append((row_id, name))
        return b"\xff\xd8jpeg" if row_id != 5 else None     # one snapshot is gone from the store

    pages = usecase_eval.write_report([good, bad, other], tmp_path, fetch_image=fetch_image,
                                      meta={"base": "http://nvr", "version": "0.1.9", "generated_at": "2026-10-24T10:30:00+00:00"})
    assert sorted(p.name for p in pages) == ["gate.md", "ppe.md"]
    index = (tmp_path / "README.md").read_text()
    assert "OpenNVR 0.1.9" in index
    assert "| gate | 2 | 1/2 | 2/4 (50 %) | 67 % | 1 | 5.0 s | 5.0 s | [gate.md](gate.md) |" in index
    assert "| ppe | 1 | 1/1 | 1/1 (100 %) | 100 % | 0 | n/a | n/a | [ppe.md](ppe.md) |" in index
    gate = (tmp_path / "gate.md").read_text()
    assert "| gate_open_3min.mp4 | 70.0 s | 2 | 2 | 0 | 0 | 4.0 s, 5.0 s | **PASS** |" in gate
    assert "| gate_fog.mp4 | 70.0 s | 2 | 0 | 2 | 1 | — | **FAIL** |" in gate
    assert "- `gate_left_open`, at 10 s (+20 s)" in gate and "- must not fire: `gate_unknown`" in gate
    assert "- 20.0 s: `gate_unknown` Gate at cam1 cannot be judged (high) — **false positive**" in gate
    assert "**Missed**" in gate and "![snapshot](gate/gate_open_3min-mp4-1-snapshot.jpg)" in gate
    assert (tmp_path / "gate" / "gate_open_3min-mp4-1-snapshot.jpg").read_bytes() == b"\xff\xd8jpeg"
    assert fetched == [(1, "snapshot"), (5, "snapshot")]
    assert "![" not in (tmp_path / "ppe.md").read_text()      # a missing snapshot is simply not embedded
