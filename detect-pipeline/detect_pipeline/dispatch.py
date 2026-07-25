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
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

log = logging.getLogger("detect_pipeline.dispatch")

# Default routing: caption (light, non-biometric) on people + common vehicles.
# face/plate are heavier and privacy/jurisdiction-sensitive → opt-in, not shipped on.
DEFAULT_ROUTES: dict[str, list[str]] = {
    "person": ["caption"],
    "bicycle": ["caption"], "car": ["caption"], "motorcycle": ["caption"],
    "bus": ["caption"], "truck": ["caption"],
}


class DispatchRouter:
    """Maps an escalated track's class → the expensive adapter(s) to run.

    A declarative map, not a `switch`: add a row for a new/custom model keyed on its
    class; `default` applies to unlisted classes (empty = run nothing for them).
    """

    def __init__(self, routes: dict[str, list[str]] | None = None,
                 default: list[str] | None = None) -> None:
        self.routes = dict(DEFAULT_ROUTES if routes is None else routes)
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
    def dispatch(self, camera_id: str, adapter: str, crop_bgr, track) -> None:
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
        self._sem = threading.Semaphore(max_inflight)
        self._pool = ThreadPoolExecutor(max_workers=max_inflight, thread_name_prefix="tier1-dispatch")

    def dispatch(self, camera_id: str, adapter: str, crop_bgr, track) -> None:
        if not self._sem.acquire(blocking=False):
            log.debug("tier1 dispatch backpressure; dropping %s/%s", camera_id, adapter)
            return
        try:
            self._pool.submit(self._run, camera_id, adapter, crop_bgr, track)
        except Exception:                          # pool shutting down, etc.
            self._sem.release()

    def _run(self, camera_id, adapter, crop_bgr, track) -> None:
        try:
            body = build_infer_body(
                self.task, _encode_jpeg(crop_bgr, self.jpeg_quality),
                {"camera_id": camera_id, "track_id": track.id, "label": track.label},
            )
            self._post(f"{self.base_url}/api/v1/infer/{adapter}", body, self.api_key, self.timeout)
        except Exception:
            log.debug("tier1 dispatch failed %s/%s", camera_id, adapter, exc_info=True)
        finally:
            self._sem.release()

    def close(self) -> None:  # pragma: no cover
        self._pool.shutdown(wait=False)


def dispatch_escalations(camera_id: str, tracks, gate_result, router: DispatchRouter,
                         dispatcher: Dispatcher | None) -> int:
    """Dispatch the expensive model for each **enforced** escalation.

    Returns the number of (adapter) dispatches issued. In `shadow`/`off`,
    ``gate_result.to_dispatch()`` is empty → nothing runs. A track with no retained
    ``best_crop`` (e.g. the tracker was never fed pixels) is skipped.
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
            dispatcher.dispatch(camera_id, adapter, crop, t)
            issued += 1
    return issued
