# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
garbage-dumping — tells you when garbage has been dumped in a watched zone.

Every ``poll_interval_seconds`` the app fetches one frame per picked
camera through core, crops it to the no-dumping zone the operator drew,
and asks the site's open-vocabulary detector through KAI-C for the
``queries`` ("garbage bag", "pile of rubbish", …). It counts what comes
back above ``min_score``. More garbage than the zone held before, seen in
``confirm_looks`` looks in a row, raises ``garbage_dumped`` with the
count; the zone going back to empty raises ``garbage_cleared`` (low).
Garbage already there when the app starts is the baseline, not an alert.

The detector is whichever registered adapter advertises
``open_vocab_detection`` (the shipped open-vocab adapter), or the one
named in ``detector_adapter``. On CPU a look is about ten seconds, so the
poll interval defaults to 60 s.

Run::

    python garbage_dumping.py --config config.yml           # daemon
    python garbage_dumping.py --config config.yml --once    # one pass
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as _dt
import io
import logging
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from PIL import Image

from opennvr_app_sdk import (
    Alert,
    AlertType,
    AppManifest,
    BaseAppConfig,
    CoreSnapshotSource,
    FrameApp,
    FrameSource,
    KaiCClient,
    KaiCError,
    Param,
    StateView,
    build_dispatcher,
    build_frame_source,
    load_app_config,
)
from opennvr_app_sdk.alerts import AlertSource
from opennvr_app_sdk.geometry import scale_vertices

logger = logging.getLogger("garbage-dumping")

APP_ID = "garbage-dumping"
SEVERITIES = ("low", "medium", "high", "critical")
DETECT_TASKS = ("open_vocab_detection",)
DEFAULT_QUERIES = ["garbage bag", "trash bag", "pile of rubbish", "litter", "cardboard box", "sack"]
CROP_MARGIN = 0.10

MANIFEST = AppManifest(
    id=APP_ID,
    name="Garbage Dumping",
    version="1.0.0",
    category="perimeter",
    summary=(
        "Alerts when garbage is dumped in a watched zone: every look asks the "
        "site's open-vocabulary detector for what the operator calls garbage, "
        "and more of it than the zone held before raises the alert; it clears "
        "when the zone is empty again."
    ),
    requires_tasks=["open_vocab_detection"],
    params=[
        Param("zone", "geometry.polygon", per_camera=True,
              description="The no-dumping zone, drawn on the camera. Whole frame when not drawn."),
        Param("poll_interval_seconds", float, default=60.0,
              description="How often each zone is looked at. Each look is one detection call."),
        Param("queries", list, default=list(DEFAULT_QUERIES),
              description="What counts as garbage, as phrases the detector looks for."),
        Param("min_score", float, default=0.2,
              description="Detections below this score are ignored."),
        Param("confirm_looks", int, default=2,
              description="New garbage must be seen this many looks in a row before it alerts."),
        Param("cooldown_seconds", float, default=600.0,
              description="After an alert, more dumping in the same zone stays quiet this long."),
        Param("look_timeout_seconds", float, default=90.0,
              description="How long one detection may take. Keep it above a CPU look."),
        Param("detector_adapter", str, default="",
              description="KAI-C adapter that runs the detection. Empty = the first one advertising open_vocab_detection."),
        Param("active_hours", "time_range",
              description="When alerts fire. Outside it the zone is still watched and counted, quietly. "
                          "Empty = around the clock."),
        Param("alert_severity", str, default="medium", choices=list(SEVERITIES),
              description="Severity of the dumping alert."),
        Param("attach_snapshot", bool, default=True,
              description="Attach the frame to the alert."),
    ],
    emits=[
        AlertType("garbage_dumped", "medium", "More garbage in the zone than before."),
        AlertType("garbage_cleared", "low", "The zone is empty again after a dumping alert."),
    ],
    state_schema=[
        StateView("items_now", "In the zones now", kind="metric", path="items_now"),
        StateView("alerted_now", "Zones alerted", kind="metric", path="alerted_now",
                  description="Zones with a dumping alert not yet cleared."),
        StateView("dumps_today", "Dumps today", kind="metric", path="today.dumps"),
        StateView("cleared_today", "Cleared today", kind="metric", path="today.cleared"),
        StateView("per_camera", "Per zone", kind="table", path="per_camera",
                  columns=["camera", "count", "baseline", "labels", "alerted", "dumps_today", "looks",
                           "last_look", "zone_drawn"]),
        StateView("recent", "Recent", kind="log", path="recent", limit=12),
        StateView("gallery", "Before and after", kind="gallery", path="gallery", limit=8,
                  description="The zone at the previous look and at the dumping alert."),
    ],
)


