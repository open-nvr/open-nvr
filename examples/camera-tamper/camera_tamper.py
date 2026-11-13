# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
camera-tamper — tells you when a camera can no longer see what it was
pointed at: covered, defocused, moved or frozen.

No model runs. Every ``poll_interval_seconds`` the app fetches one frame
per picked camera through core, reduces it to a small greyscale copy and
scores four things against a reference view it learned from the camera
itself:

* **covered** — most pixels near black, or no contrast at all;
* **defocused** — sharpness (variance of the Laplacian) fell below a
  fraction of the reference's;
* **moved** — the picture's edges no longer line up with the reference's
  (plain brightness correlation forgives a zoom of the same scene; edge
  correlation does not);
* **frozen** — poll after poll the picture does not change at all.

A condition must hold for ``confirm_seconds`` before it alerts; the same
condition on the same camera then stays quiet for ``cooldown_seconds``.
When the camera looks normal again after an alert, ``tamper_cleared``
fires once. A camera that stays "moved" for ``rebaseline_seconds`` was
re-aimed on purpose: the new view becomes the reference.

Run::

    python camera_tamper.py --config config.yml           # daemon
    python camera_tamper.py --config config.yml --once    # one pass
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as _dt
import hashlib
import io
import logging
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np
from PIL import Image

from opennvr_app_sdk import (
    Alert,
    AlertType,
    AppManifest,
    BaseAppConfig,
    CoreSnapshotSource,
    FrameApp,
    FrameSource,
    Param,
    StateView,
    build_dispatcher,
    build_frame_source,
    load_app_config,
)
from opennvr_app_sdk.alerts import AlertSource

logger = logging.getLogger("camera-tamper")

APP_ID = "camera-tamper"
CONDITIONS = ("covered", "defocused", "moved", "frozen")
SEVERITIES = ("low", "medium", "high", "critical")

#: The greyscale working size. Small enough that every check costs a few
#: milliseconds, large enough that a smear or a re-aim still shows.
WORK_W, WORK_H = 160, 120
#: Pixels at or below this (of 255) count as "near black".
DARK_LEVEL = 40.0
#: How fast the reference follows a camera that looks normal (per poll).
REFERENCE_ALPHA = 0.05


MANIFEST = AppManifest(
    id=APP_ID,
    name="Camera Tamper",
    version="1.0.0",
    category="perimeter",
    summary=(
        "Alerts when a camera is covered, defocused, moved or its stream "
        "freezes — judged against a reference view learned from the camera "
        "itself, with no model to run."
    ),
    requires_tasks=[],
    params=[
        Param("poll_interval_seconds", float, default=5.0,
              description="How often each camera is checked."),
        Param("confirm_seconds", float, default=10.0,
              description="A condition must hold this long before it alerts."),
        Param("cooldown_seconds", float, default=300.0,
              description="After an alert, the same condition on the same camera stays quiet this long."),
        Param("rebaseline_seconds", float, default=600.0,
              description="A camera 'moved' this long was re-aimed on purpose: the new view becomes the reference."),
        Param("blur_ratio", float, default=0.3,
              description="Sharpness below this fraction of the reference's is 'defocused'."),
        Param("dark_fraction", float, default=0.85,
              description="This fraction of near-black pixels is 'covered'."),
        Param("uniform_std", float, default=6.0,
              description="A picture with less contrast than this is 'covered'."),
        Param("moved_correlation", float, default=0.45,
              description="Edge correlation with the reference below this is 'moved'."),
        Param("freeze_polls", int, default=6,
              description="This many unchanged frames in a row is 'frozen'."),
        Param("frozen_mad", float, default=0.2,
              description="Mean pixel change (0-255, on the small greyscale copy) below which two "
                          "consecutive frames count as unchanged. Raise on a very still night scene."),
        Param("alert_severity", str, default="high", choices=list(SEVERITIES),
              description="Severity of covered / defocused / moved / frozen alerts."),
        Param("attach_snapshot", bool, default=True,
              description="Attach the offending frame to the alert."),
    ],
    emits=[
        AlertType("tamper_covered", "high", "The lens is covered or the picture has gone black."),
        AlertType("tamper_defocused", "high", "The picture has gone soft: a smeared or refocused lens."),
        AlertType("tamper_moved", "high", "The camera no longer looks at its reference view."),
        AlertType("tamper_frozen", "high", "The stream stopped changing."),
        AlertType("tamper_cleared", "low", "The camera looks normal again."),
    ],
    state_schema=[
        StateView("watched", "Cameras watched", kind="metric", path="watched"),
        StateView("tampered_now", "Tampered now", kind="metric", path="tampered_now",
                  description="Cameras with a tamper condition not yet cleared."),
        StateView("alerts_today", "Tamper alerts today", kind="metric", path="today.alerts"),
        StateView("focus", "Focus vs reference", kind="gauge", path="focus_pct", min=0, max=100, unit="%",
                  warn=60, danger=35,
                  description="Sharpness now as a share of the reference view's. Below blur_ratio is defocused."),
        StateView("per_camera", "Per camera", kind="table", path="per_camera",
                  columns=["camera", "condition", "for_s", "focus_pct", "match_pct", "alerts_today", "checks",
                           "has_reference"]),
        StateView("by_condition", "Today, by condition", kind="table", path="by_condition",
                  columns=["condition", "today"]),
        StateView("recent", "Recent", kind="log", path="recent", limit=12),
        StateView("gallery", "At the alert", kind="gallery", path="gallery", limit=8,
                  description="The reference view and the frame at each tamper alert."),
    ],
)


