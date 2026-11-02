#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OpenNVR acceptance — does this hardware carry OpenNVR well?

A customer-runnable, read-only measurement of a box running the OpenNVR
stack, scored against the thresholds we hold our own reference hardware
to, written as a report you can send us (or keep):

    python3 tools/acceptance.py hardware                  # ~8 min: benches + a 5 min live window
    python3 tools/acceptance.py hardware --minutes 30     # longer live window (more cameras, more truth)
    python3 tools/acceptance.py hardware --skip live      # benches only
    python3 tools/acceptance.py hardware --clip ~/footage/lobby.mp4   # decode cost of YOUR footage

It runs from the OpenNVR checkout on the box (the folder with
docker-compose.yml and .env), talks to the running stack through
``docker compose``, measures the host through /proc and /sys, and writes
``acceptance-reports/hardware-<host>-<UTC>.json`` and ``.md``. It stores no
credentials, changes no configuration and adds no cameras; the only things
it creates are two short synthetic clips inside the detect-pipeline
container's /tmp. Linux hosts only (the macOS/Windows Docker VMs expose no
hardware to measure).

Every check is pass / warn / fail with the measured number beside it, or
``unknown`` when it could not be measured — never a pass it did not earn.
The pure scoring and report code is unit-tested (tools/tests); the
measurements need the stack.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"

# ── thresholds (documented in docs/ACCEPTANCE.md; change them there too) ──

#: Host CPU busy over the live window. 70 % is the ceiling the benchmark
#: protocol in docs/HARDWARE.md adds cameras up to.
CPU_BUSY_WARN, CPU_BUSY_FAIL = 70.0, 85.0
#: Worst camera's achieved/target fps over the live window.
FPS_RATIO_WARN, FPS_RATIO_FAIL = 0.9, 0.7
#: Decoded frames detection never reached, as a share of frames decoded.
DROP_SHARE_FAIL = 0.05
#: Hottest thermal zone over the window.
THERMAL_WARN_C, THERMAL_FAIL_C = 80.0, 90.0
#: Memory in use (host-wide, without caches) as a share of total.
MEM_WARN, MEM_FAIL = 0.80, 0.92
#: Detector ms/frame → cameras the detector alone could serve at DETECT_FPS;
#: a box should carry at least this many before the detector is the limit.
DETECTOR_CAMERAS_WARN, DETECTOR_CAMERAS_FAIL = 8, 4
#: Software decode of one 720p H.264 substream (every frame — what the
#: pipeline's default -skip_frame nonref costs on an I/P-only camera
#: stream), as % of one core. A 2020 mini-PC does ~5 %.
DECODE_720P_WARN, DECODE_720P_FAIL = 15.0, 30.0

SAMPLE_EVERY_S = 5.0


# ── data ──────────────────────────────────────────────────────────────

@dataclass
class Check:
    id: str
    title: str
    status: str                       # pass | warn | fail | unknown | info
    measured: dict = field(default_factory=dict)
    detail: str = ""
    threshold: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "status": self.status,
                "measured": self.measured, "detail": self.detail, "threshold": self.threshold}


STATUS_POINTS = {"pass": 1.0, "warn": 0.5, "fail": 0.0}


def grade(checks: list[Check]) -> dict:
    """Points over the SCORED checks (info and unknown carry no weight —
    an unmeasured check cannot help the grade); letter from the share."""
    scored = [c for c in checks if c.status in STATUS_POINTS]
    points = sum(STATUS_POINTS[c.status] for c in scored)
    pct = round(100.0 * points / len(scored), 1) if scored else None
    if pct is None:
        letter = "n/a"
    elif pct >= 90:
        letter = "A"
    elif pct >= 75:
        letter = "B"
    elif pct >= 60:
        letter = "C"
    else:
        letter = "F"
    unknown = [c.id for c in checks if c.status == "unknown"]
    return {"points": points, "max": len(scored), "percent": pct, "grade": letter,
            "unknown": unknown}


# ── small parsers (pure, tested) ──────────────────────────────────────

