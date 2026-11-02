# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The acceptance tool's parsers, scorers and report, with no stack: the
measurements are dicts, the thresholds are the ones the doc names."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "acceptance", Path(__file__).resolve().parents[1] / "acceptance.py")
acc = importlib.util.module_from_spec(_spec)
sys.modules["acceptance"] = acc
_spec.loader.exec_module(acc)


# ── parsers ───────────────────────────────────────────────────────────

PROM = """
# HELP tier0_worker_up up
tier0_worker_up{camera="cam1"} 1
tier0_worker_up{camera="cam2"} 0
tier0_processing_fps{camera="cam1"} 1.9
tier0_target_fps{camera="cam1"} 2
tier0_detector_skipped_total{camera="cam1",reason="no_motion"} 40
tier0_detector_skipped_total{camera="cam1",reason="calibrating"} 10
tier0_detector_backend{backend="ort",effective="QNNExecutionProvider, CPUExecutionProvider",requested="QNNExecutionProvider(htp)"} 1
tier0_frame_age_seconds{camera="cam1"} NaN
"""


def test_prometheus_parser_reads_labels_sums_totals_and_drops_nan():
    rows = acc.parse_prom(PROM)
    assert acc.by_camera(rows, "tier0_worker_up") == {"cam1": 1.0, "cam2": 0.0}
    assert acc.by_camera(rows, "tier0_detector_skipped_total") == {"cam1": 50.0}, "split by reason → summed"
    assert acc.by_camera(rows, "tier0_frame_age_seconds") == {}, "NaN is not a value"
    assert acc.first_labels(rows, "tier0_detector_backend")["requested"] == "QNNExecutionProvider(htp)"
    assert acc.first_labels(rows, "nope") is None


def test_cpu_busy_from_two_proc_stat_readings():
    a = "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 2 3 4 5 6 7 8 9 10\n"
    b = "cpu  400 0 300 1300 0 0 0 0 0 0\n"
    # total +1000, idle +500 → 50 % busy
    assert acc.cpu_busy_percent(a, b) == 50.0
    assert acc.cpu_busy_percent(b, a) is None, "a counter that went backwards is not a sample"
    assert acc.cpu_busy_percent("", b) is None


def test_meminfo_used_share_excludes_caches():
    text = "MemTotal:       8000000 kB\nMemFree:        1000000 kB\nMemAvailable:   6000000 kB\n"
    share, used_mb, total_mb = acc.meminfo_used_share(text)
    assert (share, used_mb, total_mb) == (0.25, 1953, 7812)
    assert acc.meminfo_used_share("MemTotal: 1 kB\n") == (None, None, None)


def test_flavour_comes_from_the_compose_file_list():
    assert acc.flavour_from_compose_file("docker-compose.yml:docker-compose.accel-qualcomm.yml") == "qualcomm"
    assert acc.flavour_from_compose_file("docker-compose.yml:docker-compose.accel-intel.yml:x.yml") == "intel"
    assert acc.flavour_from_compose_file("docker-compose.yml") == "cpu"
    assert acc.flavour_from_compose_file(None) == "cpu"