# ── config ────────────────────────────────────────────────────────────


@dataclass
class AppConfig(BaseAppConfig):
    poll_interval_seconds: float = 60.0
    queries: list = field(default_factory=lambda: list(DEFAULT_QUERIES))
    min_score: float = 0.2
    confirm_looks: int = 2
    cooldown_seconds: float = 600.0
    look_timeout_seconds: float = 90.0
    detector_adapter: str = ""
    alert_severity: str = "medium"
    attach_snapshot: bool = True
    active_hours: dict | None = None
    kaic_url: str | None = None
    kaic_api_key: str | None = None
    #: Standalone only: ``[{camera_id, frame_url, zone?}]``.
    cameras: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        self.queries = [str(q).strip() for q in (self.queries or []) if str(q).strip()]
        if not self.queries:
            raise ValueError("queries must name at least one thing to look for")
        if not (0 <= self.min_score <= 1):
            raise ValueError("min_score must be between 0 and 1")
        if int(self.confirm_looks) < 1:
            raise ValueError("confirm_looks must be at least 1")
        if self.cooldown_seconds < 0 or self.look_timeout_seconds <= 0:
            raise ValueError("cooldown_seconds must be >= 0 and look_timeout_seconds > 0")
        if self.alert_severity not in SEVERITIES:
            raise ValueError(f"alert_severity must be one of {SEVERITIES}")
        self.active_hours = ActiveHours.parse(self.active_hours)


class ActiveHours:
    """A daily window in local time; cross-midnight supported."""

    def __init__(self, start: _dt.time, end: _dt.time) -> None:
        self.start, self.end = start, end

    def contains(self, when: _dt.datetime) -> bool:
        t = when.time()
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ActiveHours) and (self.start, self.end) == (other.start, other.end)

    @classmethod
    def parse(cls, raw: Any) -> "ActiveHours | None":
        if isinstance(raw, ActiveHours):
            return raw
        if not isinstance(raw, dict):
            return None
        s, e = str(raw.get("start") or "").strip(), str(raw.get("end") or "").strip()
        if not s or not e:
            return None
        try:
            return cls(_dt.time.fromisoformat(s), _dt.time.fromisoformat(e))
        except ValueError as exc:
            raise ValueError(f"active_hours must be HH:MM start/end: {exc}") from None


# ── the arithmetic ────────────────────────────────────────────────────


def thumbnail(frame_bytes: bytes, width: int = 320) -> str | None:
    """The frame as a small JPEG data URI for the catalog's gallery."""
    try:
        with Image.open(io.BytesIO(frame_bytes)) as im:
            w, h = im.size
            if w > width:
                im = im.resize((width, max(1, int(h * width / w))))
            out = io.BytesIO()
            im.convert("RGB").save(out, "JPEG", quality=70)
    except Exception:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def crop_to_zone(frame_bytes: bytes, zone: list[list[float]] | None) -> bytes:
    """The frame cropped to the drawn zone's box plus a margin; the whole
    frame when no zone is drawn or the drawing is unusable."""
    if not zone or len(zone) < 3:
        return frame_bytes
    with Image.open(io.BytesIO(frame_bytes)) as im:
        w, h = im.size
        try:
            pts = scale_vertices(zone, w, h)
        except (TypeError, ValueError, IndexError):
            return frame_bytes
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        bw, bh = max(xs) - min(xs), max(ys) - min(ys)
        if bw < 8 or bh < 8:
            return frame_bytes
        box = (max(0, int(min(xs) - CROP_MARGIN * bw)), max(0, int(min(ys) - CROP_MARGIN * bh)),
               min(w, int(max(xs) + CROP_MARGIN * bw)), min(h, int(max(ys) + CROP_MARGIN * bh)))
        out = io.BytesIO()
        im.convert("RGB").crop(box).save(out, "JPEG", quality=90)
        return out.getvalue()


