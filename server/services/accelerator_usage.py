# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How busy the accelerators are, for the dashboard's host resources.

The host has a CPU counter in ``/proc/stat``; the NPU and the GPU on most
boards do not expose one a container can read (the Qualcomm Hexagon on the
Ventuno Q has no utilisation file outside debugfs, which is root-only). What
IS known is how long every inference took, from the metrics the Tier-0
detector and KAI-C already publish: the detector's latency histogram per
camera, and KAI-C's client-observed latency per proxied adapter. Summed over
a sample interval and divided by the wall time that is the share of the
device's time spent computing: one device, requests serialised, so 100 %
means it never idled. That is what the dashboard shows, labelled as what it
is: an inference-time share, not a hardware counter.

The share is honest only while the producers take turns. Client-observed
latency includes the wait for whoever holds the device, so when the detector,
the plate reader and the captioner call at once each one's share counts the
others' time too and the sum runs past 100 — measured on a Ventuno Q
(2026-11-11): a plate burst on one camera put the detector at 117 %, the plate
reader at 72 % and the captioner at 74 % of wall time while every one of them
ran five to ten times slower than alone. Summed latency over wall time is, by
Little's law, the mean number of requests in flight on the device, so the
entry also carries ``inflight``: 1.0 is a device that never idled, 2.6 is one
with requests queued behind each other. The dashboard says "queued" above 1. Where the kernel does
export a busy counter (Intel's NPU driver: ``npu_busy_time_us``) that number
is used instead and labelled so.

Which device each producer runs on comes from the detector's own
``tier0_detector_backend`` gauge (requested vs effective provider, the same
truth the AI Adapters page shows as "accelerator in use") and, for the
adapters, from the deployment's flavour: on the qualcomm flavour the ONNX
adapters and the LiteRT-LM captioner share the Hexagon; elsewhere only the
provider the detector reports is counted. The result is deliberately
conservative: an adapter not known to be on the device is not charged to it.
"""
from __future__ import annotations

import glob
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Provider name (as the detector reports it) → device the dashboard shows.
_PROVIDER_DEVICE: dict[str, tuple[str, str]] = {
    "QNNExecutionProvider": ("npu", "Qualcomm Hexagon NPU"),
    "OpenVINOExecutionProvider:NPU": ("npu", "Intel NPU"),
    "OpenVINOExecutionProvider:GPU": ("gpu", "Intel GPU"),
    "CUDAExecutionProvider": ("gpu", "NVIDIA GPU"),
    "TensorrtExecutionProvider": ("gpu", "NVIDIA GPU"),
    "CoreMLExecutionProvider": ("npu", "Apple Neural Engine"),
}
#: Adapters that run on the Hexagon when the detector does (the qualcomm
#: flavour puts them there: docker-compose.accel-qualcomm.yml). Charged to
#: the NPU only on that flavour.
_QUALCOMM_NPU_ADAPTERS = frozenset({
    "litertlm", "litertlm-gemma-npu", "yolov8", "default", "yolo-pose", "yolo_pose",
    "insightface", "package-detection", "package_detection", "fast-plate-ocr",
    "fast_plate_ocr",
})
#: Intel's NPU driver exports a busy-time counter (microseconds) per device.
_INTEL_NPU_BUSY_GLOB = "/sys/class/accel/accel*/device/npu_busy_time_us"

_METRIC_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{([^}]*)\})?\s+([-+0-9.eE]+|NaN|\+Inf)')
_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def _samples(text: str) -> list[tuple[str, dict[str, str], float]]:
    out = []
    for line in (text or "").splitlines():
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line)
        if not m:
            continue
        try:
            value = float(m.group(4))
        except ValueError:
            continue
        labels = {k: v for k, v in _LABEL_RE.findall(m.group(3) or "")}
        out.append((m.group(1), labels, value))
    return out


def _provider_key(spec: str) -> str:
    """The FIRST provider of what the detector reports, normalised.

    The gauge carries the session's provider list as ONNX Runtime spells
    it: ``effective="QNNExecutionProvider, CPUExecutionProvider"`` (the
    accelerator first, CPU as the fallback for unsupported nodes) and
    ``requested="QNNExecutionProvider(htp)"`` (Ventuno Q, 2026-11-07).
    ``QNNExecutionProvider:htp`` → ``QNNExecutionProvider``; OpenVINO keeps
    its device suffix because NPU and GPU are different devices."""
    first = (spec or "").split(",")[0].strip()
    if first.startswith("OpenVINOExecutionProvider"):
        rest = first[len("OpenVINOExecutionProvider"):]
        dev = rest.strip(":() ").split(",")[0].upper() if rest.strip(":() ") else "CPU"
        return f"OpenVINOExecutionProvider:{dev}"
    for sep in (":", "("):
        first = first.split(sep, 1)[0]
    return first.strip()


@dataclass
class _Totals:
    """Cumulative inference seconds per producer at one instant."""
    at: float
    detector: float = 0.0
    detector_count: float = 0.0
    adapters: dict[str, float] = field(default_factory=dict)
    device: tuple[str, str] | None = None     # (kind, name) the detector runs on
    intel_busy_us: float | None = None


#: A window with fewer detector inferences than this says nothing about
#: latency (one slow frame would move the mean by itself).
DETECTOR_WINDOW_MIN_INFERENCES = 20

#: How fast the detector's latency floor follows a lasting rise (per
#: sample, 15 s apart): 0.02 re-baselines in about twelve minutes, so a
#: camera added for good stops counting as "contention" while a burst
#: of captions, which is minutes long, still does.
DETECTOR_FLOOR_DRIFT = 0.02


class AcceleratorUsage:
    """Keeps the previous sample's totals and turns the next into busy shares.

    Also keeps the detector's **latency floor**: the lowest per-window mean
    seen, drifting slowly upward. A window whose mean is a multiple of that
    floor is a detector waiting on the device behind other producers — the
    one signal the enrichment governor needs to yield, because the share
    figures cannot tell "busy computing" from "busy waiting"."""

    def __init__(self) -> None:
        self._prev: _Totals | None = None
        self.detector_floor_ms: float | None = None

    # ── reading ──────────────────────────────────────────────────────

    @staticmethod
    def _read_intel_busy_us() -> float | None:
        for path in glob.glob(_INTEL_NPU_BUSY_GLOB):
            try:
                with open(path) as f:
                    return float(f.read().strip())
            except (OSError, ValueError):
                continue
        return None

    @staticmethod
    def totals_from(detect_text: str | None, kaic_text: str | None, at: float | None = None) -> _Totals:
        t = _Totals(at=at if at is not None else time.monotonic())
        for name, labels, value in _samples(detect_text or ""):
            if name == "tier0_detector_latency_seconds_sum":
                t.detector += value
            elif name == "tier0_detector_latency_seconds_count":
                t.detector_count += value
            elif name == "tier0_detector_backend" and value:
                eff = _provider_key(labels.get("effective") or "")
                dev = _PROVIDER_DEVICE.get(eff)
                if dev:
                    t.device = dev
        for name, labels, value in _samples(kaic_text or ""):
            if name == "kaic_proxy_infer_latency_seconds_sum":
                adapter = labels.get("adapter") or ""
                if adapter:
                    t.adapters[adapter] = t.adapters.get(adapter, 0.0) + value
        return t

    # ── computing ────────────────────────────────────────────────────

    def update(self, detect_text: str | None, kaic_text: str | None,
               at: float | None = None) -> list[dict[str, Any]]:
        """The accelerators and their busy share since the previous call.
        Empty on the first call, when nothing runs on an accelerator, or
        when the metrics could not be read."""
        now = self.totals_from(detect_text, kaic_text, at)
        now.intel_busy_us = self._read_intel_busy_us()
        prev, self._prev = self._prev, now
        if now.device is None:
            return []
        kind, name = now.device
        if prev is None or now.at <= prev.at:
            return [{"kind": kind, "name": name, "busy_percent": None, "users": [],
                     "source": "inference-time", "inflight": None,
                     "detector_ms": None, "detector_floor_ms": self.detector_floor_ms,
                     "detector_slowdown": None}]
        dt = now.at - prev.at
        users: list[dict[str, Any]] = []
        det_s = max(0.0, now.detector - prev.detector)
        det = det_s / dt * 100.0
        users.append({"name": "detector", "percent": round(det, 1)})
        # The detector's mean latency over this window, and its floor.
        det_n = now.detector_count - prev.detector_count
        detector_ms: float | None = None
        if det_n >= DETECTOR_WINDOW_MIN_INFERENCES:
            detector_ms = det_s / det_n * 1000.0
            if self.detector_floor_ms is None or detector_ms < self.detector_floor_ms:
                self.detector_floor_ms = detector_ms
            else:
                self.detector_floor_ms += (detector_ms - self.detector_floor_ms) * DETECTOR_FLOOR_DRIFT
        slowdown = (round(detector_ms / self.detector_floor_ms, 2)
                    if detector_ms is not None and self.detector_floor_ms else None)
        if kind == "npu" and name.startswith("Qualcomm"):
            for adapter, total in now.adapters.items():
                if adapter in _QUALCOMM_NPU_ADAPTERS:
                    share = max(0.0, total - prev.adapters.get(adapter, 0.0)) / dt * 100.0
                    if share > 0:
                        users.append({"name": adapter, "percent": round(share, 1)})
        total = sum(u["percent"] for u in users)
        busy = min(100.0, total)
        # Mean requests in flight on the device over the window (Little's
        # law: summed latency / wall time). Above 1 the producers are
        # waiting on each other, which the capped share cannot say.
        inflight = round(total / 100.0, 2)
        source = "inference-time"
        if now.intel_busy_us is not None and prev.intel_busy_us is not None and kind == "npu":
            busy = min(100.0, max(0.0, now.intel_busy_us - prev.intel_busy_us) / (dt * 1e6) * 100.0)
            source = "sysfs"
        return [{"kind": kind, "name": name, "busy_percent": round(busy, 1),
                 "users": users, "source": source, "inflight": inflight,
                 "detector_ms": None if detector_ms is None else round(detector_ms, 1),
                 "detector_floor_ms": (None if self.detector_floor_ms is None
                                       else round(self.detector_floor_ms, 1)),
                 "detector_slowdown": slowdown}]


def fetch_text(url: str, timeout: float = 2.0) -> str | None:
    """One metrics scrape, synchronous (the monitor samples in a worker thread)."""
    try:
        import httpx
        r = httpx.get(url, timeout=timeout, trust_env=False)
        return r.text if r.status_code == 200 else None
    except Exception:  # noqa: BLE001 — an unreachable exporter is "unknown", not an error
        return None
