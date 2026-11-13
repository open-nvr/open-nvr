# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The admission gate the enrichers share (#583).

The rules under test are the ones the field box broke: never more in
flight than the adapter declared, a wait line that drops instead of
growing, a slot held until the adapter ANSWERS, and a breaker that stops
sending after repeated timeouts and says so once.
"""
from __future__ import annotations

import asyncio
import os
import secrets

from cryptography.fernet import Fernet

# The gate reads core.config.settings for its knobs, and the registry
# lookup imports a router that binds settings at import. Same bootstrap
# every other server test that touches settings uses; before the import.
os.environ.setdefault("DATABASE_URL", "sqlite:///./_enrichment_gate_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402

from services import enrichment_gate as eg  # noqa: E402

#: The real notifier, captured before the autouse fixture below stubs it.
_REAL_NOTIFY = eg._notify


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _quiet_notify(monkeypatch):
    """Record breaker edges instead of writing system events."""
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(eg, "_notify", lambda gate, *, active: seen.append((gate.adapter, active)))
    return seen


@pytest.fixture()
def clock(monkeypatch):
    """A monotonic clock the test advances by hand."""
    now = {"t": 1000.0}
    monkeypatch.setattr(eg, "_now", lambda: now["t"])
    return now


# ── slots come from the registry ──────────────────────────────────────

def test_the_budget_is_the_adapters_own_declaration(monkeypatch):
    from routers import adapters_catalog as cat

    class _E:
        def __init__(self, id, n):
            self.id = id

            class _S:
                max_inflight = n
            self.scheduling = _S()

    monkeypatch.setattr(cat, "load_adapters_index",
                        lambda: [_E("yolov8-object-detection", 4),
                                 _E("moondream-vlm", 1), _E("clip-embeddings", 3)])
    assert eg.max_inflight_for("moondream-vlm") == 1
    assert eg.max_inflight_for("clip") == 3, "the clip adapter registers as 'clip'"
    assert eg.max_inflight_for("ollamavlm") == 1, "unknown adapters get the registry default"
    # Exact or aliased, never fuzzy: a prefix match handed these the
    # detector's budget of 4.
    assert eg.max_inflight_for("") == 1
    assert eg.max_inflight_for("yolo") == 1
    assert eg.max_inflight_for("moondream") == 1


def test_the_shipped_registry_gives_every_captioner_one_slot():
    # The real file: every VQA/caption/embed adapter declares max_inflight 1.
    for name in ("moondream-vlm", "blip-scene-caption", "clip"):
        assert eg.max_inflight_for(name) == 1


# ── never more in flight than declared; a full line drops ─────────────

def test_a_full_wait_line_drops_instead_of_growing():
    gate = eg.AdapterGate("m", max_inflight=1, queue_depth=2, breaker_timeouts=5)

    async def scenario():
        assert await gate.admit() is True            # holds the one slot
        waiters = [asyncio.ensure_future(gate.admit()) for _ in range(2)]
        await asyncio.sleep(0)                        # let them queue
        assert gate.waiting == 2 and gate.inflight == 1
        assert await gate.admit() is False, "third waiter is one over the line"
        assert gate.dropped_full == 1
        gate.release("ok")                            # one waiter gets in
        await asyncio.sleep(0)
        assert gate.inflight == 1, "never more than max_inflight"
        gate.release("ok")
        await asyncio.sleep(0)
        gate.release("ok")
        assert all(w.result() is True for w in waiters)
        assert gate.inflight == 0 and gate.waiting == 0

    _run(scenario())
    assert gate.admitted == 3 and gate.completed == 3


def test_depth_zero_means_no_waiting_at_all():
    gate = eg.AdapterGate("m", max_inflight=1, queue_depth=0, breaker_timeouts=5)

    async def scenario():
        assert await gate.admit() is True
        assert await gate.admit() is False
        gate.release("ok")
        assert await gate.admit() is True

    _run(scenario())


# ── the breaker ───────────────────────────────────────────────────────

def _timeouts(gate, n):
    async def go():
        for _ in range(n):
            assert await gate.admit() is True
            gate.release("timeout")
    _run(go())


def test_repeated_timeouts_open_the_breaker_and_say_so_once(_quiet_notify, clock):
    gate = eg.AdapterGate("moondream-vlm", max_inflight=1, queue_depth=8, breaker_timeouts=3)
    _timeouts(gate, 2)
    assert not gate.is_open, "two in a row is not yet a pattern"
    _timeouts(gate, 1)
    assert gate.is_open and gate.trips == 1
    assert _quiet_notify == [("moondream-vlm", True)]
    # While open, nothing is sent and nothing waits.
    assert _run(gate.admit()) is False
    assert gate.dropped_open == 1 and gate.inflight == 0


def test_one_probe_after_the_cooldown_then_success_closes_it(_quiet_notify, clock):
    gate = eg.AdapterGate("m", max_inflight=1, queue_depth=8, breaker_timeouts=2)
    _timeouts(gate, 2)
    assert gate.is_open
    clock["t"] += eg.COOLDOWN_BASE_S - 1
    assert _run(gate.admit()) is False, "still cooling"
    clock["t"] += 2

    async def probe():
        assert await gate.admit() is True, "exactly one probe goes through"
        assert await gate.admit() is False, "a second caller while the probe is out is dropped"
        gate.release("ok")

    _run(probe())
    assert not gate.is_open
    assert gate.cooldown_s == eg.COOLDOWN_BASE_S, "a success resets the backoff"
    assert _quiet_notify[-1] == ("m", False)


def test_a_failed_probe_backs_off_further_up_to_the_ceiling(clock):
    gate = eg.AdapterGate("m", max_inflight=1, queue_depth=8, breaker_timeouts=1)
    _timeouts(gate, 1)
    first = gate.cooldown_s                       # already doubled for next time
    assert first == eg.COOLDOWN_BASE_S * 2
    for _ in range(6):
        clock["t"] += eg.COOLDOWN_MAX_S + 1
        assert _run(gate.admit()) is True
        gate.release("timeout")
        assert gate.is_open
    assert gate.cooldown_s == eg.COOLDOWN_MAX_S


def test_a_waiter_dropped_by_a_trip_gives_its_slot_back():
    gate = eg.AdapterGate("m", max_inflight=1, queue_depth=4, breaker_timeouts=1)

    async def scenario():
        assert await gate.admit() is True
        waiter = asyncio.ensure_future(gate.admit())
        await asyncio.sleep(0)
        gate.release("timeout")                   # trips; the waiter wakes into an open breaker
        await asyncio.sleep(0)
        assert waiter.result() is False
        assert gate.inflight == 0 and gate._slots._value == 1, "the slot was returned"

    _run(scenario())


# ── through the real caller ───────────────────────────────────────────

def test_the_captioner_stops_sending_once_the_breaker_trips(monkeypatch, clock):
    """End to end: N timeouts through _caption_jpeg, then the next call
    never reaches httpx — the acceptance criterion in #583."""
    import httpx

    from core.config import settings
    from services import caption_enrichment as cap

    monkeypatch.setattr(settings, "events_enrichment_breaker_timeouts", 2, raising=False)
    posts = {"n": 0}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            posts["n"] += 1
            raise httpx.ReadTimeout("")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())

    results = []

    async def go():
        for _ in range(4):
            results.append(await cap._caption_jpeg(b"jpeg", "moondream-vlm", "cam1", event_id=1))

    _run(go())
    assert posts["n"] == 2, "two timeouts tripped it; the next two were dropped at the gate"
    gate = eg.gate_for("moondream-vlm")
    assert gate.is_open and gate.dropped_open == 2
    assert results == [None, None, eg.DROPPED, eg.DROPPED], (
        "a call that was made and failed is None; one never made is DROPPED")


