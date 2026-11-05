"""Flap barriers: the admission policy, the per-gate camera worker, and the
decision's way out.

Attendly judges; it never touches a relay. Every decision becomes an
``access.decided.v1`` event on the platform bus, keyed by the gate
camera's handle, and OpenNVR's gate-controller app (dry contact, IP relay,
Modbus, ONVIF, MQTT) closes the barrier's contact on ``allow`` — the same
way it serves the licence-plate app's vehicle barriers. A gate may also
carry an ``open_url`` for a barrier whose own relay takes an HTTP trigger.

Policy:

* **hostel** gate — a student whose ``residence`` is ``hostler`` and whose
  block matches the gate's (a gate with no block admits every hostler);
  staff when the gate says ``allow_staff``.
* **mess** gate — inside a meal window only, a student holding an unused
  booking for today's meal; the booking is consumed on entry so it admits
  once. Staff when ``allow_staff``.

Anything else is ``deny`` with a reason the log and the bus carry.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import get_settings
from ..people import find_person
from ..storage.gates_repo import (
    MEALS,
    access_event_put,
    gate_list,
    meal_booking_consume,
    meal_booking_get,
)
from ..utils.time import now_local
from .recognize import recognize_faces_in_frame

logger = logging.getLogger(__name__)

ACCESS_SCHEMA = "access.decided.v1"


# ── Meal windows ────────────────────────────────────────────────────


def parse_meal_windows(spec: Optional[str]) -> Dict[str, Tuple[str, str]]:
    """``"breakfast=07:00-09:30,lunch=12:00-14:30"`` → ``{meal: (start, end)}``.
    Malformed entries are skipped, unknown meals ignored."""
    out: Dict[str, Tuple[str, str]] = {}
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        meal, _, window = part.partition("=")
        meal = meal.strip().lower()
        if meal not in MEALS or "-" not in window:
            continue
        start, _, end = window.partition("-")
        start, end = start.strip(), end.strip()
        if len(start) == 5 and len(end) == 5 and start[2] == ":" and end[2] == ":":
            out[meal] = (start, end)
    return out


def current_meal(windows: Dict[str, Tuple[str, str]], now: datetime) -> Optional[str]:
    hhmm = now.strftime("%H:%M")
    for meal in MEALS:
        win = windows.get(meal)
        if win and win[0] <= hhmm < win[1]:
            return meal
    return None


def gate_meal_windows(gate: Dict[str, Any]) -> Dict[str, Tuple[str, str]]:
    own = parse_meal_windows(gate.get("meal_windows"))
    return own or parse_meal_windows(get_settings().meal_windows)


# ── The decision ────────────────────────────────────────────────────


@dataclass
class Decision:
    decision: str  # allow | deny
    reason: str
    person_code: Optional[str] = None
    person_name: Optional[str] = None
    person_kind: Optional[str] = None
    unit: Optional[str] = None
    similarity: Optional[float] = None
    booking_id: Optional[int] = None

    @property
    def allow(self) -> bool:
        return self.decision == "allow"


def decide(gate: Dict[str, Any], code: Optional[str], similarity: Optional[float], *,
           now: Optional[datetime] = None) -> Decision:
    """Pure: nothing is written. ``code`` is what the face model returned
    (None when nobody was recognised)."""
    now = now or now_local()
    kind = str(gate.get("kind") or "")
    person = find_person(code)
    if person is None:
        return Decision("deny", "unknown", person_code=code, similarity=similarity)
    base = dict(person_code=person["code"], person_name=person["name"], person_kind=person["kind"], similarity=similarity)
    if person["kind"] == "staff":
        if gate.get("allow_staff"):
            return Decision("allow", "staff", unit=str(person["record"].get("designation") or "") or None, **base)
        return Decision("deny", "staff_not_allowed", **base)
    student = person["record"]
    if kind == "hostel":
        if str(student.get("residence") or "") != "hostler":
            return Decision("deny", "day_scholar", **base)
        block = str(student.get("hostel_block") or "") or None
        want = str(gate.get("hostel_block") or "") or None
        if want and block != want:
            return Decision("deny", "wrong_block", unit=block, **base)
        return Decision("allow", "hostler", unit=block, **base)
    if kind == "mess":
        meal = current_meal(gate_meal_windows(gate), now)
        if meal is None:
            return Decision("deny", "no_meal_window", **base)
        booking = meal_booking_get(person["code"], now.strftime("%Y-%m-%d"), meal)
        if booking is None:
            return Decision("deny", "not_booked", unit=meal, **base)
        if booking.get("consumed_at"):
            return Decision("deny", "already_used", unit=meal, **base)
        return Decision("allow", "booked", unit=meal, booking_id=int(booking["id"]), **base)
    return Decision("deny", "unknown_gate_kind", **base)


# ── The way out ─────────────────────────────────────────────────────


class AccessPublisher:
    """``access.decided.v1`` on the bus, plus the gate's own ``open_url``
    when it has one. Never raises: a barrier decision loop must not die
    because the broker blinked."""

    def __init__(self, *, nats_url: str = "", nats_token: str = "", http_get: Optional[Callable[[str, float], bool]] = None,
                 timeout: float = 3.0) -> None:
        self._publisher = None
        self.nats_url = nats_url
        if nats_url:
            try:
                from opennvr_app_sdk.domain_events import DomainEventPublisher

                self._publisher = DomainEventPublisher(nats_url, token=nats_token or None, producer="app:attendly")
            except Exception as exc:  # noqa: BLE001
                logger.warning("domain event publisher unavailable: %s", exc)
        self._http_get = http_get or self._default_get
        self._timeout = timeout
        self.published = 0
        self.last_error: Optional[str] = None

    @staticmethod
    def _default_get(url: str, timeout: float) -> bool:
        import httpx

        try:
            r = httpx.get(url, timeout=timeout, trust_env=False)
            return r.status_code < 400
        except Exception as exc:  # noqa: BLE001
            logger.warning("gate open_url %s failed: %s", url, exc)
            return False

    @staticmethod
    def payload(decision: Decision) -> Dict[str, Any]:
        """The contract's fields: the credential rides in ``plate_text``."""
        return {
            "plate_text": decision.person_code or "", "decision": decision.decision, "reason": decision.reason,
            "owner": decision.person_name, "unit": decision.unit,
            "confidence": float(decision.similarity) if decision.similarity is not None else None,
        }

    def publish(self, gate: Dict[str, Any], decision: Decision) -> bool:
        """True when the decision reached at least one actuation path."""
        acted = False
        camera_id = gate.get("camera_id")
        if self._publisher is not None and camera_id is not None:
            try:
                ok = self._publisher.publish(ACCESS_SCHEMA, camera_id=f"cam{int(camera_id)}", payload=self.payload(decision))
            except Exception as exc:  # noqa: BLE001
                ok = False
                self.last_error = str(exc)
            if ok:
                self.published += 1
                acted = True
        url = gate.get("open_url")
        if url and decision.allow:
            if self._http_get(str(url), self._timeout):
                acted = True
        return acted