_PROM_LINE = re.compile(r'^([A-Za-z_:][A-Za-z0-9_:]*)(\{([^}]*)\})?\s+(-?[0-9.eE+naNIif]+)\s*$')
_PROM_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_prom(text: str) -> list[tuple[str, dict, float]]:
    """Prometheus text exposition → ``(name, labels, value)`` rows."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _PROM_LINE.match(line)
        if not m:
            continue
        labels = {k: v.replace('\\"', '"') for k, v in _PROM_LABEL.findall(m.group(3) or "")}
        try:
            value = float(m.group(4))
        except ValueError:
            continue
        if value != value:                       # NaN
            continue
        rows.append((m.group(1), labels, value))
    return rows


def by_camera(rows, name: str, label: str = "camera") -> dict[str, float]:
    out: dict[str, float] = {}
    for n, labels, v in rows:
        if n == name and label in labels:
            out[labels[label]] = out.get(labels[label], 0.0) + v if n.endswith("_total") else v
    return out


def first_labels(rows, name: str) -> dict | None:
    for n, labels, _ in rows:
        if n == name:
            return labels
    return None


def cpu_busy_percent(stat_a: str, stat_b: str) -> float | None:
    """Host CPU busy % between two readings of /proc/stat's ``cpu`` line."""
    def split(s):
        for line in s.splitlines():
            if line.startswith("cpu "):
                f = [int(x) for x in line.split()[1:]]
                idle = f[3] + (f[4] if len(f) > 4 else 0)       # idle + iowait
                return sum(f), idle
        return None
    a, b = split(stat_a), split(stat_b)
    if not a or not b or b[0] <= a[0]:
        return None
    total = b[0] - a[0]
    return round(100.0 * (1.0 - (b[1] - a[1]) / total), 1)


def meminfo_used_share(text: str) -> tuple[float | None, int | None, int | None]:
    """``(used share, used MB, total MB)`` from /proc/meminfo, caches excluded."""
    vals = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            try:
                vals[parts[0][:-1]] = int(parts[1])
            except ValueError:
                pass
    total, avail = vals.get("MemTotal"), vals.get("MemAvailable")
    if not total or avail is None:
        return None, None, None
    used = total - avail
    return round(used / total, 3), used // 1024, total // 1024


def flavour_from_compose_file(compose_file: str | None) -> str:
    """The accelerator flavour the stack was started with, from COMPOSE_FILE."""
    cf = compose_file or ""
    if "accel-qualcomm" in cf:
        return "qualcomm"
    if "accel-intel" in cf:
        return "intel"
    if "accel-nvidia" in cf:
        return "nvidia"
    return "cpu"


def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a dotenv file (quotes stripped). Only the keys
    the report names are ever read out of it; nothing else leaves."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def last_json_object(text: str) -> dict | None:
    """The last top-level ``{...}`` in a command's stdout (the bench prints
    its JSON after a human summary)."""
    end = text.rfind("}")
    while end != -1:
        depth, i = 0, end
        while i >= 0:
            if text[i] == "}":
                depth += 1
            elif text[i] == "{":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:end + 1])
                    except ValueError:
                        break
            i -= 1
        end = text.rfind("}", 0, end)
    return None


def image_from_ps_json(text: str) -> str | None:
    """``docker compose ps --format json`` prints one object per line on
    compose ≥ 2.21 and a JSON array before that; the running image either way."""
    rows: list = []
    try:
        doc = json.loads(text)
        rows = doc if isinstance(doc, list) else [doc]
    except ValueError:
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    for row in rows:
        if isinstance(row, dict) and row.get("Image"):
            return str(row["Image"])
    return None


_BENCH = re.compile(r"bench:\s+utime=([0-9.]+)s\s+stime=([0-9.]+)s")


def parse_bench_output(text: str) -> float | None:
    """CPU seconds (user + system) from ffmpeg's own ``-benchmark`` line;
    None when ffmpeg never got that far (a bad option, a missing decoder)."""
    m = None
    for m in _BENCH.finditer(text):
        pass
    return float(m.group(1)) + float(m.group(2)) if m else None


# ── scorers (pure, tested) ────────────────────────────────────────────

def score_accelerator(flavour: str, requested: str | None, effective: str | None) -> Check:
    """Is the detector really on the accelerator the flavour promises?"""
    measured = {"flavour": flavour, "requested": requested, "effective": effective}
    if requested is None and effective is None:
        return Check("accelerator", "Detector backend", "unknown", measured,
                     "the detect-pipeline exported no tier0_detector_backend — is it running?")
    eff_first = (effective or "").split(",")[0].strip()
    req_first = (requested or "").split(",")[0].strip().split("(")[0]
    on_cpu = (not eff_first) or eff_first == "CPUExecutionProvider"
    if flavour == "cpu":
        return Check("accelerator", "Detector backend", "pass", measured,
                     f"CPU flavour; the detector runs on {eff_first or 'the CPU'} as configured. "
                     "An Intel iGPU/NPU or Qualcomm Hexagon host would carry more cameras on an "
                     "accelerated flavour (OPENNVR_ACCEL=auto picks one).",
                     "accelerated flavour: the first effective provider must be the requested one")
    if on_cpu or (req_first and eff_first != req_first):
        return Check("accelerator", "Detector backend", "fail", measured,
                     f"the {flavour} flavour asked for {requested} but the detector runs on "
                     f"{eff_first or 'the CPU'} — a silent fallback. Every detector number below is "
                     "a CPU number. Check the device node is passed to the container "
                     "(docs/HARDWARE.md, 'Is it really accelerated?').",
                     "first effective provider == requested")
    return Check("accelerator", "Detector backend", "pass", measured,
                 f"{flavour} flavour, detector on {eff_first}.",
                 "first effective provider == requested")


