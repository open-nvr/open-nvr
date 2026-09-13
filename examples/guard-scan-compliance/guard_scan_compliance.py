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
        # ── the room ──
        Param("scan_zone", "geometry.polygon", per_camera=True,
              description="Where the person being screened stands. Anyone "
                          "inside it is being scanned, so is not the guard."),
        Param("guard_post", "geometry.polygon", per_camera=True,
              description="Where the guard stands. Optional — the guard is "
                          "found by behaviour when this is left empty."),
        Param("uniform_hsv_low", list, default=[],
              description="Guard uniform colour, low HSV bound. Optional, "
                          "and worth more than any behavioural guess."),
        Param("uniform_hsv_high", list, default=[]),
        # ── the procedure ──
        Param("procedure", dict, default={},
              description="Which surfaces must be covered, what each is "
                          "worth, whether the ORDER counts, and the score "
                          "bands. Empty means the shipped default."),
        Param("dwell_s", float, default=0.6,
              description="How much time the wand must spend on a surface "
                          "for it to count. Cumulative across the screening: "
                          "a wand being swept is never still."),
        Param("dwell_decay", float, default=0.25,
              description="How fast that progress drains while the wand is "
                          "elsewhere, as a fraction of real time."),
        Param("no_scan_engaged", float, default=1.0,
              description="How much wand-on-person time is still consistent "
                          "with 'nobody scanned them'. Above it, we saw part "
                          "of a real screening and say nothing."),
        Param("step_hold_s", float, default=0.0,
              description="How long a covered surface stays covered before "
                          "it must be re-earned. 0 keeps it for the whole "
                          "screening."),
        Param("min_screen", float, default=3.0,
              description="Seconds of wand-on-person before this counts as "
                          "a screening at all. Keeps passers-by out."),
        Param("session_gap", float, default=8.0,
              description="Quiet seconds before a screening is ruled on."),
        # ── the scanner's light ──
        Param("led_ratio", float, default=0.08,
              description="How much of the area around the wand must be lit "
                          "red. Raise it if red clothing sets it off."),
        Param("led_hits", int, default=3),
        Param("led_window_s", float, default=0.8),
        # ── the video ──
        Param("fps", float, default=10.0,
              description="Frames per second per camera. The dominant cost."),
        Param("frame_width", int, default=640),
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
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.warning("%s: worker failed (%s), retrying", self.handle,
                            exc, exc_info=True)
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
    procedure: dict = field(default_factory=dict)
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
    uniform_hsv_low: list = field(default_factory=list)
    uniform_hsv_high: list = field(default_factory=list)
    session_log_dir: str = "/data/sessions"


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
        site = SiteConfig({
            "uniform": {"hsv_low": cfg.get("uniform_hsv_low") or [],
                        "hsv_high": cfg.get("uniform_hsv_high") or []},
            "scan_zone": {"polygon": cfg.get("scan_zone") or []},
        })
        rules = ScanRules(cfg.get("procedure") or None)
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
        boxes = []
        for body in bodies:
            is_guard = body.track_id == engine.guard_id
            session = engine.sessions.get(body.track_id)
            label = "Guard" if is_guard else f"Person {body.track_id}"
            if session is not None and not is_guard:
                done = len(session.done)
                label = f"{label} — {done}/4"
            boxes.append({
                "x1": body.box[0] / frame.width, "y1": body.box[1] / frame.height,
                "x2": body.box[2] / frame.width, "y2": body.box[3] / frame.height,
                "label": label,
                "colour": "#4da3ff" if is_guard else "#ffffff",
            })
        try:
            self.events.publish_overlay(camera_id=handle, boxes=boxes)
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
