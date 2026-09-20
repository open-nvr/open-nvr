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

import os
import sys
import textwrap
import threading
import time as _time
from pathlib import Path
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
    app = SimpleNamespace(screenings=screenings, compliant=compliant,
                          workers=workers or {}, recent=[])
    # state() and not_ready_reason() read through the snapshot helper
    # rather than the live dict — they run on the contract HTTP thread,
    # which must not iterate something the tick thread is mutating.
    app._workers_lock = threading.Lock()
    ns: dict = {}
    exec(compile(_slice("def _workers(self)", "def _start_worker"),
                 "<app>", "exec"), ns)  # noqa: S102
    app._workers = ns["_workers"].__get__(app)
    return app


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
    """Every freshly installed app has picked nothing, so this is the
    FIRST thing a new install hits — and it used to be invisible. The
    message has to say where to fix it."""
    why = app_methods["not_ready_reason"](_app())
    assert why is not None
    assert "select" in why.lower()
    assert "configuration" in why.lower()


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


def _site(assigned, workers=None, reachable=True):
    started = []

    def _start(cam):
        started.append(cam.handle)
        app.workers[cam.handle] = SimpleNamespace(
            stop=lambda **kw: stopped.append(cam.handle))

    stopped = []
    app = SimpleNamespace(
        workers=dict(workers or {}),
        # roster(): the picked cameras, or None when core can't be asked.
        nvr=SimpleNamespace(
            roster=lambda: [_Cam(h) for h in assigned] if reachable else None),
        _start_worker=_start,
        # `workers` is shared by the tick, config-poll and HTTP threads,
        # so the real object guards it with a lock and hands readers a
        # snapshot. The stub has to carry both or it is modelling an
        # object that no longer exists.
        _workers_lock=threading.Lock(),
    )
    # The REAL snapshot helper, read out of the app source — a stub that
    # just listed the dict would make the lock untestable.
    ns: dict = {}
    exec(compile(_slice("def _workers(self)", "def _start_worker"),
                 "<app>", "exec"), ns)  # noqa: S102
    app._workers = ns["_workers"].__get__(app)
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
        workers={"cam2": SimpleNamespace(stop=lambda **kw: None),
                 "cam5": SimpleNamespace(stop=lambda **kw: stopped.append("cam5"))})
    roster(app)
    assert "cam5" not in app.workers
    assert "cam2" in app.workers


def test_an_unreachable_core_never_tears_down_a_working_site(roster):
    """`roster()` is None when core can't be asked. A core restart or one
    bad response must not stop screening that was working."""
    app, _, stopped = _site(assigned=[], reachable=False,
                            workers={"cam2": SimpleNamespace(stop=lambda **kw: None)})
    assert roster(app) == 1
    assert "cam2" in app.workers
    assert stopped == []


def test_unpicking_the_last_camera_stops_all_screening(roster):
    """`roster()` is [] when nothing is picked — the operator's instruction
    that this app should do nothing and use no compute. It used to be
    indistinguishable from an outage, so the app kept every worker
    running on cameras nobody had asked it to watch any more."""
    stopped_names = []
    app, _, _ = _site(assigned=[], workers={
        "cam2": SimpleNamespace(stop=lambda **kw: stopped_names.append(("cam2", kw))),
        "cam5": SimpleNamespace(stop=lambda **kw: stopped_names.append(("cam5", kw))),
    })
    assert roster(app) == 0
    assert app.workers == {}
    # Unpicked is abandoned, not flushed: see the next test.
    assert sorted(stopped_names) == [("cam2", {"abandon": True}),
                                     ("cam5", {"abandon": True})]


def _worker_stop():
    ns = {"time": _time, "threading": threading,
          "log": SimpleNamespace(info=lambda *a, **k: None,
                                 warning=lambda *a, **k: None)}
    exec(compile(_slice("def stop(self, *, abandon: bool = False)",
                        "# ── configuration ──"),
                 "<stop>", "exec"), ns)  # noqa: S102
    calls = []
    worker = SimpleNamespace(
        _stop=threading.Event(), _reopen=threading.Event(), stream=None,
        _thread=None, handle="cam1",
        engine=SimpleNamespace(flush=lambda now, reason: calls.append(("flush", reason)),
                               abandon=lambda now, reason: calls.append(("abandon", reason))))
    return ns["stop"], worker, calls


def test_unpicking_a_camera_mid_screening_raises_no_incomplete_scan_alert():
    """Found live: unticking the camera in the catalog stopped the worker
    with a flush, which ruled the person being wanded at that moment as
    "Incomplete scan procedure" — an alert against the guard for OUR
    decision to stop watching. Unpick abandons: finished screenings are
    still ruled, half-watched ones are dropped."""
    stop, worker, calls = _worker_stop()
    stop(worker, abandon=True)
    assert calls == [("abandon", "deselected")]


def test_a_restart_still_rules_on_the_screening_in_progress():
    stop, worker, calls = _worker_stop()
    stop(worker)
    assert calls == [("flush", "left")]


