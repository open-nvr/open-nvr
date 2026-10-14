# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X-ray screening — what went through the baggage scanner.

Watches a baggage X-ray scanner's screen as a camera (a capture card on
the scanner's video-out, or a fake camera made from a folder of exported
scans) and, for every bag, records how many trays went through and what
they were made of, and flags the scans a person should look at:

* **dense object** — a solid shape the beam could not get through.
  Precious metal looks like this on screen. High.
* **small dense item** — compact, very dark, about jewellery size. Medium.
* **cluttered tray** — so much metal and electronics that something small
  could hide in it. Medium.

It never says "gold". It says "look here".

How it is put together
----------------------
``xray.core`` holds the analysis and knows nothing about the platform: a
frame in, a scan out. This module is the other half — where frames come
from (a scoped RTSP grant per picked camera), and where scans go (alerts
with the annotated scan as evidence, boxes on the live view, the app
page).

Run:
    python xray_screening.py --config config.yml
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
    Alert, AlertType, AppManifest, BaseAppConfig, FrameApp, OpenNVR, Param,
    StateView, camera_key, load_app_config,
)
from opennvr_app_sdk.alerts import AlertSource, build_dispatcher
from opennvr_app_sdk.domain_events import DomainEventPublisher

sys.path.insert(0, str(Path(__file__).parent))
from xray.core import (  # noqa: E402
    RepeatFilter, ScanWatcher, XraySettings, analyse, annotate, is_small_item,
    scan_id,
)

log = logging.getLogger("xray-screening")

APP_ID = "xray-screening"

MANIFEST = AppManifest(
    id=APP_ID,
    name="X-ray Screening",
    version="0.1.0",
    category="safety",
    summary=("Reads a baggage X-ray scanner's screen: counts trays, and flags "
             "dense objects and cluttered trays for a person to check."),
    subscribes=None,          # drives its own frames from the scanner's screen
    params=[
        Param("scan_area", "geometry.polygon", per_camera=True,
              label="Where the scan shows on screen (optional)",
              group="The scanner's screen",
              description="Draw round the part of the screen that shows the scan, "
                          "leaving out menus and status bars. Empty means the "
                          "whole picture."),
        Param("repeat_window_min", float, default=60.0,
              label="Ignore the same scan shown again within (minutes)",
              group="Counting",
              description="A scanner never shows the same bag twice, but a "
                          "replayed recording or an operator calling an image "
                          "back up does. Those are not counted again."),
        Param("large_dense", float, default=0.0012, advanced=True,
              label="Dense object size that is flagged outright",
              group="What is flagged",
              description="As a fraction of the scan's height squared, so it "
                          "does not depend on resolution or on how many trays "
                          "went through together."),
        Param("small_dense", float, default=0.00036, advanced=True,
              label="Smallest dense item worth a look",
              group="What is flagged",
              description="Same units. Flagged only when also compact and very "
                          "dark, so the edges of tins and tools do not count."),
        Param("clutter_metal", float, default=0.45, advanced=True,
              label="Share of metal that makes a tray too cluttered",
              group="What is flagged"),
        Param("dense_value", int, default=70, advanced=True,
              label="How dark counts as the beam not getting through (0-255)",
              group="What is flagged"),
        Param("move_threshold", float, default=0.3, advanced=True,
              label="How much the screen must change to be a new bag",
              group="The scanner's screen",
              description="Raise it if a noisy capture card makes one bag count "
                          "as several."),
        Param("settle_s", float, default=0.6, advanced=True,
              label="Seconds the screen must hold still before a scan is read",
              group="The scanner's screen"),
        Param("fps", float, default=2.0, advanced=True,
              label="Frames per second read from the screen",
              group="Video",
              description="Only needs to notice the screen changing: 2 is "
                          "plenty, and every frame costs a full decode."),
        Param("frame_width", int, default=1280, advanced=True,
              label="Width frames are decoded at (pixels)",
              group="Video"),
    ],
    emits=[
        AlertType("dense_object", severity="high",
                  description="A solid shape the X-ray could not see through."),
        AlertType("small_dense_item", severity="medium",
                  description="A small, compact, very dense item."),
        AlertType("cluttered_tray", severity="medium",
                  description="Too much metal to clear on screen."),
    ],
    state_schema=[
        StateView("scans", "Scans", path="scans"),
        StateView("trays", "Trays", path="trays"),
        StateView("flagged", "Flagged for review", path="flagged"),
        StateView("cameras", "Scanners", path="cameras", kind="table",
                  columns=["camera", "scans", "trays", "flagged", "fps", "error"]),
        StateView("review", "Scans to check", path="review", kind="gallery",
                  limit=12),
        StateView("recent", "Recent scans", path="recent", kind="log", limit=15),
    ],
    # Lets this app publish its boxes onto the live view (the bus grants
    # opennvr.events.> only to apps that provide something).
    provides=["xray_screening"],
    overlay=True,
)

#: Settings drawn per camera, in Set up on its card.
PER_CAMERA_KEYS: tuple[str, ...] = ("scan_area",)


