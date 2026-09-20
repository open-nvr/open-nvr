# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard-scan compliance — did the guard actually scan that person?

A jewellery showroom screens everyone at the door with a hand-held metal
detector. Whether that screening actually happens, properly, on every
person, is the whole control — and it is exactly the thing nobody can
verify after the fact. This app watches the entrance and rules on each
screening: were all four surfaces covered (left arm, right arm, front,
back), and did the wand's red light come on.

Two kinds of alert, kept apart on purpose, because they are two
different problems for two different people:

* **scanner-flagged person** — the wand found metal. A security matter,
  critical, with the person's face and full body attached.
* **improper / no scan** — the guard did not follow the procedure. A
  staff matter, and the alert carries the GUARD's face as well.

How it is put together
----------------------
``guard_scan.core`` holds the screening logic and knows nothing about
the platform: it takes frames with keypoints and hands back screenings.
This module is the other half — where frames come from (a scoped RTSP
grant), where keypoints come from (the pose adapter over KAI-C), and
where results go (alerts, domain events, the live overlay).

That split is why the rules can be tested on a synthetic clock with no
camera, no model and no core, which is how the awkward cases — a
two-minute screening, a tracker that renames people mid-scan — are
pinned down at all.

Run:
    python guard_scan_compliance.py --config config.yml
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import threading
import time
from pathlib import Path

import yaml

from dataclasses import dataclass, field

from opennvr_app_sdk import (
    Alert, AlertType, AppManifest, BaseAppConfig, FrameApp, InferStream,
    OpenNVR, Param, StateView, camera_key, load_app_config,
)
from opennvr_app_sdk.alerts import AlertSource, build_dispatcher
from opennvr_app_sdk.domain_events import DomainEventPublisher

sys.path.insert(0, str(Path(__file__).parent))
from guard_scan.core import ConfigError, ScanEngine, ScanRules, SiteConfig  # noqa: E402
from guard_scan.settings import ScanSettings  # noqa: E402
from guard_scan.tracking import Tracker  # noqa: E402

log = logging.getLogger("guard-scan-compliance")

APP_ID = "guard-scan-compliance"
POSE_ADAPTER = "yolo-pose"
POSE_TASK = "pose_estimation"

#: How many consecutive failed frames before the app stops calling it a
#: blip and starts calling it an outage — in the log, on /state, and in
#: the catalog's status dot. Three, because at ten frames a second a
#: single dropped call means nothing and a third of a second of silence
#: already means something.
INFER_FAILURES_BEFORE_UNHEALTHY = 3


class PoseUnavailable(RuntimeError):
    """The pose adapter could not be reached, or refused the frame.

    Raised rather than swallowed so the worker can record WHY. Without
    this the failure existed only as a log line: every /infer returned
    404, the app screened nobody, and both /state and /health went on
    reporting a perfectly well app.
    """


#: The contract for a completed screening, compliant or not. Core keeps
#: these; the compliance report is built from them, which is why a clean
#: scan is published too — a compliance rate needs the denominator.
#: EVENT_CONTRACTS.md: `<domain>.<event>` is noun.verb-in-past-tense,
#: and the domain is the subject matter — "never the producer or the
#: adapter". A screening is the noun; completing is what happened to
#: it. (It was guardscan.screening.v1, which named this app and left
#: the verb out.)
SCREENING_EVENT = "screening.completed.v1"