def count_garbage(detections: Iterable[dict[str, Any]], min_score: float) -> tuple[int, list[str]]:
    """How many detections clear the score floor, and their labels."""
    labels: list[str] = []
    for d in detections or []:
        try:
            score = float(d.get("confidence", d.get("score", 0.0)))
        except (TypeError, ValueError):
            continue
        if score >= min_score:
            labels.append(str(d.get("label") or "?"))
    return len(labels), labels


# ── per-camera state ──────────────────────────────────────────────────


@dataclass
class ZoneState:
    baseline: int | None = None        # what the zone held when the app first looked
    count: int = 0                     # from the last look
    labels: list[str] = field(default_factory=list)
    above_run: int = 0                 # looks in a row above the baseline
    alerted: bool = False
    alerted_at: float = 0.0
    alerted_count: int = 0
    looks: int = 0
    zone: list[list[float]] | None = None
    last_frame: bytes | None = None    # the previous look, for the before picture
    last_look: str | None = None


Detector = Callable[[str, bytes], list[dict[str, Any]] | None]


# ── the app ───────────────────────────────────────────────────────────


class GarbageDumping(FrameApp):
    manifest = MANIFEST

    def setup(self) -> None:
        self._state: dict[str, ZoneState] = {}
        self._clock = time.monotonic
        self._nvr = None
        self._kaic: KaiCClient | None = None
        self._adapter: str | None = (self.cfg.detector_adapter or "").strip() or None
        self._detector: Detector | None = None
        self._app_alerts_fired = 0
        self._wall = lambda: _dt.datetime.now().astimezone()
        self._today_key = self._wall().date().isoformat()
        self._today: dict[str, dict[str, int]] = {}
        self._recent: deque = deque(maxlen=30)
        self._gallery: deque = deque(maxlen=8)
        for cam in getattr(self.cfg, "cameras", None) or []:
            cid = str(cam.get("camera_id") or "") if isinstance(cam, dict) else ""
            if cid and isinstance(cam.get("zone"), list):
                self._state.setdefault(cid, ZoneState()).zone = cam["zone"]

    # -- detecting --

    def _pick_adapter(self) -> str | None:
        if self._adapter:
            return self._adapter
        nvr = self._platform()
        caps = nvr.ai.capabilities() if nvr is not None else None
        for name, card in ((caps or {}).get("adapters") or {}).items():
            tasks = set(card.get("tasks_advertised") or [])
            if tasks & set(DETECT_TASKS):
                self._adapter = name
                logger.info("detecting with %s", name)
                return name
        return None

    def detect(self, camera_id: str, jpeg: bytes) -> list[dict[str, Any]] | None:
        """The detector's detections for the crop, or None when nothing could run."""
        if self._detector is not None:
            return self._detector(camera_id, jpeg)
        adapter = self._pick_adapter()
        if adapter is None:
            logger.warning("no adapter advertises open_vocab_detection; the zone cannot be checked")
            return None
        if self._kaic is None:
            base = self.cfg.kaic_url or (self._platform().ai._base if self._platform() else None)
            if not base:
                return None
            self._kaic = KaiCClient(base, adapter, api_key=self.cfg.kaic_api_key,
                                    timeout_seconds=float(self.cfg.look_timeout_seconds))
        try:
            body = self._kaic.infer(jpeg, task="open_vocab_detection", camera_id=camera_id,
                                    params={"queries": list(self.cfg.queries),
                                            "threshold": float(self.cfg.min_score)})
        except KaiCError as exc:
            logger.warning("detection for %s failed: %s", camera_id, exc)
            return None
        result = body.get("result") if isinstance(body, dict) else None
        dets = result.get("detections") if isinstance(result, dict) else None
        return list(dets) if isinstance(dets, list) else None

    # -- the rule --

    def on_frame(self, camera_id: str, frame_bytes: bytes) -> Iterable[Alert] | None:
        st = self._state.setdefault(camera_id, ZoneState())
        now = self._clock()
        crop = crop_to_zone(frame_bytes, st.zone)
        dets = self.detect(camera_id, crop)
        if dets is None:
            return []                                   # could not look: no change of state
        st.looks += 1
        st.count, st.labels = count_garbage(dets, float(self.cfg.min_score))
        before, st.last_frame = st.last_frame, frame_bytes
        st.last_look = self._wall().isoformat(timespec="seconds")
        fired: list[Alert] = []
        if st.baseline is None:
            st.baseline = st.count                      # what was there to begin with
            self._note(camera_id, f"{camera_id}: baseline {st.count}", "info")
            return fired

        if st.count == 0 and st.alerted:
            fired.append(self._alert(camera_id, "cleared", st, severity="low"))
            st.alerted = False
            st.baseline = 0
        if st.count <= st.baseline:
            st.above_run = 0
            if st.count < st.baseline:
                st.baseline = st.count                  # some was removed: the new normal
            return fired

        st.above_run += 1
        if st.above_run < int(self.cfg.confirm_looks) or not self._in_hours():
            return fired
        if st.alerted and now - st.alerted_at < self.cfg.cooldown_seconds and st.count <= st.alerted_count:
            return fired
        fired.append(self._alert(camera_id, "dumped", st, severity=self.cfg.alert_severity,
                                 frame=frame_bytes, before=before))
        st.alerted, st.alerted_at, st.alerted_count = True, now, st.count
        st.baseline = st.count                          # a further increase is a further dump
        st.above_run = 0
        return fired

    # -- bookkeeping for the dashboard --

    def _in_hours(self) -> bool:
        hours = ActiveHours.parse(self.cfg.active_hours)   # a dict when the catalog set it
        return hours is None or hours.contains(self._wall())

    def _roll_day(self) -> None:
        key = self._wall().date().isoformat()
        if key != self._today_key:
            self._today_key, self._today = key, {}

    def _count(self, camera_id: str, what: str) -> None:
        self._roll_day()
        day = self._today.setdefault(camera_id, {"dumps": 0, "cleared": 0})
        day[what] = day.get(what, 0) + 1

    def _note(self, camera_id: str, message: str, level: str, frame: bytes | None = None,
              before: bytes | None = None) -> None:
        when = self._wall().isoformat(timespec="seconds")
        self._recent.appendleft({"message": message, "time": when, "level": level, "camera": camera_id})
        if frame is not None:
            after = thumbnail(frame)
            if after:
                self._gallery.appendleft({"image": after, "label": f"{message} — after", "time": when})
            prior = thumbnail(before) if before else None
            if prior:
                self._gallery.appendleft({"image": prior, "label": f"{message} — before", "time": when})

    # -- alerts --

    def _alert(self, camera_id: str, kind: str, st: ZoneState, *, severity: str,
               frame: bytes | None = None, before: bytes | None = None) -> Alert:
        new = max(0, st.count - (st.baseline or 0))
        titles = {
            "dumped": f"Garbage dumped at {camera_id}",
            "cleared": f"Garbage cleared at {camera_id}",
        }
        descriptions = {
            "dumped": f"{new} new item{'s' if new != 1 else ''} in the no-dumping zone "
                      f"({', '.join(st.labels[:5]) or 'garbage'}); {st.count} in total.",
            "cleared": "The no-dumping zone is empty again.",
        }
        self._app_alerts_fired += 1
        self._count(camera_id, "dumps" if kind == "dumped" else "cleared")
        self._note(camera_id, titles[kind], "alert" if kind == "dumped" else "info", frame, before)
        images = self._evidence(frame) if frame else {}
        if frame and before:
            images.update({k: v for k, v in self._evidence(before, "before").items()})
        return Alert(
            title=titles[kind],
            description=descriptions[kind],
            camera_id=camera_id,
            severity=severity,
            alert_type=f"garbage_{kind}",
            source=AlertSource(kind="app", name=APP_ID),
            evidence={"count": st.count, "new": new, "labels": st.labels[:10], "looks": st.looks},
            tags=["garbage", kind],
            images=images,
        )

    def _platform(self):
        if self._nvr is None and getattr(self.cfg, "opennvr_url", None):
            try:
                from opennvr_app_sdk import OpenNVR
                self._nvr = OpenNVR(self.cfg.opennvr_url, timeout=10.0,
                                    kaic_url=self.cfg.kaic_url, kaic_api_key=self.cfg.kaic_api_key)
            except Exception as exc:  # pragma: no cover - import/boot edge
                logger.warning("platform client unavailable: %s", exc)
        return self._nvr

    def _evidence(self, frame: bytes, name: str = "snapshot") -> dict[str, str]:
        if not self.cfg.attach_snapshot:
            return {}
        nvr = self._platform()
        if nvr is None:
            return {}
        try:
            path = nvr.save_evidence(frame)
        except Exception as exc:
            logger.warning("saving the frame as evidence failed: %s", exc)
            return {}
        return {name: path} if path else {}

    # -- live config from the catalog --

    LIVE_KNOBS = {
        "poll_interval_seconds": float, "queries": list, "min_score": float, "confirm_looks": int,
        "cooldown_seconds": float, "look_timeout_seconds": float, "detector_adapter": str,
        "alert_severity": str, "attach_snapshot": bool,
    }   # active_hours is a dict and is handled on its own below

    def on_config_update(self, config: dict[str, Any]) -> None:
        changed: list[str] = []
        for key, typ in self.LIVE_KNOBS.items():
            if key not in config or config[key] is None:
                continue
            try:
                raw = config[key]
                value = bool(raw) if typ is bool else list(raw) if typ is list else typ(raw)
                trial = AppConfig(**{**_knob_values(self.cfg), key: value})
            except (TypeError, ValueError) as exc:
                logger.warning("config edit %s=%r ignored: %s", key, config[key], exc)
                continue
            if getattr(self.cfg, key, None) != getattr(trial, key):
                setattr(self.cfg, key, getattr(trial, key))
                changed.append(key)
        if "poll_interval_seconds" in changed:
            self._interval = float(self.cfg.poll_interval_seconds)
        if "detector_adapter" in changed or "look_timeout_seconds" in changed:
            self._adapter = self.cfg.detector_adapter.strip() or None
            self._kaic = None
        if "active_hours" in config:
            try:
                hours = ActiveHours.parse(config["active_hours"])
            except ValueError as exc:
                logger.warning("config edit active_hours=%r ignored: %s", config["active_hours"], exc)
            else:
                if hours != self.cfg.active_hours:
                    self.cfg.active_hours = hours
                    changed.append("active_hours")
        zones = config.get("zone")
        if isinstance(zones, dict):
            for raw_key, drawn in zones.items():
                cam = str(raw_key)
                cam = cam if cam.startswith("cam") else f"cam{cam}"
                st = self._state.setdefault(cam, ZoneState())
                new = drawn if isinstance(drawn, list) and len(drawn) >= 3 else None
                if new != st.zone:
                    st.zone = new
                    st.baseline = None                  # a new zone starts from what it holds now
                    changed.append(f"zone[{cam}]")
        if changed:
            logger.info("config applied live: %s", ", ".join(changed))

    # -- contract surface --

    def state_snapshot(self) -> dict[str, Any]:
        self._roll_day()
        cameras = {
            cam: {"count": st.count, "baseline": st.baseline, "labels": st.labels[:10],
                  "alerted": st.alerted, "looks": st.looks, "zone_drawn": st.zone is not None,
                  "last_look": st.last_look}
            for cam, st in self._state.items()
        }
        per_camera = [{"camera": cam, "count": row["count"], "baseline": row["baseline"],
                       "labels": ", ".join(row["labels"][:4]), "alerted": row["alerted"],
                       "dumps_today": self._today.get(cam, {}).get("dumps", 0), "looks": row["looks"],
                       "last_look": row["last_look"], "zone_drawn": row["zone_drawn"]}
                      for cam, row in cameras.items()]
        return {
            "adapter": self._adapter,
            "alerts_fired": self._app_alerts_fired,
            "items_now": sum(r["count"] for r in cameras.values()),
            "alerted_now": sum(1 for r in cameras.values() if r["alerted"]),
            "in_hours": self._in_hours(),
            "today": {"dumps": sum(d.get("dumps", 0) for d in self._today.values()),
                      "cleared": sum(d.get("cleared", 0) for d in self._today.values()),
                      "since": self._today_key},
            "cameras": cameras,
            "per_camera": per_camera,
            "recent": list(self._recent),
            "gallery": list(self._gallery),
        }