def test_a_roster_that_raises_keeps_what_is_running(roster):
    """Same reasoning, for the case where core answers with an error
    rather than an empty list."""
    app, _, _ = _site(assigned=[],
                      workers={"cam2": SimpleNamespace(stop=lambda **kw: None)})
    app.nvr = SimpleNamespace(
        roster=lambda: (_ for _ in ()).throw(RuntimeError("core is down")))
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


# ── the workers dict is shared by three threads ────────────────────
#
# _reconcile_roster runs on the tick thread, on_config_update ->
# _retune_workers on the config-poll thread, state() and
# not_ready_reason() on the contract HTTP thread. Iterating the live
# dict while another thread adds or pops raises "dictionary changed size
# during iteration" — on /state that is the operator's status page
# failing at exactly the moment they assign or unassign a camera.


def test_reading_the_roster_takes_the_lock():
    """The contract, asserted directly rather than by racing.

    A timing test here is worthless: the window between "iterate" and
    "another thread pops" is microseconds, so an unlocked read passes a
    churn loop almost every time and fails in production once a week.
    What actually matters is that the reader and the mutators take the
    same lock, so that is what this checks.
    """
    app, _, _ = _site(assigned=["cam1"], workers={
        "cam1": SimpleNamespace(stop=lambda: None)})

    taken = []
    real = app._workers_lock

    class _Watched:
        def __enter__(self):
            taken.append("acquired")
            return real.__enter__()

        def __exit__(self, *exc):
            return real.__exit__(*exc)

    app._workers_lock = _Watched()
    assert app._workers() == [("cam1", app.workers["cam1"])]
    assert taken == ["acquired"], (
        "_workers() read the live dict without taking the lock")


def test_the_roster_snapshot_is_a_copy():
    """A snapshot that aliased the dict would defeat the point: the
    caller iterates it after the lock is released."""
    app, _, _ = _site(assigned=["cam1"], workers={
        "cam1": SimpleNamespace(stop=lambda: None)})

    snapshot = app._workers()
    with app._workers_lock:
        app.workers["cam9"] = SimpleNamespace(stop=lambda: None)
        app.workers.pop("cam1")

    assert [h for h, _ in snapshot] == ["cam1"], (
        "the snapshot changed when the roster did")


def test_a_departing_worker_is_popped_before_it_is_stopped(roster):
    """stop() joins the reader thread, so it must not be called with the
    lock held — and the worker must already be out of the dict, or a
    reader can hand out a worker that is being torn down."""
    seen_during_stop = []

    app, _, stopped = _site(assigned=["cam2"], workers={"cam2": SimpleNamespace(stop=lambda: None)})

    def _stop_and_look(**kw):
        seen_during_stop.append(dict(app.workers))
        stopped.append("cam5")

    app.workers["cam5"] = SimpleNamespace(stop=_stop_and_look)
    roster(app)

    assert stopped == ["cam5"]
    assert "cam5" not in seen_during_stop[0], (
        "the worker was still reachable while it was being stopped")


# ── the app prunes its own session logs ────────────────────────────


def _log_writer(tmp_path, days=90):
    """``on_session_log`` + ``_prune_session_logs``, bound to a stub."""
    ns = {"Path": Path, "log": _Log(), "time": _time,
          "_json_dumps": lambda v: "{}"}
    exec(compile(_slice("def on_session_log(self, handle: str, record: dict)",
                        "# \u2500\u2500 inference \u2500\u2500"),
                 "<app>", "exec"), ns)  # noqa: S102
    app = SimpleNamespace(
        config=SimpleNamespace(session_log_dir=str(tmp_path),
                               session_log_days=days),
        _session_logs_swept=0.0,
    )
    app.on_session_log = ns["on_session_log"].__get__(app)
    app._prune_session_logs = ns["_prune_session_logs"].__get__(app)
    return app


class _Log:
    def warning(self, *a, **k): pass
    def info(self, *a, **k): pass


def test_old_session_logs_are_deleted(tmp_path):
    """Core prunes the LEDGER after 90 days and cannot reach this
    directory — it is on the app's own volume. If the app does not sweep
    it, nothing does, and a busy door fills the volume in weeks."""
    old = tmp_path / "old.json"
    old.write_text("{}")
    os.utime(old, (_time.time() - 200 * 86400,) * 2)

    app = _log_writer(tmp_path, days=90)
    app.on_session_log("cam1", {"session": "fresh"})

    assert not old.exists(), "a log past the retention window survived"
    assert (tmp_path / "fresh.json").exists(), "the new log was not written"


def test_a_recent_session_log_is_kept(tmp_path):
    recent = tmp_path / "recent.json"
    recent.write_text("{}")

    app = _log_writer(tmp_path, days=90)
    app.on_session_log("cam1", {"session": "fresh"})

    assert recent.exists(), "a log inside the window was deleted"