MANIFEST = AppManifest(
    id=APP_ID,
    name="Guard Scan Compliance",
    version="0.1.0",
    category="safety",
    summary=("Checks that the guard wands every person entering — left "
             "arm, right arm, front, back — and flags what the scanner "
             "finds."),
    requires_tasks=[POSE_TASK],
    # RFC-0002 decision 7. requires_tasks says what capability this app
    # needs; this says WHICH adapter must be provisioned with it, and it
    # matters here because yolo-pose is not in the standard stack (yolov8
    # is, which is why nothing lists that one). Without it the one-click
    # installer ups the app alone and every /infer 404s — compose
    # depends_on covers the `docker compose up` path and nothing else.
    # Underscored, like license-plate-recognition's fast_plate_ocr: the
    # installer maps it to the compose service by
    # adapter_service_name() -> "yolo-pose-adapter".
    requires_adapters=["yolo_pose"],
    subscribes=None,          # drives its own frames, at video rate
    params=[
        # Ordered and grouped the way a room is actually set up: where
        # people stand first, then what a proper scan is, then the knobs
        # nobody should touch until they have watched it for a day.
        # Everything past the first five is `advanced` — present,
        # reachable, and out of the way of whoever installs this.
        # ── Where people stand ──
        Param("scan_zone", "geometry.polygon", per_camera=True,
              label="Where the person being scanned stands",
              group="Where people stand",
              description="Draw the spot at the door a customer stands on to be "
                          "wanded. Anyone inside it is being scanned, so is not "
                          "the guard. The single most useful thing to set."),
        Param("guard_post", "geometry.polygon", per_camera=True,
              label="Where the guard stands (optional)",
              group="Where people stand",
              description="Optional. Leave it empty and the guard is found by "
                          "behaviour — whoever reaches toward people."),
        # Per camera, like the zones: the same shirt is a different HSV
        # under each camera's lighting and white balance, so a range
        # sampled at one door is a poor match at the next.
        Param("uniform_hsv", "color.hsv_range", per_camera=True,
              label="Guard's uniform colour (optional)",
              group="Where people stand",
              description="Optional, and worth more than any behavioural guess: "
                          "drag a box over the guard's shirt in this camera's "
                          "snapshot. Sampled per camera, because lighting differs. "
                          "On the reference footage the uniform matched 75-100% "
                          "of the guard's frames and none of any customer's."),

        # ── What counts as a proper scan ──
        Param("required_surfaces", list, default=[],
              label="Surfaces that must be covered",
              group="What counts as a proper scan",
              suggestions=["left_arm", "right_arm", "front", "back"],
              description="Leave empty for all four: left arm, right arm, front, "
                          "back."),
        Param("order_weight", float, default=0.0,
              label="Does the ORDER of the scan count?",
              group="What counts as a proper scan",
              choices=[(0.0, "No - only that every surface was covered"),
                       (0.3, "A little - a wrong order makes it a partial scan"),
                       (1.0, "Fully - the sequence counts as much as coverage")],
              description="Off by default, deliberately. On real entrance footage "
                          "guards covered every surface but worked round whichever "
                          "side they were standing on, so scoring the sequence "
                          "would alarm on scans that were perfectly good. Decide "
                          "it with the site after watching a day of their own."),

        # ── Timing ──
        Param("min_screen", float, default=3.0, advanced=True,
              label="Shortest thing that counts as a screening (seconds)",
              group="Timing",
              description="Seconds of wand-on-person before this is treated as a "
                          "screening at all. Keeps passers-by out of the record."),
        Param("session_gap", float, default=8.0, advanced=True,
              label="Quiet seconds before a screening is ruled on",
              group="Timing",
              description="How long the wand may be away from someone before "
                          "their screening is considered over."),
        Param("dwell_s", float, default=0.6, advanced=True,
              label="Time the wand must spend on a surface (seconds)",
              group="Timing",
              description="Cumulative across the screening, not continuous: a "
                          "wand being swept is never still."),
        Param("dwell_decay", float, default=0.25, advanced=True,
              label="How fast that progress drains while the wand is elsewhere",
              group="Timing",
              description="A fraction of real time. Higher forgets faster."),
        Param("no_scan_engaged", float, default=1.0, advanced=True,
              label="Wand time that still counts as 'nobody scanned them'",
              group="Timing",
              description="Above this we saw part of a real screening and say "
                          "nothing, rather than accusing the guard over someone "
                          "we only glimpsed."),
        Param("step_hold_s", float, default=0.0, advanced=True,
              label="How long a covered surface stays covered (seconds)",
              group="Timing",
              description="0 keeps it for the whole screening."),

        # ── The scanner's light ──
        Param("led_ratio", float, default=0.08, advanced=True,
              label="How much red counts as the indicator being lit",
              group="The scanner's light",
              description="The share of the area around the wand that must be "
                          "red. Raise it if red clothing sets it off."),
        Param("led_hits", int, default=3, advanced=True,
              label="Sightings needed before the light is believed",
              group="The scanner's light",
              description="Guards against one red frame from compression or a "
                          "passing reflection."),
        Param("led_window_s", float, default=0.8, advanced=True,
              label="Seconds those sightings must fall within",
              group="The scanner's light",
              description="Keeps the count meaningful at any frame rate."),

        # ── Video ──
        Param("fps", float, default=10.0, advanced=True,
              label="Frames per second, per camera",
              group="Video",
              description="The dominant cost. Lower it before adding cameras."),
        Param("frame_width", int, default=640, advanced=True,
              label="Width frames are decoded at (pixels)",
              group="Video",
              description="Smaller is cheaper, and the wrist-to-torso geometry "
                          "survives the loss well."),

        # ── The full rule set ──
        Param("surface_weights", dict, default={}, advanced=True,
              label="Weight per surface",
              group="The full rule set",
              description="How much each surface is worth. Setting back to 3 "
                          "makes a missed back far more serious than a missed "
                          "arm. Empty means all equal."),
        Param("grades", list, default=[], advanced=True,
              label="Score bands, and what each one is called",
              group="The full rule set",
              description="Empty means the shipped bands: 100% complete, 75% or "
                          "more partial, 1% or more incomplete, 0 no scan."),
        Param("procedure", dict, default={}, advanced=True,
              label="Whole rule set as JSON (overrides everything above)",
              group="The full rule set",
              description="The escape hatch, and the original format. When it is "
                          "set it wins outright and the fields above are ignored. "
                          "Leave it empty unless you have a reason."),
    ],
    emits=[
        AlertType("scanner_flag", severity="critical",
                  description="The wand's indicator lit on this person."),
        AlertType("improper_scan", severity="high",
                  description="The scan missed surfaces or was out of order."),
        AlertType("no_scan", severity="high",
                  description="Someone entered without being scanned."),
    ],
    state_schema=[
        StateView("screenings", "Screenings today", path="screenings"),
        StateView("compliance", "Compliance", path="compliance",
                  description="Share of screenings that covered everything."),
        StateView("cameras", "Cameras", path="cameras", kind="table"),
        StateView("recent", "Recent screenings", path="recent", kind="log",
                  limit=10),
    ],
    # The first-class page this app owns. Without it the app still
    # works, but its results live only in the alert inbox.
    provides=["guard_scan"],
    overlay=True,       # draw what it sees on the operator's live view
)


class _NoPollSource:
    """A frame source that never has a frame.

    The base class polls; this app streams. Handing the poll loop an
    empty source (and overriding the tick below) keeps the inherited
    contract and registration without pretending to sample frames the
    workers are already reading at video rate.
    """

    def get_frame(self, camera_id: str) -> bytes | None:
        return None


