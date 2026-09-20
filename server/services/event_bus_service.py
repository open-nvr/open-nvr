# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
In-memory async pub/sub for inference events.

Motivation
----------
OpenNVR already runs the AI pipelines (person detection, faces, Whisper,
scene captions, etc.) against camera frames. External agent frameworks like
pipecat (voice agents) and GetStream vision-agents want to *react* to those
detections in real time — and if we make them run their own models on the
same stream, we double-process every frame for no reason.

The event bus is the seam that prevents that double work: the inference
manager publishes each adapter result exactly once, and any number of
subscribers (WebSocket clients, internal services, integration shims) fan
out from there.

Design notes
------------
* **In-memory only.** Single-process fan-out via per-subscriber
  ``asyncio.Queue``. Good enough for a single-node deployment; swap in
  Redis/NATS later if OpenNVR ever runs multi-instance.
* **Back-pressure by drop-oldest.** Each subscriber has a bounded queue;
  if a slow consumer can't keep up we drop the oldest events (logged), so
  one stalled client never stalls the publisher or the rest of the fleet.
* **No persistence.** This is a *live* bus — historical events live in the
  ``AIDetectionResult`` table. Agents that need history query the DB and
  then subscribe for new events.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from core.logging_config import main_logger

# Event types emitted on the bus. Kept as string constants (not an Enum) so
# JSON clients can match on plain strings without extra serialization rules.
EVENT_INFERENCE_RESULT = "inference_result"
EVENT_INFERENCE_ERROR = "inference_error"
EVENT_CAMERA_EVENT = "camera_event"
EVENT_CAMERA_STATUS = "camera_status"
EVENT_SYSTEM_ALERT = "system_alert"
# An app alert reaching the operator inbox. Distinct from system_alert,
# which is host health (disk, CPU); this one is what a camera saw.
EVENT_APP_ALERT = "app_alert"
# Live Tier-0 tracks for the detection overlay. Its own type, not
# inference_result: that type is keyed on model_id and drives the
# AI Detection Results table, and 5 fps of tracker output per camera
# would flood it. Consumers that want boxes opt in by name.
EVENT_TRACKS = "tracks"
# What a camera sees now (services/live_state.py): counts per label and
# zone, motion, and the tracks that just started or ended. Sent on change.
EVENT_LIVE_STATE = "live_state"
# An event's media can be fetched now (services/media_ready.py): which
# images it has and the clip range, once that clip is playable.
EVENT_MEDIA_READY = "media_ready"
# The site's arming mode changed (services/site_mode.py). Site-wide: it
# names no camera and reaches every subscriber entitled to its type.
EVENT_SITE_MODE = "site_mode"
# Server-described entities (services/entity_descriptors.py, HA-114): a
# resolved state or a fired event entity, and "re-fetch GET /entities".
# Both v2-only: v1 sockets never see them.
EVENT_ENTITY_STATE = "entity_state"
EVENT_DESCRIPTORS_CHANGED = "descriptors_changed"

# Reasonable default for a single slow WebSocket client. Bumping this trades
# memory for tolerance of bursty traffic.
_DEFAULT_SUBSCRIBER_QUEUE_SIZE = 100

#: How long published events stay replayable for a v2 client that
#: reconnects with ``since`` (HA-111), and a hard cap on how many.
RING_SECONDS = 300.0
RING_MAX_EVENTS = 20_000
#: Not kept for replay: live overlay boxes (up to several frames a second
#: per camera). Stale boxes are worthless to a client catching up, and
#: keeping them would crowd everything else out of the ring and hold tens
#: of MB on a large site. Live subscribers still get them.
RING_SKIP_TYPES = frozenset({EVENT_TRACKS})


