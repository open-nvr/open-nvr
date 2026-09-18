# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Resolve entity states on the server and push what changed (HA-114).

One loop for the whole site, not one per client:

* every ``TICK_S``: build the descriptor catalogue, resolve every state,
  publish ``entity_state`` for each key whose state or attributes changed,
  and ``descriptors_changed`` when the catalogue itself changed;
* every ``APP_POLL_S``: fetch ``/state`` from each enabled app that declares
  entities (the same fetch ``GET /apps/{id}/status`` does);
* every ``STATS_S``: refresh per-camera stats (diagnostic sensors);
* PTZ presets per ``ptz_presets_cache.REFRESH_S``.

``entity_state`` events are ``v2_only`` (v1 sockets never see them) and
carry the descriptor's ``required_scope``: the v2 socket delivers one only
when the connection holds that scope. Camera entities carry ``camera_id``
for the bus's camera entitlement; site and app-wide ones are ``site_wide``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

TICK_S = 2.0
APP_POLL_S = 5.0
STATS_S = 30.0
#: Resolve only while someone uses the result: a v2 socket is open, or a
#: REST client asked for states within this many seconds. An install that
#: never connects Home Assistant pays nothing.
REST_IDLE_S = 60.0
_rest_used_at: float | None = None

#: key -> {"state", "attributes"} as last published.
_states: dict[str, dict[str, Any]] = {}
#: key -> (required_scope, camera_id) of the current catalogue.
_meta: dict[str, tuple[str, int | None]] = {}
_etag: str | None = None
_stats: dict[int, dict] = {}
#: The bus seq when a cold cache was last primed. States that changed before
#: it, during the idle spell, were never published: a resume from before it
#: cannot be lossless (routers/events.py answers it with a resync).
_primed_seq: int | None = None


def note_rest_use() -> None:
    global _rest_used_at
    _rest_used_at = time.monotonic()


def wanted() -> bool:
    from services.event_bus_service import get_event_bus

    if get_event_bus().v2_subscriber_count > 0:
        return True
    return _rest_used_at is not None and time.monotonic() - _rest_used_at < REST_IDLE_S


def current_states() -> dict[str, dict[str, Any]]:
    return dict(_states)


def current_meta() -> dict[str, tuple[str, int | None]]:
    return dict(_meta)


def current_etag() -> str | None:
    return _etag


def visible_states(held: set[str] | None, allowed: set[int] | None) -> dict[str, dict]:
    """The last resolved states a connection may see."""
    out = {}
    for key, value in _states.items():
        scope, cam = _meta.get(key, (None, None))
        if held is not None and scope not in held:
            continue
        if allowed is not None and cam is not None and cam not in allowed:
            continue
        out[key] = value
    return out


def _resolve_once() -> tuple[list, dict[str, dict], str]:
    from core.database import SessionLocal
    from services import entity_descriptors as ed

    with SessionLocal() as db:
        descs = ed.all_descriptors(db)
        states = ed.resolve_states(db, descs, _stats)
    return descs, states, ed.etag_of(descs)


def _forget() -> None:
    global _etag
    _states.clear()
    _meta.clear()
    _etag = None


def is_warm() -> bool:
    """False after an idle spell: states changed then without being published."""
    return _etag is not None


def primed_seq() -> int | None:
    return _primed_seq


async def warm() -> None:
    """Fill a cold cache (silently, see ``tick``) so a snapshot built next is
    complete. The v2 socket calls it before its ``state_snapshot``."""
    if _etag is None:
        await tick()


async def tick() -> None:
    """One resolution pass; publishes the differences."""
    async with _tick_lock():
        await _tick()


async def _tick() -> None:
    global _etag, _primed_seq
    from services.event_bus_service import (
        get_event_bus,
        publish_descriptors_changed,
        publish_entity_state,
    )

    descs, states, etag = await asyncio.to_thread(_resolve_once)
    # A cold pass (the first after idling) primes the cache and publishes
    # nothing: every client that could see these states got them in its
    # snapshot (built from a warm cache, see ``warm``). Publishing all of
    # them at once would overflow a subscriber's queue and cost it a
    # ``lagged`` gap in exactly the updates that follow.
    cold = _etag is None
    if cold:
        _primed_seq = get_event_bus().current_seq
    meta = {d.key: (d.required_scope, d.camera_id) for d in descs}
    if not cold and etag != _etag:
        await publish_descriptors_changed(etag)
    _etag = etag
    _meta.clear()
    _meta.update(meta)
    for key, value in states.items():
        if _states.get(key) != value:
            _states[key] = value
            if cold:
                continue
            scope, cam = meta[key]
            await publish_entity_state(key=key, camera_id=cam, required_scope=scope,
                                       payload={"key": key, **value})
    for gone in [k for k in _states if k not in meta]:
        _states.pop(gone, None)


