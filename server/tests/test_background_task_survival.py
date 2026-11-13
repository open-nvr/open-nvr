# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Background tasks must survive the garbage collector — field incident.

``asyncio.create_task()`` holds tasks only weakly. The lifespan hook
spawned every consumer fire-and-forget, so the GC destroyed the running
alerts-inbox consumer ONE SECOND after it subscribed ("Alerts inbox
consumer failed: coroutine ignored GeneratorExit"), and the broker
confirmed no ``opennvr.alerts.>`` subscription existed while producers
published 200+ HIGH/CRITICAL alarms into the void. Three defenses are
pinned here, each of which independently would have prevented the loss:

* ``spawn_background`` keeps a strong reference for the task lifetime —
  and ``main.py`` is lockstep-checked to never bare-``create_task``.
* Consumer loops contain no awaiting ``finally``: a closed coroutine
  (GeneratorExit) unwinds cleanly instead of dying with the cryptic
  error above — while real cancellation still unsubscribes and drains.
* ``run_consumer_forever`` restarts a crashed consumer instead of
  letting one exception cost the rest of the uptime.
"""

from __future__ import annotations

import ast
import asyncio
import gc
import os
import secrets
import sys
import types as _types
import weakref
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_bg_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
try:
    from cryptography.fernet import Fernet

    os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY",
                          Fernet.generate_key().decode())
except ImportError:  # pragma: no cover
    pass

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

import core.background_tasks as bg  # noqa: E402
from services import alerts_inbox  # noqa: E402

# ── fake nats: suspending teardown, like the real client ─────────────


class FakeSub:
    def __init__(self):
        self.unsubscribed = False

    async def unsubscribe(self):
        await asyncio.sleep(0)  # real unsubscribe does network I/O
        self.unsubscribed = True


class FakeClient:
    def __init__(self):
        self.drained = False
        self.sub = FakeSub()

    async def subscribe(self, subject, cb=None):
        self.subject = subject
        self.cb = cb
        return self.sub

    async def drain(self):
        await asyncio.sleep(0)  # real drain flushes the socket
        self.drained = True


@pytest.fixture()
def fake_nats(monkeypatch):
    state: dict = {}
    mod = _types.ModuleType("nats")

    async def connect(url, **kwargs):
        client = FakeClient()
        state["client"] = client
        return client

    mod.connect = connect
    monkeypatch.setitem(sys.modules, "nats", mod)
    from core.config import settings

    monkeypatch.setattr(settings, "nats_url", "nats://broker:4222",
                        raising=False)
    monkeypatch.setattr(settings, "internal_api_key", "test-token",
                        raising=False)
    return state


# ── the field failure, reproduced: coroutine closed at the park ──────


def test_closed_consumer_coroutine_unwinds_cleanly(fake_nats):
    """GC killing the task == coro.close() at the park point. The old
    code awaited unsubscribe/drain in a ``finally`` there, which under
    GeneratorExit raises RuntimeError('coroutine ignored GeneratorExit')
    — exactly the error log from the field. The fixed loop must unwind
    without a sound."""

    async def scenario():
        coro = alerts_inbox.run_consumer_loop()
        # Drive by hand: the fake connect/subscribe never suspend, so
        # the first send() runs straight to `await asyncio.Event().wait()`
        # and yields its future — the exact spot the GC struck.
        coro.send(None)
        assert fake_nats["client"].subject == alerts_inbox.SUBJECT
        coro.close()  # buggy code: RuntimeError; fixed code: silence

    asyncio.run(scenario())


def test_cancelled_consumer_still_unsubscribes_and_drains(fake_nats):
    """Moving teardown out of ``finally`` must NOT lose it on the real
    shutdown path: cancellation still unsubscribes and drains."""

    async def scenario():
        task = asyncio.ensure_future(alerts_inbox.run_consumer_loop())
        for _ in range(20):
            await asyncio.sleep(0)
            if "client" in fake_nats:
                break
        await asyncio.sleep(0)  # let it reach the park point
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake_nats["client"].sub.unsubscribed
        assert fake_nats["client"].drained

    asyncio.run(scenario())


# ── the keeper ───────────────────────────────────────────────────────


def test_spawn_background_keeps_a_strong_reference():
    async def scenario():
        started = asyncio.Event()

        async def work():
            started.set()
            # Inline Event, exactly like the consumers' park point: the
            # waiter future is reachable ONLY through the task itself —
            # a pure reference cycle the GC collects unless the keeper
            # holds a root. This is the precise field failure shape.
            await asyncio.Event().wait()

        task = bg.spawn_background(work(), name="keeper-test")
        await started.wait()
        assert task in bg._BACKGROUND_TASKS

        # Drop every local reference — the situation that killed the
        # alerts consumer. The keeper set alone must keep it alive.
        ref = weakref.ref(task)
        del task
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()
        alive = ref()
        assert alive is not None, "keeper failed: task was collected"
        assert not alive.done(), "keeper failed: task was killed"

        # And finished tasks must not accumulate forever.
        alive.cancel()
        with pytest.raises(asyncio.CancelledError):
            await alive
        await asyncio.sleep(0)
        assert alive not in bg._BACKGROUND_TASKS

    asyncio.run(scenario())


def test_main_lifespan_never_bare_create_tasks():
    """Lockstep with main.py: every ``asyncio.create_task(...)`` whose
    result is DROPPED (a bare expression statement) is the GC footgun —
    there must be none. Assigned ones (camera_status_task, held and
    cancelled at shutdown) are fine."""
    tree = ast.parse((_HERE / "main.py").read_text())
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "create_task"
    ]
    assert offenders == [], (
        f"bare fire-and-forget asyncio.create_task at main.py lines "
        f"{offenders} — route through core.background_tasks."
        f"spawn_background so the GC cannot kill the task"
    )
    # And the alarm chain specifically goes through the keeper.
    src = (_HERE / "main.py").read_text()
    assert "spawn_background(background_alerts_inbox_consumer()" in src


def test_no_awaiting_finally_in_any_consumer_module():
    """An ``await`` inside ``finally`` runs under GeneratorExit when the
    coroutine is closed and turns a silent GC kill into a crashed —
    still dead — consumer. Ban the construct in every consumer module."""
    for rel in (
        "services/alerts_inbox.py",
        "services/plate_event_consumer.py",
        "services/occupancy_event_consumer.py",
        "core/background_tasks.py",
    ):
        tree = ast.parse((_HERE / rel).read_text())
        bad = [
            (node.lineno, inner.lineno)
            for node in ast.walk(tree)
            if isinstance(node, ast.Try) and node.finalbody
            for stmt in node.finalbody
            for inner in ast.walk(stmt)
            if isinstance(inner, (ast.Await, ast.AsyncFor, ast.AsyncWith))
        ]
        assert bad == [], f"{rel}: await inside finally at {bad}"


# ── the supervisor ───────────────────────────────────────────────────


def test_supervisor_restarts_a_crashing_consumer():
    calls: list[int] = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("boom")

    asyncio.run(bg.run_consumer_forever("flaky", flaky, restart_delay=0))
    assert len(calls) == 3  # two crashes restarted, clean return stopped


def test_supervisor_respects_deliberate_shutdowns():
    async def scenario():
        # Clean return (e.g. "no NATS_URL configured") → no restart.
        ran: list[int] = []

        async def disabled():
            ran.append(1)

        await bg.run_consumer_forever("disabled", disabled, restart_delay=0)
        assert ran == [1]

        # Cancellation (process shutdown) → re-raised, not swallowed.
        async def parked():
            await asyncio.Event().wait()

        task = asyncio.ensure_future(
            bg.run_consumer_forever("parked", parked, restart_delay=0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
