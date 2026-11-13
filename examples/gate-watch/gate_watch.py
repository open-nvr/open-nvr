# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
gate-watch — tells you when a gate has been left open.

Every ``poll_interval_seconds`` the app fetches one frame per picked
camera through core, crops it to the gate the operator drew, and asks the
site's vision model one question through KAI-C: "is the gate open or
closed?". A gate seen open for ``open_seconds`` raises ``gate_left_open``;
the first "closed" after that raises ``gate_closed`` (low) and clears it.
Looks the model cannot judge count as unknown and neither extend nor
reset the open timer, up to ``unsure_tolerance`` in a row.

The question is answered by whichever registered adapter advertises
``visual_qa`` (the shipped moondream adapter), or the one named in
``vqa_adapter``. On CPU a look is a few seconds, so the poll interval
defaults to 30 s; on a GPU box 10 s is fine.

Run::

    python gate_watch.py --config config.yml           # daemon
    python gate_watch.py --config config.yml --once    # one pass
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

logger = logging.getLogger("gate-watch")

APP_ID = "gate-watch"
SEVERITIES = ("low", "medium", "high", "critical")
VQA_TASKS = ("visual_qa", "vqa")
OPEN_WORDS = {"open", "opened", "opening", "ajar", "yes"}
CLOSED_WORDS = {"closed", "close", "shut", "locked", "no"}
#: Margin around the drawn gate, as a fraction of its size, so the posts
#: and the ground line stay in the crop the model sees.
CROP_MARGIN = 0.15

MANIFEST = AppManifest(
    id=APP_ID,
    name="Gate Watch",
    version="1.0.0",
    category="perimeter",
    summary=(
        "Alerts when a gate has been left open: every look asks the site's "
        "vision model one question about the gate the operator drew, and a "
        "gate open past the threshold raises the alert; it clears when the "
        "gate closes."
    ),
    requires_tasks=["vqa"],
    params=[
        Param("gate", "geometry.polygon", per_camera=True,
              description="The gate, drawn on the camera. Whole frame when not drawn."),
        Param("poll_interval_seconds", float, default=30.0,
              description="How often each gate is looked at. Each look is one vision-model question."),
        Param("open_seconds", float, default=120.0,
              description="Seen open this long before it alerts."),
        Param("cooldown_seconds", float, default=600.0,
              description="After an alert, the same gate stays quiet this long unless it closes."),
        Param("unsure_tolerance", int, default=2,
              description="Looks in a row the model could not judge before the gate counts as unknown."),
        Param("look_timeout_seconds", float, default=60.0,
              description="How long one vision-model answer may take. A CPU look is several seconds; keep this above it."),
        Param("vqa_adapter", str, default="",
              description="KAI-C adapter that answers the question. Empty = the first one advertising visual_qa."),
        Param("question", str,
              default="Is the gate in this picture open or closed? Answer with one word: open or closed.",
              description="The question asked about the crop. Keep the one-word instruction."),
        Param("active_hours", "time_range",
              description="When alerts fire. Outside it the gate is still watched and counted, quietly. "
                          "Empty = around the clock."),
        Param("escalate_after_seconds", float, default=600.0,
              description="Still open this long after the left-open alert: raise a second, higher one. "
                          "0 = no escalation."),
        Param("alert_severity", str, default="medium", choices=list(SEVERITIES),
              description="Severity of the left-open alert."),
        Param("attach_snapshot", bool, default=True,
              description="Attach the frame to the alert."),
    ],
    emits=[
        AlertType("gate_left_open", "medium", "The gate has been open longer than allowed."),
        AlertType("gate_still_open", "high", "The gate is still open well after the left-open alert."),
        AlertType("gate_closed", "low", "The gate closed again after a left-open alert."),
        AlertType("gate_unknown", "low", "The gate could not be judged for several looks in a row."),
    ],
    state_schema=[
        StateView("open_now", "Open now", kind="metric", path="open_now",
                  description="Gates open at this moment."),
        StateView("longest_open_s", "Longest open", kind="metric", path="longest_open_s", unit="s"),
        StateView("alerts_today", "Alerts today", kind="metric", path="today.alerts"),
        StateView("opens_today", "Open events today", kind="metric", path="today.opens"),
        StateView("per_camera", "Per gate", kind="table", path="per_camera",
                  columns=["camera", "verdict", "open_for_s", "alerted", "opens_today", "alerts_today",
                           "looks", "last_answer", "gate_drawn"]),
        StateView("recent", "Recent", kind="log", path="recent", limit=12),
        StateView("gallery", "Left open", kind="gallery", path="gallery", limit=8,
                  description="The frame at each left-open alert."),
    ],
)