_lock: asyncio.Lock | None = None


def _tick_lock() -> asyncio.Lock:
    """One pass at a time: the loop's tick and a socket's ``warm``."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def _poll_apps() -> None:
    import httpx

    from core.database import SessionLocal
    from models import InstalledApp
    from routers.apps import validate_app_url
    from services import entity_descriptors as ed
    from services.app_tls import app_verify

    def targets():
        with SessionLocal() as db:
            return [(a.id, a.url) for a in db.query(InstalledApp)
                    .filter(InstalledApp.enabled.is_(True)).all()
                    if (a.manifest_json or {}).get("entities")]

    for app_id, url in await asyncio.to_thread(targets):
        if not url or validate_app_url(url) is not None:
            ed.set_app_state(app_id, None)
            continue
        try:
            async with httpx.AsyncClient(timeout=3.0, verify=app_verify()) as client:
                r = await client.get(f"{url.rstrip('/')}/state")
            ed.set_app_state(app_id, r.json() if r.status_code == 200 else None)
        except Exception:  # noqa: BLE001 - an unreachable app reads as unknown
            ed.set_app_state(app_id, None)


async def _refresh_stats() -> None:
    """One short session per camera: never hold a pooled connection across
    the network calls get_camera_stats makes."""
    from core.database import SessionLocal
    from models import Camera
    from services.camera_stats import get_camera_stats

    def ids():
        with SessionLocal() as db:
            return [c for (c,) in db.query(Camera.id).filter(
                Camera.deleted_at.is_(None), Camera.is_active.is_(True)).all()]

    for cid in await asyncio.to_thread(ids):
        try:
            with SessionLocal() as db:
                cam = db.query(Camera).filter(Camera.id == cid).first()
                if cam is not None:
                    _stats[cid] = await get_camera_stats(db, cam)
        except Exception:  # noqa: BLE001
            continue


async def _refresh_ptz() -> None:
    from core.database import SessionLocal
    from models import Camera
    from services import entity_descriptors as ed, ptz_presets_cache

    with SessionLocal() as db:
        cams = [c for c in db.query(Camera).filter(Camera.deleted_at.is_(None),
                                                     Camera.is_active.is_(True)).all()
                if ed._is_ptz(c) and ptz_presets_cache.due(c.id)]
        for cam in cams:
            db.expunge(cam)
    for cam in cams:
        await ptz_presets_cache.refresh(cam)


async def _slow_refresh() -> None:
    await _refresh_stats()
    await _refresh_ptz()


async def run_forever() -> None:
    """Idle until someone uses entities; then resolve every TICK_S. Stats
    and PTZ presets (network calls to cameras) run beside the tick, never
    in it, so an offline camera can't stall state updates."""
    from core.background_tasks import spawn_background

    last_app = last_stats = 0.0
    slow: asyncio.Task | None = None
    while True:
        now = time.monotonic()
        try:
            if not wanted():
                # Idle: forget the cache rather than diff against stale
                # values later. The next pass primes it silently; sockets
                # opening meanwhile warm it for their snapshot, and a resume
                # across the idle spell is answered with a resync snapshot
                # (routers/events.py). REST resolves directly.
                _forget()
                await asyncio.sleep(TICK_S)
                continue
            if now - last_app >= APP_POLL_S:
                last_app = now
                await _poll_apps()
            if now - last_stats >= STATS_S and (slow is None or slow.done()):
                last_stats = now
                slow = spawn_background(_slow_refresh(), name="entity-stats-refresh")
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad pass must not stop the loop
            logger.warning("entity state pass failed", exc_info=True)
        await asyncio.sleep(TICK_S)