class _NoPollSource:
    """The base class polls; this app streams. See guard-scan-compliance."""

    def get_frame(self, camera_id: str) -> bytes | None:
        return None


@dataclass
class XrayConfig(BaseAppConfig):
    fps: float = 2.0
    frame_width: int = 1280
    repeat_window_min: float = 60.0
    move_threshold: float = 0.3
    settle_s: float = 0.6
    large_dense: float = 0.0012
    small_dense: float = 0.00036
    clutter_metal: float = 0.45
    dense_value: int = 70


def _stream_settings(cfg: dict) -> tuple:
    return (float(cfg.get("fps") or 2.0), int(cfg.get("frame_width") or 1280))


def area_mask(polygon, width: int, height: int):
    """The drawn scan area as a pixel mask, or None for the whole frame.
    The zone editor saves 0-1 fractions; literal pixels are accepted too."""
    import cv2
    import numpy as np
    from opennvr_app_sdk.geometry import scale_vertices

    pts = [p for p in (polygon or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
    if len(pts) < 3:
        return None
    px = np.array(scale_vertices(pts, width, height), dtype=np.int32)
    mask = np.zeros((height, width), np.uint8)
    cv2.fillPoly(mask, [px], 1)
    return mask.astype(bool)


class CameraWorker:
    """One scanner screen: frames in, scans out. A thread, so one feed
    falling over cannot stall another."""

    def __init__(self, app: "XrayApp", camera) -> None:
        self.app = app
        self.camera = camera
        self.handle = getattr(camera, "handle", str(camera))
        self.name = getattr(camera, "name", self.handle)
        self.stream = None
        self._stop = threading.Event()
        self._reopen = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream_settings: tuple | None = None
        self.frames = 0
        self.fps = 0.0
        self.scans = 0
        self.trays = 0
        self.flagged = 0
        self.last_error: str | None = None
        self.watcher: ScanWatcher | None = None
        self.repeats: RepeatFilter | None = None
        self._overlay: list[dict] = []

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"xray-{self.handle}")
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
        if self.watcher is not None:
            self.watcher.move_threshold = float(cfg.get("move_threshold") or 0.3)
            self.watcher.settle_s = float(cfg.get("settle_s") or 0.6)
        if self.repeats is not None:
            self.repeats.window_s = 60.0 * float(cfg.get("repeat_window_min") or 60.0)

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
        self._stream_settings = _stream_settings(cfg)
        self.watcher = ScanWatcher(settle_s=float(cfg.get("settle_s") or 0.6),
                                   move_threshold=float(cfg.get("move_threshold") or 0.3))
        if self.repeats is None:
            self.repeats = RepeatFilter(
                window_s=60.0 * float(cfg.get("repeat_window_min") or 60.0))
        fps, width = self._stream_settings
        self.stream = self.app.nvr.stream(self.camera, width=width, fps=fps)
        last = time.monotonic()
        for frame in self.stream.frames(timeout=15.0):
            if self._stop.is_set():
                return
            if self._reopen.is_set():
                self.stream.close()
                return
            self.last_error = None
            self._on_frame(frame)
            self.frames += 1
            now = time.monotonic()
            dt, last = now - last, now
            if dt > 0:
                self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)

    def _on_frame(self, frame) -> None:
        image = frame.to_ndarray()
        if self.watcher.feed(image, frame.wall_ts):
            if self.repeats.seen_recently(image, frame.wall_ts):
                log.debug("%s: same scan as one already counted", self.handle)
            else:
                self._overlay = self.app.on_scan(self, image, frame)
        # The boxes stay on the live view for as long as that scan does.
        if self._overlay:
            self.app.publish_overlay(self.handle, self._overlay, frame.seq)


