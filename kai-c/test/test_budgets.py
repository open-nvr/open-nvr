# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""RFC-0002 Phase 5: per-(adapter, camera) budgets — shed-and-report.

The discipline under test is Tier-0's region-shedding rule applied to
Tier-1: a runaway consumer degrades ITSELF (429s until its window
drains) and the shed is REPORTED (counter, metrics lines, warning) —
never a silent drop, and never collateral damage to another camera or
adapter.
"""
from __future__ import annotations

from kai_c.budgets import (
    DEFAULT_PER_CAMERA_PER_MIN,
    MAX_BUCKETS,
    SkillBudgets,
    parse_overrides,
)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _budgets(limit=3, **kw):
    return SkillBudgets(default_per_min=limit, clock=_Clock(), **kw)


def test_admits_until_the_limit_then_sheds():
    b = SkillBudgets(default_per_min=3, clock=_Clock())
    assert all(b.admit("ocr", "cam1") for _ in range(3))
    assert b.admit("ocr", "cam1") is False
    assert b.shed_total() == {("ocr", "cam1"): 1}


def test_window_slides_and_readmits():
    clock = _Clock()
    b = SkillBudgets(default_per_min=2, clock=clock)
    assert b.admit("ocr", "cam1") and b.admit("ocr", "cam1")
    assert b.admit("ocr", "cam1") is False
    clock.now += 61.0
    assert b.admit("ocr", "cam1") is True


def test_buckets_are_isolated_per_adapter_and_camera():
    b = SkillBudgets(default_per_min=1, clock=_Clock())
    assert b.admit("ocr", "cam1")
    assert b.admit("ocr", "cam1") is False       # cam1 over budget…
    assert b.admit("ocr", "cam2") is True        # …cam2 unaffected
    assert b.admit("caption", "cam1") is True    # …other skill unaffected


def test_no_camera_and_zero_limit_are_exempt():
    b = SkillBudgets(default_per_min=1, clock=_Clock())
    for _ in range(10):
        assert b.admit("ocr", None) is True      # probes never starve
        assert b.admit("ocr", "") is True
    z = SkillBudgets(default_per_min=0, clock=_Clock())
    for _ in range(10):
        assert z.admit("ocr", "cam1") is True    # 0 = budgeting off
    assert z.shed_total() == {}


def test_per_adapter_overrides_win():
    b = SkillBudgets(default_per_min=100,
                     overrides={"fast_plate_ocr": 1, "caption": 0},
                     clock=_Clock())
    assert b.limit_for("fast_plate_ocr") == 1
    assert b.limit_for("caption") == 0
    assert b.limit_for("yolov8") == 100
    assert b.admit("fast_plate_ocr", "cam1")
    assert b.admit("fast_plate_ocr", "cam1") is False
    for _ in range(5):
        assert b.admit("caption", "cam1") is True


def test_parse_overrides_skips_garbage():
    assert parse_overrides("a=30, b = 0 ,broken,c=x,d=-5") == {
        "a": 30, "b": 0, "d": 0}
    assert parse_overrides(None) == {}
    assert parse_overrides("") == {}


def test_from_env_reads_default_and_overrides():
    b = SkillBudgets.from_env({
        "KAIC_BUDGET_PER_CAMERA_PER_MIN": "7",
        "KAIC_BUDGET_OVERRIDES": "fast_plate_ocr=2",
    })
    assert b.default_per_min == 7
    assert b.overrides == {"fast_plate_ocr": 2}
    b2 = SkillBudgets.from_env({"KAIC_BUDGET_PER_CAMERA_PER_MIN": "junk"})
    assert b2.default_per_min == DEFAULT_PER_CAMERA_PER_MIN


def test_metrics_render_reports_the_shed():
    b = SkillBudgets(default_per_min=1, clock=_Clock())
    assert b.render_metrics() == ""              # nothing shed, no series
    b.admit("ocr", "cam1")
    b.admit("ocr", "cam1")
    b.admit("ocr", "cam1")
    out = b.render_metrics()
    assert "# TYPE kaic_budget_shed_total counter" in out
    assert 'kaic_budget_shed_total{adapter="ocr",camera="cam1"} 2' in out


def test_bucket_cap_evicts_stalest_not_unbounded():
    clock = _Clock()
    b = SkillBudgets(default_per_min=10, clock=clock)
    for i in range(MAX_BUCKETS + 50):
        clock.now += 0.001
        b.admit("ocr", f"cam{i}")
    assert len(b._windows) <= MAX_BUCKETS


def test_shed_warning_is_rate_limited(caplog):
    import logging
    clock = _Clock()
    b = SkillBudgets(default_per_min=1, clock=clock)
    b.admit("ocr", "cam1")
    with caplog.at_level(logging.WARNING, logger="kai-c.budgets"):
        for _ in range(50):
            b.admit("ocr", "cam1")               # a shed storm
    warnings = [r for r in caplog.records if "budget shed" in r.message]
    assert len(warnings) == 1                    # reports, doesn't flood