def _knob_values(cfg: Any) -> dict[str, Any]:
    return {k: getattr(cfg, k) for k in GarbageDumping.LIVE_KNOBS if hasattr(cfg, k)}


# ── wiring ────────────────────────────────────────────────────────────


class _StandaloneSource:
    def __init__(self, cameras: list[dict[str, Any]]) -> None:
        self._sources: dict[str, FrameSource] = {}
        for cam in cameras or []:
            cid = str(cam.get("camera_id") or "")
            url = str(cam.get("frame_url") or "")
            if cid and url:
                self._sources[cid] = build_frame_source(camera_id=cid, url=url)

    def get_frame(self, camera_id: str) -> bytes | None:
        src = self._sources.get(camera_id)
        return src.get_frame(camera_id) if src else None

    @property
    def camera_ids(self) -> list[str]:
        return list(self._sources)


class _EitherSource:
    def __init__(self, standalone: _StandaloneSource, core: CoreSnapshotSource) -> None:
        self._standalone = standalone
        self._core = core

    def get_frame(self, camera_id: str) -> bytes | None:
        if camera_id in self._standalone.camera_ids:
            return self._standalone.get_frame(camera_id)
        return self._core.get_frame(camera_id)


def build_app(config: AppConfig, dispatcher) -> GarbageDumping:
    standalone = _StandaloneSource(config.cameras)
    source = _EitherSource(standalone, CoreSnapshotSource())
    return GarbageDumping(config, dispatcher, frame_source=source,
                          cameras=standalone.camera_ids,
                          poll_interval_seconds=config.poll_interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenNVR garbage-dumping app")
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--once", action="store_true", help="one pass over the cameras, then exit")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = load_app_config(args.config, AppConfig)
    dispatcher = build_dispatcher(
        webhook_url=config.webhook_url,
        nats_alerts_url=config.nats_alerts_url,
        nats_alerts_token=config.nats_alerts_token,
        nats_alerts_subject_prefix=config.nats_alerts_subject_prefix,
    )
    app = build_app(config, dispatcher)
    if not config.cameras and not config.opennvr_url and not args.once:
        raise SystemExit("config: list cameras, or set opennvr_url and pick cameras in the App Catalog")

    loop = asyncio.new_event_loop()

    def _stop(signum, _frame):
        logger.info("signal %s: stopping", signum)
        loop.call_soon_threadsafe(app.stop)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        loop.run_until_complete(app.run(once=args.once))
    finally:
        try:
            dispatcher.close()
        finally:
            loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
