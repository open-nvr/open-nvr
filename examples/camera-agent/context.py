# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Shared per-camera state for the camera-agent.

Two things live here:

* **Frame cache** — short-TTL cache of the most recent JPEG fetched
  from each camera. Inside one LLM turn, the model often calls
  several tools on the same camera (describe + detect + recognise);
  the cache means we hit the camera once, not three times.

* **Event ring** — bounded in-memory deque of recent inference
  events received from NATS. The ``recent_events`` tool answers
  "did anyone come to the door in the last 30 minutes?" out of this
  buffer. Without NATS configured the ring is just always empty;
  the tool degrades gracefully.

Both are async-safe so multiple tool invocations in flight on one
LLM turn don't trip each other up.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from frame_sources import FrameSource, FrameSourceError

logger = logging.getLogger(__name__)


@dataclass
class CameraSpec:
    """Operator-supplied camera metadata. Lifted out of config.yml
    by ``camera_agent.load_config``."""
    camera_id: str
    frame_url: str
    role: str  # natural-language role description for the LLM
    # The camera's id in the MAIN OpenNVR server (Camera.id), when this
    # agent camera is also recorded there. Links the per-camera screen's
    # Recorded row to the server's playback API — unset = no recordings
    # surface for this camera (the agent itself stores nothing).
    opennvr_camera_id: int | None = None


@dataclass
class _CachedFrame:
    bytes_: bytes
    fetched_at: float


@dataclass
class EventRecord:
    """One inference event off the NATS bus, trimmed to the bits the
    LLM cares about. Full envelopes are too big for prompt context."""
    received_at: float
    camera_id: str
    adapter: str
    summary: str  # short human-readable summary the tool emits to the LLM
    raw: dict[str, Any] = field(default_factory=dict)
    seq: int = 0  # insertion order, stamped by CameraContext.record_event


@dataclass
class AlertRecord:
    """One app-emitted alert off the NATS bus (§11.5 envelope), trimmed
    to the bits the LLM cares about — the read side of the app door's
    alert relay. Mirrors EventRecord, including the monotonic ``seq``
    tiebreak for deterministic newest-first ordering under equal
    ``received_at``."""
    received_at: float
    app_id: str          # the source app's id (subject/source.name)
    camera_id: str
    title: str
    severity: str        # low / medium / high / critical
    summary: str         # short human-readable summary the tool emits to the LLM
    raw: dict[str, Any] = field(default_factory=dict)
    seq: int = 0         # insertion order, stamped by record_app_alert


