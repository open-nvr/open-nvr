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


# ── the camera roster ──────────────────────────────────────────────
#
# The fresh-install bug in its final form. The roster was read once, in
# setup() — the ONE moment a fresh install is guaranteed to have nothing
# assigned. The app starts with the stack; the operator assigns the
# entrance camera afterwards; nothing ever looked again. The app polled
# its config for as long as you left it, screening nobody, having been
# told everything it needed within seconds of starting.


class _Cam:
    def __init__(self, handle):
        self.handle = handle
        self.name = handle


@pytest.fixture
def roster():
    """``_reconcile_roster`` bound to nothing."""
    ns = {"log": SimpleNamespace(info=lambda *a, **k: None,
                                 warning=lambda *a, **k: None)}
    exec(compile(_slice("def _reconcile_roster(self) -> int:",
                        "def on_frame(self, camera_id"),
                 "<roster>", "exec"), ns)  # noqa: S102
    return ns["_reconcile_roster"]


def _site(assigned, workers=None):
    started = []

    def _start(cam):
        started.append(cam.handle)
        app.workers[cam.handle] = SimpleNamespace(
            stop=lambda: stopped.append(cam.handle))

    stopped = []
    app = SimpleNamespace(
        workers=dict(workers or {}),
        nvr=SimpleNamespace(cameras=lambda: [_Cam(h) for h in assigned]),
        _start_worker=_start,
    )
    return app, started, stopped


def test_a_camera_assigned_after_startup_gets_picked_up(roster):
    """The whole bug: this used to need a container restart."""
    app, started, _ = _site(assigned=["cam2"])
    assert roster(app) == 1
    assert started == ["cam2"]


def test_an_already_watched_camera_is_not_restarted(roster):
    """Reconciling every 30s must not mean reopening the stream every
    30s — that would drop whoever is mid-screening, forever."""
    app, started, stopped = _site(assigned=["cam2"],
                                  workers={"cam2": SimpleNamespace(stop=None)})
    assert roster(app) == 1
    assert started == []
    assert stopped == []


def test_unassigning_one_camera_of_several_stops_just_that_one(roster):
    app, started, stopped = _site(
        assigned=["cam2"],
        workers={"cam2": SimpleNamespace(stop=lambda: None),
                 "cam5": SimpleNamespace(stop=lambda: stopped.append("cam5"))})
    roster(app)
    assert "cam5" not in app.workers
    assert "cam2" in app.workers


def test_an_empty_roster_never_tears_down_a_working_site(roster):
    """`cameras()` returns [] for 'none assigned' AND for 'core could
    not be reached'. Treating those alike would turn a core restart into
    an outage, so a removal needs positive evidence."""
    app, _, stopped = _site(assigned=[],
                            workers={"cam2": SimpleNamespace(stop=lambda: None)})
    assert roster(app) == 0
    assert "cam2" in app.workers
    assert stopped == []


def test_a_roster_that_raises_keeps_what_is_running(roster):
    """Same reasoning, for the case where core answers with an error
    rather than an empty list."""
    app, _, _ = _site(assigned=[],
                      workers={"cam2": SimpleNamespace(stop=lambda: None)})
    app.nvr = SimpleNamespace(
        cameras=lambda: (_ for _ in ()).throw(RuntimeError("core is down")))
    assert roster(app) == 1
    assert "cam2" in app.workers


# ── an error envelope is an outage, not an empty frame ─────────────
#
# The adapter puts a §7 FailureEnvelope in the SAME "result" slot a real
# result travels in — deliberately, so one parser handles both. The app
# used to read that as "persons: []", i.e. a frame with nobody in it,
# which is the worst possible reading: _on_frame then RESETS the failure
# count and clears the outage, so /health, /state and not_ready_reason
# all stay green while the app screens nobody for as long as the adapter
# keeps failing. Every signal this file exists to protect goes back to
# lying, through a door nobody had closed.


@pytest.fixture
def bodies_from():
    """``_bodies_from`` bound to a stub PoseUnavailable, SDK-free."""
    ns: dict = {}
    src = _slice("def _bodies_from(result, frame, tracker):", "def _json_dumps")

    class PoseUnavailable(RuntimeError):
        pass

    ns["PoseUnavailable"] = PoseUnavailable
    exec(compile(src, "guard_scan_compliance.py", "exec"), ns)
    ns["_bodies_from"].PoseUnavailable = PoseUnavailable
    return ns["_bodies_from"], PoseUnavailable


FRAME = SimpleNamespace(width=1280, height=720, wall_ts=1000.0)


class _Tracker:
    def update(self, boxes, now):
        return list(range(len(boxes)))


def _person():
    return {"bbox": [10.0, 20.0, 110.0, 220.0], "score": 0.9,
            "keypoints": [[1.0, 2.0, 0.9]] * 17}


def test_an_error_envelope_is_raised_not_read_as_an_empty_frame(bodies_from):
    fn, PoseUnavailable = bodies_from
    envelope = {"status": "error",
                "error": {"category": "model_error", "code": "weights_missing",
                          "message": "not found", "transient": False}}
    with pytest.raises(PoseUnavailable):
        fn(envelope, FRAME, _Tracker())


def test_an_envelope_nested_in_result_is_also_raised(bodies_from):
    """The streaming path embeds the envelope one level down, in the
    result message's own `result` field."""
    fn, PoseUnavailable = bodies_from
    message = {"result": {"status": "error",
                          "error": {"code": "inference_runtime_crash"}}}
    with pytest.raises(PoseUnavailable):
        fn(message, FRAME, _Tracker())


def test_the_envelope_code_reaches_the_operator(bodies_from):
    """Whatever ends up on /state should name the adapter's own code,
    not a generic 'inference failed'."""
    fn, PoseUnavailable = bodies_from
    with pytest.raises(PoseUnavailable, match="weights_missing"):
        fn({"error": {"code": "weights_missing"}}, FRAME, _Tracker())


def test_a_genuinely_empty_frame_is_still_an_empty_frame(bodies_from):
    """The other half: nobody in shot is a normal answer, not an outage.
    Confusing these in the other direction would put a camera watching an
    empty corridor permanently amber."""
    fn, _ = bodies_from
    assert fn({"result": {"persons": []}}, FRAME, _Tracker()) == []


def test_a_real_result_still_parses(bodies_from):
    fn, _ = bodies_from
    bodies = fn({"result": {"persons": [_person()]}}, FRAME, _Tracker())
    assert len(bodies) == 1
    assert bodies[0].box == (10.0, 20.0, 110.0, 220.0)
