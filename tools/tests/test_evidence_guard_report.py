# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The guard report's pure parts: a dump of records in, counts and floors out."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "evidence_guard_report", Path(__file__).resolve().parents[1] / "evidence_guard_report.py")
rep = importlib.util.module_from_spec(_spec)
sys.modules["evidence_guard_report"] = rep
_spec.loader.exec_module(rep)


def _rec(score, skew, accepted, reason="", ref=(None, None)):
    return {"score": score, "skew_s": skew, "accepted": accepted, "reason": reason,
            "sim_main_ref": ref[0], "sim_sub_ref": ref[1]}


def test_summary_counts_reasons_floors_and_timing(tmp_path):
    d = tmp_path / "cam23"; d.mkdir()
    recs = [_rec(0.95, 0.1, True), _rec(0.88, 0.3, True), _rec(0.42, 1.8, False, "low_corr"),
            _rec(0.12, 1.9, False, "low_corr"), _rec(None, 0.2, False, "flat_main"),
            _rec(0.91, 0.2, True, ref=(0.9, 0.9))]
    for i, r in enumerate(recs):
        (d / f"{i:013d}_{i:04d}.json").write_text(json.dumps(r))
    loaded = rep.load(tmp_path)
    assert len(loaded) == 6 and loaded[0]["_camera"] == "cam23"
    s = rep.summarise(loaded)
    assert s["by_reason"] == {"accepted": 3, "low_corr": 2, "flat_main": 1}
    assert s["scored"] == 5 and s["refused_at_floor"][0.5] == 2 and s["refused_at_floor"][0.9] if 0.9 in s["refused_at_floor"] else True
    assert s["refused_at_floor"][0.8] == 2 and s["refused_at_floor"][0.3] == 1
    assert s["score_near_vs_far"]["near_median"] > 0.8 and s["score_near_vs_far"]["far_median"] < 0.5
    assert s["accepted_but_background_like"] == 1
    text = rep.render(s, "cam23")
    assert "timing drives the refusals" in text and "low_corr 2" in text and "0.5 → 2 (40 %)" in text


def test_empty_dump_is_reported_not_crashed(tmp_path, capsys):
    assert rep.main([str(tmp_path)]) == 1
    assert "no attempts" in capsys.readouterr().out