def test_env_file_parser_strips_quotes_and_ignores_comments(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# c\nCOMPOSE_FILE="a.yml:b.yml"\nDETECT_FPS=2\nBROKEN\n\nX=\'y\'\n')
    env = acc.parse_env_file(f)
    assert env == {"COMPOSE_FILE": "a.yml:b.yml", "DETECT_FPS": "2", "X": "y"}
    assert acc.parse_env_file(tmp_path / "missing") == {}


def test_last_json_object_survives_the_benchs_human_summary():
    out = '[yolo26n] QNN: median=14.2 ms p90=16 ms\n{"a": {"b": 1}}\n{"median_ms": 14.2, "fell_back": false}\n'
    assert acc.last_json_object(out) == {"median_ms": 14.2, "fell_back": False}
    assert acc.last_json_object("no json here") is None


def test_bench_output_and_compose_ps_parsers():
    err = "frame=  300 fps=0.0 q=-0.0 Lsize=N/A\nbench: utime=1.250s stime=0.750s rtime=0.9s\nbench: maxrss=1kB\n"
    assert acc.parse_bench_output(err) == 2.0
    assert acc.parse_bench_output("Option skip_frame: nonref not found\n") is None, "a failed ffmpeg is no sample"
    one_per_line = '{"Name":"x","Image":"ghcr.io/open-nvr/detect-pipeline:main-qualcomm"}\n'
    assert acc.image_from_ps_json(one_per_line) == "ghcr.io/open-nvr/detect-pipeline:main-qualcomm"
    assert acc.image_from_ps_json(json.dumps([{"Image": "a:b"}])) == "a:b"
    assert acc.image_from_ps_json("") is None


# ── scorers ───────────────────────────────────────────────────────────

def test_accelerator_passes_only_when_the_requested_provider_really_runs():
    ok = acc.score_accelerator("qualcomm", "QNNExecutionProvider(htp)",
                               "QNNExecutionProvider, CPUExecutionProvider")
    assert ok.status == "pass"
    fell = acc.score_accelerator("qualcomm", "QNNExecutionProvider(htp)", "CPUExecutionProvider")
    assert fell.status == "fail" and "silent fallback" in fell.detail
    cpu = acc.score_accelerator("cpu", "CPUExecutionProvider", "CPUExecutionProvider")
    assert cpu.status == "pass" and "CPU flavour" in cpu.detail
    assert acc.score_accelerator("intel", None, None).status == "unknown"


def test_detector_latency_turns_ms_into_cameras_at_the_detect_fps():
    c = acc.score_detector_latency(15.0, 2.0)            # 1000/15/2 = 33 cameras
    assert c.status == "pass" and c.measured["cameras_detector_alone"] == 33
    c = acc.score_detector_latency(100.0, 2.0)           # 5 cameras
    assert c.status == "warn" and c.measured["cameras_detector_alone"] == 5
    c = acc.score_detector_latency(200.0, 2.0)           # 2 cameras
    assert c.status == "fail"
    c = acc.score_detector_latency(200.0, 2.0, pool=4)   # a pool multiplies
    assert c.measured["cameras_detector_alone"] == 8 and c.status == "pass"
    assert acc.score_detector_latency(None, 2.0).status == "unknown"


def test_decode_cost_is_scored_on_the_default_substream_mode():
    costs = {"720p-h264": {"full": 8.0, "nokey": 1.0},
             "1080p-h265": {"full": 27.0, "nokey": 3.0}}
    c = acc.score_decode_cost(costs)
    assert c.status == "pass" and "8 % of a core" in c.detail and "8x cheaper" in c.detail
    assert "1080p H.265 main stream" in c.detail
    assert "cheaper" not in acc.score_decode_cost({"720p-h264": {"full": 1.0, "nokey": 1.8}}).detail
    assert acc.score_decode_cost({"720p-h264": {"full": 22.0}}).status == "warn"
    assert acc.score_decode_cost({"720p-h264": {"full": 45.0}}).status == "fail"
    assert acc.score_decode_cost({"720p-h264": {"full": None}}).status == "unknown"
    assert acc.score_decode_cost({}).status == "unknown"


def test_capacity_scales_linearly_to_the_70_percent_ceiling():
    assert acc.estimate_capacity(10, 35.0) == 20
    assert acc.estimate_capacity(0, 35.0) is None
    assert acc.estimate_capacity(10, None) is None


def _live(**over):
    base = {"minutes": 5, "cpu_busy_mean": 30.0, "cpu_busy_max": 40.0, "load1_max": 2.0,
            "cameras": 4, "cameras_up": 4,
            "fps_ratio_by_camera": {"cam1": 0.98, "cam2": 0.95},
            "frames_delta": 2400, "frames_dropped_delta": 0, "worker_restarts_delta": 0,
            "mediamtx_events": 0, "thermal_max_c": 61.0, "thermal_zone": "cpu-0",
            "mem_used_share_max": 0.4, "mem_used_mb_max": 3200, "mem_total_mb": 8000, "swap_used_mb": 0}
    base.update(over)
    return base


def test_live_window_all_green():
    by_id = {c.id: c for c in acc.score_live(_live())}
    assert {c.status for c in by_id.values()} == {"pass"}
    assert by_id["cpu_load"].measured["cameras_at_70pct_estimate"] == 9
    assert by_id["keep_up"].measured["worst_camera"] == "cam2"


def test_live_window_flags_each_failure_mode():
    by_id = {c.id: c for c in acc.score_live(_live(
        cpu_busy_mean=88.0, fps_ratio_by_camera={"cam1": 0.5},
        frames_dropped_delta=200, mediamtx_events=3, thermal_max_c=91.0, mem_used_share_max=0.95))}
    assert by_id["cpu_load"].status == "fail"
    assert by_id["keep_up"].status == "fail"
    assert by_id["frames"].status == "fail" and by_id["frames"].measured["drop_share"] == 0.0833
    assert by_id["streams"].status == "warn"
    assert by_id["thermal"].status == "fail"
    assert by_id["memory"].status == "fail"
    warn = {c.id: c for c in acc.score_live(_live(cpu_busy_mean=75.0, fps_ratio_by_camera={"c": 0.8},
                                                   worker_restarts_delta=1, thermal_max_c=85.0))}
    assert (warn["cpu_load"].status, warn["keep_up"].status, warn["frames"].status,
            warn["thermal"].status) == ("warn", "warn", "warn", "warn")


def test_live_window_with_no_camera_up_is_informational_not_a_pass():
    by_id = {c.id: c for c in acc.score_live(_live(cameras_up=0, fps_ratio_by_camera={}, frames_delta=0))}
    assert by_id["cpu_load"].status == "info" and "idle stack" in by_id["cpu_load"].detail
    assert by_id["keep_up"].status == "unknown"
    assert by_id["frames"].status == "unknown"


# ── grade and report ──────────────────────────────────────────────────

def test_grade_counts_only_scored_checks_and_names_the_unknown_ones():
    checks = [acc.Check("a", "A", "pass"), acc.Check("b", "B", "warn"), acc.Check("c", "C", "fail"),
              acc.Check("d", "D", "unknown"), acc.Check("e", "E", "info")]
    g = acc.grade(checks)
    assert (g["points"], g["max"], g["percent"], g["grade"], g["unknown"]) == (1.5, 3, 50.0, "F", ["d"])
    assert acc.grade([acc.Check("a", "A", "pass")] * 9 + [acc.Check("b", "B", "warn")])["grade"] == "A"
    assert acc.grade([acc.Check("a", "A", "pass")] * 3 + [acc.Check("b", "B", "fail")])["grade"] == "B"
    assert acc.grade([acc.Check("d", "D", "unknown")])["grade"] == "n/a"


def test_markdown_report_carries_grade_statuses_and_nested_measurements():
    checks = [acc.score_decode_cost({"720p-h264": {"full": 8.0, "nokey": 1.0}}),
              acc.Check("x", "Something", "unknown", {}, "not measured")]
    report = {"tool_version": "t", "started": "2026-11-02T20:00:00+00:00",
              "host": {"hostname": "box", "cpu_model": "QCS8275", "cores": 8, "mem_total_mb": 7800,
                       "flavour": "qualcomm", "image_tag": "main-qualcomm"},
              "checks": [c.as_dict() for c in checks], "grade": acc.grade(checks)}
    md = acc.render_markdown(report)
    assert "# OpenNVR hardware acceptance — box" in md
    assert "Grade **A**" in md and "Not measured: x" in md
    assert "720p-h264.full=8" in md and "**PASS**" in md and "**UNKNOWN**" in md
    assert "docs/ACCEPTANCE.md" in md
