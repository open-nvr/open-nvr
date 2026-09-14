# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the app says about itself when it is not working.

These exist because of a real fresh install that did nothing for hours
and looked perfectly well the whole time. The pose adapter was never
reachable, so every /infer failed; the app caught that, logged it, and
carried on. Meanwhile:

* ``/health`` said ``ready: true``, because it always did;
* ``/state`` showed no error at all, because the failure never left
  ``pose()``;
* the frame counter kept climbing, because frames were being DECODED
  perfectly well — just never understood;
* and compliance read **100%**, because zero screenings out of zero was
  being rendered as a perfect score.

Four separate signals, all of them green, on an app screening nobody.
Nothing here is about detection: it is about a broken app being able to
say that it is broken.

SDK-free like the rest of this suite — the methods under test are read
out of the app module's source and bound to stubs, because importing the
module would drag in the platform SDK that CI deliberately does not
install.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

APP_SRC = (Path(__file__).resolve().parents[1]
           / "guard_scan_compliance.py").read_text(encoding="utf-8")


NL = chr(10)


def _slice(start: str, end: str) -> str:
    """The source between two markers, dedented so it can be exec'd.

    Both markers rewind to the start of their line, so the slice keeps
    the indentation that makes ``dedent`` work on it.
    """
    a = APP_SRC.rindex(NL, 0, APP_SRC.index(start)) + 1
    b = APP_SRC.rindex(NL, 0, APP_SRC.index(end, a)) + 1
    return textwrap.dedent(APP_SRC[a:b])


class _Clock:
    """Stands in for ``time`` so the backoff does not really sleep."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


@pytest.fixture
def worker_methods():
    """The worker's inference-health bookkeeping, bound to nothing."""
    clock = _Clock()
    ns = {
        "time": clock,
        "log": SimpleNamespace(info=lambda *a, **k: None,
                               warning=lambda *a, **k: None),
        # Read from the module so the test cannot drift from the app.
        "INFER_FAILURES_BEFORE_UNHEALTHY": _threshold(),
    }
    exec(compile(_slice("def _note_infer_failure", "# ── the loop ──"),
                 "<worker>", "exec"), ns)  # noqa: S102
    return ns, clock


def _threshold() -> int:
    line = next(ln for ln in APP_SRC.splitlines()
                if ln.startswith("INFER_FAILURES_BEFORE_UNHEALTHY"))
    return int(line.split("=")[1].strip())


def _worker():
    return SimpleNamespace(handle="cam2", _infer_failures=0,
                           _infer_error=None, last_error=None)


# ── an outage has to leave the log ─────────────────────────────────


def test_a_single_failed_frame_is_not_an_outage(worker_methods):
    """At ten frames a second a dropped call means nothing. Going amber
    on one would make the status dot useless."""
    ns, _ = worker_methods
    w = _worker()
    ns["_note_infer_failure"](w, "connection refused")
    assert w.last_error is None
    assert ns["inference_down"].fget(w) is False


def test_a_sustained_outage_reaches_state(worker_methods):
    """The whole point: `last_error` is what /state renders, and it is
    where the operator finally sees the reason."""
    ns, clock = worker_methods
    w = _worker()
    for _ in range(_threshold()):
        ns["_note_infer_failure"](w, "404 unknown adapter: yolo-pose")

    assert ns["inference_down"].fget(w) is True
    assert "404 unknown adapter: yolo-pose" in w.last_error
    assert "pose inference failing" in w.last_error
    # And it backed off rather than hammering the adapter.
    assert clock.slept


def test_the_backoff_is_bounded(worker_methods):
    """An outage lasting an hour must not become an hour-long sleep."""
    ns, clock = worker_methods
    w = _worker()
    for _ in range(200):
        ns["_note_infer_failure"](w, "down")
    assert max(clock.slept) <= 30.0


def test_recovery_retires_the_outage(worker_methods):
    ns, _ = worker_methods
    w = _worker()
    for _ in range(_threshold()):
        ns["_note_infer_failure"](w, "down")
    assert w.last_error is not None

    ns["_clear_infer_error"](w)
    assert w.last_error is None
    assert ns["inference_down"].fget(w) is False


def test_recovery_does_not_clear_an_unrelated_error(worker_methods):
    """A config the operator typed wrong is still wrong after the
    adapter comes back. Clearing it would tell them it was accepted."""
    ns, _ = worker_methods
    w = _worker()
    w.last_error = "config refused: unknown surface 'elbows'"

    for _ in range(_threshold()):
        ns["_note_infer_failure"](w, "down")
    # The outage is the more recent news, so it shows.
    assert "pose inference failing" in w.last_error

    ns["_clear_infer_error"](w)
    # ...but retiring it must not swallow the refusal underneath.
    assert w.last_error is None or "config refused" in w.last_error


# ── the numbers the catalog shows ──────────────────────────────────


@pytest.fixture
def app_methods():
    """``state`` and ``not_ready_reason``, bound to nothing."""
    ns = {"POSE_ADAPTER": "yolo-pose"}
    exec(compile(_slice("def state(self) -> dict:",
                        "# \u2500\u2500 the FrameApp surface \u2500\u2500"),
                 "<app>", "exec"), ns)  # noqa: S102
    return ns


def _app(screenings=0, compliant=0, workers=None):
    return SimpleNamespace(screenings=screenings, compliant=compliant,
                           workers=workers or {}, recent=[])


def test_nothing_screened_is_not_a_perfect_score(app_methods):
    """The bug in its purest form: a dead app displayed the best number
    on the page. Zero out of zero has no answer."""
    state = app_methods["state"](_app())
    assert "100%" not in state["compliance"]
    assert state["screenings"] == 0


def test_a_real_rate_is_still_a_percentage(app_methods):
    """The honest case must not have been broken in the process."""
    assert app_methods["state"](_app(screenings=4, compliant=3))["compliance"] == "75%"
    assert app_methods["state"](_app(screenings=2, compliant=2))["compliance"] == "100%"


# ── the status dot ─────────────────────────────────────────────────


def test_an_app_with_no_camera_says_so(app_methods):
    """Closed-by-default camera assignment means this is the FIRST
    thing a new install hits, and it used to be invisible."""
    why = app_methods["not_ready_reason"](_app())
    assert why is not None
    assert "assign" in why.lower()


def test_a_working_app_reports_nothing(app_methods):
    w = SimpleNamespace(inference_down=False)
    assert app_methods["not_ready_reason"](_app(workers={"cam2": w})) is None


def test_a_total_outage_names_the_adapter(app_methods):
    """The operator's next move is to go look at that container, so the
    message has to say which one."""
    w = SimpleNamespace(inference_down=True)
    why = app_methods["not_ready_reason"](_app(workers={"cam2": w}))
    assert "yolo-pose" in why
    assert "Nobody is being screened" in why


def test_one_camera_down_names_that_camera_and_not_the_others(app_methods):
    """A partial outage is still a working app — it must not read as a
    total one, or an operator pulls a healthy site apart looking."""
    workers = {"cam2": SimpleNamespace(inference_down=True),
               "cam5": SimpleNamespace(inference_down=False)}
    why = app_methods["not_ready_reason"](_app(workers=workers))
    assert "cam2" in why
    assert "cam5" not in why
    assert "Nobody is being screened" not in why
