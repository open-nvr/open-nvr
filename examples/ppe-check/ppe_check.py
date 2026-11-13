# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
ppe-check — tells you when someone on camera is missing required safety kit.

Tier-0 already finds every person and keeps each track's best crop (the
sharpest, largest look it had). This app rides that stream
(``consume_tier0: true``): for every person track that is big enough and
wholly inside the frame it fetches the track's best crop from the
detect-pipeline and asks the site's open-vocabulary detector through
KAI-C for the phrases that describe each required item ("hard hat",
"safety helmet"; "safety vest", "high-visibility vest"; …). An item none
of whose phrases scores ``min_item_score`` is missing. A person found
missing an item in ``confirm_checks`` checks in a row raises
``ppe_missing`` with the item names and the crop as evidence; a
compliant person is simply counted.

People cut off by the frame edge are not judged (no head in the crop is
not "no helmet"), nor are people shorter than ``min_person_height`` of
the frame, where the kit is a few pixels. On CPU one check is about ten
seconds, so checks are queued and run one at a time; a track is checked
again after ``recheck_seconds`` while something is missing and after
``check_interval_seconds`` once it is compliant.

Run::

    python ppe_check.py --config config.yml           # daemon
    python ppe_check.py --config config.yml --once    # one event
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as _dt
import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from opennvr_app_sdk import (
    Alert,
    AlertType,
    AppManifest,
    BaseAppConfig,
    Detector,
    KaiCClient,
    KaiCError,
    Param,
    StateView,
    build_dispatcher,
    load_app_config,
)
from opennvr_app_sdk.alerts import AlertSource

logger = logging.getLogger("ppe-check")

APP_ID = "ppe-check"
SEVERITIES = ("low", "medium", "high", "critical")
DETECT_TASKS = ("open_vocab_detection",)
DEFAULT_ITEMS = ["helmet", "vest"]
DEFAULT_PHRASES = {
    "helmet": ["hard hat", "safety helmet"],
    "vest": ["safety vest", "high-visibility vest"],
    "gloves": ["work gloves", "safety gloves"],
    "boots": ["safety boots", "work boots"],
}
DEFAULT_BEST_FRAME_URL = "http://detect-pipeline:9109"

MANIFEST = AppManifest(
    id=APP_ID,
    name="PPE Check",
    version="1.0.0",
    category="safety",
    summary=(
        "Alerts when a person on camera is missing required safety kit: every "
        "person Tier-0 tracks is checked once, from its best crop, by the site's "
        "open-vocabulary detector for the helmet, vest, gloves or boots the "
        "operator requires."
    ),
    requires_tasks=["object_detection", "multi_object_tracking", "open_vocab_detection"],
    params=[
        Param("required_items", list, default=list(DEFAULT_ITEMS),
              description="The kit everyone must wear, from: helmet, vest, gloves, boots."),
        Param("min_item_score", float, default=0.3,
              description="An item counts as worn when one of its phrases scores at least this."),
        Param("min_person_score", float, default=0.5,
              description="Tier-0 person tracks below this score are not checked."),
        Param("min_person_height", float, default=0.25,
              description="People shorter than this fraction of the frame are not checked."),
        Param("confirm_checks", int, default=2,
              description="An item must be missing in this many checks in a row before it alerts."),
        Param("recheck_seconds", float, default=15.0,
              description="How soon a person with something missing is checked again."),
        Param("check_interval_seconds", float, default=120.0,
              description="How often a compliant person is checked again while still in view."),
        Param("alert_cooldown_seconds", float, default=120.0,
              description="After an alert, further violations on the same camera stay quiet this long."),
        Param("look_timeout_seconds", float, default=90.0,
              description="How long one detection may take. Keep it above a CPU look."),
        Param("detector_adapter", str, default="",
              description="KAI-C adapter that runs the detection. Empty = the first one advertising open_vocab_detection."),
        Param("active_hours", "time_range",
              description="When alerts fire (a shift). Outside it people are still checked and counted, "
                          "quietly. Empty = around the clock."),
        Param("alert_severity", str, default="high", choices=list(SEVERITIES),
              description="Severity of the missing-kit alert."),
        Param("attach_snapshot", bool, default=True,
              description="Attach the person's crop to the alert."),
    ],
    emits=[
        AlertType("ppe_missing", "high", "A person is missing required safety kit."),
    ],
    state_schema=[
        StateView("compliance_today", "Compliance today", kind="gauge", path="today.compliance_pct",
                  min=0, max=100, unit="%", warn=95, danger=80,
                  description="Checks today that found the full kit."),
        StateView("checked_today", "People checked today", kind="metric", path="today.checked"),
        StateView("violations_today", "Violations today", kind="metric", path="today.violations"),
        StateView("pending", "Waiting for a check", kind="metric", path="pending"),
        StateView("per_camera", "Per camera", kind="table", path="per_camera",
                  columns=["camera", "checked_today", "violations_today", "compliance_pct", "last_missing",
                           "last_check"]),
        StateView("by_item", "Missing, by item", kind="table", path="by_item", columns=["item", "today"]),
        StateView("recent", "Recent", kind="log", path="recent", limit=12),
        StateView("gallery", "Violations", kind="gallery", path="gallery", limit=8,
                  description="The person's crop at each alert."),
    ],
)