# ── config ────────────────────────────────────────────────────────────


@dataclass
class AppConfig(BaseAppConfig):
    poll_interval_seconds: float = 30.0
    open_seconds: float = 120.0
    cooldown_seconds: float = 600.0
    unsure_tolerance: int = 2
    look_timeout_seconds: float = 60.0
    vqa_adapter: str = ""
    question: str = "Is the gate in this picture open or closed? Answer with one word: open or closed."
    alert_severity: str = "medium"
    attach_snapshot: bool = True
    active_hours: dict | None = None
    escalate_after_seconds: float = 600.0
    kaic_url: str | None = None
    kaic_api_key: str | None = None
    #: Standalone only: ``[{camera_id, frame_url, gate?}]``.
    cameras: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        if self.open_seconds < 0 or self.cooldown_seconds < 0:
            raise ValueError("open_seconds and cooldown_seconds must be >= 0")
        if int(self.unsure_tolerance) < 1:
            raise ValueError("unsure_tolerance must be at least 1")
        if self.look_timeout_seconds <= 0:
            raise ValueError("look_timeout_seconds must be > 0")
        if self.alert_severity not in SEVERITIES:
            raise ValueError(f"alert_severity must be one of {SEVERITIES}")
        if not str(self.question).strip():
            raise ValueError("question must not be empty")
        if self.escalate_after_seconds < 0:
            raise ValueError("escalate_after_seconds must be >= 0")
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


# ── the judgement ─────────────────────────────────────────────────────


def normalise_answer(answer: Any) -> str:
    """A model's sentence reduced to ``open``, ``closed`` or ``unsure``.
    Matched on the words IN the answer: "The gate is open." still lands."""
    words = {w.strip(".,!;:'\"()").lower() for w in str(answer or "").split()}
    is_open = bool(words & OPEN_WORDS)
    is_closed = bool(words & CLOSED_WORDS)
    if is_open and not is_closed:
        return "open"
    if is_closed and not is_open:
        return "closed"
    return "unsure"


def crop_to_gate(frame_bytes: bytes, gate: list[list[float]] | None) -> bytes:
    """The frame cropped to the drawn gate's box plus a margin; the whole
    frame when no gate is drawn or the drawing is unusable."""
    if not gate or len(gate) < 3:
        return frame_bytes
    with Image.open(io.BytesIO(frame_bytes)) as im:
        w, h = im.size
        try:
            pts = scale_vertices(gate, w, h)
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


# ── per-camera state ──────────────────────────────────────────────────


@dataclass
class GateState:
    verdict: str | None = None         # open | closed | unsure, from the last look
    open_since: float | None = None    # when the current open run began
    unsure_run: int = 0
    alerted: bool = False              # a left-open alert is outstanding
    alerted_at: float = 0.0
    escalated: bool = False
    unknown_alerted: bool = False
    looks: int = 0
    last_answer: str | None = None
    gate: list[list[float]] | None = None


Asker = Callable[[str, bytes], str | None]


# ── the app ───────────────────────────────────────────────────────────