# ── config ────────────────────────────────────────────────────────────


@dataclass
class AppConfig(BaseAppConfig):
    poll_interval_seconds: float = 5.0
    confirm_seconds: float = 10.0
    cooldown_seconds: float = 300.0
    rebaseline_seconds: float = 600.0
    blur_ratio: float = 0.3
    dark_fraction: float = 0.85
    uniform_std: float = 6.0
    moved_correlation: float = 0.45
    freeze_polls: int = 6
    frozen_mad: float = 0.2
    alert_severity: str = "high"
    attach_snapshot: bool = True
    #: Standalone only: ``[{camera_id, frame_url}]``. On a compose install
    #: the picked cameras are fetched through core instead.
    cameras: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        if self.alert_severity not in SEVERITIES:
            raise ValueError(f"alert_severity must be one of {SEVERITIES}")
        if not (0 < self.blur_ratio < 1):
            raise ValueError("blur_ratio must be between 0 and 1")
        if not (0 < self.dark_fraction <= 1):
            raise ValueError("dark_fraction must be between 0 and 1")
        if not (-1 <= self.moved_correlation < 1):
            raise ValueError("moved_correlation must be below 1")
        if int(self.freeze_polls) < 2:
            raise ValueError("freeze_polls must be at least 2")
        if self.frozen_mad < 0:
            raise ValueError("frozen_mad must be >= 0")


# ── the arithmetic ────────────────────────────────────────────────────


def to_gray(frame_bytes: bytes) -> np.ndarray:
    """A small float greyscale copy of a JPEG/PNG frame."""
    with Image.open(io.BytesIO(frame_bytes)) as im:
        g = im.convert("L").resize((WORK_W, WORK_H), Image.BILINEAR)
        return np.asarray(g, dtype=np.float32)


def sharpness(g: np.ndarray) -> float:
    """Variance of a 4-neighbour Laplacian: high for crisp edges, near
    zero for a smear."""
    lap = (4.0 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1]
           - g[1:-1, :-2] - g[1:-1, 2:])
    return float(lap.var())


def dark_share(g: np.ndarray) -> float:
    return float((g <= DARK_LEVEL).mean())


def correlation(a: np.ndarray, b: np.ndarray) -> float:
    """Zero-mean normalised cross-correlation, -1..1; 1 is the same view."""
    a0 = a - a.mean()
    b0 = b - b.mean()
    denom = float(np.sqrt((a0 * a0).sum() * (b0 * b0).sum()))
    if denom < 1e-6:
        return 0.0
    return float((a0 * b0).sum() / denom)


def structure(g: np.ndarray) -> np.ndarray:
    """Edge-magnitude map, box-blurred 3x3 so a pixel or two of jitter still
    lines up. Correlating these is what tells a re-aimed camera from the
    same view: brightness correlation stays high for a zoom of the same
    scene (sky above, asphalt below), edge correlation collapses."""
    gx = np.zeros_like(g)
    gy = np.zeros_like(g)
    gx[:, 1:-1] = g[:, 2:] - g[:, :-2]
    gy[1:-1, :] = g[2:, :] - g[:-2, :]
    m = np.sqrt(gx * gx + gy * gy)
    p = np.pad(m, 1, mode="edge")
    h, w = m.shape
    return sum(p[i:i + h, j:j + w] for i in range(3) for j in range(3)) / 9.0