class CameraWorker:
    """One camera: frames in, screenings out.

    A thread rather than a coroutine because the decoder and the pose
    call are both blocking, and because one camera falling over should
    not be able to stall the others.
    """

    def __init__(self, app: "GuardScanApp", camera) -> None:
        self.app = app
        self.camera = camera
        self.handle = getattr(camera, "handle", str(camera))
        self.engine: ScanEngine | None = None
        self.stream = None
        self.infer = None
        self._stop = threading.Event()
        self.frames = 0
        self.fps = 0.0
        self.last_error: str | None = None
        self._infer_failures = 0
        # Held separately from `last_error` so that recovering from an
        # outage clears the outage and NOT an unrelated config refusal
        # that is still true.
        self._infer_error: str | None = None
        # Identity across frames is the app's job: the adapter detects,
        # it does not track. One tracker per camera.
        self.tracker = Tracker()
        # What the current stream was opened with, and the flag that
        # asks the run loop for a fresh one.
        self._stream_settings: tuple | None = None
        self._reopen = threading.Event()
        #: The reader thread, once start() has run. stop() joins it.
        self._thread: threading.Thread | None = None

    # ── lifecycle ──

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"scan-{self.handle}")
        self._thread.start()

    def stop(self, *, abandon: bool = False) -> None:
        """Stop this camera's worker, in an order that is safe to call
        from another thread.

        ``abandon=True`` is for a camera the operator UNPICKED: we stop
        watching mid-screening by choice, so rule only the screenings
        that had already finished and drop the rest — ruling a scan we
        walked away from as incomplete blames the guard for our decision
        (it used to raise "Incomplete scan procedure" on every deselect).

        It always was called from another thread — `_reconcile_roster`
        runs on the tick thread, shutdown on the signal handler's — and
        it used to close the stream and flush the engine straight away,
        while the worker thread was still inside `_handle` iterating and
        popping the same `self.sessions`. Two threads mutating one
        engine, with the flush firing alerts and writing files for
        sessions the other thread was still updating. It also never
        joined, so the worker could outlive the object that owned it.

        So: set the flag, close the stream (the only thing that unblocks
        a reader parked on a frame), JOIN, and only then flush — by
        which point this is the only thread left holding the engine. The
        run loop flushes on a clean stream end too; a second flush finds
        no sessions and does nothing.
        """
        self._stop.set()
        self._reopen.set()          # wake a loop waiting to reopen
        if self.stream is not None:
            self.stream.close()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10.0)
            if thread.is_alive():
                # Say so rather than flushing underneath it — a
                # half-stopped worker is worth a log line, and flushing
                # now would recreate exactly the race this method fixes.
                log.warning("%s: worker did not stop within 10s; leaving "
                            "its screening in place", self.handle)
                return
        if self.engine is not None:
            if abandon:
                self.engine.abandon(time.time(), reason="deselected")
            else:
                # A restart is not a reason to lose the screening in progress.
                self.engine.flush(time.time(), reason="left")

    # ── configuration ──

    def stream_settings_changed(self, cfg: dict) -> bool:
        """Would this config need a different STREAM?

        Frame rate and decode width are properties of the decode, fixed
        when the stream was opened. Everything else about a screening —
        the procedure, the thresholds, the zone, the uniform — is read
        per frame and can simply be swapped underneath.
        """
        if self._stream_settings is None:
            return False
        return self._stream_settings != _stream_settings(cfg)

    def reopen(self, why: str) -> None:
        """End the current session so the run loop opens a fresh one.

        Deliberately NOT a thread restart: `_run` already reopens after
        `_session` returns, and going through it keeps one path for
        "start a session" rather than two that can drift.
        """
        log.info("%s: reopening the stream (%s)", self.handle, why)
        self._reopen.set()

    # ── inference health ──

    def _note_infer_failure(self, reason: str) -> None:
        """Count a failed frame, back off, and — once it is clearly an
        outage rather than a blip — say so where an operator will see it.

        Retrying every frame turns one outage into a hundred connection
        attempts a second and buries the reason in its own log spam, so
        the backoff stays. What is new is that the reason now leaves the
        log: `last_error` reaches /state and the catalog, and
        `not_ready_reason` turns the app's status dot amber.
        """
        self._infer_failures += 1
        if self._infer_failures >= INFER_FAILURES_BEFORE_UNHEALTHY:
            self._infer_error = f"pose inference failing: {reason}"
            self.last_error = self._infer_error
            time.sleep(min(2.0 * self._infer_failures, 30.0))

    def _clear_infer_error(self) -> None:
        """Inference is answering again. Retire the outage — and only
        the outage."""
        if self._infer_error is None:
            return
        if self.last_error == self._infer_error:
            self.last_error = None
        self._infer_error = None

    @property
    def inference_down(self) -> bool:
        return self._infer_error is not None

    # ── the loop ──

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except BaseException as exc:  # noqa: BLE001
                # BaseException, not Exception, and deliberately: a bad
                # `procedure` used to raise SystemExit while the engine
                # was being built, which `except Exception` does not
                # catch. The thread died silently, that camera stopped
                # being screened, and the app stayed green. Whatever
                # comes out of a worker gets recorded and retried.
                self.last_error = str(exc) or exc.__class__.__name__
                log.warning("%s: worker failed (%s), retrying", self.handle,
                            exc, exc_info=True)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    # Shutdown still has to be able to stop this thread.
                    if self._stop.is_set():
                        return
                self._stop.wait(10.0)

    def _session(self) -> None:
        cfg = self.app.camera_config(self.handle)
        self._reopen.clear()
        self._stream_settings = _stream_settings(cfg)
        self.engine = self.app.build_engine(self.handle, cfg)
        self.stream = self.app.nvr.stream(
            self.camera, width=int(cfg.get("frame_width", 640)),
            fps=float(cfg.get("fps", 10.0)))
        self.infer = InferStream(self.app.kaic_url, self.app.kaic_key,
                                 adapter=POSE_ADAPTER, camera_id=self.handle)

        last = time.monotonic()
        ended_at = None
        for frame in self.stream.frames(timeout=15.0):
            ended_at = frame.wall_ts
            if self._stop.is_set():
                return
            if self._reopen.is_set():
                # Rule on whoever is mid-screening before the stream
                # goes: they were really scanned, and the new frame rate
                # is no reason to throw that away.
                if self.engine is not None:
                    self.engine.flush(frame.wall_ts, reason="left")
                self.stream.close()
                return
            if frame.restarted and self.engine is not None:
                # The feed broke. Whatever was half-scanned, we did not
                # see the rest of it, so do not pretend otherwise.
                self.engine.abandon(frame.wall_ts)
            self._on_frame(frame)
            self.frames += 1
            now = time.monotonic()
            dt = now - last
            last = now
            if dt > 0:
                self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)

        # The stream ended. Rule on whoever was still being screened —
        # including anyone parked in the orphan hold, whose expiry only
        # ticks while frames arrive. Skipping this loses the LAST
        # screening every time a feed ends, which on a looping test clip
        # is every screening.
        if self.engine is not None and ended_at is not None:
            self.engine.flush(ended_at, reason="left")

    def _on_frame(self, frame) -> None:
        image = frame.to_ndarray()
        try:
            bodies = self.app.pose(self.infer, image, frame, self.tracker)
        except PoseUnavailable as exc:
            was_down = self.inference_down
            self._note_infer_failure(str(exc))
            if self.inference_down and not was_down and self.engine is not None:
                # The moment this stops being a blip and becomes an
                # outage, let go of whatever was mid-screening. Nothing
                # ticks while we are not receiving keypoints — not
                # orphan expiry, not _session_over — so on recovery the
                # first frame sees a huge gap, calls it "left", and
                # publishes a screening that had two of four surfaces
                # when WE went blind as improper_scan against the guard.
                # abandon() is exactly the right verb: rule the ones
                # that had already finished, drop the rest.
                self.engine.abandon(frame.wall_ts, reason="inference_down")
            return
        if bodies is None:
            # A frame we could not encode. Not an outage — nothing to
            # report, and no reason to back off.
            return
        if self.inference_down:
            log.info("%s: pose inference recovered", self.handle)
        self._infer_failures = 0
        self._clear_infer_error()
        engine = self.engine
        bodies = engine._dedupe(bodies, image)
        engine.guard_id = engine.guard.update(bodies, frame.wall_ts,
                                              engine._being_scanned())
        engine._handle(image, bodies, frame.wall_ts)
        self.app.publish_overlay(self.handle, bodies, engine, frame)


