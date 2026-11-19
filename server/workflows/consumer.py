# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Feed the bus and the clock to the workflow engine.

A second subscription to ``opennvr.alerts.>``, beside the inbox's (the
inbox stores every alert, the engine starts the workflows an alert
triggers), and one to ``opennvr.events.>`` for contracted events. A tick
every few seconds times out approvals, starts schedules and starts
camera_offline workflows from the camera status service. Same best-effort posture as the other consumers: no NATS URL or
no nats-py means no workflow runs, and the loop never raises into the
task runner. At start, runs the last process left half-done are closed
as failed, once (:func:`start_engine`).
"""
from __future__ import annotations

import asyncio
import json
import logging

from workflows.engine import Engine

log = logging.getLogger(__name__)

SUBJECT = "opennvr.alerts.>"
#: Contracted domain events (docs/EVENT_CONTRACTS.md), for when: event.
EVENTS_SUBJECT = "opennvr.events.>"
_RETRY_SECONDS = 60.0


#: How often open approvals are checked for their timeout.
SWEEP_SECONDS = 15.0
_engine: Engine | None = None


def make_engine() -> Engine:
    from core.database import SessionLocal
    return Engine(SessionLocal)


def engine() -> Engine:
    """The process's engine: the one the consumer runs, which the
    approvals route hands answers to."""
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def start_engine() -> Engine:
    """The process's one engine, with the runs a stop left half-done
    closed. Once per process: a consumer restart must not fail the runs
    this process is still walking. Waiting runs are left waiting: their
    approvals carry them on."""
    eng = engine()
    closed = eng.close_interrupted()
    if closed:
        log.warning("workflows: %d run(s) were interrupted by a restart and marked failed",
                    closed)
    return eng


def camera_status() -> dict[int, bool | None]:
    """Every camera's last-known connectivity, from the camera status
    service (None: not known yet, e.g. just after a restart)."""
    from core.database import SessionLocal
    from models import Camera
    from services.camera_status_service import get_camera_status_service

    db = SessionLocal()
    try:
        ids = [c for (c,) in db.query(Camera.id).all()]
    finally:
        db.close()
    return get_camera_status_service().snapshot(ids)


async def sweep_timeouts(eng: Engine) -> None:
    """For the process lifetime, every few seconds: time out open
    approvals, start schedules, and start camera_offline workflows
    (Engine.tick). Runs whether or not the bus is configured."""
    while True:
        try:
            eng.tick(camera_status())
        except Exception:   # a bad row must not stop the next tick
            log.warning("workflows: tick failed", exc_info=True)
        await asyncio.sleep(SWEEP_SECONDS)


async def run_consumer_loop(engine: Engine) -> None:
    from core.config import settings

    url = (getattr(settings, "nats_url", "") or "").strip()
    if not url:
        log.info("workflow engine idle (no NATS_URL): alerts cannot start workflows")
        return
    try:
        import nats
    except ImportError:
        log.warning("workflow engine idle: nats-py not installed")
        return
    token = (getattr(settings, "internal_api_key", "") or "").strip() or None
    warming = asyncio.get_running_loop().create_task(engine.warm_up())

    async def handle(msg) -> None:
        try:
            envelope = json.loads(msg.data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(envelope, dict):
            return
        try:
            if msg.subject.startswith("opennvr.events."):
                engine.start_from_event(envelope)
            else:
                engine.start_from_alert(envelope)
        except Exception:   # one bad message must not end the subscription
            log.warning("workflows: %s not handled", msg.subject, exc_info=True)

    # As in alerts_inbox: no finally that awaits; teardown on the two paths.
    while True:
        client = sub = events = None
        try:
            client = await nats.connect(url, connect_timeout=5, token=token)
            sub = await client.subscribe(SUBJECT, cb=handle)
            events = await client.subscribe(EVENTS_SUBJECT, cb=handle)
            log.info("workflow engine subscribed to %s and %s", SUBJECT, EVENTS_SUBJECT)
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            warming.cancel()
            await _teardown(events, None)
            await _teardown(sub, client)
            raise
        except Exception as exc:
            await _teardown(events, None)
            await _teardown(sub, client)
            log.warning("workflow engine: connect/subscribe failed (%s); retrying in %.0fs",
                        exc, _RETRY_SECONDS)
            await asyncio.sleep(_RETRY_SECONDS)


async def _teardown(sub, client) -> None:
    try:
        if sub is not None:
            await sub.unsubscribe()
    except Exception:
        pass
    try:
        if client is not None:
            await client.drain()
    except Exception:
        pass
