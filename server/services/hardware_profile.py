# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an app or an adapter needs, and what this box has.

The requirement is not declared by the app: it is derived from the
tasks the app requires, whose ``hardware:`` blocks in
``server/config/tasks.yml`` carry the guidance as numbers (``cores``,
``ram_gb``, ``gpu``). A skill manifest is the task registry; an app's
manifest names tasks and inherits their bar. An adapter's bar is the
same derivation over the tasks it advertises.

The box is what this process can see: cores after the cgroup quota
(``docker run --cpus``, compose ``cpus:``), RAM from ``/proc/meminfo``
or psutil, the accelerator from ``DETECT_HWACCEL``. The verdict is
advisory — "detect, recommend, never gate", the rule the installer and
``embedding_store`` already follow — and the UI shows it as a badge
beside "needs <task>", never as a disabled button.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GPU_RANK = {"none": 0, "optional": 1, "recommended": 2, "required": 3}


@dataclass(frozen=True)
class HostProfile:
    cores: int
    ram_gb: float | None
    #: cpu | cuda | vaapi | qsv | videotoolbox | ... — DETECT_HWACCEL as set,
    #: which is the only accelerator the server knows about.
    accelerator: str

    @property
    def has_gpu(self) -> bool:
        return self.accelerator.lower() in {"cuda", "nvidia", "metal", "rocm", "tensorrt"}

    def as_dict(self) -> dict[str, Any]:
        return {"cores": self.cores, "ram_gb": self.ram_gb,
                "accelerator": self.accelerator, "has_gpu": self.has_gpu}


@dataclass
class Requirement:
    cores: int | None = None
    ram_gb: float | None = None
    gpu: str = "none"
    tasks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"cores": self.cores, "ram_gb": self.ram_gb, "gpu": self.gpu, "tasks": list(self.tasks)}


def available_cores() -> int:
    """Cores THIS process may use: affinity, capped by a cgroup quota,
    which ``os.cpu_count()`` never reflects. Never below 1."""
    cores = None
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        pass
    cores = cores or os.cpu_count() or 1
    quota = _cgroup_cpu_quota()
    if quota:
        cores = min(cores, max(1, int(math.ceil(quota))))
    return max(1, int(cores))


def _cgroup_cpu_quota(root: Path = Path("/sys/fs/cgroup")) -> float | None:
    try:  # v2
        raw = (root / "cpu.max").read_text().split()
        if raw and raw[0] != "max":
            return float(raw[0]) / float(raw[1])
        return None
    except (OSError, ValueError, IndexError):
        pass
    try:  # v1
        q = float((root / "cpu" / "cpu.cfs_quota_us").read_text())
        per = float((root / "cpu" / "cpu.cfs_period_us").read_text())
        if q > 0 and per > 0:
            return q / per
    except (OSError, ValueError):
        pass
    return None


def total_ram_gb() -> float | None:
    """Total RAM in GB (one decimal), from psutil when present, else
    /proc/meminfo; None where neither answers (a Mac without psutil)."""
    try:
        import psutil  # type: ignore
        return round(psutil.virtual_memory().total / 1024 ** 3, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024 ** 2, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


def host_profile() -> HostProfile:
    from core.config import settings
    accel = str(getattr(settings, "detect_hwaccel", "cpu") or "cpu")
    return HostProfile(cores=available_cores(), ram_gb=total_ram_gb(), accelerator=accel)


def requirement_for_tasks(tasks: list[str], registry: list[Any] | None = None) -> Requirement:
    """The bar for a set of tasks: the largest cores and RAM any of them
    asks for, the strongest GPU word, and which tasks set it. Names are
    canonicalised through the registry (``scene_caption`` →
    ``image_captioning``); a task with no hardware block adds nothing."""
    from routers.ai_models import _load_tasks_registry, canonicalize_task
    registry = registry if registry is not None else _load_tasks_registry()
    by_name = {e.task.lower(): e for e in registry}
    req = Requirement()
    for raw in tasks or []:
        name = canonicalize_task(str(raw), registry).lower()
        entry = by_name.get(name)
        hw = getattr(entry, "hardware", None) if entry is not None else None
        if hw is None:
            continue
        if name not in req.tasks:
            req.tasks.append(name)
        if hw.cores is not None:
            req.cores = max(req.cores or 0, int(hw.cores))
        if hw.ram_gb is not None:
            req.ram_gb = max(req.ram_gb or 0.0, float(hw.ram_gb))
        if hw.gpu and GPU_RANK.get(hw.gpu, 0) > GPU_RANK.get(req.gpu, 0):
            req.gpu = hw.gpu
    return req


def fit(req: Requirement, host: HostProfile) -> dict[str, Any]:
    """``{"verdict": ok|tight|short, "reasons": [...]}``. ``short`` when a
    hard number is not met (fewer cores than the bar, less RAM, no GPU
    where one is required); ``tight`` when it is met exactly or a GPU is
    recommended and absent; ``ok`` otherwise. RAM unknown is not held
    against the box."""
    reasons: list[str] = []
    verdict = "ok"
    if req.cores is not None:
        if host.cores < req.cores:
            verdict = "short"
            reasons.append(f"needs {req.cores} cores, this box has {host.cores}")
        elif host.cores == req.cores:
            verdict = "tight" if verdict == "ok" else verdict
            reasons.append(f"{req.cores} cores is the floor and this box has {host.cores}")
    if req.ram_gb is not None and host.ram_gb is not None:
        if host.ram_gb < req.ram_gb:
            verdict = "short"
            reasons.append(f"needs {req.ram_gb:g} GB RAM, this box has {host.ram_gb:g}")
    if req.gpu == "required" and not host.has_gpu:
        verdict = "short"
        reasons.append("needs a GPU and this box runs on the CPU")
    elif req.gpu == "recommended" and not host.has_gpu:
        verdict = "tight" if verdict == "ok" else verdict
        reasons.append("a GPU is recommended; this box runs on the CPU")
    return {"verdict": verdict, "reasons": reasons}


def describe(tasks: list[str], host: HostProfile | None = None,
             registry: list[Any] | None = None) -> dict[str, Any] | None:
    """The wire shape for an app or an adapter: the requirement plus its
    fit on this box, or None when the tasks set no bar."""
    req = requirement_for_tasks(tasks, registry)
    if not req.tasks:
        return None
    host = host or host_profile()
    out = req.as_dict()
    out.update(fit(req, host))
    return out
