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
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from core.logging_config import main_logger

# Event types emitted on the bus. Kept as string constants (not an Enum) so
# JSON clients can match on plain strings without extra serialization rules.
EVENT_INFERENCE_RESULT = "inference_result"
EVENT_INFERENCE_ERROR = "inference_error"

# Reasonable default for a single slow WebSocket client. Bumping this trades
# memory for tolerance of bursty traffic.
_DEFAULT_SUBSCRIBER_QUEUE_SIZE = 100


class _Subscriber:
    """One subscription slot. Owns the queue and the optional filters."""

    __slots__ = ("queue", "camera_id", "tasks", "dropped", "created_at")

    def __init__(
        self,
        queue_size: int,
        camera_id: int | None,
        tasks: frozenset[str] | None,
    ):
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self.camera_id = camera_id
        self.tasks = tasks
        self.dropped: int = 0
        self.created_at = time.time()

    def matches(self, event: dict[str, Any]) -> bool:
        if self.camera_id is not None and event.get("camera_id") != self.camera_id:
            return False
        if self.tasks is not None and event.get("task") not in self.tasks:
            return False
        return True


class EventBus:
    """Single-process broadcast bus for inference events."""

    def __init__(self, subscriber_queue_size: int = _DEFAULT_SUBSCRIBER_QUEUE_SIZE):
        self._subscribers: set[_Subscriber] = set()
        self._lock = asyncio.Lock()
        self._subscriber_queue_size = subscriber_queue_size

    async def publish(self, event: dict[str, Any]) -> None:
        """
        Broadcast ``event`` to all matching subscribers. Safe to call with
        no subscribers attached (no-op).

        If a subscriber's queue is full we drop the oldest event for THAT
        subscriber only — other subscribers still receive the new event.
        """
        if not self._subscribers:
            return

        # Timestamp here (not at each publisher site) so every consumer sees a
        # consistent monotonic-ish ordering even if producers forget.
        event.setdefault("timestamp", int(time.time() * 1000))

        # Snapshot under lock; deliver outside the lock so one slow subscriber
        # can't block the snapshot path.
        async with self._lock:
            targets = [s for s in self._subscribers if s.matches(event)]

        for sub in targets:
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop-oldest: pop one, enqueue new. Counter tracks how many
                # events a given subscriber has missed so the WS layer can
                # surface it to the client (e.g. as a "lagged" notice).
                try:
                    sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    sub.queue.put_nowait(event)
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
        )
        async with self._lock:
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
