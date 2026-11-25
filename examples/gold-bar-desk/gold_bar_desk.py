# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gold bar desk — every bar on the weighing desk, accounted for.

Watches an overhead camera on the desk where bullion bars are weighed and
trimmed, and keeps count of the bars on it: in the trays, on the precision
balance, and in the worker's hands.

* **Possible missing bar** — not every bar has been seen at once for a
  while (25 s by default). High.
* **Extra bar on the desk** — more bars than the shift started with: a
  lookalike swapped in? Medium.
* **Bar held away from the trays** — a bar close to the worker's body,
  outside the trays and the balance. Medium, for review: a timestamp to
  look at, not an accusation.
* **Desk camera blocked or moved.** High.

And every weighing on the precision balance is filed with a photo of the
balance's display, so the reading before and after each trim can be read
back later.

How it is put together
----------------------
``bar_desk.core`` holds the rules and knows nothing about the platform;
``bar_desk.segmenter`` finds bars (FastSAM-s, run here with onnxruntime —
no adapter, the model is small and the app crops to the trays). This
module is where frames come from, where the drawn zones come from, and
where results go: alerts, the live overlay, the app page.

Run:
    python gold_bar_desk.py --config config.yml
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import signal
import sys
import threading
import time
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path

from opennvr_app_sdk import (
    Action, Alert, AlertType, AppManifest, BaseAppConfig, FrameApp, OpenNVR,
    Param, StateView, camera_key, load_app_config,
)
from opennvr_app_sdk.alerts import AlertSource, build_dispatcher
from opennvr_app_sdk.domain_events import DomainEventPublisher

sys.path.insert(0, str(Path(__file__).parent))
from bar_desk.core import DeskMonitor, DeskSettings, Zones, draw  # noqa: E402

log = logging.getLogger("gold-bar-desk")

APP_ID = "gold-bar-desk"
LEDGER_KEY = "weighings"
LEDGER_MAX = 200