class _Subscriber:
    """One subscription slot. Owns the queue and the optional filters."""

    __slots__ = ("queue", "camera_id", "tasks", "allowed_camera_ids",
                 "allowed_event_types", "event_types", "with_seq", "start_seq",
                 "dropped", "created_at")

    def __init__(
        self,
        queue_size: int,
        camera_id: int | None,
        tasks: frozenset[str] | None,
        allowed_camera_ids: frozenset[int] | None = None,
        allowed_event_types: frozenset[str] | None = None,
        event_types: frozenset[str] | None = None,
        with_seq: bool = False,
    ):
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self.camera_id = camera_id
        self.tasks = tasks
        #: Cameras this subscriber is ENTITLED to, or ``None`` for
        #: unrestricted (a superuser, or an internal caller that has
        #: already done its own authorization).
        #:
        #: This is an authorization boundary, not a filter, and it lives
        #: here rather than in the route on purpose: ``camera_id`` below
        #: is a client-supplied preference and omitting it used to mean
        #: "every camera". Enforcing entitlement in the same place the
        #: event is matched means no present or future caller can widen
        #: its own scope by leaving a query parameter off.
        self.allowed_camera_ids = allowed_camera_ids
        #: Event types this subscriber is ENTITLED to, or ``None`` for all.
        #: Also a boundary, not a filter: an API token only receives the
        #: types its scopes cover (services.api_tokens.TOKEN_EVENT_SCOPES).
        self.allowed_event_types = allowed_event_types
        #: The client's own event-type preference (v2 ``types=``). A
        #: filter, unlike allowed_event_types, which is a boundary.
        self.event_types = event_types
        #: v2 subscribers get ``(seq, event)`` pairs; v1 the bare event, so
        #: v1 frames stay exactly what they were.
        self.with_seq = with_seq
        #: The bus sequence number when this subscriber was added: every
        #: event after it reaches the queue, everything up to it is history
        #: (replay or snapshot).
        self.start_seq = 0
        self.dropped: int = 0
        self.created_at = time.time()

    def matches(self, event: dict[str, Any]) -> bool:
        if event.get("v2_only") is True and not self.with_seq:
            return False
        # Entitlement first: a subscriber never sees a camera it was not
        # granted, whatever it asked to filter on.
        site_wide = event.get("site_wide") is True
        if self.allowed_camera_ids is not None and not site_wide:
            cam = event.get("camera_id")
            if cam is None or cam not in self.allowed_camera_ids:
                return False
        if (self.allowed_event_types is not None
                and event.get("event_type") not in self.allowed_event_types):
            return False
        if (self.camera_id is not None and not site_wide
                and event.get("camera_id") != self.camera_id):
            return False
        if self.tasks is not None and event.get("task") not in self.tasks:
            return False
        if self.event_types is not None and event.get("event_type") not in self.event_types:
            return False
        return True