def score_detector_latency(median_ms: float | None, fps: float, pool: int = 1,
                           extra: dict | None = None) -> Check:
    """Detector ms/frame → cameras the detector alone could serve at ``fps``."""
    measured = dict(extra or {})
    if median_ms is None or median_ms <= 0:
        return Check("detector_latency", "Detector latency", "unknown", measured,
                     "the in-image bench produced no latency figure")
    cams = int(1000.0 / median_ms / max(fps, 0.1)) * max(1, pool)
    measured.update({"median_ms": median_ms, "detect_fps": fps, "pool": pool,
                     "cameras_detector_alone": cams})
    thr = (f"cameras the detector alone could serve at {fps:g} fps: "
           f"≥{DETECTOR_CAMERAS_WARN} pass, ≥{DETECTOR_CAMERAS_FAIL} warn")
    if cams >= DETECTOR_CAMERAS_WARN:
        status = "pass"
    elif cams >= DETECTOR_CAMERAS_FAIL:
        status = "warn"
    else:
        status = "fail"
    return Check("detector_latency", "Detector latency", status, measured,
                 f"{median_ms:g} ms per frame → the detector alone could serve about {cams} "
                 f"cameras at {fps:g} fps (decode is the usual limit before that).", thr)


def score_decode_cost(costs: dict[str, dict[str, float | None]]) -> Check:
    """``costs[clip][mode]`` = % of one core at real time. Scored on the
    720p H.264 substream decoded in full — what the pipeline's default
    ``-skip_frame nonref`` costs on a camera stream, which has no B-frames
    to skip."""
    measured = {clip: {m: v for m, v in modes.items()} for clip, modes in costs.items()}
    sub = (costs.get("720p-h264") or {}).get("full")
    if sub is None:
        return Check("decode_cost", "Software decode cost", "unknown", measured,
                     "the 720p H.264 decode could not be timed")
    thr = f"720p H.264: ≤{DECODE_720P_WARN:g} % of a core pass, ≤{DECODE_720P_FAIL:g} % warn"
    if sub <= DECODE_720P_WARN:
        status = "pass"
    elif sub <= DECODE_720P_FAIL:
        status = "warn"
    else:
        status = "fail"
    per_core = int(100.0 / sub) if sub > 0 else None
    hint = ""
    nokey = (costs.get("720p-h264") or {}).get("nokey")
    if nokey is not None and 0 < nokey < sub:
        hint = f" Keyframe-only (idle) decode is {nokey:g} %, {sub / nokey:.0f}x cheaper."
    elif nokey is not None:
        hint = f" Keyframe-only (idle) decode is {nokey:g} %."
    main = (costs.get("1080p-h265") or {}).get("full")
    if main is not None:
        hint += f" A 1080p H.265 main stream decoded in full costs {main:g} % of a core."
    return Check("decode_cost", "Software decode cost", status, measured,
                 f"one 720p H.264 substream costs {sub:g} % of a core (≈{per_core} per core)."
                 f"{hint}", thr)


def estimate_capacity(cameras_up: int, cpu_busy_mean: float | None) -> int | None:
    """Cameras this box could carry at the 70 % ceiling, scaling linearly
    from what it carries now. Rough on purpose — the live window measures
    THESE cameras, not an average camera."""
    if not cameras_up or cpu_busy_mean is None or cpu_busy_mean <= 0:
        return None
    return int(cameras_up * CPU_BUSY_WARN / cpu_busy_mean)