MANIFEST = AppManifest(
    id=APP_ID,
    name="Gold Bar Desk",
    version="0.1.0",
    category="safety",
    summary=("Counts the bars on a weighing desk — trays, balance and hands — "
             "alarms when one goes unaccounted for, and photographs every "
             "weighing's reading."),
    subscribes=None,          # drives its own frames, from the desk camera
    params=[
        # ── The desk (drawn per camera, in Set up on its card) ──
        Param("tray_zone", "geometry.polygon", per_camera=True,
              label="Bar trays", group="The desk", required=False,
              description="Draw round the trays the bars sit in. Required: "
                          "nothing is counted until it is drawn."),
        Param("balance_zone", "geometry.polygon", per_camera=True,
              label="Precision balance pan", group="The desk",
              description="The pan a bar sits on to be weighed. Each bar placed "
                          "here is a weighing."),
        Param("balance_display", "geometry.polygon", per_camera=True,
              label="Balance display", group="The desk",
              description="The balance's read-out. Photographed for every "
                          "weighing, so the weight can be read back later."),
        Param("handling_zone", "geometry.polygon", per_camera=True,
              label="Where bars are handled", group="The desk",
              description="The desk area off the trays and the balance where the "
                          "worker holds a bar while working on it. A bar seen "
                          "here is counted as in hand, not missing."),
        Param("body_zone", "geometry.polygon", per_camera=True,
              label="Close to the worker's body", group="The desk",
              description="Where the worker sits or stands. A bar held here for "
                          "more than a moment is logged for review."),
        Param("bars_at_start", int, default=0,
              label="Bars on the desk at the start of the shift",
              group="The desk",
              description="Enter the issued count. 0 learns it from the first "
                          "30 seconds instead, which is weaker: whatever is hidden "
                          "at that moment is never expected back. With several "
                          "desks, give each its own count with Start a new shift."),

        # ── Alarms ──
        Param("missing_after_s", float, default=25.0,
              label="Seconds without seeing every bar before alarming",
              group="Alarms",
              description="One bar is nearly always in hand or under an arm, so "
                          "this is not 'the count is low'. It is 'the full count "
                          "has not been seen even once for this long'."),
        Param("missing_min_frames", int, default=50, advanced=True,
              label="...and over at least this many analysed frames",
              group="Alarms",
              description="The full count shows only in brief moments between "
                          "arms. A busy machine that looks less often misses "
                          "more of them, so the alarm also waits for this many "
                          "looks. 50 is 25 s at 2 frames a second."),
        Param("min_analysis_fps", float, default=1.0, advanced=True,
              label="Slowest analysis that may raise a missing-bar alarm (fps)",
              group="Alarms",
              description="Below this the app cannot tell a hidden bar from a "
                          "missing one, so the alarm is paused and the status "
                          "says so, rather than guessing."),
        Param("near_body_s", float, default=1.0,
              label="Seconds a bar is held near the body before it is logged",
              group="Alarms"),
        Param("near_body_cooldown_s", float, default=60.0,
              label="Quiet time after a near-body log (seconds)",
              group="Alarms"),

        # ── Video and compute ──
        Param("fps", float, default=2.0, advanced=True,
              label="Frames analysed per second", group="Video",
              description="Every rule is timed in seconds, so this trades "
                          "responsiveness for CPU."),
        Param("frame_width", int, default=1920, advanced=True,
              label="Width frames are decoded at (pixels)", group="Video",
              description="Keep this at the camera's full width: a bar must be "
                          "about 30 pixels wide to be segmented."),
        Param("inference_threads", int, default=2, advanced=True,
              label="CPU threads for the segmentation model", group="Video"),
    ],
    actions=[
        Action("start_shift", "Start a new shift",
               params=[Param("camera", str, default="",
                             description="Leave blank for every desk."),
                       Param("bars", int, default=0,
                             description="Bars issued to this desk. 0 uses the "
                                         "'Bars at the start' setting.")],
               description="Clear the alarms and count from now against the bars "
                           "issued (or learn the count, when none is given)."),
        Action("acknowledge_count", "Acknowledge the count alarm",
               params=[Param("camera", str, default="")],
               description="Clear a missing/extra alarm once the bars are checked."),
    ],
    emits=[
        AlertType("bar_missing", severity="high",
                  description="Not every bar has been seen at once for a while."),
        AlertType("bar_extra", severity="medium",
                  description="More bars on the desk than at the start."),
        AlertType("bar_near_body", severity="medium",
                  description="A bar held close to the worker, off the trays."),
        AlertType("camera_tamper", severity="high",
                  description="The desk camera went dark or was moved."),
    ],
    state_schema=[
        StateView("cameras", "Desks", path="cameras", kind="table",
                  columns=["camera", "bars", "at_start", "in_trays", "on_balance",
                           "in_hand", "weighings", "status", "fps", "error"]),
        StateView("weighings", "Weighings — balance display", path="weighings",
                  kind="gallery", limit=12),
        StateView("recent", "Recent", path="recent", kind="log", limit=15),
    ],
    # Lets this app publish its boxes onto the live view.
    provides=["bar_count"],
    overlay=True,
)

#: Settings drawn or typed per camera, in Set up on its card.
PER_CAMERA_KEYS: tuple[str, ...] = ("tray_zone", "balance_zone", "balance_display",
                                    "handling_zone", "body_zone")
ZONE_KEYS = {"tray_zone": "tray", "balance_zone": "balance",
             "balance_display": "display", "handling_zone": "handling",
             "body_zone": "body"}


class _NoPollSource:
    """The base class polls; this app streams. See guard-scan-compliance."""

    def get_frame(self, camera_id: str) -> bytes | None:
        return None


@dataclass
class DeskConfig(BaseAppConfig):
    fps: float = 2.0
    frame_width: int = 1920
    inference_threads: int = 2
    model_path: str = "/weights/FastSAM-s.onnx"
    bars_at_start: int = 0
    missing_after_s: float = 25.0
    missing_min_frames: int = 50
    min_analysis_fps: float = 1.0
    near_body_s: float = 1.0
    near_body_cooldown_s: float = 60.0


def _stream_settings(cfg: dict) -> tuple:
    return (float(cfg.get("fps") or 2.0), int(cfg.get("frame_width") or 1920))