class EventBus:
    """Single-process broadcast bus for inference events."""

    def __init__(self, subscriber_queue_size: int = _DEFAULT_SUBSCRIBER_QUEUE_SIZE):
        self._subscribers: set[_Subscriber] = set()
        self._lock = asyncio.Lock()
        self._subscriber_queue_size = subscriber_queue_size
        #: Per-process id. Sequence numbers restart with the process, so a
        #: client resuming with ``since`` must also send the epoch it got
        #: them in; a different epoch means "resync from a snapshot".
        self.epoch = uuid.uuid4().hex[:12]
        self._seq = 0
        #: (seq, monotonic time, event), oldest first.
        self._ring: deque[tuple[int, float, dict[str, Any]]] = deque()

    @property
    def current_seq(self) -> int:
        return self._seq

    def _prune(self, now: float) -> None:
        while self._ring and (len(self._ring) > RING_MAX_EVENTS
                              or now - self._ring[0][1] > RING_SECONDS):
            self._ring.popleft()

    async def replay(
        self, sub: _Subscriber, since: int
    ) -> tuple[list[tuple[int, dict[str, Any]]], bool]:
        """Events after ``since`` up to the subscriber's start, filtered by
        its entitlements and filters. ``complete`` is False when events in
        that range have already left the ring (or ``since`` is from the
        future): the client must resync from a snapshot."""
        async with self._lock:
            self._prune(time.monotonic())
            upto = sub.start_seq
            if since > upto:
                return [], False
            if since == upto:
                return [], True
            oldest = self._ring[0][0] if self._ring else upto + 1
            if since + 1 < oldest:
                return [], False
            return [(s, e) for s, _t, e in self._ring
                    if since < s <= upto and sub.matches(e)], True

    async def publish(self, event: dict[str, Any]) -> None:
        """
        Broadcast ``event`` to all matching subscribers. Safe to call with
        no subscribers attached (no-op).

        If a subscriber's queue is full we drop the oldest event for THAT
        subscriber only — other subscribers still receive the new event.
        """
        # Timestamp here (not at each publisher site) so every consumer sees a
        # consistent monotonic-ish ordering even if producers forget.
        event.setdefault("timestamp", int(time.time() * 1000))

        # Numbered and kept for replay even with no subscriber attached:
        # the moment a client is disconnected is exactly when it will need
        # to catch up. Snapshot the targets under the lock; deliver outside
        # it so one slow subscriber can't block the snapshot path.
        async with self._lock:
            self._seq += 1
            seq = self._seq
            now = time.monotonic()
            if event.get("event_type") not in RING_SKIP_TYPES:
                self._ring.append((seq, now, event))
            self._prune(now)
            targets = [s for s in self._subscribers if s.matches(event)]

        for sub in targets:
            item = (seq, event) if sub.with_seq else event
            try:
                sub.queue.put_nowait(item)
            except asyncio.QueueFull:
                # Drop-oldest: pop one, enqueue new. Counter tracks how many
                # events a given subscriber has missed so the WS layer can
                # surface it to the client (e.g. as a "lagged" notice).
                try:
                    sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    sub.queue.put_nowait(item)
                    sub.dropped += 1
                except asyncio.QueueFull:
                    # Should not happen — we just drained a slot — but be
                    # defensive so publish never raises.
                    sub.dropped += 1

    @asynccontextmanager
    async def subscribe(
        self,
        camera_id: int | None = None,
        tasks: list[str] | None = None,
        allowed_camera_ids: set[int] | frozenset[int] | None = None,
        allowed_event_types: set[str] | frozenset[str] | None = None,
        event_types: set[str] | frozenset[str] | list[str] | None = None,
        with_seq: bool = False,
    ) -> AsyncIterator[_Subscriber]:
        """
        Context-managed subscription. Use as::

            async with event_bus.subscribe(camera_id=3) as sub:
                while True:
                    event = await sub.queue.get()
                    ...

        The subscription is removed automatically on exit, including on
        exceptions — we specifically avoid requiring callers to call an
        unsubscribe method because they will forget.
        """
        sub = _Subscriber(
            queue_size=self._subscriber_queue_size,
            camera_id=camera_id,
            tasks=frozenset(tasks) if tasks else None,
            allowed_camera_ids=(
                None if allowed_camera_ids is None
                else frozenset(allowed_camera_ids)),
            allowed_event_types=(
                None if allowed_event_types is None
                else frozenset(allowed_event_types)),
            event_types=frozenset(event_types) if event_types else None,
            with_seq=with_seq,
        )
        async with self._lock:
            sub.start_seq = self._seq
            self._subscribers.add(sub)

        try:
            yield sub
        finally:
            async with self._lock:
                self._subscribers.discard(sub)
            if sub.dropped:
                main_logger.warning(
                    "EventBus subscriber (camera_id=%s tasks=%s) dropped %d events "
                    "during its lifetime — consumer was too slow",
                    sub.camera_id, sub.tasks, sub.dropped,
                )

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def v2_subscriber_count(self) -> int:
        """Sockets on protocol v2 (the only ones that receive entity_state)."""
        return sum(1 for s in self._subscribers if s.with_seq)


# Singleton accessor — matches the pattern used by other services
# (inference_manager, kai_c_service, etc.).
_event_bus_instance: EventBus | None = None


def get_event_bus() -> EventBus:
    global _event_bus_instance
    if _event_bus_instance is None:
        _event_bus_instance = EventBus()
    return _event_bus_instance


async def publish_inference_result(
    *,
    camera_id: int,
    model_id: int,
    task: str,
    payload: dict[str, Any],
) -> None:
    """
    Convenience helper used at the inference-manager publication sites.

    Keeps the publishing code a one-liner and the event shape consistent,
    which matters because many different consumers (pipecat shims,
    vision-agents shims, dashboard UIs) are going to depend on it.
    """
    await get_event_bus().publish({
        "event_type": EVENT_INFERENCE_RESULT,
        "camera_id": camera_id,
        "model_id": model_id,
        "task": task,
        "payload": payload,
    })


async def publish_live_state(
    *,
    camera_id: int,
    state: dict[str, Any],
    started: list[dict[str, Any]] | None = None,
    ended: list[dict[str, Any]] | None = None,
) -> None:
    """Publish a camera's live state after it changed (HA-110). Per-camera
    entitlement applies like every camera event."""
    await get_event_bus().publish({
        "event_type": EVENT_LIVE_STATE,
        "camera_id": camera_id,
        "task": "live_state",
        "payload": {"state": state, "started": started or [], "ended": ended or []},
    })