@dataclass
class GuardScanConfig(BaseAppConfig):
    """This app's settings on top of the SDK's standard ones."""

    kaic_url: str = ""
    kaic_api_key: str | None = None
    fps: float = 10.0
    frame_width: int = 640
    # What a proper scan is. `procedure` is the whole declarative rule
    # set and stays the engine's contract; the three fields under it are
    # the parts an operator actually decides, assembled into it below.
    # An install that already set `procedure` keeps winning, so nothing
    # configured before this split changes meaning.
    procedure: dict = field(default_factory=dict)
    required_surfaces: list = field(default_factory=list)
    order_weight: float = 0.0
    surface_weights: dict = field(default_factory=dict)
    grades: list = field(default_factory=list)
    # These mirror ScanSettings. Three places carry a default — the
    # engine, this config, and the manifest the catalog renders — and
    # they must agree, or the app runs on numbers nobody chose.
    dwell_s: float = 0.6
    dwell_decay: float = 0.25
    no_scan_engaged: float = 1.0
    step_hold_s: float = 0.0
    min_screen: float = 3.0
    session_gap: float = 8.0
    led_ratio: float = 0.08
    led_hits: int = 3
    led_window_s: float = 0.8
    # Per camera, keyed by camera id: {"3": {"low": [h,s,v], "high": [h,s,v]}},
    # h on OpenCV's 0-179 scale. camera_config() hands each camera its own.
    uniform_hsv: dict = field(default_factory=dict)
    session_log_dir: str = "/data/sessions"
    #: How long to keep those per-frame keypoint logs, in days. They are
    #: the training data this app collects as it runs, ~700KB-1MB per
    #: screening at 10fps, on a volume nothing else sweeps: core prunes
    #: the LEDGER after 90 days but cannot touch this directory, which
    #: lives in this container. Unbounded, a busy entrance fills the
    #: volume in weeks. 0 disables the prune for anyone who is shipping
    #: these somewhere themselves.
    session_log_days: int = 90


def _stream_settings(cfg: dict) -> tuple:
    """The parts of the config that decide how the STREAM is opened."""
    return (float(cfg.get("fps", 10.0) or 10.0),
            int(cfg.get("frame_width", 640) or 640))


#: The settings drawn or sampled per camera, in Set up on its card.
PER_CAMERA_KEYS: tuple[str, ...] = ("scan_zone", "guard_post", "uniform_hsv")


def _uniform_bounds(cfg: dict) -> tuple[list, list]:
    """This camera's uniform colour, from ``camera_config`` — or no
    colour at all, and the guard is found by behaviour."""
    picked = cfg.get("uniform_hsv")
    if isinstance(picked, dict):
        low, high = picked.get("low"), picked.get("high")
        if (isinstance(low, list) and isinstance(high, list)
                and len(low) == 3 and len(high) == 3):
            return list(low), list(high)
    return [], []


