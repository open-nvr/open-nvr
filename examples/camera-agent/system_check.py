# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end capability self-check for the camera agent.

The disease this treats is *silent degradation*: every container answers its
HTTP healthcheck while the actual capability path is broken — the caption
adapter that was registered, healthy, and never received a single request
(sovereignty 403); the stub detector that "runs" while detecting nothing;
the frame source whose signed URL expired an hour ago. Process-level health
says "up"; only exercising the REAL path with a REAL payload says "works".

This module is the runner: it takes named async checks, executes each with a
timeout, and returns a structured board. The checks themselves are built by
the runtime (they need its clients); each returns ``(status, detail)`` where
status is ``ok`` | ``degraded`` | ``down``. A check that raises is ``down``
with the exception as detail; a check that hangs is ``down`` with a timeout
note — a self-check must never wedge the agent.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

OK = "ok"
DEGRADED = "degraded"
DOWN = "down"

_STATUS_ICON = {OK: "✅", DEGRADED: "⚠️", DOWN: "❌"}

CheckFn = Callable[[], Awaitable[tuple[str, str]]]


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    latency_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "latency_ms": self.latency_ms}

    def line(self) -> str:
        icon = _STATUS_ICON.get(self.status, "❓")
        return f"{icon} {self.name}: {self.detail} ({self.latency_ms} ms)"


@dataclass
class SystemReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return all(r.status == OK for r in self.results)

    @property
    def summary(self) -> str:
        down = sum(1 for r in self.results if r.status == DOWN)
        degraded = sum(1 for r in self.results if r.status == DEGRADED)
        if not down and not degraded:
            return "all capabilities working"
        parts = []
        if down:
            parts.append(f"{down} down")
        if degraded:
            parts.append(f"{degraded} degraded")
        return ", ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "summary": self.summary,
            "checks": [r.as_dict() for r in self.results],
        }

    def lines(self) -> list[str]:
        return [r.line() for r in self.results]


async def run_checks(
    checks: dict[str, CheckFn], *, timeout_s: float = 10.0,
) -> SystemReport:
    """Run every check (sequentially — they may share clients/models),
    bounding each with ``timeout_s``. Never raises."""
    report = SystemReport()
    for name, fn in checks.items():
        t0 = time.monotonic()
        try:
            status, detail = await asyncio.wait_for(fn(), timeout=timeout_s)
        except asyncio.TimeoutError:
            status, detail = DOWN, f"no answer within {timeout_s:.0f}s"
        except Exception as exc:  # a check must never crash the runner
            status, detail = DOWN, f"{type(exc).__name__}: {exc}"
        detail = str(detail)[:300]
        if status not in (OK, DEGRADED, DOWN):
            status = DOWN
        report.results.append(
            CheckResult(name, status,
                        detail, int((time.monotonic() - t0) * 1000))
        )
    return report