# ── config ────────────────────────────────────────────────────────────


@dataclass
class AppConfig(BaseAppConfig):
    required_items: list = field(default_factory=lambda: list(DEFAULT_ITEMS))
    item_phrases: dict = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_PHRASES.items()})
    person_label: str = "person"
    min_item_score: float = 0.3
    min_person_score: float = 0.5
    min_person_height: float = 0.25
    edge_margin: float = 0.01
    confirm_checks: int = 2
    recheck_seconds: float = 15.0
    check_interval_seconds: float = 120.0
    alert_cooldown_seconds: float = 120.0
    look_timeout_seconds: float = 90.0
    track_ttl_seconds: float = 600.0
    #: A queued person not seen for this long is not checked: on CPU the
    #: queue can lag a busy gate, and the crop of someone gone is wasted.
    stale_after_seconds: float = 30.0
    detector_adapter: str = ""
    alert_severity: str = "high"
    attach_snapshot: bool = True
    active_hours: dict | None = None
    best_frame_url: str = ""
    kaic_url: str | None = None
    kaic_api_key: str | None = None

    def __post_init__(self) -> None:
        self.required_items = [str(i).strip().lower() for i in (self.required_items or []) if str(i).strip()]
        if not self.required_items:
            raise ValueError("required_items must name at least one item")
        phrases = {str(k).strip().lower(): [str(p).strip() for p in (v or []) if str(p).strip()]
                   for k, v in (self.item_phrases or {}).items()}
        self.item_phrases = {**{k: list(v) for k, v in DEFAULT_PHRASES.items()}, **phrases}
        unknown = [i for i in self.required_items if not self.item_phrases.get(i)]
        if unknown:
            raise ValueError(f"no phrases for required item(s) {unknown}; add them under item_phrases")
        for name in ("min_item_score", "min_person_score", "min_person_height", "edge_margin"):
            if not (0 <= float(getattr(self, name)) <= 1):
                raise ValueError(f"{name} must be between 0 and 1")
        if int(self.confirm_checks) < 1:
            raise ValueError("confirm_checks must be at least 1")
        if min(self.recheck_seconds, self.check_interval_seconds, self.alert_cooldown_seconds) < 0:
            raise ValueError("recheck_seconds, check_interval_seconds and alert_cooldown_seconds must be >= 0")
        if min(self.look_timeout_seconds, self.track_ttl_seconds, self.stale_after_seconds) <= 0:
            raise ValueError("look_timeout_seconds, track_ttl_seconds and stale_after_seconds must be > 0")
        if self.alert_severity not in SEVERITIES:
            raise ValueError(f"alert_severity must be one of {SEVERITIES}")
        self.best_frame_url = (self.best_frame_url or os.environ.get("DETECT_PIPELINE_METRICS_URL")
                               or DEFAULT_BEST_FRAME_URL).rstrip("/")
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