def test_caption_and_vqa_share_one_gate_per_adapter():
    assert eg.gate_for("moondream-vlm") is eg.gate_for("Moondream-VLM")
    assert eg.gate_for("moondream-vlm") is not eg.gate_for("blip-scene-caption")


# ── the operator sees it ──────────────────────────────────────────────

def test_a_trip_records_the_system_event_and_publishes_on_the_bus(monkeypatch, clock):
    """Not stubbed this time: the trip must reach system_events (the
    edge the UI reads) and the bus (the live push) — with the adapter
    named and the state right both ways."""
    import services.event_bus_service as bus
    import services.system_events as se

    edges: list[dict] = []
    pushes: list[dict] = []
    monkeypatch.setattr(se, "record_system_event_edge",
                        lambda **kw: edges.append(kw) or {"id": 1})

    async def _publish(**kw):
        pushes.append(kw)

    monkeypatch.setattr(bus, "publish_system_alert", _publish)
    monkeypatch.setattr(eg, "_notify", _REAL_NOTIFY)

    gate = eg.AdapterGate("moondream-vlm", max_inflight=1, queue_depth=8, breaker_timeouts=1)

    async def scenario():
        assert await gate.admit() is True
        gate.release("timeout")                   # trip
        await asyncio.gather(*eg._notify_tasks)
        clock["t"] += eg.COOLDOWN_BASE_S + 1
        assert await gate.admit() is True         # probe
        gate.release("ok")                        # close
        await asyncio.gather(*eg._notify_tasks)

    _run(scenario())

    assert [e["active"] for e in edges] == [True, False]
    # Per adapter: system events dedupe on the type, and two adapters
    # sharing one type made the second trip vanish and the first
    # recovery announce "resumed" for both.
    assert all(e["event_type"] == f"{eg.ALERT_TYPE}:moondream-vlm" for e in edges)
    assert edges[0]["data"]["adapter"] == "moondream-vlm"
    assert "too slow" in edges[0]["description"]
    assert [p["state"] for p in pushes] == ["active", "inactive"]