class CameraContext:
    """One instance shared across all tool calls. Owns the cache, the
    ring buffer, and the frame sources."""

    def __init__(
        self,
        *,
        cameras: list[CameraSpec],
        frame_cache_ttl_seconds: float = 2.0,
        event_ring_size: int = 256,
    ) -> None:
        self._cameras: dict[str, CameraSpec] = {
            cam.camera_id: cam for cam in cameras
        }
        self._frame_sources: dict[str, FrameSource] = {}
        self._frame_cache: dict[str, _CachedFrame] = {}
        self._cache_ttl = float(frame_cache_ttl_seconds)
        # Frames pinned by the operator ("ask about THIS frame") — served
        # by get_frame verbatim (conversation path only; autonomous pollers
        # pass allow_pinned=False), keyed camera -> (owner token, jpeg) so
        # one request's cleanup can never wipe another request's pin.
        self._pinned: dict[str, tuple[int, bytes]] = {}
        self._pin_seq = 0
        # Short review ring per camera: (wall-clock ts, jpeg) appended on
        # every REAL fetch (thumbnail polls, tool calls, monitor polls all
        # feed it for free). Powers the per-camera screen's scrub-back
        # timeline — the honest "last few minutes", NOT NVR storage
        # (recordings live in the main OpenNVR UI). Bounded BOTH by frame
        # count and by bytes: pollers feed this 24/7, and 90 uncapped 4K
        # JPEGs per camera would eat hundreds of MB on the modest boxes
        # the lite profile targets.
        self._review: dict[str, deque[tuple[float, bytes]]] = {}
        self._review_bytes: dict[str, int] = {}
        self._review_size = 90
        self._review_byte_budget = 24 * 1024 * 1024   # per camera
        self._review_frame_cap = 2_000_000            # same cap as _frames_for
        # Per-camera fetch locks: a fetch on cam1 (which can take seconds —
        # RTSP keyframe wait, up to the source timeout) must not block a
        # concurrent tool call or monitor poll on cam2. One global lock here
        # would serialize ALL frame access and starve interactive "what do
        # you see?" calls behind hosted-monitor polling. The dict is bounded
        # by the configured camera set (get_frame rejects unknown ids first).
        self._cache_locks: dict[str, asyncio.Lock] = {}

        # One ring per camera + a global ring for events that don't
        # carry a camera_id (rare). bounded so a chatty bus doesn't
        # grow memory unboundedly.
        self._event_ring_size = max(1, int(event_ring_size))
        self._events: dict[str, deque[EventRecord]] = {}
        # Monotonic insertion counter — a stable tiebreak for newest-first
        # ordering when events share a received_at (same-tick bursts, or a
        # clock with coarse resolution). Without it, equal timestamps sort
        # nondeterministically and the LLM sees a scrambled recent-events list.
        self._event_seq = 0

        # App alert ring — the read side of the app door's ALERT RELAY. One
        # ring per source app id, bounded like the event ring so a chatty app
        # can't grow memory unboundedly. Same seq tiebreak so equal-timestamp
        # bursts stay deterministic newest-first.
        self._alert_ring_size = self._event_ring_size
        self._app_alerts: dict[str, deque[AlertRecord]] = {}
        self._app_alert_seq = 0
        # Cap on DISTINCT app rings. app_id comes off the bus (the alert's
        # own source.name / subject segment), i.e. publisher-controlled — a
        # buggy or malicious publisher minting a fresh app_id per message
        # would otherwise create a new ring per alert, unbounded. When full,
        # the ring with the OLDEST newest-alert is evicted (stalest app).
        self._max_app_rings = 64

    # ── Cameras ────────────────────────────────────────────────────

    @property
    def cameras(self) -> list[CameraSpec]:
        return list(self._cameras.values())

    def add_camera(self, spec: CameraSpec) -> None:
        """Register a camera at runtime (e.g. a local device discovered via
        the demo's 'use this machine's camera' button)."""
        self._cameras[spec.camera_id] = spec

    def known_camera(self, camera_id: str) -> bool:
        return camera_id in self._cameras

    def remove_camera(self, camera_id: str) -> bool:
        """Forget a camera: its spec, frame source, and cached/pinned frames.

        Its event and app-alert rings are deliberately KEPT (they're bounded):
        a deleted camera's past is still history — "did anyone come by the old
        gate cam yesterday?" should keep working from memory even though the
        camera is gone. Returns False when the id wasn't known."""
        if camera_id not in self._cameras:
            return False
        self._cameras.pop(camera_id, None)
        self._frame_sources.pop(camera_id, None)
        self._frame_cache.pop(camera_id, None)
        self._pinned.pop(camera_id, None)
        self._review.pop(camera_id, None)
        self._review_bytes.pop(camera_id, None)
        self._cache_locks.pop(camera_id, None)
        return True

    def get_camera(self, camera_id: str) -> CameraSpec | None:
        return self._cameras.get(camera_id)

    def register_frame_source(self, camera_id: str, source: FrameSource) -> None:
        """Called by camera_agent.py during startup so tests can
        inject fakes without standing up real HTTP."""
        self._frame_sources[camera_id] = source

    # ── Frame fetch + cache ────────────────────────────────────────

    async def get_frame(self, camera_id: str, *, allow_pinned: bool = True) -> bytes:
        """Return the most recent JPEG for ``camera_id``, fetching if
        the cache is empty or stale. Raises ``LookupError`` if the
        camera isn't configured; ``FrameSourceError`` if the fetch
        itself fails.

        ``allow_pinned=False`` is for AUTONOMOUS callers (alarm/monitor/
        hosted-detector polls, the live-thumbnail endpoint): they must
        always see the real current frame — an operator pinning a
        historical frame for a question must never ring an alarm or
        freeze a monitor on it."""
        if camera_id not in self._cameras:
            raise LookupError(
                f"camera_id {camera_id!r} is not configured; "
                f"available: {sorted(self._cameras.keys())}"
            )
        # An operator-pinned frame wins over any fetch: the user asked
        # about the exact frame they clicked, not the current moment.
        if allow_pinned:
            pinned = self._pinned.get(camera_id)
            if pinned is not None:
                return pinned[1]
        source = self._frame_sources.get(camera_id)
        if source is None:
            raise LookupError(
                f"no frame source registered for camera {camera_id!r}"
            )

        # Cache check under the camera's OWN lock so two concurrent tool
        # calls on the same camera don't race-fetch twice — while a slow
        # fetch on another camera proceeds in parallel (monitor polls must
        # not starve interactive calls).
        lock = self._cache_locks.setdefault(camera_id, asyncio.Lock())
        async with lock:
            cached = self._frame_cache.get(camera_id)
            now = time.monotonic()
            if cached is not None and (now - cached.fetched_at) < self._cache_ttl:
                return cached.bytes_

            # The fetch (RTSP keyframe wait: seconds) runs under this
            # camera's lock — deliberate, so concurrent callers coalesce on
            # one fetch instead of hammering the camera. Other cameras
            # proceed in parallel on their own locks.
            try:
                frame = await asyncio.to_thread(source.fetch)
            except FrameSourceError:
                raise
            self._frame_cache[camera_id] = _CachedFrame(
                bytes_=frame, fetched_at=now
            )
            # Feed the review ring on every real fetch (cache hits and
            # pins deliberately don't — one ring entry per real moment).
            # Oversized frames are skipped (same 2 MB cap as _frames_for),
            # and the ring is trimmed to a per-camera byte budget as well
            # as a frame count, so high-resolution cameras can't balloon
            # a 24/7-fed ring into hundreds of MB.
            if len(frame) <= self._review_frame_cap:
                ring = self._review.setdefault(camera_id, deque())
                ring.append((time.time(), frame))
                total = self._review_bytes.get(camera_id, 0) + len(frame)
                while ring and (len(ring) > self._review_size
                                or total > self._review_byte_budget):
                    _, old = ring.popleft()
                    total -= len(old)
                self._review_bytes[camera_id] = total
            return frame

    def get_cached_frame(self, camera_id: str) -> bytes | None:
        """Return the JPEG bytes most recently fetched for ``camera_id`` this
        session, or None if nothing is cached. Best-effort, no fetch — used to
        show the operator the exact frame a tool looked at, in the chat."""
        pinned = self._pinned.get(camera_id)
        if pinned is not None:
            return pinned[1]
        cached = self._frame_cache.get(camera_id)
        return cached.bytes_ if cached is not None else None

    # ── Pinned frames ("ask about THIS frame") ─────────────────────

    def pin_frame(self, camera_id: str, jpeg: bytes) -> int:
        """Pin a specific JPEG as ``camera_id``'s frame: the conversation
        path's ``get_frame`` returns it verbatim (no fetch, no TTL) until
        the owning request clears it. Returns an owner token — pass it to
        :meth:`clear_pins` so cleanup removes ONLY this pin (a concurrent
        request's ``finally`` must never wipe someone else's pin).

        Powers the demo's click-a-thumbnail flow — the user asks about
        the exact frame they clicked, not whatever the camera shows by
        the time the tools run. Autonomous pollers are immune
        (``allow_pinned=False``). Residual single-operator edge, accepted:
        a VOICE turn overlapping a pinned typed turn reads the pin for
        those seconds (both turns belong to the same operator)."""
        if camera_id not in self._cameras:
            raise LookupError(f"camera_id {camera_id!r} is not configured")
        self._pin_seq += 1
        token = self._pin_seq
        self._pinned[camera_id] = (token, jpeg)
        # Deliberately NOT seeded into _frame_cache: autonomous callers
        # (allow_pinned=False) read the cache, and pinned bytes there
        # would poison an alarm/monitor poll. The turn's "what I saw"
        # reads get_cached_frame (pin-aware) BEFORE the pin is cleared.
        return token

    def clear_pins(self, token: int | None = None) -> None:
        """Remove pins. With a token, only the pin(s) that request
        created; with no token, everything (tests / shutdown)."""
        if token is None:
            self._pinned.clear()
            return
        for cam in [c for c, (t, _) in self._pinned.items() if t == token]:
            self._pinned.pop(cam, None)

    # ── Review ring (per-camera scrub-back) ────────────────────────

    def review_timestamps(self, camera_id: str) -> list[float]:
        """Wall-clock timestamps of the ring frames, oldest→newest."""
        return [ts for ts, _ in self._review.get(camera_id, ())]

    def review_frame_at(self, camera_id: str, at: float) -> bytes | None:
        """The ring frame nearest ``at`` (wall-clock), or None if the
        ring is empty. Nearest-match: the scrub sends the timestamp it
        got from review_timestamps, but never trust float round-trips."""
        ring = self._review.get(camera_id)
        if not ring:
            return None
        return min(ring, key=lambda e: abs(e[0] - at))[1]

    def invalidate_frame_cache(self, camera_id: str | None = None) -> None:
        """Drop cached frames. Without an arg, drops all — useful for
        tests."""
        if camera_id is None:
            self._frame_cache.clear()
        else:
            self._frame_cache.pop(camera_id, None)

    # ── Event ring ─────────────────────────────────────────────────

    def record_event(self, event: EventRecord) -> None:
        ring = self._events.setdefault(
            event.camera_id, deque(maxlen=self._event_ring_size)
        )
        event.seq = self._event_seq
        self._event_seq += 1
        ring.append(event)

    def recent_events(
        self,
        *,
        camera_id: str | None,
        window_seconds: float,
    ) -> list[EventRecord]:
        """Return events from the last ``window_seconds``. Filter by
        camera if given; otherwise across all cameras."""
        cutoff = time.time() - max(0.0, float(window_seconds))
        rings: list[deque[EventRecord]]
        if camera_id is None:
            rings = list(self._events.values())
        else:
            ring = self._events.get(camera_id)
            rings = [ring] if ring else []
        out: list[EventRecord] = []
        for ring in rings:
            for ev in ring:
                if ev.received_at >= cutoff:
                    out.append(ev)
        # Newest-first so the LLM sees the most relevant context before the
        # prompt-token budget runs out. The seq tiebreak keeps ordering
        # deterministic when events share a received_at.
        out.sort(key=lambda e: (e.received_at, e.seq), reverse=True)
        return out

    def latest_inference(
        self, camera_id: str, *, adapter: str = "tier0"
    ) -> EventRecord | None:
        """The newest inference event for a camera from a given adapter, or None.

        Used by ``camera_snapshot`` to answer count/presence questions from the
        always-on Tier-0 stream with **no new inference** — the detection already
        ran; we just read its latest result off the ring."""
        ring = self._events.get(camera_id)
        if not ring:
            return None
        for ev in reversed(ring):            # newest-first
            if ev.adapter == adapter:
                return ev
        return None

    # ── App alert ring (app door — read/relay) ─────────────────────

    def record_app_alert(self, alert: AlertRecord) -> None:
        if (
            alert.app_id not in self._app_alerts
            and len(self._app_alerts) >= self._max_app_rings
        ):
            # At capacity and a NEW app id arrived: evict the app whose most
            # recent alert is oldest. Bounds memory against a publisher
            # minting fresh app_ids per message; a legitimately busy app is
            # never evicted (its newest alert is recent).
            stalest = min(
                self._app_alerts,
                key=lambda k: self._app_alerts[k][-1].received_at
                if self._app_alerts[k] else 0.0,
            )
            del self._app_alerts[stalest]
        ring = self._app_alerts.setdefault(
            alert.app_id, deque(maxlen=self._alert_ring_size)
        )
        alert.seq = self._app_alert_seq
        self._app_alert_seq += 1
        ring.append(alert)

    def recent_app_alerts(
        self,
        *,
        app_id: str | None = None,
        window_seconds: float,
    ) -> list[AlertRecord]:
        """Return app alerts from the last ``window_seconds``. Filter by
        source app if given; otherwise across all apps. Newest-first and
        deterministic (the seq tiebreak orders equal-timestamp bursts) —
        the same pattern as ``recent_events``."""
        cutoff = time.time() - max(0.0, float(window_seconds))
        rings: list[deque[AlertRecord]]
        if app_id is None:
            rings = list(self._app_alerts.values())
        else:
            ring = self._app_alerts.get(app_id)
            rings = [ring] if ring else []
        out: list[AlertRecord] = []
        for ring in rings:
            for al in ring:
                if al.received_at >= cutoff:
                    out.append(al)
        out.sort(key=lambda a: (a.received_at, a.seq), reverse=True)
        return out