def score_live(summary: dict) -> list[Check]:
    """The live window: host load, per-camera keep-up, drops, restarts,
    thermal and memory. ``summary`` is what ``live_window`` collected."""
    checks: list[Check] = []
    busy = summary.get("cpu_busy_mean")
    cams_up = summary.get("cameras_up", 0)
    cams = summary.get("cameras", 0)
    cap = estimate_capacity(cams_up, busy)
    m = {"cpu_busy_mean": busy, "cpu_busy_max": summary.get("cpu_busy_max"),
         "load1_max": summary.get("load1_max"), "cameras": cams, "cameras_up": cams_up,
         "cameras_at_70pct_estimate": cap, "minutes": summary.get("minutes")}
    if busy is None:
        checks.append(Check("cpu_load", "Host CPU over the window", "unknown", m,
                            "no /proc/stat samples"))
    else:
        status = "pass" if busy < CPU_BUSY_WARN else ("warn" if busy < CPU_BUSY_FAIL else "fail")
        if cams_up == 0:
            status = "info"
        detail = f"{busy:g} % busy (max {m['cpu_busy_max']:g} %) with {cams_up} of {cams} cameras up"
        if cap is not None:
            detail += f"; at this rate about {cap} cameras would reach {CPU_BUSY_WARN:g} %"
        if cams_up == 0:
            detail += " — no camera was up, so this is the idle stack, not a capacity number"
        checks.append(Check("cpu_load", "Host CPU over the window", status, m, detail + ".",
                            f"mean busy <{CPU_BUSY_WARN:g} % pass, <{CPU_BUSY_FAIL:g} % warn"))

    ratios = summary.get("fps_ratio_by_camera") or {}
    if ratios:
        worst_cam = min(ratios, key=ratios.get)
        worst = ratios[worst_cam]
        status = "pass" if worst >= FPS_RATIO_WARN else ("warn" if worst >= FPS_RATIO_FAIL else "fail")
        checks.append(Check("keep_up", "Cameras keep their target fps", status,
                            {"worst_camera": worst_cam, "worst_ratio": round(worst, 3),
                             "ratios": {k: round(v, 3) for k, v in ratios.items()}},
                            f"worst camera {worst_cam} achieved {worst:.0%} of its target fps.",
                            f"worst achieved/target ≥{FPS_RATIO_WARN:g} pass, ≥{FPS_RATIO_FAIL:g} warn"))
    else:
        checks.append(Check("keep_up", "Cameras keep their target fps", "unknown",
                            {"cameras_up": cams_up}, "no camera reported an fps over the window"))

    frames = summary.get("frames_delta", 0)
    dropped = summary.get("frames_dropped_delta", 0)
    restarts = summary.get("worker_restarts_delta", 0)
    m2 = {"frames": frames, "frames_dropped": dropped, "worker_restarts": restarts}
    if frames <= 0:
        checks.append(Check("frames", "Frames decoded reach detection", "unknown", m2,
                            "no frames were decoded over the window"))
    else:
        share = dropped / frames
        if dropped == 0 and restarts == 0:
            status, why = "pass", "every decoded frame reached detection and no worker restarted"
        elif share > DROP_SHARE_FAIL:
            status, why = "fail", f"{share:.1%} of decoded frames were dropped before detection — the box is behind"
        else:
            status, why = "warn", f"{dropped} frames dropped, {restarts} worker restarts"
        m2["drop_share"] = round(share, 4)
        checks.append(Check("frames", "Frames decoded reach detection", status, m2, why + ".",
                            f"0 drops and 0 restarts pass; drops >{DROP_SHARE_FAIL:.0%} fail"))

    mtx = summary.get("mediamtx_events")
    if mtx is not None:
        status = "pass" if mtx == 0 else "warn"
        checks.append(Check("streams", "Stream server stable", status, {"mediamtx_reloads_or_errors": mtx},
                            f"{mtx} MediaMTX reload/close/error lines over the window.",
                            "0 pass, otherwise warn"))

    temp = summary.get("thermal_max_c")
    if temp is None:
        checks.append(Check("thermal", "Thermal headroom", "info", {},
                            "no thermal zones readable on this host"))
    else:
        status = "pass" if temp < THERMAL_WARN_C else ("warn" if temp < THERMAL_FAIL_C else "fail")
        checks.append(Check("thermal", "Thermal headroom", status,
                            {"hottest_zone_max_c": temp, "zone": summary.get("thermal_zone")},
                            f"hottest zone peaked at {temp:g} °C.",
                            f"<{THERMAL_WARN_C:g} °C pass, <{THERMAL_FAIL_C:g} °C warn"))

    share = summary.get("mem_used_share_max")
    if share is None:
        checks.append(Check("memory", "Memory headroom", "unknown", {}, "no /proc/meminfo samples"))
    else:
        status = "pass" if share < MEM_WARN else ("warn" if share < MEM_FAIL else "fail")
        checks.append(Check("memory", "Memory headroom", status,
                            {"used_share_max": share, "used_mb_max": summary.get("mem_used_mb_max"),
                             "total_mb": summary.get("mem_total_mb"), "swap_used_mb": summary.get("swap_used_mb")},
                            f"{share:.0%} of {summary.get('mem_total_mb')} MB in use at peak.",
                            f"<{MEM_WARN:.0%} pass, <{MEM_FAIL:.0%} warn"))
    return checks


# ── report ────────────────────────────────────────────────────────────

ICON = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "unknown": "UNKNOWN", "info": "INFO"}


