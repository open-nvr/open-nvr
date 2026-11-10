# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The governor: can the box afford enrichment right now? (#583)"""
from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite:///./_enrichment_governor_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402

from services import enrichment_gate as eg  # noqa: E402
from services import enrichment_governor as gov  # noqa: E402
from services.enrichment_governor import State  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def signals(monkeypatch):
    """The governor's inputs, under the test's control."""
    s = {"cpu": None, "cpu_high": False, "edges": []}
    monkeypatch.setattr(gov, "host_cpu_percent", lambda: s["cpu"])
    monkeypatch.setattr(gov, "host_cpu_high", lambda: s["cpu_high"])
    monkeypatch.setattr(eg, "notify_edge",
                        lambda t, *, active, description, data: s["edges"].append((t, active, data.get("state"))))
    monkeypatch.setattr(eg, "_notify", lambda gate, *, active: None)
    return s


@pytest.fixture()
def clock(monkeypatch):
    now = {"t": 5000.0}
    monkeypatch.setattr(gov, "_now", lambda: now["t"])
    monkeypatch.setattr(eg, "_now", lambda: now["t"])
    return now


# ── the window ────────────────────────────────────────────────────────

def test_window_parsing_and_wrap_past_midnight(caplog):
    assert gov.parse_window("") is None
    assert gov.parse_window("01:00-06:00") == (60, 360)
    assert gov.parse_window(" 22:00 - 06:00 ") == (1320, 360)
    assert gov.parse_window("25:00-06:00") is None, "an impossible hour is ignored"
    assert gov.parse_window("night") is None
    assert "not HH:MM-HH:MM" in caplog.text
    w = gov.parse_window("22:00-06:00")
    assert gov.in_window(w, datetime(2026, 9, 28, 23, 30))
    assert gov.in_window(w, datetime(2026, 9, 28, 2, 0))
    assert not gov.in_window(w, datetime(2026, 9, 28, 14, 0))
    assert gov.in_window(None, datetime(2026, 9, 28, 14, 0)), "no window = always open"


# ── the state machine ─────────────────────────────────────────────────

def test_unknown_load_is_normal_not_idle(signals):
    assert gov.governor().evaluate() is State.NORMAL
    assert signals["edges"] == []


def test_cpu_over_the_soft_line_throttles_at_once(signals):
    signals["cpu"] = 70
    g = gov.governor()
    assert g.evaluate() is State.LIVE_ONLY
    assert "70%" in g.reason
    assert signals["edges"] == [(gov.ALERT_TYPE, True, "live_only")]


def test_stepping_down_waits_for_the_calm_to_hold(signals, clock):
    signals["cpu"] = 70
    g = gov.governor()
    assert g.evaluate() is State.LIVE_ONLY
    signals["cpu"] = 40
    assert g.evaluate() is State.LIVE_ONLY, "one calm sample is not a trend"
    clock["t"] += gov.STABLE_S - 1
    assert g.evaluate() is State.LIVE_ONLY
    clock["t"] += 2
    assert g.evaluate() is State.NORMAL
    assert signals["edges"][-1] == (gov.ALERT_TYPE, False, "normal")


def test_a_bounce_back_up_resets_the_calm_timer(signals, clock):
    signals["cpu"] = 70
    g = gov.governor()
    g.evaluate()
    signals["cpu"] = 40
    g.evaluate()
    clock["t"] += gov.STABLE_S - 5
    signals["cpu"] = 70
    g.evaluate()
    signals["cpu"] = 40
    clock["t"] += 10
    assert g.evaluate() is State.LIVE_ONLY, "the calm did not hold for STABLE_S"


def test_the_monitors_own_alert_or_an_open_breaker_pauses(signals):
    signals["cpu_high"] = True
    g = gov.governor()
    assert g.evaluate() is State.PAUSED and "cpu_high" in g.reason
    signals["cpu_high"] = False
    gov._reset_for_tests()
    gate = eg.gate_for("moondream-vlm")
    gate.breaker_timeouts = 1
    _run(gate.admit()); gate.release("timeout")
    assert gate.is_open
    g = gov.governor()
    assert g.evaluate() is State.PAUSED and "breaker" in g.reason


def test_a_slow_adapter_throttles_even_on_an_idle_cpu(signals, monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "events_enrichment_slow_call_s", 10.0, raising=False)
    gate = eg.gate_for("moondream-vlm")
    for _ in range(3):
        _run(gate.admit()); gate.release("ok", elapsed_s=30.0)
    assert gate.latency_ewma_s and gate.latency_ewma_s > 10
    g = gov.governor()
    assert g.evaluate() is State.LIVE_ONLY
    assert "moondream-vlm" in g.reason and "a call" in g.reason


# ── what the live path gets ───────────────────────────────────────────

def test_paused_drops_without_touching_a_slot(signals):
    signals["cpu_high"] = True
    gate = eg.gate_for("moondream-vlm")
    assert _run(gov.admit_live(gate, "caption")) is False
    assert gate.inflight == 0 and gate.dropped_open == 1