def judge(detections: Iterable[dict[str, Any]], required: list[str], phrases: dict[str, list[str]],
          min_score: float) -> tuple[list[str], dict[str, float]]:
    """Which required items are missing from a crop's detections, and the
    best score seen for each required item (0 when none of its phrases
    came back)."""
    best: dict[str, float] = {}
    for d in detections or []:
        label = str(d.get("label") or "").strip().lower()
        try:
            score = float(d.get("confidence", d.get("score", 0.0)))
        except (TypeError, ValueError):
            continue
        for item in required:
            if label in (p.lower() for p in phrases.get(item, [])):
                best[item] = max(best.get(item, 0.0), score)
    scores = {item: round(best.get(item, 0.0), 3) for item in required}
    missing = [item for item in required if scores[item] < min_score]
    return missing, scores


def judgeable(det: dict[str, Any], *, person_label: str, min_score: float, min_height: float,
              edge_margin: float) -> bool:
    """A person track worth checking: confident, tall enough for the kit
    to be visible, and wholly inside the frame (a head above the top edge
    is not a missing helmet)."""
    if str(det.get("label") or "").lower() != person_label:
        return False
    try:
        score = float(det.get("score") or det.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return False
    if score < min_score:
        return False
    bbox = det.get("bbox")
    if not isinstance(bbox, dict):
        return False
    try:
        x, y, w, h = (float(bbox[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return False
    if h < min_height:
        return False
    return x > edge_margin and y > edge_margin and x + w < 1 - edge_margin and y + h < 1 - edge_margin


# ── per-track state ───────────────────────────────────────────────────


@dataclass
class TrackState:
    camera_id: str
    track_id: str
    last_seen: float = 0.0
    last_checked: float = 0.0
    checks: int = 0
    missing: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    missing_run: int = 0                # checks in a row with something missing
    alerted: bool = False


@dataclass
class CameraState:
    checked: int = 0
    compliant: int = 0
    violations: int = 0
    suppressed: int = 0
    last_alert_at: float = 0.0
    last_missing: list[str] = field(default_factory=list)
    last_check: str | None = None


Detect = Callable[[str, bytes], list[dict[str, Any]] | None]
FetchBest = Callable[[str, str], bytes | None]


# ── the app ───────────────────────────────────────────────────────────


class PpeCheck(Detector):
    manifest = MANIFEST

    def setup(self) -> None:
        self._tracks: dict[tuple[str, str], TrackState] = {}
        self._cameras: dict[str, CameraState] = {}
        self._pending: dict[tuple[str, str], TrackState] = {}   # latest wins per track
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._stopping = False
        self._mono = time.monotonic
        self._nvr = None
        self._kaic: KaiCClient | None = None
        self._adapter: str | None = (self.cfg.detector_adapter or "").strip() or None
        self._detect: Detect | None = None
        self._fetch_best: FetchBest | None = None
        self._app_alerts_fired = 0
        self._checks = 0
        self._failed_checks = 0
        self._skipped = 0
        self._wall = lambda: _dt.datetime.now().astimezone()
        self._today_key = self._wall().date().isoformat()
        self._today: dict[str, dict[str, int]] = {}      # per camera: checked, compliant, violations
        self._by_item: dict[str, int] = {}               # today, per missing item
        self._recent: deque = deque(maxlen=30)
        self._gallery: deque = deque(maxlen=8)

    # -- the rule, event side: who is due a check --

    def on_detections(self, camera_id: str, detections: list[dict[str, Any]],
                      event: dict[str, Any]) -> Iterable[Alert] | None:
        now = self._mono()
        cfg = self.cfg
        for det in detections:
            if not isinstance(det, dict) or not det.get("best"):
                continue
            if not judgeable(det, person_label=cfg.person_label, min_score=float(cfg.min_person_score),
                             min_height=float(cfg.min_person_height), edge_margin=float(cfg.edge_margin)):
                continue
            tid = str(det.get("track_id") or "")
            if not tid:
                continue
            st = self._tracks.setdefault((camera_id, tid), TrackState(camera_id, tid))
            st.last_seen = now
            if self._due(st, now):
                with self._lock:
                    self._pending[(camera_id, tid)] = st
                self._wake.set()
        self._forget_old(now)
        return []                                   # alerts come from the checks, not the events

    def _due(self, st: TrackState, now: float) -> bool:
        if st.checks == 0:
            return True
        wait = self.cfg.recheck_seconds if st.missing else self.cfg.check_interval_seconds
        return now - st.last_checked >= float(wait)

    def _forget_old(self, now: float) -> None:
        ttl = float(self.cfg.track_ttl_seconds)
        gone = [k for k, st in self._tracks.items() if now - st.last_seen > ttl]
        for k in gone:
            self._tracks.pop(k, None)
            with self._lock:
                self._pending.pop(k, None)

    # -- the rule, check side: one person, one crop, one detection --

    def run_pending(self, limit: int | None = None) -> list[Alert]:
        """Run the queued checks, oldest first, and fire what they raise.
        The worker thread calls this; tests call it directly."""
        fired: list[Alert] = []
        while limit is None or len(fired) < limit or limit == 0:
            with self._lock:
                if not self._pending:
                    break
                key, st = next(iter(self._pending.items()))
                self._pending.pop(key)
            if self._mono() - st.last_seen > float(self.cfg.stale_after_seconds):
                self._skipped += 1                  # they left before their turn came
                continue
            alert = self.check(st)
            if alert is not None:
                self._dispatcher.fire(alert)
                self._contract_note_alerts(1)
                fired.append(alert)
            if limit == 0:
                break
        return fired

    def check(self, st: TrackState) -> Alert | None:
        now = self._mono()
        st.last_checked = now
        crop = self.fetch_best(st.camera_id, st.track_id)
        if crop is None:
            self._failed_checks += 1
            logger.info("no best frame for %s track %s yet; will try again", st.camera_id, st.track_id)
            return None
        dets = self.detect(st.camera_id, crop)
        if dets is None:
            self._failed_checks += 1
            return None
        self._checks += 1
        st.checks += 1
        cam = self._cameras.setdefault(st.camera_id, CameraState())
        cam.checked += 1
        cam.last_check = self._wall().isoformat(timespec="seconds")
        self._count(st.camera_id, "checked")
        st.missing, st.scores = judge(dets, list(self.cfg.required_items), self.cfg.item_phrases,
                                      float(self.cfg.min_item_score))
        if not st.missing:
            st.missing_run = 0
            cam.compliant += 1
            self._count(st.camera_id, "compliant")
            return None
        st.missing_run += 1
        cam.last_missing = list(st.missing)
        if st.missing_run < int(self.cfg.confirm_checks) or st.alerted:
            return None
        cam.violations += 1
        st.alerted = True
        self._count(st.camera_id, "violations")
        for item in st.missing:
            self._by_item[item] = self._by_item.get(item, 0) + 1
        title = f"Person without {', '.join(st.missing)} at {st.camera_id}"
        self._note(st.camera_id, title, "alert", crop)
        if not self._in_hours():
            cam.suppressed += 1                         # off shift: counted, not raised
            return None
        if now - cam.last_alert_at < float(self.cfg.alert_cooldown_seconds):
            cam.suppressed += 1
            return None
        cam.last_alert_at = now
        return self._alert(st, crop)

    # -- bookkeeping for the dashboard --

    def _in_hours(self) -> bool:
        hours = ActiveHours.parse(self.cfg.active_hours)   # a dict when the catalog set it
        return hours is None or hours.contains(self._wall())

    def _roll_day(self) -> None:
        key = self._wall().date().isoformat()
        if key != self._today_key:
            self._today_key, self._today, self._by_item = key, {}, {}

    def _count(self, camera_id: str, what: str) -> None:
        self._roll_day()
        day = self._today.setdefault(camera_id, {"checked": 0, "compliant": 0, "violations": 0})
        day[what] = day.get(what, 0) + 1

    def _note(self, camera_id: str, message: str, level: str, crop: bytes | None = None) -> None:
        when = self._wall().isoformat(timespec="seconds")
        self._recent.appendleft({"message": message, "time": when, "level": level, "camera": camera_id})
        if crop:
            self._gallery.appendleft({"image": "data:image/jpeg;base64," + base64.b64encode(crop).decode(),
                                      "label": message, "time": when})

    # -- the inputs --

    def fetch_best(self, camera_id: str, track_id: str) -> bytes | None:
        """The track's best crop from the detect-pipeline, or None."""
        if self._fetch_best is not None:
            return self._fetch_best(camera_id, track_id)
        try:
            import httpx
            with httpx.Client(timeout=5.0, trust_env=False) as client:
                resp = client.get(f"{self.cfg.best_frame_url}/best_frame",
                                  params={"camera": camera_id, "track": track_id})
        except Exception as exc:
            logger.warning("best frame for %s track %s failed: %s", camera_id, track_id, exc)
            return None
        return resp.content if resp.status_code == 200 and resp.content else None

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

    def _queries(self) -> list[str]:
        out: list[str] = []
        for item in self.cfg.required_items:
            for phrase in self.cfg.item_phrases.get(item, []):
                if phrase not in out:
                    out.append(phrase)
        return out

    def detect(self, camera_id: str, jpeg: bytes) -> list[dict[str, Any]] | None:
        """The detector's detections for the crop, or None when nothing could run."""
        if self._detect is not None:
            return self._detect(camera_id, jpeg)
        adapter = self._pick_adapter()
        if adapter is None:
            logger.warning("no adapter advertises open_vocab_detection; nobody can be checked")
            return None
        if self._kaic is None:
            base = self.cfg.kaic_url or (self._platform().ai._base if self._platform() else None)
            if not base:
                return None
            self._kaic = KaiCClient(base, adapter, api_key=self.cfg.kaic_api_key,
                                    timeout_seconds=float(self.cfg.look_timeout_seconds))
        try:
            body = self._kaic.infer(jpeg, task="open_vocab_detection", camera_id=camera_id,
                                    params={"queries": self._queries(),
                                            "threshold": min(0.1, float(self.cfg.min_item_score))})
        except KaiCError as exc:
            logger.warning("detection for %s failed: %s", camera_id, exc)
            return None
        result = body.get("result") if isinstance(body, dict) else None
        dets = result.get("detections") if isinstance(result, dict) else None
        return list(dets) if isinstance(dets, list) else None

    # -- alerts --

    def _alert(self, st: TrackState, crop: bytes) -> Alert:
        missing = ", ".join(st.missing)
        self._app_alerts_fired += 1
        return Alert(
            title=f"Person without {missing} at {st.camera_id}",
            description=f"Track {st.track_id} was checked {st.missing_run} time"
                        f"{'s' if st.missing_run != 1 else ''} and is missing: {missing}.",
            camera_id=st.camera_id,
            severity=self.cfg.alert_severity,
            alert_type="ppe_missing",
            source=AlertSource(kind="app", name=APP_ID),
            evidence={"track_id": st.track_id, "missing": list(st.missing), "scores": dict(st.scores),
                      "checks": st.checks},
            tags=["ppe", *st.missing],
            images=self._evidence(crop),
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

    def _evidence(self, crop: bytes) -> dict[str, str]:
        if not self.cfg.attach_snapshot:
            return {}
        nvr = self._platform()
        if nvr is None:
            return {}
        try:
            path = nvr.save_evidence(crop)
        except Exception as exc:
            logger.warning("saving the crop as evidence failed: %s", exc)
            return {}
        return {"snapshot": path} if path else {}

    # -- the worker --

    def _work(self) -> None:
        while not self._stopping:
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            try:
                self.run_pending()
            except Exception:
                logger.exception("a check failed")

    async def run(self, *, once: bool = False) -> None:
        self._stopping = False
        self._worker = threading.Thread(target=self._work, name="ppe-check-worker", daemon=True)
        self._worker.start()
        try:
            await super().run(once=once)
            if once:
                self.run_pending()
        finally:
            self._stopping = True
            self._wake.set()

    # -- live config from the catalog --

    LIVE_KNOBS = {
        "required_items": list, "min_item_score": float, "min_person_score": float,
        "min_person_height": float, "confirm_checks": int, "recheck_seconds": float,
        "check_interval_seconds": float, "alert_cooldown_seconds": float,
        "look_timeout_seconds": float, "detector_adapter": str, "alert_severity": str,
        "attach_snapshot": bool,
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
        if "detector_adapter" in changed or "look_timeout_seconds" in changed:
            self._adapter = self.cfg.detector_adapter.strip() or None
            self._kaic = None
        if "required_items" in changed:
            for st in self._tracks.values():        # everyone is judged afresh against the new list
                st.checks, st.missing_run, st.alerted = 0, 0, False
        if "active_hours" in config:
            try:
                hours = ActiveHours.parse(config["active_hours"])
            except ValueError as exc:
                logger.warning("config edit active_hours=%r ignored: %s", config["active_hours"], exc)
            else:
                if hours != self.cfg.active_hours:
                    self.cfg.active_hours = hours
                    changed.append("active_hours")
        if changed:
            logger.info("config applied live: %s", ", ".join(changed))

    # -- contract surface --

    def state_snapshot(self) -> dict[str, Any]:
        with self._lock:
            pending = len(self._pending)
        self._roll_day()

        def pct(day: dict[str, int]) -> float | None:
            return None if not day.get("checked") else round(100.0 * day.get("compliant", 0) / day["checked"], 1)

        per_camera = []
        for cam, c in self._cameras.items():
            day = self._today.get(cam, {})
            per_camera.append({"camera": cam, "checked_today": day.get("checked", 0),
                               "violations_today": day.get("violations", 0), "compliance_pct": pct(day),
                               "last_missing": ", ".join(c.last_missing), "last_check": c.last_check})
        total = {k: sum(d.get(k, 0) for d in self._today.values()) for k in ("checked", "compliant", "violations")}
        return {
            "adapter": self._adapter,
            "alerts_fired": self._app_alerts_fired,
            "checks": self._checks,
            "failed_checks": self._failed_checks,
            "skipped": self._skipped,
            "pending": pending,
            "in_hours": self._in_hours(),
            "required_items": list(self.cfg.required_items),
            "today": {**total, "compliance_pct": pct(total), "since": self._today_key},
            "cameras": {
                cam: {"checked": c.checked, "compliant": c.compliant, "violations": c.violations,
                      "suppressed": c.suppressed, "last_missing": list(c.last_missing)}
                for cam, c in self._cameras.items()
            },
            "per_camera": per_camera,
            "by_item": [{"item": item, "today": n} for item, n in sorted(self._by_item.items(), key=lambda kv: -kv[1])],
            "recent": list(self._recent),
            "gallery": list(self._gallery),
        }


def _knob_values(cfg: Any) -> dict[str, Any]:
    out = {k: getattr(cfg, k) for k in PpeCheck.LIVE_KNOBS if hasattr(cfg, k)}
    out["item_phrases"] = getattr(cfg, "item_phrases", {})
    return out


# ── wiring ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenNVR ppe-check app")
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--once", action="store_true", help="handle one event, run its checks, then exit")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = load_app_config(args.config, AppConfig)
    if not config.nats_url:
        raise SystemExit("config: nats_url is required (the Tier-0 stream is the input)")
    dispatcher = build_dispatcher(
        webhook_url=config.webhook_url,
        nats_alerts_url=config.nats_alerts_url,
        nats_alerts_token=config.nats_alerts_token,
        nats_alerts_subject_prefix=config.nats_alerts_subject_prefix,
    )
    app = PpeCheck(config, dispatcher, clock=lambda: _dt.datetime.now(_dt.timezone.utc))

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