class GateWatch(FrameApp):
    manifest = MANIFEST

    def setup(self) -> None:
        self._state: dict[str, GateState] = {}
        self._clock = time.monotonic
        self._nvr = None
        self._kaic: KaiCClient | None = None
        self._adapter: str | None = (self.cfg.vqa_adapter or "").strip() or None
        self._asker: Asker | None = None
        self._app_alerts_fired = 0
        self._wall = lambda: _dt.datetime.now().astimezone()
        self._today_key = self._wall().date().isoformat()
        self._today: dict[str, dict[str, int]] = {}
        self._recent: deque = deque(maxlen=30)
        self._gallery: deque = deque(maxlen=8)
        for cam in getattr(self.cfg, "cameras", None) or []:
            cid = str(cam.get("camera_id") or "") if isinstance(cam, dict) else ""
            if cid and isinstance(cam.get("gate"), list):
                self._state.setdefault(cid, GateState()).gate = cam["gate"]

    # -- asking --

    def _pick_adapter(self) -> str | None:
        """The configured adapter, else the first registered one that
        advertises a VQA task. Cached once found."""
        if self._adapter:
            return self._adapter
        nvr = self._platform()
        caps = nvr.ai.capabilities() if nvr is not None else None
        for name, card in ((caps or {}).get("adapters") or {}).items():
            tasks = set(card.get("tasks_advertised") or [])
            if tasks & set(VQA_TASKS):
                self._adapter = name
                logger.info("asking %s (advertises %s)", name, sorted(tasks & set(VQA_TASKS)))
                return name
        return None

    def ask(self, camera_id: str, jpeg: bytes) -> str | None:
        """The model's raw answer, or None when nothing could answer."""
        if self._asker is not None:
            return self._asker(camera_id, jpeg)
        adapter = self._pick_adapter()
        if adapter is None:
            logger.warning("no adapter advertises visual_qa; the gate cannot be judged")
            return None
        if self._kaic is None:
            base = self.cfg.kaic_url or (self._platform().ai._base if self._platform() else None)
            if not base:
                return None
            self._kaic = KaiCClient(base, adapter, api_key=self.cfg.kaic_api_key,
                                    timeout_seconds=float(self.cfg.look_timeout_seconds))
        q = self.cfg.question
        try:
            body = self._kaic.infer(jpeg, task="visual_qa", camera_id=camera_id,
                                    params={"question": q, "prompt": q})
        except KaiCError as exc:
            logger.warning("vision question for %s failed: %s", camera_id, exc)
            return None
        result = body.get("result") if isinstance(body, dict) else None
        return str(result.get("answer")) if isinstance(result, dict) and result.get("answer") is not None else None

    # -- the rule --

    def on_frame(self, camera_id: str, frame_bytes: bytes) -> Iterable[Alert] | None:
        st = self._state.setdefault(camera_id, GateState())
        now = self._clock()
        crop = crop_to_gate(frame_bytes, st.gate)
        answer = self.ask(camera_id, crop)
        verdict = normalise_answer(answer) if answer is not None else "unsure"
        st.looks += 1
        st.last_answer = answer
        st.verdict = verdict
        fired: list[Alert] = []

        if verdict == "unsure":
            st.unsure_run += 1
            if st.unsure_run >= int(self.cfg.unsure_tolerance) and not st.unknown_alerted:
                fired.append(self._alert(camera_id, "unknown", st, severity="low"))
                st.unknown_alerted = True
            return fired
        if st.unsure_run and st.unknown_alerted:
            st.unknown_alerted = False
        st.unsure_run = 0

        if verdict == "closed":
            st.open_since = None
            if st.alerted:
                fired.append(self._alert(camera_id, "closed", st, severity="low"))
                st.alerted, st.escalated = False, False
            return fired

        # open
        if st.open_since is None:
            st.open_since = now
            self._count(camera_id, "opens")
            self._note(camera_id, f"{camera_id}: gate opened", "info")
        held = now - st.open_since
        if held < self.cfg.open_seconds or not self._in_hours():
            return fired
        if st.alerted:
            if (self.cfg.escalate_after_seconds > 0 and not st.escalated
                    and now - st.alerted_at >= self.cfg.escalate_after_seconds):
                fired.append(self._alert(camera_id, "still_open", st, severity="high",
                                         frame=frame_bytes, held=held))
                st.escalated = True
            if now - st.alerted_at < self.cfg.cooldown_seconds:
                return fired
        fired.append(self._alert(camera_id, "left_open", st, severity=self.cfg.alert_severity,
                                 frame=frame_bytes, held=held))
        st.alerted, st.alerted_at = True, now
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
        day = self._today.setdefault(camera_id, {"opens": 0, "alerts": 0})
        day[what] = day.get(what, 0) + 1

    def _note(self, camera_id: str, message: str, level: str, frame: bytes | None = None) -> None:
        when = self._wall().isoformat(timespec="seconds")
        self._recent.appendleft({"message": message, "time": when, "level": level, "camera": camera_id})
        if frame is not None:
            image = thumbnail(frame)
            if image:
                self._gallery.appendleft({"image": image, "label": message, "time": when})

    # -- alerts --

    def _alert(self, camera_id: str, kind: str, st: GateState, *, severity: str,
               frame: bytes | None = None, held: float = 0.0) -> Alert:
        titles = {
            "left_open": f"Gate left open at {camera_id}",
            "still_open": f"Gate still open at {camera_id}",
            "closed": f"Gate closed at {camera_id}",
            "unknown": f"Gate at {camera_id} cannot be judged",
        }
        descriptions = {
            "left_open": f"The gate has been open for {int(held)} s, longer than the {int(self.cfg.open_seconds)} s allowed.",
            "still_open": f"The gate is still open after {int(held)} s; nobody has closed it since the alert.",
            "closed": "The gate closed again.",
            "unknown": "The vision model could not tell whether the gate is open; check the camera's view of it.",
        }
        self._app_alerts_fired += 1
        if kind in ("left_open", "still_open"):
            self._count(camera_id, "alerts")
        self._note(camera_id, titles[kind], {"closed": "info", "unknown": "warning"}.get(kind, "alert"),
                   frame if kind in ("left_open", "still_open") else None)
        return Alert(
            title=titles[kind],
            description=descriptions[kind],
            camera_id=camera_id,
            severity=severity,
            alert_type=f"gate_{kind}",
            source=AlertSource(kind="app", name=APP_ID),
            evidence={"answer": st.last_answer, "open_for_s": int(held), "looks": st.looks},
            tags=["gate", kind],
            images=self._evidence(frame) if frame else {},
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

    def _evidence(self, frame: bytes) -> dict[str, str]:
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
        return {"snapshot": path} if path else {}

    # -- live config from the catalog --

    LIVE_KNOBS = {
        "poll_interval_seconds": float, "open_seconds": float, "cooldown_seconds": float,
        "unsure_tolerance": int, "look_timeout_seconds": float, "vqa_adapter": str, "question": str,
        "alert_severity": str, "attach_snapshot": bool, "escalate_after_seconds": float,
    }

    def on_config_update(self, config: dict[str, Any]) -> None:
        changed: list[str] = []
        for key, typ in self.LIVE_KNOBS.items():
            if key not in config or config[key] is None:
                continue
            try:
                value = bool(config[key]) if typ is bool else typ(config[key])
                trial = AppConfig(**{**_knob_values(self.cfg), key: value})
            except (TypeError, ValueError) as exc:
                logger.warning("config edit %s=%r ignored: %s", key, config[key], exc)
                continue
            if getattr(self.cfg, key, None) != getattr(trial, key):
                setattr(self.cfg, key, getattr(trial, key))
                changed.append(key)
        if "poll_interval_seconds" in changed:
            self._interval = float(self.cfg.poll_interval_seconds)
        if "vqa_adapter" in changed or "look_timeout_seconds" in changed:
            self._adapter = self.cfg.vqa_adapter.strip() or None
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
        gates = config.get("gate")
        if isinstance(gates, dict):
            for raw_key, drawn in gates.items():
                cam = str(raw_key)
                cam = cam if cam.startswith("cam") else f"cam{cam}"
                st = self._state.setdefault(cam, GateState())
                new = drawn if isinstance(drawn, list) and len(drawn) >= 3 else None
                if new != st.gate:
                    st.gate = new
                    changed.append(f"gate[{cam}]")
        if changed:
            logger.info("config applied live: %s", ", ".join(changed))

    # -- contract surface --

    def state_snapshot(self) -> dict[str, Any]:
        now = self._clock()
        self._roll_day()
        cameras = {
            cam: {
                "verdict": st.verdict,
                "open_for_s": None if st.open_since is None else int(now - st.open_since),
                "alerted": st.alerted,
                "escalated": st.escalated,
                "looks": st.looks,
                "last_answer": st.last_answer,
                "gate_drawn": st.gate is not None,
            }
            for cam, st in self._state.items()
        }
        per_camera = []
        for cam, row in cameras.items():
            day = self._today.get(cam, {})
            per_camera.append({"camera": cam, **{k: row[k] for k in ("verdict", "open_for_s", "alerted")},
                               "opens_today": day.get("opens", 0), "alerts_today": day.get("alerts", 0),
                               "looks": row["looks"], "last_answer": row["last_answer"],
                               "gate_drawn": row["gate_drawn"]})
        open_for = [r["open_for_s"] for r in cameras.values() if r["open_for_s"] is not None]
        return {
            "adapter": self._adapter,
            "alerts_fired": self._app_alerts_fired,
            "open_now": len(open_for),
            "longest_open_s": max(open_for, default=0),
            "in_hours": self._in_hours(),
            "today": {"opens": sum(d.get("opens", 0) for d in self._today.values()),
                      "alerts": sum(d.get("alerts", 0) for d in self._today.values()),
                      "since": self._today_key},
            "cameras": cameras,
            "per_camera": per_camera,
            "recent": list(self._recent),
            "gallery": list(self._gallery),
        }


def _knob_values(cfg: Any) -> dict[str, Any]:
    return {k: getattr(cfg, k) for k in GateWatch.LIVE_KNOBS if hasattr(cfg, k)}


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


def build_app(config: AppConfig, dispatcher) -> GateWatch:
    standalone = _StandaloneSource(config.cameras)
    source = _EitherSource(standalone, CoreSnapshotSource())
    return GateWatch(config, dispatcher, frame_source=source,
                     cameras=standalone.camera_ids,
                     poll_interval_seconds=config.poll_interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenNVR gate-watch app")
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