def render_markdown(report: dict) -> str:
    g = report["grade"]
    host = report["host"]
    lines = [f"# OpenNVR hardware acceptance — {host.get('hostname', '?')}", "",
             f"Grade **{g['grade']}** ({g['points']:g}/{g['max']} points"
             + (f", {g['percent']:g} %" if g["percent"] is not None else "") + ")  ",
             f"Run {report['started']} · tool {report['tool_version']} · "
             f"{host.get('cpu_model', '?')} · {host.get('cores', '?')} cores · "
             f"{host.get('mem_total_mb', '?')} MB · flavour `{host.get('flavour', '?')}` · "
             f"images `{host.get('image_tag', '?')}`", ""]
    if g["unknown"]:
        lines += [f"Not measured: {', '.join(g['unknown'])} — these carry no points either way.", ""]
    lines += ["| Check | Result | Measured | Threshold |", "|---|---|---|---|"]
    for c in report["checks"]:
        flat = []
        for k, v in c["measured"].items():
            if isinstance(v, dict):
                flat += [(f"{k}.{k2}", v2) for k2, v2 in v.items() if not isinstance(v2, dict)]
            else:
                flat.append((k, v))
        meas = ", ".join(f"{k}={_fmt(v)}" for k, v in flat if v is not None) or "—"
        lines.append(f"| {c['title']} | **{ICON[c['status']]}** | {meas} | {c['threshold'] or '—'} |")
    lines += [""]
    for c in report["checks"]:
        if c["detail"]:
            lines.append(f"- **{c['title']}** — {c['detail']}")
    lines += ["", "Thresholds and what each check means: docs/ACCEPTANCE.md. "
              "Benches ran beside whatever the stack was doing; for the cleanest "
              "detector and decode numbers run them on an idle box.", ""]
    return "\n".join(lines)


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, (list, tuple)):
        return "/".join(str(x) for x in v)
    return str(v)


# ── the stack (docker compose) ────────────────────────────────────────

class Stack:
    """``docker compose`` in the project dir; the compose files come from
    the .env's COMPOSE_FILE exactly as start.sh left them."""

    def __init__(self, project_dir: Path, service: str = "detect-pipeline") -> None:
        self.dir = project_dir
        self.service = service

    def run(self, args: list[str], timeout: float = 120.0, check: bool = False) -> subprocess.CompletedProcess:
        cmd = ["docker", "compose", *args]
        return subprocess.run(cmd, cwd=self.dir, capture_output=True, text=True,
                              timeout=timeout, check=check)

    def exec(self, argv: list[str], timeout: float = 120.0, service: str | None = None) -> subprocess.CompletedProcess:
        return self.run(["exec", "-T", service or self.service, *argv], timeout=timeout)

    def env(self, keys: list[str]) -> dict[str, str]:
        r = self.exec(["sh", "-c", " ".join(f'printf "%s=%s\\n" {k} "${k}";' for k in keys)], timeout=30)
        out: dict[str, str] = {}
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
        return out

    def metrics(self) -> list[tuple[str, dict, float]] | None:
        port = self.env(["DETECT_METRICS_PORT"]).get("DETECT_METRICS_PORT") or "9109"
        if port == "0":
            return None
        r = self.exec(["python3", "-c",
                       "import urllib.request,sys;"
                       f"sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:{port}/metrics',timeout=5).read().decode())"],
                      timeout=30)
        if r.returncode != 0:
            return None
        return parse_prom(r.stdout)

    def image_tag(self) -> str | None:
        r = self.run(["ps", "--format", "json", self.service], timeout=30)
        if r.returncode != 0:
            return None
        return image_from_ps_json(r.stdout)

    def logs_count(self, service: str, since: str, pattern: str) -> int | None:
        r = self.run(["logs", "--no-color", "--since", since, service], timeout=60)
        if r.returncode != 0:
            return None
        rx = re.compile(pattern, re.I)
        return sum(1 for line in r.stdout.splitlines() if rx.search(line))


# ── measurements ──────────────────────────────────────────────────────

CLIPS = {
    # name: (ffmpeg source/encode args) — short synthetic clips, encoded
    # once inside the container, I/P frames only (-bf 0) like an IP camera's
    # streams. testsrc2 is easy content; real footage costs more, which is
    # what --clip and the live window are for.
    "720p-h264": ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=15", "-t", "20",
                  "-c:v", "libx264", "-preset", "veryfast", "-g", "30", "-bf", "0", "-pix_fmt", "yuv420p"],
    "1080p-h265": ["-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=15", "-t", "20",
                   "-c:v", "libx265", "-preset", "ultrafast", "-g", "30", "-bf", "0", "-pix_fmt", "yuv420p",
                   "-x265-params", "log-level=error"],
}
#: ``full`` is what the pipeline's default ``-skip_frame nonref`` costs on a
#: camera stream (I/P only, nothing to skip); ``nokey`` is adaptive idle.
DECODE_MODES = {"full": [], "nokey": ["-skip_frame", "nokey"]}