def test_paused_refuses_a_requested_call_without_counting_a_drop(signals):
    """The lane's worker holds and asks again: the work is not lost, so
    it is not a drop — a flapping PAUSE must not bury real breaker
    drops under 600 retries of it."""
    signals["cpu_high"] = True
    gate = eg.gate_for("moondream-vlm")
    for _ in range(4):
        assert _run(gov.admit_live(gate, "caption", priority="requested")) is False
    assert gate.inflight == 0 and gate.dropped_open == 0


def test_live_only_gives_vqa_a_line_and_captions_none(signals):
    signals["cpu"] = 80
    gate = eg.AdapterGate("m", max_inflight=1, queue_depth=8, breaker_timeouts=5)

    async def scenario():
        assert await gov.admit_live(gate, "vqa") is True          # holds the slot
        assert await gov.admit_live(gate, "caption") is False      # no line for a sentence
        assert await gov.admit_live(gate, "embed") is False
        waiter = asyncio.ensure_future(gov.admit_live(gate, "vqa"))  # VQA may wait
        await asyncio.sleep(0)
        assert gate.waiting == 1
        gate.release("ok")
        await asyncio.sleep(0)
        assert waiter.result() is True
        gate.release("ok")

    _run(scenario())
    assert gate.dropped_full == 2


def test_normal_is_the_plain_gate(signals):
    gate = eg.AdapterGate("m", max_inflight=1, queue_depth=8, breaker_timeouts=5)

    async def scenario():
        assert await gov.admit_live(gate, "caption") is True
        w = asyncio.ensure_future(gov.admit_live(gate, "caption"))
        await asyncio.sleep(0)
        assert gate.waiting == 1, "in NORMAL a caption may wait like anything else"
        gate.release("ok"); await asyncio.sleep(0)
        assert w.result() is True
        gate.release("ok")

    _run(scenario())


# ── what the backfill gets ────────────────────────────────────────────

def test_backfill_needs_normal_idle_for_a_while_and_the_window(signals, clock, monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "events_enrichment_backfill_idle_s", 120, raising=False)
    g = gov.governor()
    assert not gov.backfill_allowed(g, starting=True), "fresh governor: not idle long enough to START"
    assert gov.backfill_allowed(g), "...but a pass already under way may continue"
    clock["t"] += 121
    assert gov.backfill_allowed(g, starting=True)
    gate = eg.gate_for("moondream-vlm")
    _run(gate.admit())
    assert not gov.backfill_allowed(g), "live in flight"
    gate.release("ok")
    assert gov.backfill_allowed(g)
    signals["cpu"] = 70
    assert not gov.backfill_allowed(g), "throttled"
    signals["cpu"] = 40
    g.evaluate()                              # the first calm sample starts the calm timer
    clock["t"] += gov.STABLE_S + 1
    g.evaluate()                              # ...and this one steps down
    assert g.state is State.NORMAL
    assert not gov.backfill_allowed(g, starting=True), "just stepped down; the idle clock restarted"
    clock["t"] += 121
    assert gov.backfill_allowed(g, starting=True)
    monkeypatch.setattr(settings, "events_enrichment_backfill_window", "03:00-03:01", raising=False)
    monkeypatch.setattr(gov, "in_window", lambda w, now=None: False)
    assert not gov.backfill_allowed(g), "outside the operator's window"


def test_wait_for_backfill_slot_holds_then_releases(signals, clock, monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "events_enrichment_backfill_idle_s", 0, raising=False)
    signals["cpu"] = 90

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(gov.wait_for_backfill_slot(poll_s=0.01), timeout=0.05)
        signals["cpu"] = 10
        gov.governor().evaluate()                 # calm observed; the timer starts
        clock["t"] += gov.STABLE_S + 1            # ...and has held long enough
        await asyncio.wait_for(gov.wait_for_backfill_slot(poll_s=0.01), timeout=1)

    _run(scenario())


# ── wiring ────────────────────────────────────────────────────────────

def test_the_three_enrichers_and_the_backfill_go_through_the_governor():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "services"
    gate_src = (root / "enrichment_gate.py").read_text()
    assert "await admit_live(gate, kind, priority)" in gate_src, "the shared call bypasses the governor"
    for name, kind in (("caption_enrichment.py", "caption"),
                       ("descriptor_enrichment.py", "vqa"),
                       ("embed_enrichment.py", "embed")):
        src = (root / name).read_text()
        assert f'kind="{kind}"' in src and "infer_through_gate(" in src, (
            f"{name} does not make its call through infer_through_gate with its kind")
        assert "gate.admit()" not in src, f"{name} calls the gate directly"
    bf = (root / "enrichment_backfill.py").read_text()
    assert "wait_for_backfill_slot(starting=True)" in bf, "a pass must wait for the idle period"
    assert "wait_for_backfill_slot()" in bf, "and every item re-checks the live conditions"


# ── review round three (#592) ─────────────────────────────────────────