# ── NATS subscriber ────────────────────────────────────────────────


async def run_event_subscriber(
    *,
    context: CameraContext,
    nats_url: str,
    nats_token: str | None,
    stop_event: asyncio.Event,
) -> None:
    """Subscribe to ``opennvr.inference.>`` and feed parsed events
    into the camera context's ring buffer.

    Runs forever until ``stop_event`` fires. Connection errors are
    logged + back off; we don't crash the agent if NATS is offline.
    """
    try:
        import nats  # type: ignore
    except ImportError:
        logger.warning("nats-py not installed; recent_events tool will be empty")
        await stop_event.wait()
        return

    while not stop_event.is_set():
        try:
            options: dict[str, Any] = {"servers": [nats_url]}
            if nats_token:
                options["token"] = nats_token
            nc = await nats.connect(**options)
        except Exception as exc:
            logger.warning(
                "nats subscriber: connect failed (%s); retrying in 5s", exc
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            return

        try:
            async def _handler(msg) -> None:  # noqa: ANN001 — nats msg type
                try:
                    payload = json.loads(msg.data.decode("utf-8"))
                except Exception:
                    logger.debug("nats subscriber: dropping non-JSON payload")
                    return
                record = _parse_inference_event(msg.subject, payload)
                if record is not None:
                    context.record_event(record)

            sub = await nc.subscribe("opennvr.inference.>", cb=_handler)
            logger.info("nats subscriber: connected to %s", nats_url)
            # Park until stop — but wake periodically to check the connection
            # still exists. nats-py gives up auto-reconnecting after its
            # default budget (60 attempts x 2s ≈ 2 min) and CLOSES the
            # connection without telling this coroutine; without the
            # liveness check a longer NATS outage would leave the subscriber
            # deaf for the rest of the process while recent_events keeps
            # answering "no events". On close, fall through to the outer
            # loop and redial.
            while not stop_event.is_set() and not nc.is_closed:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    continue
            if nc.is_closed:
                logger.warning(
                    "nats subscriber: connection closed; reconnecting"
                )
            else:
                await sub.unsubscribe()
        except Exception:
            logger.exception("nats subscriber: error in run loop; reconnecting")
        finally:
            if not nc.is_closed:
                try:
                    await nc.drain()
                except Exception:
                    logger.exception("nats subscriber: drain failed")


def _parse_inference_event(
    subject: str, payload: dict[str, Any]
) -> EventRecord | None:
    """Coerce an opennvr.inference.* envelope into a compact
    EventRecord. Returns None if the message isn't shaped like an
    inference event."""
    if not isinstance(payload, dict):
        return None
    camera_id = payload.get("camera_id") or payload.get("source", {}).get("camera_id")
    if not camera_id:
        return None
    adapter = payload.get("adapter_name") or _adapter_from_subject(subject)
    summary = _summarise_event(payload)
    return EventRecord(
        received_at=time.time(),
        camera_id=str(camera_id),
        adapter=adapter,
        summary=summary,
        raw=payload,
    )


def _adapter_from_subject(subject: str) -> str:
    # ``opennvr.inference.<adapter>.<camera_id>.completed`` → adapter
    parts = subject.split(".")
    if len(parts) >= 3 and parts[0] == "opennvr" and parts[1] == "inference":
        return parts[2]
    return "unknown"


def _summarise_event(payload: dict[str, Any]) -> str:
    """One-line human-readable summary the LLM can read directly.
    Falls back to a generic phrase when the payload shape isn't
    one we recognise."""
    result = payload.get("result") or {}
    task = result.get("task") or payload.get("task") or ""
    if task == "face_recognition":
        if result.get("recognized"):
            who = result.get("name") or result.get("person_id") or "someone"
            sim = result.get("similarity")
            extra = f" (similarity {sim:.2f})" if isinstance(sim, (int, float)) else ""
            return f"face recognised: {who}{extra}"
        return "face detected but not recognised"
    if task == "face_detection":
        n = result.get("face_count", 0)
        return f"{n} face(s) detected"
    if task == "object_detection":
        dets = result.get("detections") or []
        if not dets:
            return "no objects detected"
        labels = sorted({(d.get("label") or "?") for d in dets[:8]})
        return f"objects detected: {', '.join(labels)}"
    if task == "scene_caption":
        return f"scene: {result.get('caption', '')}"[:140]
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return f"event from adapter ({task or 'unknown task'})"


# ── App alert relay (subscriber + parser) ──────────────────────────


async def run_app_alert_subscriber(
    *,
    context: CameraContext,
    nats_url: str,
    nats_token: str | None,
    stop_event: asyncio.Event,
    on_alert=None,  # optional callback(AlertRecord) — the notification bridge
) -> None:
    """Subscribe to ``opennvr.alerts.app.>`` and feed parsed app alerts
    into the camera context's alert ring — the READ side of the app
    door's alert relay. This is a near-copy of ``run_event_subscriber``:
    the agent only CONSUMES and REPORTS these alerts (query tool +
    optional notification bridge); it never publishes back or acts on the
    app.

    On each parsed alert we call ``context.record_app_alert`` AND the
    optional ``on_alert`` callback (used by camera_agent to push the alert
    into the existing notification feed so it surfaces proactively).

    Runs forever until ``stop_event`` fires. Connection errors log + back
    off; we never crash the agent if NATS is offline. nats-py missing →
    just wait on stop_event (the recent_app_alerts tool stays empty).
    """
    try:
        import nats  # type: ignore
    except ImportError:
        logger.warning(
            "nats-py not installed; recent_app_alerts tool will be empty"
        )
        await stop_event.wait()
        return

    while not stop_event.is_set():
        try:
            options: dict[str, Any] = {"servers": [nats_url]}
            if nats_token:
                options["token"] = nats_token
            nc = await nats.connect(**options)
        except Exception as exc:
            logger.warning(
                "app-alert subscriber: connect failed (%s); retrying in 5s", exc
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            return

        try:
            async def _handler(msg) -> None:  # noqa: ANN001 — nats msg type
                try:
                    payload = json.loads(msg.data.decode("utf-8"))
                except Exception:
                    logger.debug(
                        "app-alert subscriber: dropping non-JSON payload"
                    )
                    return
                record = _parse_app_alert(msg.subject, payload)
                if record is None:
                    return
                context.record_app_alert(record)
                if on_alert is not None:
                    try:
                        on_alert(record)
                    except Exception:  # pragma: no cover - defensive
                        logger.exception(
                            "app-alert subscriber: on_alert callback raised"
                        )

            sub = await nc.subscribe("opennvr.alerts.app.>", cb=_handler)
            logger.info("app-alert subscriber: connected to %s", nats_url)
            # Liveness-checked park (same rationale as run_event_subscriber):
            # nats-py closes the connection after its reconnect budget
            # (~2 min) expires; detect that and redial via the outer loop
            # instead of staying deaf while recent_app_alerts reports
            # all-clear.
            while not stop_event.is_set() and not nc.is_closed:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    continue
            if nc.is_closed:
                logger.warning(
                    "app-alert subscriber: connection closed; reconnecting"
                )
            else:
                await sub.unsubscribe()
        except Exception:
            logger.exception(
                "app-alert subscriber: error in run loop; reconnecting"
            )
        finally:
            if not nc.is_closed:
                try:
                    await nc.drain()
                except Exception:
                    logger.exception("app-alert subscriber: drain failed")


def _parse_app_alert(
    subject: str, payload: dict[str, Any]
) -> AlertRecord | None:
    """Coerce an ``opennvr.alerts.app.<id>.<cam>`` §11.5 envelope into a
    compact AlertRecord. Returns None if the message isn't shaped like an
    app alert (no title → not an alert; guards against inference/other
    traffic that shares the bus).

    App id: from the ``source.name`` block, falling back to the subject's
    app-id segment. Camera: from ``camera_id`` in the payload, falling
    back to the subject's trailing segment(s)."""
    if not isinstance(payload, dict):
        return None
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        # §11.5 alerts always carry a title; without one this isn't an
        # app alert we can relay.
        return None
    source = payload.get("source")
    source = source if isinstance(source, dict) else {}
    app_id = (
        (source.get("name") if isinstance(source.get("name"), str) else None)
        or _app_id_from_subject(subject)
        or "unknown"
    )
    camera_id = (
        payload.get("camera_id")
        or _camera_from_alert_subject(subject)
        or "unknown"
    )
    # severity + title come straight off the bus, i.e. from whatever app
    # published the alert — bound them before they reach the notification
    # feed, webhooks, TTS, or the LLM tool output. severity is collapsed to
    # its first whitespace-delimited token (kills embedded newlines that
    # could smuggle text into the model context) and capped; title is
    # capped like summary is (a megabyte title must not ride into the
    # webhook fan-out verbatim).
    severity_raw = str(payload.get("severity") or "high").strip().lower()
    severity = (severity_raw.split() or ["high"])[0][:16]
    summary = _summarise_app_alert(payload)
    return AlertRecord(
        received_at=time.time(),
        app_id=str(app_id)[:64],
        camera_id=str(camera_id)[:64],
        title=" ".join(title.split())[:160],
        severity=severity,
        summary=summary,
        raw=payload,
    )


def _app_id_from_subject(subject: str) -> str | None:
    # ``opennvr.alerts.app.<app-id>.<camera>`` → app-id
    parts = subject.split(".")
    if len(parts) >= 4 and parts[:3] == ["opennvr", "alerts", "app"]:
        return parts[3]
    return None


def _camera_from_alert_subject(subject: str) -> str | None:
    # ``opennvr.alerts.app.<app-id>.<camera…>`` → camera (any trailing
    # segments joined, so a future extra token doesn't lose the camera).
    parts = subject.split(".")
    if len(parts) >= 5 and parts[:3] == ["opennvr", "alerts", "app"]:
        return ".".join(parts[4:])
    return None


def _summarise_app_alert(payload: dict[str, Any]) -> str:
    """One-line human-readable summary of an app alert the LLM can read
    directly. Prefers the envelope's description, falling back to the
    title, then a generic phrase."""
    title = str(payload.get("title") or "").strip()
    desc = payload.get("description")
    if isinstance(desc, str) and desc.strip():
        base = f"{title}: {desc.strip()}" if title else desc.strip()
    else:
        base = title or "app alert"
    return base[:200]