def _procedure(cfg: dict) -> dict | None:
    """The rule set, assembled from the parts the form now collects.

    `procedure` remains the engine's contract and the escape hatch: an
    operator (or an install predating the split) who set the whole
    object keeps it, untouched. Otherwise the surfaces, their weights,
    whether order counts and the grade bands are each their own field,
    because one JSON blob in a textarea is not a thing a showroom
    manager can edit — and a typo in it used to take the camera down.

    Returns None when nothing was configured, so ScanRules uses its own
    defaults rather than being handed an empty shell.
    """
    whole = cfg.get("procedure")
    if isinstance(whole, dict) and whole:
        return whole

    out: dict = {}
    weights = cfg.get("surface_weights")
    weights = weights if isinstance(weights, dict) else {}
    surfaces = [s for s in (cfg.get("required_surfaces") or []) if isinstance(s, str)]
    if surfaces:
        out["steps"] = [{"name": s, "weight": float(weights.get(s, 1.0))}
                        for s in surfaces]
    elif weights:
        # Weights alone re-weight the shipped surfaces rather than
        # silently doing nothing.
        out["steps"] = [{"name": s["name"],
                         "weight": float(weights.get(s["name"], s["weight"]))}
                        for s in ScanRules.DEFAULT["steps"]]
    if "order_weight" in cfg:
        # Only when the operator actually chose: writing it
        # unconditionally made `out` non-empty for an app nobody has
        # configured, so this never returned None and the engine was
        # always handed a shell instead of using its own defaults.
        try:
            out["order_weight"] = max(0.0, min(1.0, float(cfg["order_weight"])))
        except (TypeError, ValueError):
            out["order_weight"] = 0.0
    grades = cfg.get("grades")
    if isinstance(grades, list) and grades:
        out["grades"] = grades
    return out or None


