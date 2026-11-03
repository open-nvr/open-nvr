# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Feed app alerts from the bus to the workflow engine.

A second subscription to ``opennvr.alerts.>``, beside the inbox's: the
inbox stores every alert, the engine starts the workflows an alert
triggers. Same best-effort posture as the other consumers: no NATS URL or
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


async def sweep_timeouts(eng: Engine) -> None:
    """Time out open approvals for the process lifetime; their runs take
    the no_answer path. Runs whether or not the bus is configured."""
    while True:
        try:
            eng.expire_due()
        except Exception:   # a bad row must not stop timeouts for every other
            log.warning("workflows: timeout sweep failed", exc_info=True)
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
            engine.start_from_alert(envelope)
        except Exception:   # one bad alert must not end the subscription
            log.warning("workflows: alert not handled", exc_info=True)

    # As in alerts_inbox: no finally that awaits; teardown on the two paths.
    while True:
        client = sub = None
        try:
            client = await nats.connect(url, connect_timeout=5, token=token)
            sub = await client.subscribe(SUBJECT, cb=handle)
            log.info("workflow engine subscribed to %s", SUBJECT)
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            warming.cancel()
            await _teardown(sub, client)
            raise
        except Exception as exc:
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