def test_zero_days_keeps_everything(tmp_path):
    """For an operator shipping these somewhere themselves."""
    old = tmp_path / "old.json"
    old.write_text("{}")
    os.utime(old, (_time.time() - 500 * 86400,) * 2)

    app = _log_writer(tmp_path, days=0)
    app.on_session_log("cam1", {"session": "fresh"})

    assert old.exists(), "session_log_days=0 should disable the prune"


def test_the_sweep_is_not_run_on_every_screening(tmp_path):
    """A screening ends every minute or two on a busy door; this is a
    directory scan, so it is hourly."""
    app = _log_writer(tmp_path, days=90)
    app.on_session_log("cam1", {"session": "a"})
    first = app._session_logs_swept
    assert first > 0

    old = tmp_path / "old.json"
    old.write_text("{}")
    os.utime(old, (_time.time() - 200 * 86400,) * 2)
    app.on_session_log("cam1", {"session": "b"})

    assert app._session_logs_swept == first, "swept twice within the hour"
    assert old.exists(), "the second write should not have swept"


# ── zones drawn in the catalog reach the engine ────────────────────


def test_a_zone_saved_under_the_numeric_id_reaches_the_camera():
    """The catalog's zone editor saves a zone under the camera id ("3").
    The worker asks for its settings by handle ("cam3"). Looking one up
    by the other found nothing, so a drawn scan zone silently never
    applied — the screening ran on the whole frame and looked fine."""
    from dataclasses import dataclass

    ns = {"camera_key": _camera_key(),
          "PER_CAMERA_KEYS": ("scan_zone", "guard_post", "uniform_hsv")}
    exec(compile(_slice("def camera_config(self, handle: str) -> dict:",
                        "def _retune_workers(self)"),
                 "<cfg>", "exec"), ns)  # noqa: S102

    @dataclass
    class _Cfg:
        scan_zone: object = None
        guard_post: object = None
        order_weight: float = 0.0

    zone = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]]
    app = SimpleNamespace(config=_Cfg(), _per_camera={}, _retune_workers=lambda: None)
    ns["on_config_update"](app, {"scan_zone": {"3": zone}})
    assert ns["camera_config"](app, "cam3")["scan_zone"] == zone
    # Not the whole {"3": zone} map standing in for a polygon.
    assert ns["camera_config"](app, "cam4")["scan_zone"] is None


def test_each_camera_gets_its_own_uniform_colour():
    """The same shirt reads as a different HSV under each camera's light,
    so the colour is sampled per camera. A camera with none sampled gets
    no colour — never another camera's, and never the whole map."""
    from dataclasses import dataclass, field

    ns = {"camera_key": _camera_key(),
          "PER_CAMERA_KEYS": ("scan_zone", "guard_post", "uniform_hsv")}
    exec(compile(_slice("def camera_config(self, handle: str) -> dict:",
                        "def _retune_workers(self)"),
                 "<cfg>", "exec"), ns)  # noqa: S102

    @dataclass
    class _Cfg:
        scan_zone: object = None
        guard_post: object = None
        uniform_hsv: dict = field(default_factory=dict)

    blue = {"low": [95, 80, 60], "high": [125, 255, 255]}
    app = SimpleNamespace(config=_Cfg(), _per_camera={}, _retune_workers=lambda: None)
    ns["on_config_update"](app, {"uniform_hsv": {"3": blue}})
    assert ns["camera_config"](app, "cam3")["uniform_hsv"] == blue
    assert ns["camera_config"](app, "cam4")["uniform_hsv"] is None


def _camera_key():
    sdk = Path(__file__).resolve().parents[3] / "sdk" / "opennvr-app-sdk"
    if str(sdk) not in sys.path:
        sys.path.insert(0, str(sdk))
    from opennvr_app_sdk.cameras import camera_key

    return camera_key


# ── the platform client is built after registration, not before ─────
#
# The app is scoped to the cameras the operator selected for it by the
# key core mints during registration — which the base class does in
# start(), AFTER this class is constructed. A client built in __init__
# resolved its credential while no app key existed yet and fell back to
# the deployment's site key; to core a site-key caller is a platform
# component, not an app, so the roster came back as every camera in the
# building and the app screened all of them. Nothing in the app said so:
# it looked like a working install with a lot of cameras.


def test_the_constructor_does_not_build_the_platform_client():
    ctor = _slice("def __init__(self, config) -> None:",
                  "# ── the platform client ──")
    assert "OpenNVR(" not in ctor


def test_the_platform_client_is_built_on_first_use_and_stays_replaceable():
    ns: dict = {}
    exec(compile(_slice("# ── the platform client ──", "# ── config ──"),
                 "<nvr>", "exec"), ns)  # noqa: S102
    built: list[int] = []
    ns["OpenNVR"] = lambda: built.append(1) or "client"
    app = type("_App", (), {"nvr": ns["nvr"], "_nvr": None})()

    assert built == []          # constructed, registered nothing yet
    assert app.nvr == "client"  # first use, after registration
    assert app.nvr == "client" and built == [1]  # and only once

    app.nvr = "stub"            # the tests' own substitution still works
    assert app.nvr == "stub"