class GuardScanApp(FrameApp):
    """The app: roster, config, inference, and where results go.

    A FrameApp by inheritance — it gets the contract endpoints, registry
    self-registration and live config from the base, and every app in
    the catalog is then the same shape to read.

    It does NOT use the base's poll loop for the actual work, because
    this app watches rather than samples: the screening logic needs ten
    frames a second and the poll loop is a tick every few seconds. The
    per-camera workers below run at video rate; the inherited tick is
    left as a slow heartbeat that keeps /health and /state honest.
    """

    def __init__(self, config) -> None:
        self.config = config
        #: The platform client is built on FIRST USE, never here — see
        #: the ``nvr`` property below.
        self._nvr: OpenNVR | None = None
        self.kaic_url = getattr(config, "kaic_url", "") or ""
        self.kaic_key = getattr(config, "kaic_api_key", "") or ""
        self.events = DomainEventPublisher(
            getattr(config, "nats_alerts_url", "") or "",
            token=getattr(config, "nats_alerts_token", None),
            # `app:<name>`, per the envelope table — the same shape
            # license-plate-recognition and occupancy-counting send.
            producer=f"app:{APP_ID}")
        self.workers: dict[str, CameraWorker] = {}
        #: `workers` is touched by three threads — the tick thread
        #: (_reconcile_roster), the config-poll thread (on_config_update
        #: -> _retune_workers) and the contract HTTP thread (state,
        #: not_ready_reason). Iterating it while another thread adds or
        #: pops raises "dictionary changed size during iteration", which
        #: on /state means the operator's status page 500s at exactly
        #: the moment they assign or unassign a camera. Mutations take
        #: this lock; readers take a snapshot through _workers().
        self._workers_lock = threading.Lock()
        self.screenings = 0
        self.compliant = 0
        self.recent: list[str] = []
        self._per_camera: dict[int, dict] = {}
        #: Monotonic-ish stamp of the last session-log sweep. 0.0 so the
        #: first screening after a boot sweeps, which is when a volume
        #: that filled while the app was down gets dealt with.
        self._session_logs_swept = 0.0
        dispatcher = build_dispatcher(
            webhook_url=getattr(config, "webhook_url", None),
            nats_alerts_url=getattr(config, "nats_alerts_url", None),
            nats_alerts_token=getattr(config, "nats_alerts_token", None))
        self.dispatcher = dispatcher
        super().__init__(config, dispatcher, frame_source=_NoPollSource(),
                         cameras=[], poll_interval_seconds=30.0)

    # ── the platform client ──

    @property
    def nvr(self) -> OpenNVR:
        """The client this app talks to core with, built on first use.

        Not in ``__init__``: core mints this app's own key during
        registration, which the base class does in ``start()`` — after
        the constructor has run. A client built before that resolves its
        credential when no app key exists yet and falls back to the
        deployment's site key, and to core a site-key caller is a
        platform component rather than an app: unscoped, every camera in
        the building, the operator's camera selection bypassed. The SDK
        now re-reads the key per call as well (belt and braces), but the
        cheapest way not to hold a credential from before registration
        is not to build the client until it is wanted.
        """
        if self._nvr is None:
            self._nvr = OpenNVR()
        return self._nvr

    @nvr.setter
    def nvr(self, client) -> None:
        self._nvr = client

    # ── config ──

    def camera_config(self, handle: str) -> dict:
        """This camera's settings: the app's, with per-camera overrides.

        Zones are drawn per camera in the catalog, so two entrances on
        one instance each get their own geometry over shared thresholds.
        """
        from dataclasses import asdict, is_dataclass

        merged = (asdict(self.config) if is_dataclass(self.config)
                  else dict(self.config or {}))
        # The app-wide value of a per-camera setting is the WHOLE map
        # ({"3": zone, "4": zone}). A camera with no entry of its own must
        # get nothing, not that map standing in for a polygon or a colour.
        for key in PER_CAMERA_KEYS:
            merged[key] = None
        # Keyed by camera id: the catalog's zone editor saves "3", the
        # roster calls the same camera "cam3", and looking one up by the
        # other found nothing — a drawn zone that silently never applied.
        merged.update(self._per_camera.get(camera_key(handle), {}))
        return merged

    def on_config_update(self, config: dict) -> None:
        """Apply a saved setting to the running engines.

        This used to stop at the dataclass, on the reasoning that
        workers would pick changes up "on their next reconnect rather
        than mid-screening". The instinct was right and the delivery was
        not: a worker's stream ends only when the app shuts down — a
        camera drop is repaired inside the stream itself, which keeps
        yielding to the SAME engine — so "next reconnect" meant a
        container restart, and an operator's change sat in the database
        doing nothing with nothing on screen to say so.

        The care survives, in a better place: `ScanEngine.retune` leaves
        a screening already in progress under the rules it began with,
        so nobody is re-judged half way through being wanded. It is the
        NEXT screening that gets the new procedure.

        Zones arrive here too, and until now they were never read at
        all: workers are built before the config poll starts, so the
        engines were created without the polygons an operator had drawn.
        """
        for key, value in (config or {}).items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        per_camera: dict[int, dict] = {}
        for key in PER_CAMERA_KEYS:
            drawn = config.get(key)
            if isinstance(drawn, dict):
                for handle, value in drawn.items():
                    cam = camera_key(handle)
                    if cam is not None:
                        per_camera.setdefault(cam, {})[key] = value
        self._per_camera = per_camera
        self._retune_workers()

    def _retune_workers(self) -> None:
        """Push the new settings into every running engine."""
        for handle, worker in self._workers():
            cfg = self.camera_config(handle)
            # Frame rate and decode width belong to the STREAM, not the
            # rules, and cannot be changed under a live one. Ask the
            # worker to reopen instead — and only when they actually
            # changed, so an unrelated save never drops the picture.
            if worker.stream_settings_changed(cfg):
                worker.reopen("frame rate or width changed")
                continue
            engine = worker.engine
            if engine is None:
                continue          # not started yet; it will read this config
            try:
                low, high = _uniform_bounds(cfg)
                engine.retune(
                    ScanSettings.from_config(cfg),
                    site=SiteConfig({
                        "uniform": {"hsv_low": low, "hsv_high": high},
                        "scan_zone": {"polygon": cfg.get("scan_zone") or []},
                    }),
                    rules=ScanRules(_procedure(cfg)),
                )
            except ConfigError as exc:
                # A procedure that will not parse must not stop the
                # camera. Keep what was working, and say why — on the
                # app's health line as well as in the log, because an
                # operator who just pressed Save is owed an answer.
                worker.last_error = f"config refused: {exc}"
                log.warning("%s: new config refused (%s) — keeping the "
                            "previous settings", handle, exc)
            else:
                worker.last_error = None
                log.info("%s: settings applied live (order_weight=%s, "
                         "surfaces=%s, zone=%s)", handle,
                         engine.rules.order_weight, engine.rules.steps,
                         "set" if (cfg.get("scan_zone") or []) else "none")

    def build_engine(self, handle: str, cfg: dict) -> ScanEngine:
        low, high = _uniform_bounds(cfg)
        site = SiteConfig({
            "uniform": {"hsv_low": low, "hsv_high": high},
            "scan_zone": {"polygon": cfg.get("scan_zone") or []},
        })
        rules = ScanRules(_procedure(cfg))
        return ScanEngine(
            ScanSettings.from_config(cfg),
            site=site, rules=rules, camera=handle,
            on_alert=lambda record: self.on_alert(handle, record),
            on_screening=lambda summary: self.on_screening(handle, summary),
            on_session_log=lambda record: self.on_session_log(handle, record),
            save_image=self.save_image,
        )

    # ── sinks ──

    def save_image(self, name: str, jpeg: bytes) -> str | None:
        """Store one crop and return its path.

        Photos go to the evidence store, never into the alert: an alert
        is a bus message with a payload ceiling, and crops blow past it —
        at which point the broker drops the alert and nobody is told
        anything at all.
        """
        return self.nvr.save_evidence(jpeg)

    def on_alert(self, handle: str, record: dict) -> None:
        kind = record["kind"]
        severity = record["severity"]
        self.dispatcher.fire(Alert(
            # Named explicitly, not left to the SDK default. The bus
            # lets an app publish only under its OWN id
            # (opennvr.alerts.app.<id>.<camera>), and the default source
            # name is the generic "opennvr-app" — an alert fired under
            # that is refused as a Publish Violation and reaches nobody
            # while this app's log says it fired. Not set_default_source
            # either: that is a ContextVar, and these alerts are raised
            # on per-camera worker THREADS, which do not inherit it.
            source=AlertSource(kind="app", name=APP_ID,
                               version=MANIFEST.version),
            title=record["title"],
            description=record["detail"],
            camera_id=handle,
            severity=severity,
            alert_type=kind,
            images=record.get("images") or {},
            evidence={
                "score": record.get("score"),
                "steps_done": record.get("steps_done"),
                "steps_missing": record.get("steps_missing"),
                "observed_at": record.get("at"),
                "session": record.get("id"),
            },
            tags=["guard-scan", kind],
        ))

    def on_screening(self, handle: str, summary: dict) -> None:
        self.screenings += 1
        if summary.get("verdict") == "compliant":
            self.compliant += 1
        self.recent.append(
            f"{summary.get('at')} {handle} {summary.get('verdict')} "
            f"{summary.get('score')}%")
        del self.recent[:-10]
        # Core remembers; the app measures. Every screening is published,
        # the clean ones included — a compliance rate with no denominator
        # is just a complaint count.
        self.events.publish(SCREENING_EVENT, camera_id=handle, payload=summary)

    def on_session_log(self, handle: str, record: dict) -> None:
        """The per-frame keypoints: the training set this collects as it
        runs. Written into this app's own volume, not the evidence
        store, whose sweep only ever deletes JPEGs."""
        folder = Path(getattr(self.config, "session_log_dir", None)
                      or "/data/sessions")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{record['session']}.json"
            path.write_text(_json_dumps(record), encoding="utf-8")
        except OSError as exc:
            log.warning("could not write session log: %s", exc)
            return
        self._prune_session_logs(folder)

    def _prune_session_logs(self, folder: Path) -> None:
        """Drop keypoint logs past `session_log_days`.

        This has to happen HERE. Core prunes the screening ledger after
        90 days and used to believe it pruned these too, by unlinking
        `<recordings>/.guardscan/<session>.json` — a path nothing has
        ever written. These live on this container's own volume, so core
        cannot reach them, and nothing was being deleted while two
        comments said otherwise. At roughly 700KB-1MB per screening, a
        busy door fills the volume in weeks.

        Swept at most once an hour rather than on every write: a
        screening ends every minute or two on a busy door and this is a
        directory scan.
        """
        days = int(getattr(self.config, "session_log_days", 90) or 0)
        if days <= 0:
            return
        now = time.time()
        if now - self._session_logs_swept < 3600:
            return
        self._session_logs_swept = now
        cutoff = now - days * 86400
        dropped = 0
        try:
            for entry in folder.glob("*.json"):
                try:
                    if entry.stat().st_mtime < cutoff:
                        entry.unlink()
                        dropped += 1
                except OSError:
                    continue        # vanished under us, or not ours to delete
        except OSError as exc:
            log.warning("could not sweep session logs: %s", exc)
            return
        if dropped:
            log.info("pruned %d session log(s) older than %d days",
                     dropped, days)

    # ── inference ──

    def pose(self, infer, image, frame, tracker):
        """Keypoints for one frame, or None when the adapter is unhappy."""
        import cv2

        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return None
        try:
            result = infer.infer(buf.tobytes())
        except Exception as exc:  # noqa: BLE001
            # The symptom of an unreachable or unregistered adapter is a
            # 404 per frame. Saying why in the log was the first half of
            # the fix; RAISING is the second, so the worker can put it
            # somewhere an operator actually looks. Swallowing it here
            # meant the app screened nobody for hours while /state
            # reported no error at all.
            log.warning("pose inference failed (%s) — is the %s adapter "
                        "registered with KAI-C?", exc, POSE_ADAPTER)
            raise PoseUnavailable(str(exc) or exc.__class__.__name__) from exc
        return _bodies_from(result, frame, tracker)

    def publish_overlay(self, handle, bodies, engine, frame) -> None:
        """Boxes for the operator's live view: who the guard is, who is
        being screened, and which surfaces are done so far."""
        from guard_scan.core import STEP_LABEL, STEPS

        boxes = []
        for body in bodies:
            is_guard = body.track_id == engine.guard_id
            session = engine.sessions.get(body.track_id)
            if is_guard:
                label = "Guard"
            elif session is not None:
                # The checklist, on the person it is about: which
                # surfaces are done and what the scan is worth so far.
                done = [STEP_LABEL[s][:1] for s in STEPS if s in session.done]
                result = session.result()
                label = (f"Scanning {''.join(done) or '—'} "
                         f"{round(result['score'])}%")
                # The wand going off is the one thing on this camera an
                # operator must not miss, and until now it appeared only
                # in the inbox — never on the picture they are watching.
                if session.flagged:
                    label = f"FLAGGED · {label}"

            else:
                label = f"Person {body.track_id}"
            x1, y1, x2, y2 = body.box
            boxes.append({
                "label": label,
                # The contract's shape: x, y, w, h as fractions of the
                # frame, because an app never knows what resolution the
                # operator is watching at.
                "box": [x1 / frame.width, y1 / frame.height,
                        (x2 - x1) / frame.width, (y2 - y1) / frame.height],
                "id": int(body.track_id),
            })
        try:
            self.events.publish_overlay(camera_id=handle, boxes=boxes,
                                        seq=frame.seq)
        except Exception:  # noqa: BLE001
            log.debug("overlay publish failed", exc_info=True)

    # ── state the catalog shows ──

    def state(self) -> dict:
        # No screenings is NOT 100% compliance. It used to read that
        # way, which meant a completely dead app — no cameras, no
        # adapter, nothing screened at all — displayed the best number
        # on the page. An empty denominator has no answer, and saying so
        # is the honest one.
        compliance = (
            f"{100.0 * self.compliant / self.screenings:.0f}%"
            if self.screenings else "— (nothing screened yet)"
        )
        return {
            "screenings": self.screenings,
            "compliance": compliance,
            "cameras": [
                {"camera": handle, "fps": round(w.fps, 1),
                 # Frames DECODED, which is not the same as frames
                 # understood: during an adapter outage this number
                 # keeps climbing while nothing is screened. That is
                 # why the error column beside it matters.
                 "frames": w.frames, "error": w.last_error or ""}
                for handle, w in self._workers()
            ],
            "recent": list(self.recent),
        }

    def not_ready_reason(self) -> str | None:
        """Why this app cannot currently do its job, in one sentence.

        The catalog turns this into the status dot (``routers/apps.py``
        maps it to ``degraded``). Before it existed, every one of the
        conditions below looked exactly like a healthy app: the
        container was up, /health said ready, and the only evidence that
        anything was wrong was that alerts never arrived.

        Ordered by what an operator can act on first.
        """
        workers = self._workers()
        if not workers:
            return ("No cameras selected — select the entrance camera in this "
                    "app's configuration (App Catalog → Configure → Cameras).")
        down = [h for h, w in workers if w.inference_down]
        if len(down) == len(workers):
            return (f"Pose inference is failing on every camera — check the "
                    f"'{POSE_ADAPTER}' adapter is running and registered. "
                    f"Nobody is being screened.")
        if down:
            return (f"Pose inference is failing on {', '.join(sorted(down))} "
                    f"— those cameras are not being screened.")
        return None

    # ── the FrameApp surface ──

    def setup(self) -> None:
        """Start one worker per picked camera.

        The cameras are the ones picked for this app in its own
        configuration — there is no camera list in the config file,
        deliberately, so adding a camera is a click rather than a file edit.
        """
        if not self._reconcile_roster():
            log.warning("no cameras selected for this app yet — select the "
                        "entrance camera in its configuration")

    def on_cameras_update(self, camera_ids) -> None:
        """A pick changed in the catalog: follow it now rather than on the
        next 30 s tick."""
        self._reconcile_roster()

    def _workers(self) -> list[tuple[str, "CameraWorker"]]:
        """A stable list of (handle, worker) to iterate outside the lock.

        Cheap — a handful of cameras — and it keeps every reader off the
        live dict, so a roster change during a /state render cannot
        raise.
        """
        with self._workers_lock:
            return list(self.workers.items())

    def _start_worker(self, cam) -> None:
        worker = CameraWorker(self, cam)
        with self._workers_lock:
            self.workers[cam.handle] = worker
        worker.start()
        log.info("watching %s (%s)", cam.handle, cam.name)

    def _reconcile_roster(self) -> int:
        """Make the running workers match the cameras picked for this app.
        Returns how many cameras are picked (or running, when core can't
        be asked).

        Re-checked on every tick and whenever the picks change, so picking
        a camera in the catalog starts screening it without a restart.

        Two answers must never be confused, and ``roster()`` keeps them
        apart:

        * ``None`` — core could not be asked. Keep every worker running: a
          core restart or one bad response must not tear a working site
          down.
        * ``[]`` — nothing is picked. Stop every worker. Nothing picked
          means this app does nothing and uses no compute, and an operator
          unpicking the last camera is exactly that instruction.
        """
        try:
            cameras = self.nvr.roster()
        except Exception as exc:  # noqa: BLE001
            cameras = None
            log.warning("could not read the camera roster (%s)", exc)
        if cameras is None:
            if self.workers:
                log.warning("camera roster unavailable — keeping the %d camera(s) "
                            "already being screened", len(self.workers))
            return len(self.workers)

        picked = {c.handle: c for c in cameras}
        for handle, cam in picked.items():
            if handle not in self.workers:
                self._start_worker(cam)

        # Pop under the lock, stop outside it: stop() joins the reader
        # thread, and holding the lock across a join would block /state
        # for as long as a wedged worker takes to notice.
        with self._workers_lock:
            gone = [h for h in self.workers if h not in picked]
            departed = [(h, self.workers.pop(h)) for h in gone]
        for handle, worker in departed:
            log.info("%s is no longer selected for this app — stopping", handle)
            worker.stop(abandon=True)
        return len(picked)

    def on_frame(self, camera_id: str, frame_bytes: bytes):
        """Unused: the workers read frames themselves, at video rate."""
        return None

    def handle_tick(self):
        """The inherited heartbeat. The workers do the work; this reports
        how fast each is actually going.

        The frame rate is the number to watch: every threshold in the
        rules is in seconds, but a step the wand holds for half a second
        is only SEEN if frames arrive during it. A camera quietly running
        at two frames a second loses the short passes and reports a
        clean scan as incomplete.
        """
        # Pick up a camera the operator assigned since the last tick.
        # A click in the catalog does not change this app's CONFIG, so
        # the config poll never hears about it — the roster has to be
        # asked for.
        self._reconcile_roster()
        for handle, worker in self._workers():
            log.info("%s: %.1f fps, %d frames, %d tracked%s", handle,
                     worker.fps, worker.frames, worker.tracker.live,
                     f", last error: {worker.last_error}"
                     if worker.last_error else "")
        return []

    def stop(self) -> None:
        for _, worker in self._workers():
            worker.stop()
        super().stop()


