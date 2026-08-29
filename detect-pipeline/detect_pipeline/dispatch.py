# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tier-1 dispatch (#10) — run the expensive model on gate escalations.

Turns the gate's decisions into *action*: when the gate escalates a track (in
`enforce`), route it to the declared expensive adapter and run it **once, on the
track's best-frame crop, through KAI-C** (governed, sovereignty-checked, audited).
KAI-C publishes the result to the existing `opennvr.inference.<adapter>.<cam>.completed`
subject, so apps/agents consume it unchanged — **no new contract**.

Model-agnostic by construction: routing is a **declarative class→adapter map** (the
caption default is one editable row), and a model may opt out (`TriggerPolicy.none`),
in (`always`), or bring its own trigger — see `docs/design/trigger-policies.md`.

Off by default: a dispatcher is only built when a KAI-C URL is configured, and it
fires only on **enforce** escalations (`shadow`/`off` dispatch nothing). Best-effort:
KAI-C down or slow never blocks or crashes the Tier-0 worker.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from .metrics import metrics

log = logging.getLogger("detect_pipeline.dispatch")

# Default routing: caption (light, non-biometric) on people + common vehicles.
# face/plate are heavier and privacy/jurisdiction-sensitive → opt-in, not shipped on.
DEFAULT_ROUTES: dict[str, list[str]] = {
    "person": ["caption"],
    "bicycle": ["caption"], "car": ["caption"], "motorcycle": ["caption"],
    "bus": ["caption"], "truck": ["caption"],
}

# Per-adapter §3.5 task strings. One dispatcher serves many adapters, and
# the infer body's ``task`` must match what each adapter actually serves —
# the shared ``KaicDispatcher.task`` ("caption") stays the fallback for
# unlisted adapters.
ADAPTER_TASKS: dict[str, str] = {
    "fast_plate_ocr": "license_plate_recognition",
}

# RFC-0002 Phase 4 (decision 9): the plate chain is a declarative route,
# activated PER CAMERA by the assignment table — a camera assigned the
# license_plate_recognition skill routes its escalated vehicles through
# the OCR adapter. Not a default: plates are privacy-sensitive, so the
# route exists only where an operator (or an app's claim) assigned it.
PLATE_SKILL = "license_plate_recognition"
PLATE_ROUTE_LABELS: tuple[str, ...] = ("bicycle", "bus", "car",
                                       "motorcycle", "truck")
PLATE_ADAPTER = "fast_plate_ocr"

# Adapters dispatched at most ONCE per (camera, track): a vehicle visit
# has one plate — re-OCRing it on every escalate_cooldown tick is pure
# cost. caption stays cooldown-paced (a scene evolves; a plate doesn't).
ONCE_PER_TRACK_ADAPTERS = frozenset({PLATE_ADAPTER})


def router_for_skills(skills, base: "DispatchRouter | None" = None) -> "DispatchRouter | None":
    """The per-camera router: ``base`` plus the routes this camera's
    assigned skills activate. Returns ``base`` unchanged (identity —
    including None) when the skills add nothing, so unassigned cameras
    share the one base router."""
    if not skills or PLATE_SKILL not in skills:
        return base
    src = base.routes if base is not None else None
    router = DispatchRouter(routes=src,
                            default=list(base.default) if base else None)
    for label in PLATE_ROUTE_LABELS:
        row = router.routes.setdefault(label, [])
        if PLATE_ADAPTER not in row:
            row.append(PLATE_ADAPTER)
    return router


class OncePerTrack:
    """Bounded memory of (track_id, adapter) pairs already dispatched.

    One instance per worker (per camera), so track ids can't collide
    across cameras. Bounded FIFO: at ``maxlen`` the oldest pair ages
    out — a recycled track id after thousands of visits re-OCRs once,
    which is the cheap failure direction."""

    def __init__(self, maxlen: int = 1024) -> None:
        from collections import OrderedDict
        self._seen: "OrderedDict[tuple, None]" = OrderedDict()
        self._maxlen = max(1, int(maxlen))

    def seen(self, track_id, adapter: str) -> bool:
        """Has this pair been marked? Read-only — see :meth:`mark`."""
        key = (track_id, adapter)
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        return False

    def mark(self, track_id, adapter: str) -> None:
        """Record a pair. Callers mark AFTER the dispatcher accepted the
        call, never before: a backpressure drop must not consume the
        track's one OCR — the next escalation retries it."""
        self._seen[(track_id, adapter)] = None
        if len(self._seen) > self._maxlen:
            self._seen.popitem(last=False)