def structure_correlation(a: np.ndarray, b: np.ndarray) -> float:
    return correlation(structure(a), structure(b))


def frame_change(a: np.ndarray, b: np.ndarray) -> float:
    """Mean absolute pixel difference between two working copies."""
    return float(np.abs(a - b).mean())


@dataclass
class Scores:
    sharpness: float
    dark: float
    std: float
    corr: float | None          # brightness correlation, None until a reference exists
    edge_corr: float | None     # edge correlation, the one 'moved' is judged on
    change: float | None        # mean pixel change since the previous frame
    digest: str


def score(g: np.ndarray, frame_bytes: bytes, reference: np.ndarray | None,
          previous: np.ndarray | None = None) -> Scores:
    return Scores(
        sharpness=sharpness(g),
        dark=dark_share(g),
        std=float(g.std()),
        corr=correlation(g, reference) if reference is not None else None,
        edge_corr=structure_correlation(g, reference) if reference is not None else None,
        change=frame_change(g, previous) if previous is not None else None,
        digest=hashlib.sha1(frame_bytes).hexdigest(),
    )


def _knob_values(cfg: Any) -> dict[str, Any]:
    """The tunable fields as they stand on ``cfg``, for re-validation."""
    return {k: getattr(cfg, k) for k in CameraTamper.LIVE_KNOBS if hasattr(cfg, k)}


# ── per-camera state ──────────────────────────────────────────────────


@dataclass
class CameraState:
    reference: np.ndarray | None = None
    reference_sharpness: float = 0.0
    last_digest: str | None = None
    last_gray: np.ndarray | None = None
    same_in_a_row: int = 0
    condition: str | None = None       # what this tick saw, after judging
    since: float = 0.0                 # when the current condition began
    alerted: str | None = None         # the condition last alerted on
    alerted_at: float = 0.0
    last_scores: Scores | None = None
    checks: int = 0
    reference_jpeg: bytes | None = None   # the frame the reference was learnt from, for the gallery


# ── the app ───────────────────────────────────────────────────────────


