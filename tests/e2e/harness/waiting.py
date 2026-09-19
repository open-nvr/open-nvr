# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``eventually()`` — the only permitted wait in this suite.

Nothing in OpenNVR reports completion. There is no job queue, no callback and
no terminal status for most of the chain: ``server/core/background_tasks.py``
spawns periodic loops, Tier-0 posts a visit only when a *track ends*, and plate
enrichment is a FastAPI ``BackgroundTask`` behind a semaphore. The honest
signal that async work finished is the row, file or metric showing up.

So tests poll. The rule is that they poll through here, because a bare
``time.sleep(30)`` costs thirty seconds even when the answer arrived in two,
and tells you nothing at all when it never arrives.

What this buys, beyond not sleeping:

* **The last-seen value is captured.** A timeout reports what the probe
  actually returned on its final attempt, not merely that it timed out.
  "expected a visit row, saw ``[]`` after 47 polls over 240s" is a diagnosis;
  "timed out" is a shrug.
* **Exceptions are data.** A 404 while MediaMTX is still provisioning is a
  legitimate "not yet". The exception is recorded and retried, and only
  surfaces if the wait as a whole fails.
* **Every wait is recorded** for the evidence bundle (see ``evidence.py``), so
  a failure report can show the whole timeline of what the test was waiting on.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# Populated by every eventually() call; conftest clears it per test and
# evidence.py drains it when building a failure report. pytest runs tests
# single-threaded, so a module-level list is sufficient and keeps the call
# sites free of plumbing.
_RECORDS: list["WaitRecord"] = []


@dataclass
class WaitRecord:
    """One completed (or failed) wait, kept for the evidence bundle."""

    describe: str
    budget: float
    elapsed: float
    attempts: int
    satisfied: bool
    last_value: Any = None
    last_error: str | None = None


@dataclass
class _Attempt:
    value: Any = None
    error: BaseException | None = None
    error_text: str | None = None
    ok: bool = False


class WaitTimeout(AssertionError):
    """A wait exhausted its budget.

    Subclasses AssertionError so pytest renders it as a failure rather than an
    error — a wait that never came true is a failed expectation about the
    system, not a broken test.
    """

    def __init__(self, record: WaitRecord) -> None:
        self.record = record
        super().__init__(_format_timeout(record))


def _format_timeout(rec: WaitRecord) -> str:
    lines = [
        f"Timed out waiting for {rec.describe}.",
        f"  budget   : {rec.budget:.0f}s (exhausted after {rec.elapsed:.1f}s)",
        f"  attempts : {rec.attempts}",
    ]
    if rec.last_error:
        lines.append(f"  last error: {rec.last_error}")
    else:
        lines.append(f"  last value: {_truncate(rec.last_value)}")
    lines.append("")
    lines.append(
        "  Raise the budget for a slow machine without editing this test:\n"
        "      E2E_BUDGET_<NAME>=<seconds>   (see tests/e2e/harness/budgets.py)"
    )
    return "\n".join(lines)


def _truncate(value: Any, limit: int = 800) -> str:
    try:
        text = repr(value)
    except Exception:  # a repr that raises must not mask the real failure
        text = f"<unrepresentable {type(value).__name__}>"
    if len(text) > limit:
        return text[:limit] + f"… ({len(text)} chars total)"
    return text


def _default_until(value: Any) -> bool:
    """Truthiness, with the empty-collection cases spelled out.

    ``[]``, ``{}``, ``""`` and ``0`` are all falsey, which is almost always
    what a test means ("wait until the list is non-empty"). ``None`` from a
    lookup that has not happened yet is likewise falsey.
    """
    return bool(value)


def eventually(
    probe: Callable[[], T],
    *,
    budget: float,
    describe: str,
    until: Callable[[T], bool] | None = None,
    interval: float = 1.0,
    catch: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    """Poll ``probe`` until ``until(probe())`` holds, or ``budget`` runs out.

    Args:
        probe: called repeatedly; its return value is the thing being waited on.
        budget: seconds. Always pass a named budget from ``BUDGETS`` — never a
            literal, so a slow environment is tunable without editing tests.
        describe: a noun phrase completing "waiting for …". This string is what
            a failing run shows first, so make it specific: "the MediaMTX path
            for camera 7 to report ready", not "the camera".
        until: predicate on the probe's value. Defaults to truthiness.
        interval: seconds between attempts.
        catch: exception types treated as "not yet". Defaults to ``Exception``,
            because a not-yet-provisioned resource legitimately 404s. Narrow it
            when you want a specific error to fail fast.

    Returns:
        The first value satisfying ``until``.

    Raises:
        WaitTimeout: budget exhausted. The message carries the last value or
            error, the attempt count and the env knob to raise the budget.
    """
    predicate = until or _default_until
    deadline = time.monotonic() + budget
    started = time.monotonic()
    attempts = 0
    last = _Attempt()

    while True:
        attempts += 1
        last = _probe_once(probe, predicate, catch)
        if last.ok:
            _record(describe, budget, time.monotonic() - started, attempts, True, last)
            return last.value  # type: ignore[return-value]

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            rec = _record(
                describe, budget, time.monotonic() - started, attempts, False, last
            )
            raise WaitTimeout(rec)

        time.sleep(min(interval, remaining))


def _probe_once(
    probe: Callable[[], T],
    predicate: Callable[[T], bool],
    catch: tuple[type[BaseException], ...],
) -> _Attempt:
    """Run the probe once, converting an expected exception into 'not yet'."""
    try:
        value = probe()
    except catch as exc:
        return _Attempt(
            error=exc,
            error_text=f"{type(exc).__name__}: {exc}",
        )
    try:
        ok = bool(predicate(value))
    except catch as exc:
        # A predicate that blows up on a half-built payload (KeyError on a
        # field that appears later) is also a legitimate "not yet".
        return _Attempt(
            value=value,
            error=exc,
            error_text=f"predicate raised {type(exc).__name__}: {exc}",
        )
    return _Attempt(value=value, ok=ok)


def _record(
    describe: str,
    budget: float,
    elapsed: float,
    attempts: int,
    satisfied: bool,
    last: _Attempt,
) -> WaitRecord:
    rec = WaitRecord(
        describe=describe,
        budget=budget,
        elapsed=elapsed,
        attempts=attempts,
        satisfied=satisfied,
        last_value=last.value,
        last_error=last.error_text,
    )
    _RECORDS.append(rec)
    return rec


# ---------------------------------------------------------------------------
# Evidence plumbing — used by conftest.py and evidence.py, not by tests.
# ---------------------------------------------------------------------------
def reset_records() -> None:
    """Drop recorded waits. Called at the start of each test."""
    _RECORDS.clear()


def records() -> list[WaitRecord]:
    """Every wait this test performed, in order."""
    return list(_RECORDS)


def failed_record() -> WaitRecord | None:
    """The wait that timed out, if any — the headline of a failure report."""
    for rec in reversed(_RECORDS):
        if not rec.satisfied:
            return rec
    return None


__all__ = [
    "eventually",
    "WaitTimeout",
    "WaitRecord",
    "reset_records",
    "records",
    "failed_record",
]