def test_a_stale_latency_figure_stops_throttling(signals, clock, monkeypatch):
    """The EWMA only moves on calls. After a breaker episode it sits at
    ~90 s; if that alone kept the box LIVE-ONLY, the backfill — which
    needs NORMAL — could never run, and nothing would refresh the figure
    because nothing was being sent. It expires."""
    from core.config import settings
    monkeypatch.setattr(settings, "events_enrichment_slow_call_s", 10.0, raising=False)
    gate = eg.gate_for("moondream-vlm")
    _run(gate.admit()); gate.release("timeout", elapsed_s=90.0)
    g = gov.governor()
    assert g.evaluate() is State.LIVE_ONLY
    clock["t"] += gov.SLOW_SIGNAL_TTL_S + 1
    g._calmer_since = None
    g.evaluate(); clock["t"] += gov.STABLE_S + 1
    assert g.evaluate() is State.NORMAL, "a figure nobody refreshed in 5 min says nothing about now"


def test_a_long_silence_counts_as_calm(signals, clock):
    """evaluate() runs on admissions. A box paused at 14:00 whose alert
    cleared at 14:05 and saw no visit until 17:00 has been calm for
    hours; its first visit must not be the one that is dropped."""
    signals["cpu_high"] = True
    g = gov.governor()
    assert g.evaluate() is State.PAUSED
    signals["cpu_high"] = False
    clock["t"] += 3 * 3600
    assert g.evaluate() is State.NORMAL, "three silent hours are not zero seconds of calm"


def test_snapshot_never_moves_the_governor(signals):
    g = gov.governor()
    signals["cpu"] = 95
    snap = g.snapshot()
    assert snap["state"] == "normal", "a status read does not evaluate"
    assert g.state is State.NORMAL and signals["edges"] == []
    assert g.evaluate() is State.LIVE_ONLY


def test_the_search_query_is_embedded_even_when_the_box_is_paused(signals, monkeypatch):
    """embed_text is a person waiting on an answer, not background work.
    PAUSED refuses enrichment; it must not refuse the operator."""
    import httpx

    from services import embed_enrichment as emb

    signals["cpu_high"] = True
    assert gov.governor().evaluate() is State.PAUSED

    async def _adapter():
        return "clip"

    monkeypatch.setattr(emb, "_resolve_embed_adapter", _adapter)

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"result": {"embedding": [1.0, 0.0]}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    vector, adapter = _run(emb.embed_text("white van"))
    assert vector == [1.0, 0.0] and adapter == "clip"


# ── the requested lane's priority ─────────────────────────────────────

def test_a_requested_visit_keeps_the_full_line_in_live_only_but_not_paused(signals):
    signals["cpu"] = 80
    gate = eg.AdapterGate("m", max_inflight=1, queue_depth=8, breaker_timeouts=5)

    async def scenario():
        assert await gov.admit_live(gate, "caption") is True            # holds the slot
        assert await gov.admit_live(gate, "caption") is False           # live: no line
        w = asyncio.ensure_future(gov.admit_live(gate, "caption", "requested"))
        await asyncio.sleep(0)
        assert gate.waiting == 1, "somebody is waiting on this one: it may queue"
        gate.release("ok"); await asyncio.sleep(0)
        assert w.result() is True
        gate.release("ok")
        signals["cpu"] = None; signals["cpu_high"] = True
        assert await gov.admit_live(gate, "vqa", "requested") is False, (
            "PAUSED refuses the requested lane too — the box is losing")

    _run(scenario())


# ── opportunistic: a share of the clock, and people first ───────────────

def test_pace_sleep_keeps_the_backfill_to_its_share(monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "events_enrichment_backfill_share", 0.5, raising=False)
    assert gov.pace_sleep(5.0) == 5.0, "half the clock: rest as long as it worked"
    assert gov.pace_sleep(5.0, share=0.25) == 15.0
    assert gov.pace_sleep(5.0, share=1.0) == 0.0, "no pacing"
    assert gov.pace_sleep(5.0, share=1.0, floor_s=0.5) == 0.5, "the fixed pause still applies"
    assert gov.pace_sleep(0.0) == 0.0
    assert gov.pace_sleep(2.0, share=0.001) == gov.pace_sleep(2.0, share=0.05), "clamped"


def test_a_person_using_the_box_holds_the_backfill(signals, clock, monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "events_enrichment_backfill_idle_s", 0, raising=False)
    monkeypatch.setattr(settings, "events_enrichment_backfill_interactive_hold_s", 90, raising=False)
    gov._reset_for_tests()
    g = gov.governor()
    assert gov.backfill_allowed(g, starting=True)
    gov.note_interactive("camera-agent")
    assert not gov.backfill_allowed(g), "held for a conversation"
    assert gov.interactive_recently()
    clock["t"] += 89
    assert not gov.backfill_allowed(g)
    clock["t"] += 2
    assert gov.backfill_allowed(g), "the hold passed"
    gov.note_interactive("search")
    monkeypatch.setattr(settings, "events_enrichment_backfill_interactive_hold_s", 0, raising=False)
    assert gov.backfill_allowed(g), "0 = off"