_publisher: Optional[AccessPublisher] = None
_publisher_lock = threading.Lock()


def get_publisher() -> AccessPublisher:
    global _publisher
    with _publisher_lock:
        if _publisher is None:
            s = get_settings()
            _publisher = AccessPublisher(nats_url=s.nats_url, nats_token=s.effective_nats_token,
                                         timeout=s.open_url_timeout_seconds)
        return _publisher


def set_publisher(p: Optional[AccessPublisher]) -> None:
    global _publisher
    with _publisher_lock:
        _publisher = p


def decide_and_record(
    gate: Dict[str, Any], code: Optional[str], similarity: Optional[float], *, source: str = "camera",
    actuate: bool = True, now: Optional[datetime] = None, publisher: Optional[AccessPublisher] = None,
) -> Dict[str, Any]:
    """Decide, consume a meal booking on allow, write the log row, publish.
    Returns the log row (with the decision's reason)."""
    now = now or now_local()
    d = decide(gate, code, similarity, now=now)
    if d.allow and d.booking_id is not None:
        if not meal_booking_consume(d.booking_id, gate_id=int(gate["id"]), ts=now.isoformat()):
            d = Decision("deny", "already_used", person_code=d.person_code, person_name=d.person_name,
                         person_kind=d.person_kind, unit=d.unit, similarity=d.similarity)
    acted = False
    if actuate:
        pub = publisher or get_publisher()
        acted = pub.publish(gate, d)
    row = access_event_put(
        gate=gate, decision=d.decision, reason=d.reason, person_code=d.person_code, person_name=d.person_name,
        person_kind=d.person_kind, unit=d.unit, similarity=d.similarity, actuated=acted, source=source,
        ts=now.isoformat(),
    )
    logger.info("gate %s (%s): %s %s for %s (%s) sim=%s", gate.get("name"), gate.get("kind"), d.decision, d.reason,
                d.person_code or "?", d.person_name or "unknown", f"{d.similarity:.2f}" if d.similarity else "-")
    return row


# ── The camera worker ───────────────────────────────────────────────