def _bodies_from(result, frame, tracker):
    """Adapter output → the Body objects the engine reasons about.

    The adapter answers "where are the people in this picture" and
    stops there. Numbering them in arrival order would be worse than
    useless: detections come back ordered by confidence, so that order
    flips between frames and the guard and the customer trade
    identities several times a second — which quietly turns a complete
    scan into an incomplete one. The tracker gives each person an id
    that survives the next frame.
    """
    import numpy as np

    from guard_scan.core import Body

    body = (result or {}).get("result") or result or {}

    # A §7 FailureEnvelope travels in the SAME "result" slot as a real
    # result — that is deliberate on the adapter's side, so one parser
    # handles both — and the old expression turned it into an empty
    # persons list. An error then looked exactly like a frame with
    # nobody in it: _on_frame reset _infer_failures, cleared the outage,
    # and left /health green while the app screened nobody for as long
    # as the adapter kept failing. The whole PoseUnavailable apparatus
    # exists for this case; it was being walked around.
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) or body.get("status") == "error":
            detail = ""
            if isinstance(error, dict):
                detail = str(error.get("code") or error.get("message") or "")
            raise PoseUnavailable(detail or "adapter returned an error envelope")

    persons = body.get("persons") or []
    kept, boxes = [], []
    for person in persons:
        kps = person.get("keypoints") or []
        if len(kps) < 17:
            continue
        box = tuple(float(v) for v in
                    (person.get("bbox") or [0, 0, frame.width, frame.height]))
        kept.append((person, box))
        boxes.append(box)

    ids = tracker.update(boxes, frame.wall_ts)
    bodies = []
    for (person, box), track_id in zip(kept, ids):
        arr = np.array(
            [[float(p[0]), float(p[1]), float(p[2])]
             for p in person["keypoints"][:17]], dtype=np.float32)
        bodies.append(Body(track_id, box, arr, 0.35))
    return bodies


def _json_dumps(value) -> str:
    import json

    return json.dumps(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_app_config(args.config, GuardScanConfig)
    app = GuardScanApp(cfg)
    app.manifest = MANIFEST

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: app.stop())
        except ValueError:
            pass          # not the main thread (tests)
    loop.run_until_complete(app.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