class XrayApp(FrameApp):
    """Roster, config, and where scans go. Shaped like guard-scan."""

    def __init__(self, config) -> None:
        self.config = config
        self._nvr: OpenNVR | None = None
        self.events = DomainEventPublisher(
            getattr(config, "nats_alerts_url", "") or "",
            token=getattr(config, "nats_alerts_token", None),
            producer=f"app:{APP_ID}")
        self.workers: dict[str, CameraWorker] = {}
        self._workers_lock = threading.Lock()
        self._results_lock = threading.Lock()
        self._per_camera: dict[int, dict] = {}
        self.recent: list[dict] = []
        self.review: list[dict] = []
        self.dispatcher = build_dispatcher(
            webhook_url=getattr(config, "webhook_url", None),
            nats_alerts_url=getattr(config, "nats_alerts_url", None),
            nats_alerts_token=getattr(config, "nats_alerts_token", None))
        super().__init__(config, self.dispatcher, frame_source=_NoPollSource(),
                         cameras=[], poll_interval_seconds=30.0)

    # Built on first use, after registration has minted this app's key:
    # see GuardScanApp.nvr for why that matters.
    @property
    def nvr(self) -> OpenNVR:
        if self._nvr is None:
            self._nvr = OpenNVR()
        return self._nvr

    @nvr.setter
    def nvr(self, client) -> None:
        self._nvr = client

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
            else:
                worker.retune(cfg)

    # ── one bag ──

    def on_scan(self, worker: CameraWorker, image, frame) -> list[dict]:
        """Analyse a settled scan, record it, alert if it needs a look.
        Returns the overlay boxes to keep on the live view."""
        cfg = self.camera_config(worker.handle)
        settings = XraySettings.from_config(cfg)
        h, w = image.shape[:2]
        scan = analyse(image, settings, area_mask(cfg.get("scan_area"), w, h))
        if scan is None:
            return []
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(frame.wall_ts))
        with self._results_lock:
            worker.scans += 1
            worker.trays += scan.trays
            if scan.flags:
                worker.flagged += 1
            self.recent.append({
                "time": when, "level": "warning" if scan.flags else "info",
                "message": "%s: %d tray%s, %s%s" % (
                    worker.name, scan.trays, "" if scan.trays == 1 else "s",
                    scan.verdict,
                    (" — " + "; ".join(f["title"] for f in scan.flags)) if scan.flags else ""),
            })
            del self.recent[:-50]

        boxes = []
        for o in scan.objects:
            if not (o.large or is_small_item(o, settings)):
                continue
            x, y, bw, bh = o.box
            boxes.append({"label": "Dense" if o.large else "Small dense",
                          "box": [x / w, y / h, bw / w, bh / h]})
        if not scan.flags:
            return boxes

        marked = annotate(image, scan, settings)
        jpeg = _jpeg(marked, 85)
        path = None
        try:
            path = self.nvr.save_evidence(jpeg) if jpeg else None
        except Exception as exc:  # noqa: BLE001
            log.warning("could not store the scan as evidence: %s", exc)
        thumb = _jpeg(_fit(marked, 360), 70)
        with self._results_lock:
            if thumb:
                self.review.append({
                    "image": "data:image/jpeg;base64," + base64.b64encode(thumb).decode(),
                    "label": "%s — %s" % (worker.name, scan.flags[0]["title"]),
                    "time": when})
                del self.review[:-12]
        sid = scan_id(image)
        self.dispatcher.fire(Alert(
            # Named explicitly: the bus only lets an app publish under its
            # own id, and these fire on worker threads (see guard-scan).
            source=AlertSource(kind="app", name=APP_ID, version=MANIFEST.version),
            title=scan.flags[0]["title"],
            description=" ".join(f["detail"] for f in scan.flags),
            camera_id=worker.handle,
            severity=scan.severity,
            alert_type=scan.flags[0]["kind"],
            images={"scan": path} if path else {},
            evidence={
                "observed_at": frame.wall_ts,
                "scan": sid,
                "trays": scan.trays,
                "flags": [f["kind"] for f in scan.flags],
                "materials": {k: round(v, 3) for k, v in scan.materials.items()},
            },
            tags=["xray", *[f["kind"] for f in scan.flags]],
        ))
        log.info("%s: scan flagged (%s)", worker.handle,
                 ", ".join(f["kind"] for f in scan.flags))
        return boxes

    def publish_overlay(self, handle: str, boxes: list[dict], seq) -> None:
        try:
            self.events.publish_overlay(camera_id=handle, boxes=boxes, seq=seq)
        except Exception:  # noqa: BLE001
            log.debug("overlay publish failed", exc_info=True)

    # ── state the app page shows ──

    def state_snapshot(self) -> dict:
        workers = self._workers()
        with self._results_lock:
            return {
                "scans": sum(w.scans for _, w in workers),
                "trays": sum(w.trays for _, w in workers),
                "flagged": sum(w.flagged for _, w in workers),
                "cameras": [
                    {"camera": w.name, "scans": w.scans, "trays": w.trays,
                     "flagged": w.flagged, "fps": round(w.fps, 1),
                     "error": w.last_error or ""}
                    for _, w in workers],
                "review": list(reversed(self.review)),
                "recent": list(self.recent),
            }

    def not_ready_reason(self) -> str | None:
        workers = self._workers()
        if not workers:
            return ("No scanner selected — pick the camera showing the X-ray "
                    "screen in this app's configuration.")
        broken = [h for h, w in workers if w.last_error]
        if broken and len(broken) == len(workers):
            return "Cannot read the scanner screen: %s" % workers[0][1].last_error
        return None

    # ── roster: identical in shape to guard-scan's ──

    def setup(self) -> None:
        if not self._reconcile_roster():
            log.warning("no scanner camera selected for this app yet")

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
            return len(self.workers)       # core unreachable: keep what runs
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
            log.info("%s: %.1f fps, %d scans, %d trays, %d flagged%s", handle,
                     w.fps, w.scans, w.trays, w.flagged,
                     f", last error: {w.last_error}" if w.last_error else "")
        return []

    def stop(self) -> None:
        for _, worker in self._workers():
            worker.stop()
        super().stop()


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
    cfg = load_app_config(args.config, XrayConfig)
    app = XrayApp(cfg)
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
