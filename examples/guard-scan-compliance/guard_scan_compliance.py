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
    OpenNVR, Param, StateView, load_app_config,
)
from opennvr_app_sdk.alerts import AlertSource, build_dispatcher
from opennvr_app_sdk.domain_events import DomainEventPublisher

sys.path.insert(0, str(Path(__file__).parent))
from guard_scan.core import ScanEngine, ScanRules, SiteConfig  # noqa: E402
from guard_scan.settings import ScanSettings  # noqa: E402
from guard_scan.tracking import Tracker  # noqa: E402

log = logging.getLogger("guard-scan-compliance")

APP_ID = "guard-scan-compliance"
POSE_ADAPTER = "yolo-pose"
POSE_TASK = "pose_estimation"

#: The contract for a completed screening, compliant or not. Core keeps
#: these; the compliance report is built from them, which is why a clean
#: scan is published too — a compliance rate needs the denominator.
SCREENING_EVENT = "guardscan.screening.v1"


MANIFEST = AppManifest(
    id=APP_ID,
    name="Guard Scan Compliance",
    version="0.1.0",
    category="safety",
    summary=("Checks that the guard wands every person entering — left "
             "arm, right arm, front, back — and flags what the scanner "
             "finds."),
    requires_tasks=[POSE_TASK],
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
        Param("uniform_hsv", "color.hsv_range", default={},
              label="Guard's uniform colour (optional)",
              group="Where people stand",
              description="Optional, and worth more than any behavioural guess: "
                          "drag a box over the guard's shirt in the snapshot. On "
                          "the reference footage the uniform matched 75-100% of "
                          "the guard's frames and none of any customer's."),

        # Superseded by the picker above, and still declared: PUT
        # /config REPLACES the stored config with the declared params
        # only, so dropping these from the manifest would delete a
        # colour an existing install had configured, the first time
        # anyone pressed Save.
        Param("uniform_hsv_low", list, default=[], advanced=True,
              label="Uniform colour, low HSV bound (older format)",
              group="Where people stand",
              description="Only read when no colour has been picked above."),
        Param("uniform_hsv_high", list, default=[], advanced=True,
              label="Uniform colour, high HSV bound (older format)",
              group="Where people stand",
              description="Only read when no colour has been picked above."),

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
        # Identity across frames is the app's job: the adapter detects,
        # it does not track. One tracker per camera.
        self.tracker = Tracker()

    # ── lifecycle ──

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"scan-{self.handle}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.stream is not None:
            self.stream.close()
        if self.engine is not None:
            # A restart is not a reason to lose the screening in progress.
            self.engine.flush(time.time(), reason="left")

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
        bodies = self.app.pose(self.infer, image, frame, self.tracker)
        if bodies is None:
            # Inference is down. Retrying every frame turns one outage
            # into a hundred connection attempts a second and buries the
            # reason in its own log spam.
            self._infer_failures += 1
            if self._infer_failures >= 3:
                time.sleep(min(2.0 * self._infer_failures, 30.0))
            return
        self._infer_failures = 0
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
    # Picked off a camera snapshot: {"low": [h,s,v], "high": [h,s,v]},
    # h on OpenCV's 0-179 scale. The two bare lists below are what this
    # replaced; they are still read when no picked range is stored.
    uniform_hsv: dict = field(default_factory=dict)
    uniform_hsv_low: list = field(default_factory=list)
    uniform_hsv_high: list = field(default_factory=list)
    session_log_dir: str = "/data/sessions"


def _uniform_bounds(cfg: dict) -> tuple[list, list]:
    """The guard's uniform colour, however it was configured.

    A range picked off a camera snapshot wins. The two bare HSV lists
    are what the operator used to have to type, and an install that
    still holds them keeps working — nobody has to re-pick a colour
    because we improved the form.
    """
    picked = cfg.get("uniform_hsv")
    if isinstance(picked, dict):
        low, high = picked.get("low"), picked.get("high")
        if isinstance(low, list) and isinstance(high, list)                 and len(low) == 3 and len(high) == 3:
            return list(low), list(high)
    return list(cfg.get("uniform_hsv_low") or []),         list(cfg.get("uniform_hsv_high") or [])


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
        self.nvr = OpenNVR()
        self.kaic_url = getattr(config, "kaic_url", "") or ""
        self.kaic_key = getattr(config, "kaic_api_key", "") or ""
        self.events = DomainEventPublisher(
            getattr(config, "nats_alerts_url", "") or "",
            token=getattr(config, "nats_alerts_token", None), producer=APP_ID)
        self.workers: dict[str, CameraWorker] = {}
        self.screenings = 0
        self.compliant = 0
        self.recent: list[str] = []
        self._per_camera: dict[str, dict] = {}
        dispatcher = build_dispatcher(
            webhook_url=getattr(config, "webhook_url", None),
            nats_alerts_url=getattr(config, "nats_alerts_url", None),
            nats_alerts_token=getattr(config, "nats_alerts_token", None))
        self.dispatcher = dispatcher
        super().__init__(config, dispatcher, frame_source=_NoPollSource(),
                         cameras=[], poll_interval_seconds=30.0)

    # ── config ──

    def camera_config(self, handle: str) -> dict:
        """This camera's settings: the app's, with per-camera overrides.

        Zones are drawn per camera in the catalog, so two entrances on
        one instance each get their own geometry over shared thresholds.
        """
        from dataclasses import asdict, is_dataclass

        merged = (asdict(self.config) if is_dataclass(self.config)
                  else dict(self.config or {}))
        merged.update(self._per_camera.get(handle, {}))
        return merged

    def on_config_update(self, config: dict) -> None:
        """Live config from the catalog. Workers pick it up on their next
        reconnect rather than mid-screening, so a saved setting cannot
        change the rules half way through ruling on somebody."""
        for key, value in (config or {}).items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        per_camera = {}
        for key in ("scan_zone", "guard_post"):
            drawn = config.get(key)
            if isinstance(drawn, dict):
                for handle, value in drawn.items():
                    per_camera.setdefault(handle, {})[key] = value
        self._per_camera = per_camera

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
        runs. Written where a prune can find it, not into the evidence
        store, whose sweep only ever deletes JPEGs."""
        folder = Path(getattr(self.config, "session_log_dir", None)
                      or "/data/sessions")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{record['session']}.json"
            path.write_text(_json_dumps(record), encoding="utf-8")
        except OSError as exc:
            log.warning("could not write session log: %s", exc)

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
            # A restart of core silently unregisters adapters, and the
            # symptom is a 404 per frame with nothing in the log to say
            # why. Say why.
            log.warning("pose inference failed (%s) — is the %s adapter "
                        "registered with KAI-C?", exc, POSE_ADAPTER)
            return None
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
        rate = (100.0 * self.compliant / self.screenings) if self.screenings else 100.0
        return {
            "screenings": self.screenings,
            "compliance": f"{rate:.0f}%",
            "cameras": [
                {"camera": handle, "fps": round(w.fps, 1),
                 "frames": w.frames, "error": w.last_error or ""}
                for handle, w in self.workers.items()
            ],
            "recent": list(self.recent),
        }

    # ── the FrameApp surface ──

    def setup(self) -> None:
        """Start one worker per assigned camera.

        The roster is whatever the operator assigned this app in the
        catalog — there is no camera list in the config, deliberately,
        so adding a camera is a click rather than a file edit.
        """
        cameras = self.nvr.cameras()
        if not cameras:
            log.warning("no cameras assigned to this app yet — assign an "
                        "entrance camera in the App Catalog")
        for cam in cameras:
            worker = CameraWorker(self, cam)
            self.workers[cam.handle] = worker
            worker.start()
            log.info("watching %s (%s)", cam.handle, cam.name)

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
        for handle, worker in self.workers.items():
            log.info("%s: %.1f fps, %d frames, %d tracked%s", handle,
                     worker.fps, worker.frames, worker.tracker.live,
                     f", last error: {worker.last_error}"
                     if worker.last_error else "")
        return []

    def stop(self) -> None:
        for worker in self.workers.values():
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

    persons = ((result or {}).get("result") or result or {}).get("persons") or []
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