class CameraTamper(FrameApp):
    manifest = MANIFEST

    def setup(self) -> None:
        self._state: dict[str, CameraState] = {}
        self._clock = time.monotonic
        self._nvr = None
        self._app_alerts_fired = 0
        self._wall = lambda: _dt.datetime.now().astimezone()
        self._today_key = self._wall().date().isoformat()
        self._today: dict[str, int] = {}                 # alerts today, per camera
        self._by_condition: dict[str, int] = {}          # alerts today, per condition
        self._recent: deque = deque(maxlen=30)
        self._gallery: deque = deque(maxlen=8)

    # -- bookkeeping for the dashboard --

    def _roll_day(self) -> None:
        key = self._wall().date().isoformat()
        if key != self._today_key:
            self._today_key, self._today, self._by_condition = key, {}, {}

    def _note(self, camera_id: str, message: str, level: str, frame: bytes | None = None,
              reference: bytes | None = None) -> None:
        when = self._wall().isoformat(timespec="seconds")
        self._recent.appendleft({"message": message, "time": when, "level": level, "camera": camera_id})
        for label, image in ((f"{message} — now", frame), (f"{camera_id} — reference view", reference)):
            thumb = thumbnail(image) if image else None
            if thumb:
                self._gallery.appendleft({"image": thumb, "label": label, "time": when})

    # -- judging one frame --

    def judge(self, st: CameraState, s: Scores) -> str | None:
        """Which condition, if any, this frame shows. Order matters: a
        covered lens is also blurry, uncorrelated and, being dark, nearly
        unchanging, so it is tested first and names the fault; frozen is
        next, on the picture not changing at all; the two view tests
        need a reference."""
        cfg = self.cfg
        if s.dark >= cfg.dark_fraction or s.std <= cfg.uniform_std:
            return "covered"
        if st.same_in_a_row >= int(cfg.freeze_polls) - 1:
            return "frozen"
        if st.reference is None:
            return None
        if st.reference_sharpness > 0 and s.sharpness < cfg.blur_ratio * st.reference_sharpness:
            return "defocused"
        if s.edge_corr is not None and s.edge_corr < cfg.moved_correlation:
            return "moved"
        return None

    def on_frame(self, camera_id: str, frame_bytes: bytes) -> Iterable[Alert] | None:
        st = self._state.setdefault(camera_id, CameraState())
        now = self._clock()
        g = to_gray(frame_bytes)
        s = score(g, frame_bytes, st.reference, st.last_gray)
        unchanged = s.digest == st.last_digest or (
            s.change is not None and s.change <= self.cfg.frozen_mad)
        st.same_in_a_row = st.same_in_a_row + 1 if unchanged else 0
        st.last_digest = s.digest
        st.last_gray = g
        st.last_scores = s
        st.checks += 1

        condition = self.judge(st, s)
        fired: list[Alert] = []

        if condition is None:
            # A normal frame: learn from it, and clear an outstanding alert.
            if st.reference is None:
                st.reference = g.copy()
                st.reference_sharpness = s.sharpness
                st.reference_jpeg = frame_bytes
            else:
                st.reference = (1 - REFERENCE_ALPHA) * st.reference + REFERENCE_ALPHA * g
                st.reference_sharpness = ((1 - REFERENCE_ALPHA) * st.reference_sharpness
                                          + REFERENCE_ALPHA * s.sharpness)
            if st.alerted is not None:
                fired.append(self._alert(camera_id, "cleared", s, severity="low"))
                st.alerted = None
            st.condition, st.since = None, now
            return fired

        if condition != st.condition:
            st.condition, st.since = condition, now
        held = now - st.since

        if condition == "moved" and held >= self.cfg.rebaseline_seconds:
            # Re-aimed on purpose: adopt the new view and start again.
            st.reference, st.reference_sharpness = g.copy(), s.sharpness
            st.reference_jpeg = frame_bytes
            st.condition, st.since = None, now
            if st.alerted is not None:
                fired.append(self._alert(camera_id, "cleared", s, severity="low"))
                st.alerted = None
            return fired

        if held < self.cfg.confirm_seconds:
            return fired
        if st.alerted == condition and now - st.alerted_at < self.cfg.cooldown_seconds:
            return fired
        fired.append(self._alert(camera_id, condition, s, severity=self.cfg.alert_severity,
                                 frame=frame_bytes))
        st.alerted, st.alerted_at = condition, now
        return fired

    # -- alerts --

    def _alert(self, camera_id: str, condition: str, s: Scores, *, severity: str,
               frame: bytes | None = None) -> Alert:
        titles = {
            "covered": f"Camera {camera_id} covered",
            "defocused": f"Camera {camera_id} defocused",
            "moved": f"Camera {camera_id} moved",
            "frozen": f"Camera {camera_id} stream frozen",
            "cleared": f"Camera {camera_id} back to normal",
        }
        descriptions = {
            "covered": "Most of the picture is near black or has no contrast: something is over the lens.",
            "defocused": "The picture has gone soft: a smeared or refocused lens.",
            "moved": "The picture no longer matches the view this camera is meant to hold.",
            "frozen": "The same frame keeps arriving: the stream has stopped changing.",
            "cleared": "The camera looks normal again.",
        }
        evidence: dict[str, Any] = {
            "condition": condition,
            "sharpness": round(s.sharpness, 1),
            "dark_fraction": round(s.dark, 3),
            "contrast_std": round(s.std, 1),
            "correlation": None if s.corr is None else round(s.corr, 3),
            "edge_correlation": None if s.edge_corr is None else round(s.edge_corr, 3),
            "frame_change": None if s.change is None else round(s.change, 3),
        }
        self._app_alerts_fired += 1
        if condition != "cleared":
            self._roll_day()
            self._today[camera_id] = self._today.get(camera_id, 0) + 1
            self._by_condition[condition] = self._by_condition.get(condition, 0) + 1
        st = self._state.get(camera_id)
        self._note(camera_id, titles[condition], "info" if condition == "cleared" else "alert",
                   frame, st.reference_jpeg if (st and frame) else None)
        return Alert(
            title=titles[condition],
            description=descriptions[condition],
            camera_id=camera_id,
            severity=severity,
            alert_type=f"tamper_{condition}",
            source=AlertSource(kind="app", name=APP_ID),
            evidence=evidence,
            tags=["tamper", condition],
            images=self._evidence(frame) if frame else {},
        )

    def _platform(self):
        if self._nvr is None and getattr(self.cfg, "opennvr_url", None):
            try:
                from opennvr_app_sdk import OpenNVR
                self._nvr = OpenNVR(self.cfg.opennvr_url, timeout=3.0)
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

    #: The knobs the catalog form edits, with the type each is read as.
    LIVE_KNOBS = {
        "poll_interval_seconds": float, "confirm_seconds": float, "cooldown_seconds": float,
        "rebaseline_seconds": float, "blur_ratio": float, "dark_fraction": float,
        "uniform_std": float, "moved_correlation": float, "freeze_polls": int, "frozen_mad": float,
        "alert_severity": str, "attach_snapshot": bool,
    }

    def on_config_update(self, config: dict[str, Any]) -> None:
        """Catalog edits, applied live and idempotently: each knob is
        coerced, checked by the same rules as the YAML, and swapped in
        only when it changed. A bad value is logged and skipped, the
        rest still apply."""
        changed: list[str] = []
        for key, typ in self.LIVE_KNOBS.items():
            if key not in config or config[key] is None:
                continue
            try:
                value = bool(config[key]) if typ is bool else typ(config[key])
                trial = AppConfig(**{**_knob_values(self.cfg), key: value})   # validates
            except (TypeError, ValueError) as exc:
                logger.warning("config edit %s=%r ignored: %s", key, config[key], exc)
                continue
            if getattr(self.cfg, key, None) != getattr(trial, key):
                setattr(self.cfg, key, getattr(trial, key))
                changed.append(key)
        if "poll_interval_seconds" in changed:
            self._interval = float(self.cfg.poll_interval_seconds)
        if changed:
            logger.info("config applied live: %s", ", ".join(changed))

    # -- contract surface --

    def state_snapshot(self) -> dict[str, Any]:
        self._roll_day()
        now = self._clock()
        cams = {}
        per_camera = []
        focus: dict[str, float] = {}
        for cam, st in self._state.items():
            s = st.last_scores
            focus_pct = (None if s is None or st.reference_sharpness <= 0
                         else round(min(100.0, 100.0 * s.sharpness / st.reference_sharpness), 1))
            match_pct = None if s is None or s.corr is None else round(100.0 * max(0.0, s.corr), 1)
            if focus_pct is not None:
                focus[cam] = focus_pct
            per_camera.append({"camera": cam, "condition": st.condition or "normal",
                               "for_s": int(now - st.since) if st.since else 0, "focus_pct": focus_pct,
                               "match_pct": match_pct, "alerts_today": self._today.get(cam, 0),
                               "checks": st.checks, "has_reference": st.reference is not None})
            cams[cam] = {
                "condition": st.condition,
                "alerted": st.alerted,
                "checks": st.checks,
                "has_reference": st.reference is not None,
                "sharpness": None if s is None else round(s.sharpness, 1),
                "reference_sharpness": round(st.reference_sharpness, 1),
                "dark_fraction": None if s is None else round(s.dark, 3),
                "correlation": None if s is None or s.corr is None else round(s.corr, 3),
                "edge_correlation": None if s is None or s.edge_corr is None else round(s.edge_corr, 3),
                "frame_change": None if s is None or s.change is None else round(s.change, 3),
                "same_in_a_row": st.same_in_a_row,
            }
        return {
            "cameras": cams,
            "alerts_fired": self._app_alerts_fired,
            "watched": len(cams),
            "tampered_now": sum(1 for st in self._state.values() if st.condition),
            "focus_pct": focus,
            "today": {"alerts": sum(self._today.values()), "since": self._today_key},
            "per_camera": per_camera,
            "by_condition": [{"condition": c, "today": n}
                             for c, n in sorted(self._by_condition.items(), key=lambda kv: -kv[1])],
            "recent": list(self._recent),
            "gallery": list(self._gallery),
        }


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


# ── wiring ────────────────────────────────────────────────────────────


class _StandaloneSource:
    """Frames for the cameras listed in config.yml (standalone runs)."""

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
    """Core for picked cameras; the config's own list for standalone ones."""

    def __init__(self, standalone: _StandaloneSource, core: CoreSnapshotSource) -> None:
        self._standalone = standalone
        self._core = core

    def get_frame(self, camera_id: str) -> bytes | None:
        if camera_id in self._standalone.camera_ids:
            return self._standalone.get_frame(camera_id)
        return self._core.get_frame(camera_id)


def build_app(config: AppConfig, dispatcher) -> CameraTamper:
    standalone = _StandaloneSource(config.cameras)
    source = _EitherSource(standalone, CoreSnapshotSource())
    return CameraTamper(config, dispatcher, frame_source=source,
                        cameras=standalone.camera_ids,
                        poll_interval_seconds=config.poll_interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenNVR camera-tamper app")
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
