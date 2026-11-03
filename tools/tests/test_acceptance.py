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


# ── camera suite scorers ──────────────────────────────────────────────

def test_report_urls_carry_no_credentials():
    assert acc.redact_url("rtsp://admin:s3cret@10.0.0.5:554/cam/realmonitor?channel=1") == \
        "rtsp://10.0.0.5:554/cam/realmonitor?channel=1"
    assert acc.redact_url("rtsp://10.0.0.5/x") == "rtsp://10.0.0.5/x"
    assert acc.redact_url(None) is None


MAIN = {"codec": "hevc", "width": 1920, "height": 1080, "fps": 15.0}
SUB = {"codec": "h264", "width": 704, "height": 576, "fps": 10.0}


def test_streams_and_sub_fit():
    assert acc.score_streams(MAIN, SUB).status == "pass"
    only_main = acc.score_streams(MAIN, None)
    assert only_main.status == "warn" and "main stream" in only_main.detail
    assert acc.score_streams(None, None).status == "fail"
    assert acc.score_sub_fit(SUB, 2.0).status == "pass"
    assert acc.score_sub_fit(dict(SUB, height=240), 2.0).status == "warn"
    assert acc.score_sub_fit(dict(SUB, fps=1.0), 2.0).status == "warn"
    assert acc.score_sub_fit(None, 2.0).status == "unknown"


def test_gop_uses_the_pipelines_jitter_allowance_then_the_smart_codec_ceiling():
    assert acc.score_gop(2.03, 2.0).status == "pass", "a nominal 2 s camera measures a little over"
    assert acc.score_gop(2.3, 2.0).status == "pass"
    warn = acc.score_gop(4.0, 2.0)
    assert warn.status == "warn" and "every main frame" in warn.detail
    fail = acc.score_gop(30.0, 2.0)
    assert fail.status == "fail" and "smart codec" in fail.detail
    assert acc.score_gop(None, 2.0).status == "unknown"
    assert acc.score_gop(2.8, 2.5).status == "pass", "the limit is the configured one"


def test_fov_thresholds_come_from_the_measured_camera():
    assert acc.score_fov(0.998).status == "pass"            # CP Plus, same view
    assert acc.score_fov(0.80).status == "warn"
    assert acc.score_fov(0.68).status == "fail"             # the simulated 80 % crop
    assert acc.score_fov(None).status == "unknown"


def test_gate_floor_is_scored_only_for_a_still_scene_and_names_an_osd():
    hot = {"share": 0.97, "where": "row 1 col 10 of 10x10", "polygon": [[0.9, 0.0], [1.0, 0.0], [1.0, 0.1], [0.9, 0.1]]}
    c = acc.score_gate_floor(0.94, hot, quiet=True)
    assert c.status == "fail" and "burned-in clock" in c.detail and "[0.9, 0.0]" in c.detail
    assert c.measured["hotspot"] == "row 1 col 10 of 10x10"
    assert acc.score_gate_floor(0.0, None, quiet=True).status == "pass"
    assert acc.score_gate_floor(0.12, {"share": 0.3, "where": "x", "polygon": []}, quiet=True).status == "warn"
    info = acc.score_gate_floor(0.94, hot, quiet=False)
    assert info.status == "info" and "--quiet" in info.detail
    assert acc.score_gate_floor(None, None, quiet=True).status == "unknown"


def _cam(**over):
    base = {"measured": True, "minutes": 5, "up": True, "fps_ratio": 0.96, "frames": 600,
            "gate_open_share": 0.0, "restarts": 0, "flips": 2, "evidence_main": 0, "evidence_detect": 0,
            "evidence_mismatch": 0, "keyframes_only": 1.0, "gop_s": 2.03}
    base.update(over)
    return base


def test_camera_live_green_still_scene_without_evidence():
    by_id = {c.id: c for c in acc.score_camera_live(_cam(), quiet=True)}
    assert by_id["cam_live"].status == "pass" and by_id["cam_feed"].status == "pass"
    assert by_id["cam_gate"].status == "pass"
    assert by_id["cam_evidence"].status == "unknown" and "walk through" in by_id["cam_evidence"].detail


def test_camera_live_flags_down_restarts_noise_and_refused_evidence():
    by_id = {c.id: c for c in acc.score_camera_live(
        _cam(up=False, restarts=5, gate_open_share=0.9, evidence_main=20, evidence_detect=2,
             evidence_mismatch=8), quiet=True)}
    assert by_id["cam_live"].status == "fail"
    assert by_id["cam_feed"].status == "fail"
    assert by_id["cam_gate"].status == "fail"
    assert by_id["cam_evidence"].status == "warn" and by_id["cam_evidence"].measured["refused_share"] == 0.286
    walk = acc.score_camera_live(_cam(evidence_main=1, evidence_detect=16, evidence_mismatch=16), True)[-1]
    assert walk.status == "fail" and walk.measured["refused_share"] == 0.941, "16 of 17 attempts, not 16 of 33"
    assert "wants full decode" in walk.detail
    ok = {c.id: c for c in acc.score_camera_live(_cam(evidence_main=30, evidence_mismatch=1), quiet=False)}
    assert ok["cam_evidence"].status == "pass" and ok["cam_gate"].status == "info"
    assert acc.score_camera_live({"measured": False, "reason": "nope"}, True)[0].status == "unknown"


def test_camera_markdown_header_names_the_camera_not_the_host():
    checks = [acc.score_fov(0.998)]
    report = {"suite": "camera", "tool_version": "t", "started": "2026-11-03T10:00:00+00:00",
              "camera": {"name": "lobby", "main": "rtsp://h/main", "sub": "rtsp://h/sub", "camera_id": "cam23"},
              "host": {"hostname": "box", "image_tag": "main-qualcomm"},
              "checks": [c.as_dict() for c in checks], "grade": acc.grade(checks)}
    md = acc.render_markdown(report)
    assert "# OpenNVR camera acceptance — lobby" in md and "pipeline camera `cam23`" in md
    assert "--quiet" in md