def build_zones(cfg: dict, width: int, height: int) -> Zones | None:
    """The drawn zones in this frame's pixels, or None without a tray zone.
    The zone editor saves 0-1 fractions; literal pixels are accepted too."""
    import numpy as np
    from opennvr_app_sdk.geometry import scale_vertices

    polys = {}
    for key, name in ZONE_KEYS.items():
        pts = [p for p in (cfg.get(key) or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
        polys[name] = (np.array(scale_vertices(pts, width, height), np.float32)
                       if len(pts) >= 3 else None)
    if polys["tray"] is None:
        return None
    return Zones(shape=(height, width), **polys)


class CameraWorker:
    """One desk: frames in, counts and alarms out."""

    def __init__(self, app: "DeskApp", camera) -> None:
        self.app = app
        self.camera = camera
        self.handle = getattr(camera, "handle", str(camera))
        self.name = getattr(camera, "name", self.handle)
        self.stream = None
        self.monitor: DeskMonitor | None = None
        self._stop = threading.Event()
        self._reopen = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream_settings: tuple | None = None
        self._zones_key = None
        #: A count given for this desk alone by Start a new shift; it
        #: outranks the app-wide setting until the next shift starts.
        self.shift_bars = 0
        self.frames = 0
        self.fps = 0.0
        self.last_error: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"desk-{self.handle}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._reopen.set()
        if self.stream is not None:
            self.stream.close()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10.0)

    def stream_settings_changed(self, cfg: dict) -> bool:
        return (self._stream_settings is not None and
                self._stream_settings != _stream_settings(cfg))

    def reopen(self, why: str) -> None:
        log.info("%s: reopening the stream (%s)", self.handle, why)
        self._reopen.set()

    def retune(self, cfg: dict) -> None:
        """Settings saved: thresholds apply now, zones on the next frame."""
        mon = self.monitor
        if mon is None:
            return
        mon.s = DeskSettings.from_config(cfg)
        mon.tracker.hold_s = mon.s.hold_s
        if not self.shift_bars:
            mon.set_baseline(int(cfg.get("bars_at_start") or 0))
        self._zones_key = None            # rebuild zones on the next frame

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except BaseException as exc:  # noqa: BLE001 — see guard-scan
                self.last_error = str(exc) or exc.__class__.__name__
                log.warning("%s: worker failed (%s), retrying", self.handle, exc,
                            exc_info=True)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)) and self._stop.is_set():
                    return
                self._stop.wait(10.0)

    def _session(self) -> None:
        cfg = self.app.camera_config(self.handle)
        self._reopen.clear()
        if not cfg.get("tray_zone"):
            self.last_error = ("draw the bar trays in Set up on this camera's card — "
                               "nothing is counted until then")
            self._stop.wait(15.0)
            return
        # The bars must be seen at full resolution. Core hands apps the
        # substream when a camera has one, and on a 640x360 substream a
        # bar is a dozen pixels wide: nothing would be segmented, and the
        # count would read zero rather than say why.
        grant = self.app.nvr.stream_grant(self.camera)
        if grant and grant.get("substream"):
            self.last_error = ("this camera has a substream, and apps are given it: "
                               "bars are too small to count on it. Remove the "
                               "camera's substream, or set it to full resolution.")
            self._stop.wait(60.0)
            return
        self._stream_settings = _stream_settings(cfg)
        fps, width = self._stream_settings
        self.monitor = DeskMonitor(
            None, Zones(tray=None, shape=(0, 0)),           # set on the first frame
            DeskSettings.from_config(cfg),
            on_event=lambda e: self.app.on_event(self, e),
            bars_at_start=self.shift_bars or int(cfg.get("bars_at_start") or 0))
        self._zones_key = None
        self.stream = self.app.nvr.stream(self.camera, width=width, fps=fps)
        last = time.monotonic()
        for frame in self.stream.frames(timeout=15.0):
            if self._stop.is_set():
                return
            if self._reopen.is_set():
                self.stream.close()
                return
            self._on_frame(frame)
            self.frames += 1
            now = time.monotonic()
            dt, last = now - last, now
            if dt > 0:
                self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)

    def _on_frame(self, frame) -> None:
        image = frame.to_ndarray()
        h, w = image.shape[:2]
        key = (w, h)
        if self._zones_key != key:
            zones = build_zones(self.app.camera_config(self.handle), w, h)
            if zones is None:
                self.last_error = "draw the bar trays in Set up on this camera's card"
                return
            self.monitor.zones = zones
            self.monitor.segment = self.app.segment_fn(w * h)
            self._zones_key = key
        if frame.restarted:
            # The feed broke: what was hidden then is not evidence now.
            self.monitor.full_seen_at = None
        result = self.monitor.analyse(image, frame.wall_ts)
        self.last_error = None
        self.app.publish_overlay(self, result, w, h, frame.seq)