class DispatchRouter:
    """Maps an escalated track's class → the expensive adapter(s) to run.

    A declarative map, not a `switch`: add a row for a new/custom model keyed on its
    class; `default` applies to unlisted classes (empty = run nothing for them).
    """

    def __init__(self, routes: dict[str, list[str]] | None = None,
                 default: list[str] | None = None) -> None:
        # deep-copy the list values so a caller mutating router.routes[k] can't
        # bleed into DEFAULT_ROUTES (the module global) or another router.
        src = DEFAULT_ROUTES if routes is None else routes
        self.routes = {k: list(v) for k, v in src.items()}
        self.default = list(default or [])

    def route(self, label: str) -> list[str]:
        return list(self.routes.get(label, self.default))


def build_infer_body(task: str, jpeg_bytes: bytes, params: dict | None = None) -> dict:
    """KAI-C contract-v1 image request body: ``{task, frame_b64, **params}``."""
    body = dict(params or {})
    body["task"] = task
    body["frame_b64"] = base64.b64encode(bytes(jpeg_bytes)).decode("ascii")
    return body


def _encode_jpeg(crop_bgr, quality: int = 85) -> bytes:
    import cv2
    ok, buf = cv2.imencode(".jpg", crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise ValueError("jpeg encode failed")
    return buf.tobytes()


class Dispatcher(Protocol):
    def dispatch(self, camera_id: str, adapter: str, crop_bgr, track) -> "bool | None":
        ...


def _http_post_json(url: str, body: dict, api_key: str | None, timeout: float) -> None:  # pragma: no cover
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    if api_key:
        req.add_header("X-Internal-Api-Key", api_key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        resp.read()


class KaicDispatcher:
    """Runs the expensive adapter via KAI-C's governed ``/api/v1/infer/{adapter}``.

    Async + best-effort + concurrency-capped: submits to a small thread pool and
    **drops** (logs) once ``max_inflight`` are already in flight, so a slow/down
    KAI-C never backs up or blocks the worker. ``http_post`` is injectable for tests.
    """

    def __init__(self, base_url: str, *, api_key: str | None = None, task: str = "caption",
                 max_inflight: int = 4, timeout: float = 10.0, jpeg_quality: int = 85,
                 http_post=None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.task = task
        self.timeout = timeout
        self.jpeg_quality = jpeg_quality
        self._post = http_post or _http_post_json
        self.max_inflight = max(1, int(max_inflight))
        # Global admission, plus a per-camera share of it. Permits used to be
        # a pure race: dispatch_escalations fires once per escalated track in
        # a tight loop, so ONE camera with several escalating tracks took
        # every permit in a single frame and locked out the whole fleet until
        # they returned. A camera may now hold at most `per_camera`, so a busy
        # scene degrades itself rather than everyone else.
        self._sem = threading.Semaphore(self.max_inflight)
        # Reserve one permit rather than capping each camera at half. A fixed
        # half starved the installs with no contention at all (a two-camera
        # NVR could never use more than 2 of its 4), while leaving the whole
        # pool open lets one camera lock everyone out — which is the bug.
        # Holding back a single slot does both jobs: a busy camera runs nearly
        # flat out, and a newcomer always finds a permit waiting.
        self._per_camera = max(1, self.max_inflight - 1)
        self._cam_inflight: dict[str, int] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=self.max_inflight, thread_name_prefix="tier1-dispatch"
        )
        self._ilock = threading.Lock()
        self._inflight = 0

    def _bump_inflight(self, delta: int) -> None:
        with self._ilock:
            self._inflight += delta
            metrics.gauge("tier1_dispatch_inflight", float(self._inflight))

    def _claim_camera_slot(self, camera_id: str) -> bool:
        with self._ilock:
            if self._cam_inflight.get(camera_id, 0) >= self._per_camera:
                return False
            self._cam_inflight[camera_id] = self._cam_inflight.get(camera_id, 0) + 1
            return True

    def _release_camera_slot(self, camera_id: str) -> None:
        with self._ilock:
            left = self._cam_inflight.get(camera_id, 0) - 1
            if left > 0:
                self._cam_inflight[camera_id] = left
            else:
                self._cam_inflight.pop(camera_id, None)

    def dispatch(self, camera_id: str, adapter: str, crop_bgr, track) -> bool:
        """True = submitted; False = dropped (backpressure / camera share /
        pool shutdown). The once-per-track filter marks only on True."""
        if not self._claim_camera_slot(camera_id):
            metrics.inc("tier1_dispatch_dropped_total",
                        {"camera": camera_id, "adapter": adapter})
            log.debug("tier1 dispatch: %s already at its share; dropping", camera_id)
            return False
        if not self._sem.acquire(blocking=False):
            self._release_camera_slot(camera_id)
            metrics.inc("tier1_dispatch_dropped_total", {"camera": camera_id, "adapter": adapter})
            log.debug("tier1 dispatch backpressure; dropping %s/%s", camera_id, adapter)
            return False
        try:
            self._pool.submit(self._run, camera_id, adapter, crop_bgr, track)
        except Exception:                          # pool shutting down, etc.
            self._sem.release()
            self._release_camera_slot(camera_id)
            return False
        return True

    def _run(self, camera_id, adapter, crop_bgr, track) -> None:
        self._bump_inflight(1)
        metrics.inc("tier1_dispatch_total", {"camera": camera_id, "adapter": adapter})
        t0 = time.monotonic()
        try:
            body = build_infer_body(
                ADAPTER_TASKS.get(adapter, self.task),
                _encode_jpeg(crop_bgr, self.jpeg_quality),
                {"camera_id": camera_id, "track_id": track.id, "label": track.label},
            )
            self._post(f"{self.base_url}/api/v1/infer/{adapter}", body, self.api_key, self.timeout)
            metrics.observe("tier1_dispatch_latency_seconds", time.monotonic() - t0, {"adapter": adapter})
        except Exception:
            metrics.inc("tier1_dispatch_errors_total", {"camera": camera_id, "adapter": adapter})
            log.debug("tier1 dispatch failed %s/%s", camera_id, adapter, exc_info=True)
        finally:
            self._bump_inflight(-1)
            self._sem.release()
            self._release_camera_slot(camera_id)

    def close(self) -> None:  # pragma: no cover
        self._pool.shutdown(wait=False)


def dispatch_escalations(camera_id: str, tracks, gate_result, router: DispatchRouter,
                         dispatcher: Dispatcher | None,
                         once: OncePerTrack | None = None) -> int:
    """Dispatch the expensive model for each **enforced** escalation.

    Returns the number of (adapter) dispatches issued. In `shadow`/`off`,
    ``gate_result.to_dispatch()`` is empty → nothing runs. A track with no retained
    ``best_crop`` (e.g. the tracker was never fed pixels) is skipped.

    ``once`` (per-camera) suppresses re-dispatch of ONCE_PER_TRACK_ADAPTERS
    for a track already served — one OCR per vehicle visit, not one per
    escalate-cooldown tick.
    """
    if dispatcher is None:
        return 0
    by_id = {t.id: t for t in tracks}
    issued = 0
    for d in gate_result.to_dispatch():           # escalations, enforce-only
        t = by_id.get(d.track_id)
        crop = getattr(t, "best_crop", None) if t is not None else None
        if crop is None:
            continue
        for adapter in router.route(d.label):
            gated = once is not None and adapter in ONCE_PER_TRACK_ADAPTERS
            if gated and once.seen(t.id, adapter):
                continue
            accepted = dispatcher.dispatch(camera_id, adapter, crop, t)
            # Mark only when the dispatcher took the call. False = an
            # explicit drop (backpressure / camera share) — the track's
            # one OCR must survive for the next escalation. None (older
            # dispatchers, test fakes) counts as accepted.
            if gated and accepted is not False:
                once.mark(t.id, adapter)
            issued += 1
    return issued