@dataclass
class GateWorker:
    gate: Dict[str, Any]
    recognizer: Any = None
    frames: Any = None
    publisher: Optional[AccessPublisher] = None
    running: bool = False
    thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    polls: int = 0
    decisions: int = 0
    last_event: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None
    # Test hooks
    clock: Callable[[], datetime] = now_local
    poll_seconds: Optional[float] = None
    cooldown_seconds: Optional[float] = None
    _recent: Dict[str, float] = field(default_factory=dict)

    @property
    def gate_id(self) -> int:
        return int(self.gate["id"])

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"attendly-gate-{self.gate_id}")
        self.thread.start()
        logger.info("Gate worker %s started for %s (%s)", self.gate_id, self.gate.get("name"), self.gate.get("kind"))

    def stop(self) -> None:
        if not self.running:
            return
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5.0)
        self.running = False
        logger.info("Gate worker %s stopped", self.gate_id)

    def _fetch_frame(self):
        cam = self.gate.get("camera_id")
        if self.frames is None or cam is None:
            return None
        try:
            return self.frames.snapshot(int(cam))
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return None

    def tick(self) -> Optional[Dict[str, Any]]:
        """One look at the camera: the largest face decides. Returns the
        log row written, or None when nothing new happened."""
        self.polls += 1
        frame = self._fetch_frame()
        if frame is None:
            return None
        settings = get_settings()
        faces = recognize_faces_in_frame(self.recognizer, frame, threshold=settings.effective_gate_threshold,
                                         tag=f"gate{self.gate_id}", largest_only=True)
        if not faces:
            return None
        face = faces[0]
        code = face.get("code")
        key = code or "__unknown__"
        cooldown = self.cooldown_seconds if self.cooldown_seconds is not None else settings.gate_cooldown_seconds
        now_s = time.monotonic()
        last = self._recent.get(key)
        if last is not None and now_s - last < cooldown:
            return None
        self._recent[key] = now_s
        # forget stale entries
        for k in [k for k, t in self._recent.items() if now_s - t > max(cooldown, 1.0) * 10]:
            self._recent.pop(k, None)
        row = decide_and_record(self.gate, code, face.get("similarity"), source="camera", now=self.clock(),
                                publisher=self.publisher)
        self.decisions += 1
        self.last_event = row
        return row

    def _run(self) -> None:
        settings = get_settings()
        poll = self.poll_seconds if self.poll_seconds is not None else settings.gate_poll_seconds
        try:
            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001
                    self.last_error = str(exc)
                    logger.exception("Gate worker %s tick error", self.gate_id)
                elapsed = time.monotonic() - started
                self.stop_event.wait(max(0.05, poll - elapsed))
        finally:
            self.running = False

    def status(self) -> Dict[str, Any]:
        return {
            "gate_id": self.gate_id, "name": self.gate.get("name"), "kind": self.gate.get("kind"),
            "camera_id": self.gate.get("camera_id"), "running": self.running, "polls": self.polls,
            "decisions": self.decisions, "last_event": self.last_event, "last_error": self.last_error,
        }


class GateEngine:
    """One worker per enabled gate; re-read from the database periodically
    so the Gates page's changes take effect without a restart."""

    _instance: Optional["GateEngine"] = None

    def __init__(self, *, recognizer: Any = None, frames: Any = None, publisher: Optional[AccessPublisher] = None,
                 refresh_seconds: float = 15.0) -> None:
        self._lock = threading.RLock()
        self._workers: Dict[int, GateWorker] = {}
        self._recognizer = recognizer
        self._frames = frames
        self._publisher = publisher
        self._refresh = refresh_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def get_shared(cls) -> "GateEngine":
        if cls._instance is None:
            cls._instance = GateEngine()
        return cls._instance

    @classmethod
    def set_shared(cls, engine: Optional["GateEngine"]) -> None:
        cls._instance = engine

    def _deps(self):
        if self._recognizer is None:
            try:
                from ..face.engine import FaceEngine

                self._recognizer = FaceEngine.get_shared()
            except Exception as exc:  # noqa: BLE001
                logger.warning("face engine unavailable for gates: %s", exc)
        if self._frames is None:
            try:
                from ..cameras import CameraSource

                self._frames = CameraSource.get_shared()
            except Exception as exc:  # noqa: BLE001
                logger.warning("camera source unavailable for gates: %s", exc)
        if self._publisher is None:
            self._publisher = get_publisher()
        return self._recognizer, self._frames, self._publisher

    def reload(self) -> None:
        """Start workers for enabled gates, stop the rest, restart any whose
        camera changed."""
        wanted = {int(g["id"]): g for g in gate_list(enabled_only=True) if g.get("camera_id") is not None}
        recognizer, frames, publisher = self._deps()
        with self._lock:
            for gid in [g for g in self._workers if g not in wanted]:
                w = self._workers.pop(gid)
                w.stop()
            for gid, gate in wanted.items():
                w = self._workers.get(gid)
                if w is not None and w.running and w.gate.get("camera_id") == gate.get("camera_id"):
                    w.gate = gate  # policy fields (block, windows, open_url) refresh in place
                    continue
                if w is not None:
                    w.stop()
                w = GateWorker(gate=gate, recognizer=recognizer, frames=frames, publisher=publisher)
                self._workers[gid] = w
                w.start()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
        try:
            self.reload()
        except Exception:
            logger.exception("initial gate load failed; the refresh loop will retry")
        with self._lock:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="attendly-gates")
            self._thread.start()
        logger.info("GateEngine started")

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._refresh)
            if self._stop.is_set():
                break
            try:
                self.reload()
            except Exception:
                logger.exception("gate refresh error")

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            try:
                w.stop()
            except Exception:
                pass
        logger.info("GateEngine stopped")

    def status(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [w.status() for w in self._workers.values()]
