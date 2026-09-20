# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""What a camera sees, in words (HA-501: Assist's ``describe_camera``).

One frame from the capture pool (the same one ``/snapshot`` serves) goes to
a KAI-C adapter that advertises a caption or visual-question task, through
KAI-C's governed route, so ``AI_SOVEREIGNTY`` and adapter approval apply
exactly as for every other inference. With a ``question`` the adapter is
asked to answer it (``visual_qa``); without, to caption the scene.

No such adapter: ``available: false`` and no description; the caller still
knows the camera is there. A VLM on a CPU is slow, so one description runs
at a time, at most three wait (each up to 30 s, else ``Busy``), and each
person gets a few a minute.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

CAPTION_TASKS = ("scene_caption", "image_captioning")
QA_TASKS = ("visual_qa",)
#: Descriptions per caller per minute.
PER_MINUTE = 6
TIMEOUT_S = 45.0
#: At most this many callers wait for the model, each at most LOCK_WAIT_S.
MAX_WAITING = 3
LOCK_WAIT_S = 30.0
_ADAPTERS_TTL_S = 60.0

_lock = asyncio.Lock()
_calls: dict[str, deque[float]] = {}
_adapters: tuple[float, list[dict]] | None = None


_waiting = 0


class RateLimited(Exception):
    pass


class Busy(Exception):
    """The model is taken and the queue is full, or the wait ran out."""


def _allow(caller: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    for key in [k for k, v in _calls.items() if not v or now - v[-1] > 60]:
        del _calls[key]  # nobody left in the window: no entry either
    q = _calls.setdefault(caller, deque())
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= PER_MINUTE:
        return False
    q.append(now)
    return True


def _headers() -> dict[str, str]:
    from core.config import settings

    return {"Content-Type": "application/json", "Accept": "application/json",
            "X-Internal-Api-Key": settings.internal_api_key or ""}


async def _registry() -> list[dict]:
    """KAI-C's approved, healthy adapters (cached a minute)."""
    global _adapters
    import httpx

    from core.config import settings

    now = time.monotonic()
    if _adapters is not None and now - _adapters[0] < _ADAPTERS_TTL_S:
        return _adapters[1]
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            resp = await client.get(f"{settings.kai_c_url}/api/v1/adapters", headers=_headers())
        rows = resp.json().get("adapters") if resp.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001 - no KAI-C: nothing describes
        logger.debug("scene description: KAI-C registry unavailable: %s", exc)
        rows = []
    usable = [a for a in rows or [] if isinstance(a, dict)
              and a.get("approval_status", "approved") == "approved"
              and a.get("health_status", "ok") == "ok"]
    _adapters = (now, usable)
    return usable


def pick(adapters: list[dict], question: str | None) -> tuple[str, str] | None:
    """``(adapter name, task)``: a visual-QA adapter for a question when
    there is one, else a captioner."""
    def having(tasks):
        for a in adapters:
            advertised = set(a.get("tasks_advertised") or [])
            for task in tasks:
                if task in advertised:
                    return a.get("name"), task
        return None

    if question:
        found = having(QA_TASKS)
        if found:
            return found
    return having(CAPTION_TASKS + QA_TASKS)


def text_of(body: Any) -> str | None:
    """The adapter's words: ``answer`` (VQA) or ``caption``."""
    if not isinstance(body, dict):
        return None
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    for key in ("answer", "caption", "text", "description"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:1000]
    return None


async def describe(camera, caller: str, question: str | None = None) -> dict[str, Any]:
    """``{available, description, model, task}``. Raises RateLimited, Busy,
    or LookupError when no frame can be captured."""
    global _waiting
    import httpx

    from core.config import settings
    from services.adapter_contract import build_infer_payload
    from services.kai_c_service import get_kai_c_service

    if not _allow(caller):
        raise RateLimited()
    if _waiting >= MAX_WAITING:  # refuse before grabbing a frame for nothing
        raise Busy()
    choice = pick(await _registry(), question)
    if choice is None:
        return {"available": False, "description": None, "model": None, "task": None}
    name, task = choice
    jpeg = await get_kai_c_service().capture_frame_bytes(camera.rtsp_url or "", camera.id)
    if not jpeg:
        raise LookupError("Could not capture a frame (camera offline?)")
    params: dict[str, Any] = {"camera_id": str(camera.id)}
    if question:
        params["question"] = params["prompt"] = question
    payload = build_infer_payload(task=task, jpeg_bytes=jpeg, params=params)
    if _waiting >= MAX_WAITING:
        raise Busy()
    _waiting += 1
    try:
        await asyncio.wait_for(_lock.acquire(), LOCK_WAIT_S)
    except TimeoutError as exc:
        raise Busy() from exc
    finally:
        _waiting -= 1
    try:  # one VLM call at a time
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, trust_env=False) as client:
                resp = await client.post(f"{settings.kai_c_url}/api/v1/infer/{name}",
                                         json=payload, headers=_headers())
        except httpx.HTTPError as exc:
            logger.info("scene description: %s unreachable: %s", name, exc)
            return {"available": False, "description": None, "model": name, "task": task}
    finally:
        _lock.release()
    if resp.status_code != 200:
        # 403: refused by KAI-C (sovereignty, approval); 404: gone.
        logger.info("scene description: %s answered %s", name, resp.status_code)
        return {"available": False, "description": None, "model": name, "task": task}
    try:
        text = text_of(resp.json())
    except ValueError:
        text = None
    return {"available": text is not None, "description": text, "model": name, "task": task}