class DeskApp(FrameApp):
    """Roster, config, the model, and where results go."""

    def __init__(self, config) -> None:
        self.config = config
        self._nvr: OpenNVR | None = None
        self._segmenter = None
        self._segmenter_lock = threading.Lock()
        self.events = DomainEventPublisher(
            getattr(config, "nats_alerts_url", "") or "",
            token=getattr(config, "nats_alerts_token", None),
            producer=f"app:{APP_ID}")
        self.workers: dict[str, CameraWorker] = {}
        self._workers_lock = threading.Lock()
        self._results_lock = threading.Lock()
        self._per_camera: dict[int, dict] = {}
        self.recent: list[dict] = []
        self.gallery: list[dict] = []
        self.dispatcher = build_dispatcher(
            webhook_url=getattr(config, "webhook_url", None),
            nats_alerts_url=getattr(config, "nats_alerts_url", None),
            nats_alerts_token=getattr(config, "nats_alerts_token", None))
        super().__init__(config, self.dispatcher, frame_source=_NoPollSource(),
                         cameras=[], poll_interval_seconds=30.0)

    @property
    def nvr(self) -> OpenNVR:
        # Built on first use, after registration minted this app's key:
        # see GuardScanApp.nvr for why that matters.
        if self._nvr is None:
            self._nvr = OpenNVR()
        return self._nvr

    @nvr.setter
    def nvr(self, client) -> None:
        self._nvr = client

    # ── the model: one session shared by every desk ──

    def segment_fn(self, frame_area: int):
        """``segment(crop)`` for frames of ``frame_area`` pixels."""
        from bar_desk.segmenter import Segmenter

        with self._segmenter_lock:
            if self._segmenter is None:
                path = Path(getattr(self.config, "model_path", "") or "/weights/FastSAM-s.onnx")
                if not path.is_file():
                    raise RuntimeError(
                        f"segmentation model missing at {path} — is the "
                        f"fastsam-weights-init service run?")
                self._segmenter = Segmenter(
                    path, threads=int(getattr(self.config, "inference_threads", 2) or 2))
                log.info("segmentation model loaded from %s", path)
        seg = self._segmenter
        return lambda crop: seg.bars(crop, frame_area=frame_area)

    # ── config ──

    def camera_config(self, handle: str) -> dict:
        merged = (asdict(self.config) if is_dataclass(self.config)
                  else dict(self.config or {}))
        for key in PER_CAMERA_KEYS:
            merged[key] = None
        merged.update(self._per_camera.get(camera_key(handle), {}))
        return merged

    def on_config_update(self, config: dict) -> None:
        for key, value in (config or {}).items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        per_camera: dict[int, dict] = {}
        for key in PER_CAMERA_KEYS:
            drawn = (config or {}).get(key)
            if isinstance(drawn, dict):
                for handle, value in drawn.items():
                    cam = camera_key(handle)
                    if cam is not None:
                        per_camera.setdefault(cam, {})[key] = value
        self._per_camera = per_camera
        for handle, worker in self._workers():
            cfg = self.camera_config(handle)
            if worker.stream_settings_changed(cfg):
                worker.reopen("frame rate or width changed")
            elif worker.monitor is None and cfg.get("tray_zone"):
                worker.reopen("trays drawn")
            else:
                worker.retune(cfg)

    # ── results ──

    def on_event(self, worker: CameraWorker, event: dict) -> None:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["t"]))
        if event["type"] == "weighing":
            self._on_weighing(worker, event, when)
            return
        frame = event["frame"]
        jpeg = _jpeg(_mark(draw(frame, worker.monitor), event.get("box")), 82)
        path = None
        try:
            path = self.nvr.save_evidence(jpeg) if jpeg else None
        except Exception as exc:  # noqa: BLE001
            log.warning("could not store the evidence photo: %s", exc)
        mon = worker.monitor
        self.dispatcher.fire(Alert(
            # Named explicitly: the bus only lets an app publish under its
            # own id, and these fire on worker threads (see guard-scan).
            source=AlertSource(kind="app", name=APP_ID, version=MANIFEST.version),
            title=event["title"],
            description=event["detail"],
            camera_id=worker.handle,
            severity=event["severity"],
            alert_type=event["kind"],
            images={"scene": path} if path else {},
            # Plain types only: a numpy value here makes the alert
            # unserialisable, and it is dropped before it is sent.
            evidence={"observed_at": float(event["t"]),
                      "bars": _int(mon.last.get("bars")),
                      "at_start": _int(mon.baseline),
                      "in_trays": _int(mon.last.get("in_trays")),
                      "on_balance": bool(mon.last.get("on_pan")),
                      "in_hand": _int(mon.last.get("in_hand"))},
            tags=["gold-bar-desk", event["kind"]],
        ))
        self._note(worker, when, event["title"], "warning"
                   if event["severity"] in ("high", "critical") else "info")
        log.info("%s: %s", worker.handle, event["detail"])

    def _on_weighing(self, worker: CameraWorker, event: dict, when: str) -> None:
        paths, thumb = [], None
        for crop in event.get("displays") or []:
            jpeg = _jpeg(crop, 85)
            if not jpeg:
                continue
            thumb = thumb or _jpeg(_fit(crop, 240), 75)
            try:
                path = self.nvr.save_evidence(jpeg)
                if path:
                    paths.append(path)
            except Exception as exc:  # noqa: BLE001
                log.debug("could not store a display photo: %s", exc)
        record = {"camera": worker.handle, "n": event["n"], "at": when,
                  "duration_s": event["duration_s"], "display": paths}
        with self._results_lock:
            if thumb:
                self.gallery.append({
                    "image": "data:image/jpeg;base64," + base64.b64encode(thumb).decode(),
                    "label": "%s · weighing %d · %.0f s" % (worker.name, event["n"],
                                                             event["duration_s"]),
                    "time": when})
                del self.gallery[:-12]
        self._remember(record)
        self._note(worker, when, "weighing %d (%.0f s on the balance)"
                   % (event["n"], event["duration_s"]), "info")

    def _remember(self, record: dict) -> None:
        """Keep the weighing ledger across restarts, in core's per-app
        store (the gallery thumbnails are only kept in memory)."""
        try:
            ledger = self.nvr.state.get(LEDGER_KEY, []) or []
            ledger = (list(ledger) + [record])[-LEDGER_MAX:]
            self.nvr.state.set(LEDGER_KEY, ledger)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not save the weighing ledger: %s", exc)

    def _note(self, worker: CameraWorker, when: str, message: str, level: str) -> None:
        with self._results_lock:
            self.recent.append({"time": when, "level": level,
                                "message": f"{worker.name}: {message}"})
            del self.recent[:-50]

    def publish_overlay(self, worker: CameraWorker, result: dict, w: int, h: int, seq) -> None:
        boxes = []
        for tid, tr in (result.get("tracks") or {}).items():
            x, y, bw, bh = tr["box"]
            boxes.append({"label": "Bar" if tr["seen"] == result.get("t") else "Bar (hidden)",
                          "box": [x / w, y / h, bw / w, bh / h], "id": int(tid)})
        for x, y, bw, bh in result.get("hidden_boxes") or []:
            boxes.append({"label": "Bar (partly hidden)",
                          "box": [x / w, y / h, bw / w, bh / h]})
        mon = worker.monitor
        if result.get("on_pan") and mon.zones.balance is not None:
            x0, y0, x1, y1 = mon.zones.rect("balance")
            boxes.append({"label": "On the balance",
                          "box": [x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h]})
        try:
            self.events.publish_overlay(camera_id=worker.handle, boxes=boxes, seq=seq)
        except Exception:  # noqa: BLE001
            log.debug("overlay publish failed", exc_info=True)

    # ── actions ──

    def on_action(self, name: str, params: dict):
        wanted = str((params or {}).get("camera") or "").strip()
        targets = [(h, w) for h, w in self._workers()
                   if not wanted or camera_key(h) == camera_key(wanted)]
        if wanted and not targets:
            raise ValueError(f"no desk camera {wanted!r} is selected for this app")
        if name == "start_shift":
            try:
                bars = int((params or {}).get("bars") or 0)
            except (TypeError, ValueError):
                raise ValueError("bars must be a whole number") from None
            if bars < 0:
                raise ValueError("bars cannot be negative")
            for handle, w in targets:
                w.shift_bars = bars
                if w.monitor is not None:
                    w.monitor.recount()
                    w.monitor.set_baseline(
                        bars or int(self.camera_config(handle).get("bars_at_start") or 0))
            return {"ok": True, "cameras": [h for h, _ in targets], "bars": bars or None}
        if name == "acknowledge_count":
            for _, w in targets:
                if w.monitor is not None:
                    w.monitor.count_alarm = None
                    w.monitor.full_seen_at = None
            return {"ok": True, "cameras": [h for h, _ in targets]}
        raise KeyError(name)

    # ── state the app page shows ──

    def state_snapshot(self) -> dict:
        rows = []
        for handle, w in self._workers():
            mon = w.monitor
            last = (mon.last if mon else {}) or {}
            status = "—"
            if mon is not None:
                status = {"missing": "POSSIBLE MISSING BAR", "extra": "EXTRA BAR"}.get(
                    mon.count_alarm, "learning the start count" if mon.baseline is None
                    else "all accounted for")
            if mon is not None and mon.too_slow:
                # Say so rather than look healthy: at this rate the app
                # cannot tell a hidden bar from a missing one.
                status = ("TOO SLOW TO VERIFY THE COUNT (%.1f fps; needs %.0f) — "
                          "missing-bar alarm paused" % (mon.analysis_fps() or 0,
                                                       mon.s.min_analysis_fps))
            rows.append({"camera": w.name, "bars": last.get("bars", ""),
                         "at_start": (mon.baseline if mon else "") or "",
                         "in_trays": last.get("in_trays", ""),
                         "on_balance": "yes" if last.get("on_pan") else "",
                         "in_hand": last.get("in_hand", ""),
                         "weighings": mon.weighings if mon else 0,
                         "status": status, "fps": round(w.fps, 1),
                         "error": w.last_error or ""})
        with self._results_lock:
            return {"cameras": rows, "weighings": list(reversed(self.gallery)),
                    "recent": list(self.recent)}

    def not_ready_reason(self) -> str | None:
        workers = self._workers()
        if not workers:
            return ("No desk camera selected — pick the camera over the weighing "
                    "desk in this app's configuration.")
        broken = [w for _, w in workers if w.last_error]
        if broken and len(broken) == len(workers):
            return broken[0].last_error
        slow = [w for _, w in workers if w.monitor is not None and w.monitor.too_slow]
        if slow:
            return ("%s analysed too slowly to verify the bar count (%s) — the "
                    "missing-bar alarm is paused there. Give this box more CPU, "
                    "or watch fewer cameras on it." % (
                        ", ".join(w.name for w in slow),
                        ", ".join("%.1f fps" % (w.monitor.analysis_fps() or 0)
                                  for w in slow)))
        return None

    # ── roster: the same shape as guard-scan's ──

    def setup(self) -> None:
        if not self._reconcile_roster():
            log.warning("no desk camera selected for this app yet")

    def on_cameras_update(self, camera_ids) -> None:
        self._reconcile_roster()

    def _workers(self) -> list[tuple[str, CameraWorker]]:
        with self._workers_lock:
            return list(self.workers.items())

    def _reconcile_roster(self) -> int:
        try:
            cameras = self.nvr.roster()
        except Exception as exc:  # noqa: BLE001
            cameras = None
            log.warning("could not read the camera roster (%s)", exc)
        if cameras is None:
            return len(self.workers)
        picked = {c.handle: c for c in cameras}
        for handle, cam in picked.items():
            if handle not in self.workers:
                worker = CameraWorker(self, cam)
                with self._workers_lock:
                    self.workers[handle] = worker
                worker.start()
                log.info("watching %s (%s)", handle, getattr(cam, "name", handle))
        with self._workers_lock:
            departed = [(h, self.workers.pop(h)) for h in list(self.workers)
                        if h not in picked]
        for handle, worker in departed:
            log.info("%s is no longer selected — stopping", handle)
            worker.stop()
        return len(picked)

    def on_frame(self, camera_id: str, frame_bytes: bytes):
        return None

    def handle_tick(self):
        self._reconcile_roster()
        for handle, w in self._workers():
            last = (w.monitor.last if w.monitor else {}) or {}
            log.info("%s: %.1f fps, bars %s/%s%s", handle, w.fps, last.get("bars", "-"),
                     (w.monitor.baseline if w.monitor else None) or "-",
                     f", last error: {w.last_error}" if w.last_error else "")
        return []

    def stop(self) -> None:
        for _, worker in self._workers():
            worker.stop()
        super().stop()


def _int(value):
    return None if value is None else int(value)


def _mark(image, box):
    import cv2
    if box is not None:
        x, y, w, h = box
        cv2.rectangle(image, (x - 20, y - 20), (x + w + 20, y + h + 20), (0, 0, 255), 4)
    return image


def _fit(image, width: int):
    import cv2
    h, w = image.shape[:2]
    if w <= width:
        return image
    return cv2.resize(image, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)


def _jpeg(image, quality: int) -> bytes | None:
    import cv2
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_app_config(args.config, DeskConfig)
    app = DeskApp(cfg)
    app.manifest = MANIFEST
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: app.stop())
        except ValueError:
            pass
    loop.run_until_complete(app.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