# ── review round two (#591) ───────────────────────────────────────────

def test_a_non_200_answer_is_neither_a_recovery_nor_a_reset(_quiet_notify, clock, monkeypatch):
    """A 503 from KAI-C is an HTTP answer, not the adapter keeping up.
    It must not zero the timeout streak, and while the breaker is open a
    probe answered 'busy' in 50 ms must not resume full traffic."""
    import httpx

    from core.config import settings
    from services import caption_enrichment as cap

    monkeypatch.setattr(settings, "events_enrichment_breaker_timeouts", 2, raising=False)
    script = {"seq": []}

    class _Resp:
        def __init__(self, code):
            self.status_code = code

        @staticmethod
        def json():
            return {"result": {"caption": "x"}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            step = script["seq"].pop(0)
            if step == "timeout":
                raise httpx.ReadTimeout("")
            return _Resp(step)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())

    async def call():
        return await cap._caption_jpeg(b"jpeg", "moondream-vlm", "cam1", event_id=1)

    gate = eg.gate_for("moondream-vlm")
    # timeout, 503, timeout: the 503 in the middle must not break the run
    script["seq"] = ["timeout", 503, "timeout"]
    _run(call()); _run(call()); _run(call())
    assert gate.is_open, "two timeouts with a 503 between them is still two timeouts"
    # cooldown over; the probe gets a 503 → stays open, backs off further
    clock["t"] += eg.COOLDOWN_BASE_S + 1
    script["seq"] = [503]
    assert _run(call()) is None
    assert gate.is_open and gate.cooldown_s > eg.COOLDOWN_BASE_S * 2
    assert _quiet_notify == [("moondream-vlm", True)], "no 'resumed' was announced"
    # ...and a real 200 closes it
    clock["t"] += eg.COOLDOWN_MAX_S + 1
    script["seq"] = [200]
    assert _run(call()) == "x"
    assert not gate.is_open


def test_an_expired_cooldown_with_no_traffic_reads_as_idle(clock):
    """The breaker only moves inside admit(). With cameras quiet after a
    trip, nothing calls admit(), so 'open' would be forever — and the
    backfill, the one caller that could probe, was the one told to wait."""
    gate = eg.gate_for("moondream-vlm")
    gate.breaker_timeouts = 1
    _run(gate.admit()); gate.release("timeout")
    assert gate.is_open and gate.cooling and not gate.idle()
    clock["t"] += eg.COOLDOWN_BASE_S + 1
    assert gate.is_open and not gate.cooling and gate.idle(), (
        "cooldown over, nobody probing: the next caller may be the backfill")
    assert eg.all_idle()


def test_a_connect_timeout_is_short_and_is_not_a_breaker_timeout(monkeypatch, caplog):
    """The 90 s limit is for a model computing. A host that never answers
    the SYN must cost the connect ceiling, log as unreachable, and count
    as an error — not pin the adapter's only slot for 90 s per call."""
    import logging

    import httpx

    from services import caption_enrichment as cap

    seen = {}

    class _Client:
        def __init__(self, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            raise httpx.ConnectTimeout("")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(**kw))
    gate = eg.gate_for("moondream-vlm")
    gate.breaker_timeouts = 1
    with caplog.at_level(logging.WARNING, logger="caption_enrichment"):
        assert _run(cap._caption_jpeg(b"jpeg", "moondream-vlm", "cam1", event_id=1)) is None
    assert isinstance(seen["timeout"], httpx.Timeout)
    assert seen["timeout"].connect == eg.CONNECT_TIMEOUT_S
    assert seen["timeout"].read == eg.timeout_s()
    assert "unreachable" in caplog.text and "ConnectTimeout" in caplog.text
    assert not gate.is_open and gate.errors == 1 and gate.timeouts == 0


# ── review round three (#592) ─────────────────────────────────────────