async def publish_media_ready(*, camera_id: int, payload: dict[str, Any]) -> None:
    """Publish that an event's or alert's media is ready (HA-113)."""
    await get_event_bus().publish({
        "event_type": EVENT_MEDIA_READY,
        "camera_id": camera_id,
        "task": payload.get("source") or "event",
        "payload": payload,
    })


async def publish_site_mode(value: dict[str, Any]) -> None:
    """Publish a site-mode change (HA-118). ``site_wide`` lets it through
    camera entitlement: it carries no camera data, only the mode. Only
    ever set here, on events that are about the site, not a camera."""
    await get_event_bus().publish({
        "event_type": EVENT_SITE_MODE,
        "site_wide": True,
        "task": "site_mode",
        "payload": value,
    })


async def publish_entity_state(
    *, key: str, camera_id: int | None, required_scope: str, payload: dict[str, Any],
) -> None:
    """A descriptor's new state, or an event entity firing (HA-114). The v2
    socket delivers it only to connections holding ``required_scope``."""
    event: dict[str, Any] = {
        "event_type": EVENT_ENTITY_STATE,
        "task": "entity",
        "v2_only": True,
        "required_scope": required_scope,
        "payload": payload,
    }
    if camera_id is None:
        event["site_wide"] = True
    else:
        event["camera_id"] = camera_id
    await get_event_bus().publish(event)


async def publish_descriptors_changed(etag: str) -> None:
    await get_event_bus().publish({
        "event_type": EVENT_DESCRIPTORS_CHANGED, "task": "entity", "v2_only": True,
        "site_wide": True, "payload": {"etag": etag},
    })


async def publish_tracks(
    *,
    camera_id: int,
    payload: dict[str, Any],
    task: str = "tier0",
) -> None:
    """Publish one frame's worth of Tier-0 tracks (normalized boxes, see
    services/tier0_track_consumer.py) for live overlays. Carries a
    camera_id, so it is subject to the per-camera entitlement the bus
    enforces — a viewer never receives boxes for a camera they cannot
    see, exactly as with the video itself."""
    await get_event_bus().publish({
        "event_type": EVENT_TRACKS,
        "camera_id": camera_id,
        # "tier0" for the platform detector, "overlay" for an app's boxes —
        # the WS task filter lets a client take either or both.
        "task": task,
        "payload": payload,
    })


async def publish_camera_status(
    *,
    camera_id: int,
    status: str,
    payload: dict[str, Any],
) -> None:
    """Publish a camera connectivity transition (online/offline) so live UIs
    can react instantly (offline overlays, auto-resume of live streams)."""
    await get_event_bus().publish({
        "event_type": EVENT_CAMERA_STATUS,
        "camera_id": camera_id,
        "task": "connection",
        "payload": {"status": status, **payload},
    })


async def publish_camera_event(
    *,
    camera_id: int,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Publish a camera-native alarm (motion/tamper/etc) to the live bus so the
    dashboard/agents see it in real time, alongside inference results."""
    await get_event_bus().publish({
        "event_type": EVENT_CAMERA_EVENT,
        "camera_id": camera_id,
        "task": event_type,
        "payload": payload,
    })


async def publish_app_alert(
    *,
    camera_id: int | None,
    severity: str,
    alert_type: str | None,
    payload: dict[str, Any],
) -> None:
    """Tell open browsers an app alert just landed, so the desk sees it
    NOW rather than up to a poll later — the bell polls every 10s, and
    "a person walked in unscanned" is not a 10-second-old fact worth
    sitting on.

    ``camera_id`` is the numeric id when the producer's handle resolves
    to one; None for an alert about nothing in particular, which then
    reaches unfiltered dashboard sockets only (see _Subscriber.matches).
    """
    await get_event_bus().publish({
        "event_type": EVENT_APP_ALERT,
        "camera_id": camera_id,
        "task": alert_type or "alert",
        "payload": {"severity": severity, "alert_type": alert_type, **payload},
    })


async def publish_system_alert(
    *,
    alert_type: str,
    state: str | None,
    severity: str,
    payload: dict[str, Any],
) -> None:
    """Publish a host-level alert (disk/CPU/RAM/purge). Carries no camera_id,
    so camera-filtered subscribers correctly never see it while unfiltered
    dashboard sockets do (see _Subscriber.matches)."""
    await get_event_bus().publish({
        "event_type": EVENT_SYSTEM_ALERT,
        "task": alert_type,
        "payload": {"state": state, "severity": severity, **payload},
    })