def make_clips(stack: Stack, user_clip: Path | None, log) -> dict[str, tuple[str, float]]:
    """Encode the reference clips in the container; copy the user's clip in.
    Returns ``name -> (container path, duration s)``."""
    clips: dict[str, tuple[str, float]] = {}
    for name, args in CLIPS.items():
        path = f"/tmp/acceptance-{name}.mp4"
        r = stack.exec(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, path], timeout=300)
        if r.returncode == 0:
            clips[name] = (path, 20.0)
        else:
            log(f"  could not encode the {name} reference clip ({r.stderr.strip()[-120:]})")
    if user_clip is not None:
        dest = "/tmp/acceptance-user" + user_clip.suffix
        r = stack.run(["cp", str(user_clip), f"{stack.service}:{dest}"], timeout=300)
        if r.returncode == 0:
            dur = probe_duration(stack, dest)
            if dur:
                clips["user:" + user_clip.name] = (dest, dur)
            else:
                log("  the user clip could not be probed; skipped")
        else:
            log(f"  could not copy the user clip into the container ({r.stderr.strip()[-120:]})")
    return clips


def probe_duration(stack: Stack, path: str) -> float | None:
    r = stack.exec(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=nw=1:nk=1", path], timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def decode_costs(stack: Stack, clips: dict[str, tuple[str, float]], threads: int, log) -> dict:
    """% of one core each clip costs at real time, per skip mode: CPU
    seconds of a full-speed decode over the clip's duration."""
    costs: dict[str, dict[str, float | None]] = {}
    for name, (path, dur) in clips.items():
        costs[name] = {}
        for mode, flag in DECODE_MODES.items():
            # -benchmark reports at info level; -nostats keeps the progress
            # line out of what we parse.
            r = stack.exec(["ffmpeg", "-hide_banner", "-loglevel", "info", "-nostats", "-benchmark",
                            "-threads", str(threads), *flag, "-i", path, "-f", "null", "-"], timeout=600)
            cpu_s = parse_bench_output(r.stderr + r.stdout) if r.returncode == 0 else None
            costs[name][mode] = round(100.0 * cpu_s / dur, 1) if cpu_s is not None and dur else None
        log(f"  {name}: " + ", ".join(f"{m}={v if v is not None else '?'} %" for m, v in costs[name].items()))
    return costs


def detector_bench(stack: Stack, clip: str, env: dict[str, str], log) -> dict | None:
    """The in-image latency bench with the service's own model, providers
    and input size, so the number is the one the stack sees."""
    model = env.get("DETECT_ONNX_MODEL") or "/app/model_weights/yolo26n.onnx"
    argv = ["python", "-m", "detect_pipeline.bench", "--source", clip, "--detector", "onnx",
            "--model", model, "--latency", "30", "--warmup", "10", "--max-frames", "60"]
    providers = (env.get("DETECT_ONNX_PROVIDERS") or "").strip()
    if providers:
        argv += ["--backend", "ort", "--providers", providers]
    inp = (env.get("DETECT_ONNX_INPUT") or "").strip()
    if inp.isdigit():
        argv += ["--input", inp]
    r = stack.exec(argv, timeout=900)
    out = last_json_object(r.stdout)
    if out is None:
        log(f"  bench produced no JSON (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-200:]}")
        return None
    out["exit_code"] = r.returncode
    return out


def read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def thermal_zones() -> list[tuple[str, Path]]:
    zones = []
    for z in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            zones.append(((z / "type").read_text().strip(), z / "temp"))
        except OSError:
            continue
    return zones


def host_facts(stack: Stack, env_file: dict[str, str]) -> dict:
    cpu_model = None
    info = read("/proc/cpuinfo") or ""
    for line in info.splitlines():
        if line.lower().startswith(("model name", "hardware", "cpu model")):
            cpu_model = line.split(":", 1)[1].strip()
            break
    if not cpu_model:
        # ARM boards name themselves in the device tree, not in cpuinfo.
        dt = read("/proc/device-tree/model")
        cpu_model = (dt.replace("\x00", "").strip() if dt else "") or platform.machine()
    _, _, total_mb = meminfo_used_share(read("/proc/meminfo") or "")
    docker = subprocess.run(["docker", "--version"], capture_output=True, text=True).stdout.strip()
    return {
        "hostname": socket.gethostname(), "os": platform.platform(), "arch": platform.machine(),
        "cpu_model": cpu_model, "cores": os.cpu_count(), "mem_total_mb": total_mb,
        "docker": docker, "flavour": flavour_from_compose_file(env_file.get("COMPOSE_FILE")),
        "compose_file": env_file.get("COMPOSE_FILE"), "image_tag": stack.image_tag(),
        "thermal_zones": len(thermal_zones()),
    }


def live_window(stack: Stack, minutes: float, log) -> dict:
    """Sample the host and the pipeline for ``minutes``."""
    rows0 = stack.metrics()
    zones = thermal_zones()
    busy: list[float] = []
    load1: list[float] = []
    temps: list[tuple[float, str]] = []
    mem_share: list[tuple[float, int, int]] = []
    swap_used = None
    prev_stat = read("/proc/stat") or ""
    t_end = time.monotonic() + minutes * 60.0
    n = 0
    while time.monotonic() < t_end:
        time.sleep(SAMPLE_EVERY_S)
        stat = read("/proc/stat") or ""
        b = cpu_busy_percent(prev_stat, stat)
        prev_stat = stat
        if b is not None:
            busy.append(b)
        try:
            load1.append(float((read("/proc/loadavg") or "0").split()[0]))
        except (ValueError, IndexError):
            pass
        share, used, total = meminfo_used_share(read("/proc/meminfo") or "")
        if share is not None:
            mem_share.append((share, used, total))
        mi = read("/proc/meminfo") or ""
        st = {l.split(":")[0]: l.split()[1] for l in mi.splitlines() if ":" in l and len(l.split()) > 1}
        if "SwapTotal" in st and "SwapFree" in st:
            swap_used = (int(st["SwapTotal"]) - int(st["SwapFree"])) // 1024
        for ztype, zpath in zones:
            try:
                temps.append((int(zpath.read_text().strip()) / 1000.0, ztype))
            except (OSError, ValueError):
                continue
        n += 1
        if n % 12 == 0:
            log(f"  {n * SAMPLE_EVERY_S / 60:.0f} min: cpu {busy[-1] if busy else '?'} % busy")
    rows1 = stack.metrics()

    summary: dict = {"minutes": minutes, "samples": n,
                     "cpu_busy_mean": round(sum(busy) / len(busy), 1) if busy else None,
                     "cpu_busy_max": max(busy) if busy else None,
                     "load1_max": max(load1) if load1 else None,
                     "mem_used_share_max": max(s for s, _, _ in mem_share) if mem_share else None,
                     "mem_used_mb_max": max(u for _, u, _ in mem_share) if mem_share else None,
                     "mem_total_mb": mem_share[0][2] if mem_share else None,
                     "swap_used_mb": swap_used}
    if temps:
        summary["thermal_max_c"], summary["thermal_zone"] = max(temps)
    else:
        summary["thermal_max_c"] = None
    if rows0 is not None and rows1 is not None:
        up = by_camera(rows1, "tier0_worker_up")
        fps = by_camera(rows1, "tier0_processing_fps")
        tgt = by_camera(rows1, "tier0_target_fps")
        summary["cameras"] = len(up)
        summary["cameras_up"] = sum(1 for v in up.values() if v >= 1.0)
        summary["fps_ratio_by_camera"] = {c: fps[c] / tgt[c] for c in fps
                                          if c in tgt and tgt[c] > 0 and up.get(c, 0) >= 1.0}
        for key, name in (("frames_delta", "tier0_frames_total"),
                          ("frames_dropped_delta", "tier0_frames_dropped_total"),
                          ("worker_restarts_delta", "tier0_worker_restarts_total")):
            a, b = by_camera(rows0, name), by_camera(rows1, name)
            summary[key] = int(sum(b.values()) - sum(a.values()))
        summary["decode_idle_cameras"] = sum(1 for v in by_camera(rows1, "tier0_decode_idle").values() if v >= 1.0)
    else:
        summary.update({"cameras": 0, "cameras_up": 0, "fps_ratio_by_camera": {},
                        "frames_delta": 0, "frames_dropped_delta": 0, "worker_restarts_delta": 0})
    summary["mediamtx_events"] = stack.logs_count(
        "mediamtx", f"{int(minutes * 60) + 5}s", r"reload|closing|panic|fatal|error")
    return summary


# ── the hardware suite ────────────────────────────────────────────────

def run_hardware(args: argparse.Namespace) -> int:
    def log(msg: str) -> None:
        print(msg, flush=True)

    if not sys.platform.startswith("linux"):
        log("hardware acceptance measures /proc and /sys — run it on the Linux host that runs OpenNVR.")
        return 2
    project = Path(args.project_dir).resolve()
    if not (project / "docker-compose.yml").exists():
        log(f"{project} has no docker-compose.yml — run from the OpenNVR checkout or pass --project-dir.")
        return 2
    skip = set(args.skip or [])
    stack = Stack(project)
    env_file = parse_env_file(project / ".env")
    started = datetime.now(timezone.utc)
    log(f"OpenNVR hardware acceptance {VERSION} — {started:%Y-%m-%d %H:%M} UTC")

    checks: list[Check] = []
    host = host_facts(stack, env_file)
    log(f"host: {host['cpu_model']} · {host['cores']} cores · {host['mem_total_mb']} MB · "
        f"flavour {host['flavour']} · image {host['image_tag'] or '(not running)'}")

    rows = stack.metrics()
    backend = first_labels(rows, "tier0_detector_backend") if rows else None
    checks.append(score_accelerator(host["flavour"],
                                    backend.get("requested") if backend else None,
                                    backend.get("effective") if backend else None))
    log(f"[{checks[-1].status}] {checks[-1].title}: {checks[-1].detail}")

    env = stack.env(["DETECT_ONNX_MODEL", "DETECT_ONNX_PROVIDERS", "DETECT_ONNX_INPUT",
                     "DETECT_FPS", "DETECT_DETECTOR_POOL", "DETECT_DECODE_THREADS", "DETECT_HWACCEL"])
    try:
        fps = float(env.get("DETECT_FPS") or 2)
    except ValueError:
        fps = 2.0
    try:
        pool = max(1, int(env.get("DETECT_DETECTOR_POOL") or 1))
    except ValueError:
        pool = 1
    try:
        threads = int(env.get("DETECT_DECODE_THREADS") or 2) or 2
    except ValueError:
        threads = 2

    clips: dict[str, tuple[str, float]] = {}
    if host["image_tag"] and ({"decode", "latency"} - skip):
        log("encoding reference clips in the container…")
        clips = make_clips(stack, Path(args.clip).resolve() if args.clip else None, log)

    if "latency" in skip:
        checks.append(Check("detector_latency", "Detector latency", "unknown", {}, "skipped (--skip latency)"))
    elif "720p-h264" not in clips:
        checks.append(Check("detector_latency", "Detector latency", "unknown", {},
                            "no reference clip (is the detect-pipeline container running?)"))
    else:
        log("detector latency (in-image bench, 30 timed frames)…")
        out = detector_bench(stack, clips["720p-h264"][0], env, log)
        extra = {k: out.get(k) for k in ("model_id", "effective", "input", "threads", "load_s")} if out else {}
        checks.append(score_detector_latency(out.get("median_ms") if out else None, fps, pool, extra))
        if out and out.get("fell_back"):
            checks[-1].status = "fail"
            checks[-1].detail = (f"asked for {out.get('requested')} but ran on {out.get('effective')}: "
                                 "a CPU number on an accelerated flavour. " + checks[-1].detail)
    log(f"[{checks[-1].status}] {checks[-1].title}: {checks[-1].detail}")

    if "decode" in skip:
        checks.append(Check("decode_cost", "Software decode cost", "unknown", {}, "skipped (--skip decode)"))
    elif not clips:
        checks.append(Check("decode_cost", "Software decode cost", "unknown", {},
                            "no reference clips (is the detect-pipeline container running?)"))
    else:
        log(f"decode cost per stream ({threads} threads, full / nokey)…")
        checks.append(score_decode_cost(decode_costs(stack, clips, threads, log)))
        if (env.get("DETECT_HWACCEL") or "cpu") != "cpu":
            checks[-1].detail += (f" (DETECT_HWACCEL={env['DETECT_HWACCEL']} is configured; these are "
                                  "the SOFTWARE numbers, the fallback every flavour has.)")
    log(f"[{checks[-1].status}] {checks[-1].title}: {checks[-1].detail}")

    if "live" in skip:
        checks.append(Check("cpu_load", "Host CPU over the window", "unknown", {}, "skipped (--skip live)"))
    elif not host["image_tag"]:
        checks.append(Check("cpu_load", "Host CPU over the window", "unknown", {},
                            "the stack is not running"))
    else:
        log(f"live window: {args.minutes:g} min of the running stack…")
        summary = live_window(stack, args.minutes, log)
        for c in score_live(summary):
            checks.append(c)
            log(f"[{c.status}] {c.title}: {c.detail}")

    g = grade(checks)
    report = {"tool": "opennvr-acceptance", "suite": "hardware", "tool_version": VERSION,
              "started": started.isoformat(timespec="seconds"),
              "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "host": host, "dials": {k: v for k, v in env.items() if v},
              "checks": [c.as_dict() for c in checks], "grade": g}
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"hardware-{host['hostname']}-{started:%Y%m%dT%H%M%SZ}"
    (out_dir / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n")
    md = render_markdown(report)
    (out_dir / f"{stem}.md").write_text(md)
    print()
    print(md)
    print(f"report: {out_dir / stem}.md (+ .json)")
    return 0 if g["grade"] in ("A", "B") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="acceptance", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="suite", required=True)
    h = sub.add_parser("hardware", help="score this box: accelerator, detector latency, decode cost, live load")
    h.add_argument("--project-dir", default=".", help="the OpenNVR checkout (docker-compose.yml + .env)")
    h.add_argument("--minutes", type=float, default=5.0, help="live window length (default 5)")
    h.add_argument("--skip", action="append", choices=["live", "decode", "latency"],
                   help="skip a measurement (repeatable)")
    h.add_argument("--clip", default=None, help="a clip of your own footage to time the decode of")
    h.add_argument("--out", default="acceptance-reports", help="report folder (default acceptance-reports/)")
    h.set_defaults(func=run_hardware)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