def test_a_straggler_finishing_while_open_does_not_move_the_breaker(_quiet_notify, clock):
    """max_inflight=2: A trips the breaker; B, admitted before the trip,
    times out later. B is old news — it must not extend the cooldown or
    double the backoff, and a 200 from B must not close the breaker
    either. Only the PROBE's outcome moves it."""
    gate = eg.AdapterGate("m", max_inflight=2, queue_depth=8, breaker_timeouts=1)

    async def scenario():
        assert await gate.admit() is True      # A
        assert await gate.admit() is True      # B
        gate.release("timeout")                # A trips
        assert gate.is_open
        opened_until, cooldown = gate.open_until, gate.cooldown_s
        gate.release("timeout")                # B, a straggler
        assert gate.open_until == opened_until and gate.cooldown_s == cooldown, (
            "a straggler's timeout extended the cooldown")
        assert gate.trips == 1
        clock["t"] += eg.COOLDOWN_BASE_S + 1
        assert await gate.admit() is True      # the probe
        assert await gate.admit() is False, "while the probe is out nobody else goes"
        gate.release("ok")                     # the probe's answer closes it
        assert not gate.is_open

    _run(scenario())


def test_a_probe_refused_by_the_wait_line_does_not_burn_its_token():
    """Cooldown over, a straggler still holds the only slot, the governor
    passes max_waiting=0: the probe cannot get in. It must not have
    spent the probe token, or the gate stays cooling with no probe out
    until the straggler releases."""
    import services.enrichment_gate as m

    gate = eg.AdapterGate("m", max_inflight=1, queue_depth=8, breaker_timeouts=1)
    now = {"t": 100.0}
    m_now = m._now
    m._now = lambda: now["t"]
    try:
        async def scenario():
            assert await gate.admit() is True          # the straggler
            gate2 = None
            # trip via a second gate call path: simulate with a direct trip
            gate.timeouts_in_row = 1
            gate._trip()
            now["t"] += eg.COOLDOWN_BASE_S + 1
            assert await gate.admit(max_waiting=0) is False, "slot busy, no line"
            assert gate._probe_out is False, "the token was not spent"
            assert gate.dropped_full == 1 and gate.dropped_open == 0
            gate.release("ok")                         # straggler done; breaker still open
            assert gate.is_open
            assert await gate.admit() is True, "now the probe gets in"
            assert gate._probe_out is True
            gate.release("ok")
            assert not gate.is_open

        _run(scenario())
    finally:
        m._now = m_now


def test_latency_is_stamped_so_a_stale_figure_can_expire(clock):
    gate = eg.AdapterGate("m", max_inflight=1, queue_depth=8, breaker_timeouts=5)
    assert gate.snapshot()["latency_age_s"] is None
    _run(gate.admit()); gate.release("ok", elapsed_s=30.0)
    assert gate.snapshot()["latency_age_s"] == 0.0
    clock["t"] += 400
    assert gate.snapshot()["latency_age_s"] == 400.0


def test_an_interactive_call_waits_its_turn_but_is_never_refused_for_load(monkeypatch):
    """The search box embedding its query goes through the adapter's
    slots (the model cannot do more) but not through the governor."""
    import httpx

    from services import enrichment_governor as gov

    async def _no(gate, kind):
        raise AssertionError("an interactive call asked the governor")

    monkeypatch.setattr(gov, "admit_live", _no)

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"result": {"embedding": [0.1, 0.2]}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    body = _run(eg.infer_through_gate("clip", {"text": "white van"}, kind="embed",
                                      log=eg.logger, caller="t", interactive=True))
    assert body == {"result": {"embedding": [0.1, 0.2]}}


def test_a_504_from_kaic_counts_as_a_timeout(_quiet_notify, clock, monkeypatch):
    """KAI-C's proxy gave up waiting for the adapter (its own timeout) and
    answered 504: the model is slow, not down. Read as a plain error the
    breaker never tripped and the Search lane asked the same visit again
    every time (Ventuno Q, 2026-11-05)."""
    import httpx

    from core.config import settings
    from services import caption_enrichment as cap

    monkeypatch.setattr(settings, "events_enrichment_breaker_timeouts", 2, raising=False)

    class _Resp:
        status_code = 504

        @staticmethod
        def json():
            return {"detail": "adapter timeout"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())

    async def call():
        return await cap._caption_jpeg(b"jpeg", "moondream-vlm", "cam1", event_id=1)

    gate = eg.gate_for("moondream-vlm")
    assert _run(call()) is None and not gate.is_open
    assert _run(call()) is None
    assert gate.is_open, "two 504s in a row are two timeouts: the breaker trips"
    assert gate.timeouts == 2
