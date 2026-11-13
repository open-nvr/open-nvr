# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GateController — decisions in, contact closures out, fail closed.

The rules worth protecting with tests, in the order of how much damage
breaking them does:

1. Anything that is not a literal ``allow`` actuates nothing, including
   decision values that do not exist yet.
2. A failed open is retriable by the very next car, and loud.
3. A momentary-only wiring is never faked into a hold.
4. A gate that cannot report its position never claims one.
"""
from __future__ import annotations

import struct
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import gate_controller as gc
import transports
from gate_controller import (
    AppConfig,
    GateController,
    HoldWindow,
    load_config,
    parse_schedule,
)
from transports import OpenerError, build_opener


# ── Harness ─────────────────────────────────────────────────────────


class FakeOpener(transports.Opener):
    """A wiring that records instead of moving a barrier."""

    def __init__(self, *, can_hold=False, state=None, fail=False, kind="fake"):
        super().__init__(kind=kind, address="test/0", vendor="Test Rig")
        self.can_hold = can_hold
        self._state = state          # None = unmonitored
        self.fail = fail
        self.pulses = 0
        self.holds: list[bool] = []

    def pulse(self):
        if self.fail:
            raise OpenerError("relay did not answer")
        self.pulses += 1

    def hold(self, on: bool):
        if not self.can_hold:
            raise transports.NotSupported("momentary only")
        if self.fail:
            raise OpenerError("latch refused")
        self.holds.append(on)
        self._state = "open" if on else "closed"

    def read_state(self):
        return self._state


def _config(**overrides) -> AppConfig:
    base = AppConfig(nats_url="nats://test:4222", gates={})
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _controller(opener: FakeOpener | None = None, *, gates=None,
                **overrides) -> tuple[GateController, MagicMock]:
    """A controller with one gate on ``cam1``, wired to ``opener``."""
    dispatcher = MagicMock()
    ctl = GateController(_config(gates=gates or {}, **overrides), dispatcher)
    if opener is not None:
        gate = gc.Gate(camera_id="cam1", name="Main Gate", opener=opener)
        gate.monitored = opener.read_state() is not None
        ctl._gates["cam1"] = gate
    return ctl, dispatcher


def _decision(decision="allow", *, camera="cam1", plate="MH12DE1433",
              reason="registered", schema="access.decided.v1",
              confidence=0.9, **extra):
    env = {
        "id": "evt_0123456789ab",
        "schema": schema,
        "correlation_id": "corr-1",
        "camera_id": camera,
        "ts": "2026-08-30T10:00:00+00:00",
        "producer": "app:license-plate-recognition",
        "payload": {"plate_text": plate, "decision": decision, "reason": reason,
                    "owner": "A. Sharma", "unit": "B-402",
                    "confidence": confidence},
    }
    env.update(extra)
    return env


class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


# ── Fail closed ─────────────────────────────────────────────────────


def _fake_modbus(monkeypatch, *, discrete: int = 0, exception_code: int = 0,
                 wrong_tid: bool = False, bad_length: bool = False,
                 truncate: bool = False, fail_after: int | None = None):
    """A Modbus TCP peer that answers correctly — or in one specific
    broken way. Returns the list of request frames it was sent.

    Written out rather than mocked at the method boundary because the
    bytes ARE the contract here: an earlier version of this test echoed
    the MBAP header back as the body and passed anyway, which meant the
    whole response-parsing path was untested.
    """
    sent: list[bytes] = []

    class _Sock:
        def __init__(self):
            self._reply = b""
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def settimeout(self, _t): pass

        def sendall(self, data):
            sent.append(data)
            if fail_after is not None and len(sent) > fail_after:
                raise OSError("connection refused")
            tid, _proto, _len, unit, fc = struct.unpack(">HHHBB", data[:8])
            if exception_code:
                pdu = struct.pack(">BB", fc | 0x80, exception_code)
            elif fc == 0x02:
                pdu = struct.pack(">BBB", fc, 1, discrete)
            else:
                pdu = data[7:]                     # echo the request PDU
            if wrong_tid:
                tid = (tid + 1) & 0xFFFF
            length = 0xFFFF if bad_length else len(pdu) + 1
            self._reply = struct.pack(">HHHB", tid, 0, length, unit) + pdu

        def recv(self, count):
            if truncate:
                return b""
            chunk, self._reply = self._reply[:count], self._reply[count:]
            return chunk

    monkeypatch.setattr(transports.socket, "create_connection",
                        lambda *a, **kw: _Sock())
    return sent


class TestFailClosed:

    @pytest.mark.parametrize("decision", ["deny", "DENY", "maybe", "", None,
                                          "allow_with_escort", "quarantine"])
    def test_anything_but_allow_actuates_nothing(self, decision):
        """Rule 1. Includes decision values no producer has invented
        yet — an unknown future value must never lift a boom."""
        opener = FakeOpener()
        ctl, dispatcher = _controller(opener)
        assert ctl.handle_event(_decision(decision)) == []
        assert opener.pulses == 0
        dispatcher.fire.assert_not_called()
        assert ctl.state_snapshot()["today"]["denied"] == 1

    def test_foreign_and_malformed_events_are_ignored(self):
        opener = FakeOpener()
        ctl, _ = _controller(opener)
        for event in ({"schema": "alert.v1"}, {}, None, "nope", 42,
                      _decision(schema="access.decided.v2"),
                      {"schema": "access.decided.v1", "camera_id": "cam1"},
                      {"schema": "access.decided.v1", "payload": {}}):
            assert ctl.handle_event(event) == []
        assert opener.pulses == 0

    def test_allow_at_an_unwired_camera_is_recorded_not_alerted(self):
        ctl, dispatcher = _controller()
        assert ctl.handle_event(_decision(camera="cam9")) == []
        dispatcher.fire.assert_not_called()
        state = ctl.state_snapshot()
        assert state["needs_wiring"] == ["cam9"]
        assert state["events"][0]["action"] == "no_relay"


# ── The happy path, and the fault path ──────────────────────────────


class TestActuation:

    def test_allow_pulses_and_alerts_low(self):
        opener = FakeOpener()
        ctl, dispatcher = _controller(opener)
        fired = ctl.handle_event(_decision())
        assert opener.pulses == 1
        assert fired[0].severity == "low"
        assert "MH12DE1433" in fired[0].title
        assert fired[0].evidence["transport"] == "fake"
        assert dispatcher.fire.call_count == 1
        assert ctl.state_snapshot()["today"]["opened"] == 1

    def test_fault_alerts_high_and_is_retriable_by_the_next_car(self):
        """Rule 2. The cooldown must NOT start on a failed open, or one
        bad pulse locks the gate for the whole cooldown."""
        opener = FakeOpener(fail=True)
        ctl, _ = _controller(opener, pulse_cooldown_seconds=30.0)
        fired = ctl.handle_event(_decision())
        assert fired[0].severity == "high"
        assert "did NOT open" in fired[0].title
        snapshot = ctl.state_snapshot()
        assert snapshot["today"]["faults"] == 1
        assert snapshot["gates"][0]["state"] == "faulted"
        assert snapshot["gates"][0]["fault_note"]

        opener.fail = False
        fired = ctl.handle_event(_decision(plate="MH14XY0001"))
        assert opener.pulses == 1 and fired[0].severity == "low"
        assert ctl.state_snapshot()["gates"][0]["state"] == "open"

    def test_cooldown_is_one_car_one_pulse(self):
        opener = FakeOpener()
        ctl, _ = _controller(opener, pulse_cooldown_seconds=30.0)
        ctl.handle_event(_decision())
        assert ctl.handle_event(_decision()) == []
        assert opener.pulses == 1
        assert ctl.state_snapshot()["events"][0]["action"] == "cooldown"

    def test_dry_run_records_without_touching_the_wiring(self):
        opener = FakeOpener(fail=True)      # would fault if it were called
        ctl, dispatcher = _controller(opener, dry_run=True)
        fired = ctl.handle_event(_decision())
        assert opener.pulses == 0
        assert fired[0].severity == "low" and "[dry run]" in fired[0].title
        assert dispatcher.fire.call_count == 1

    def test_an_unexpected_wiring_error_is_a_fault_not_a_crash(self):
        opener = FakeOpener()
        opener.pulse = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        ctl, _ = _controller(opener)
        fired = ctl.handle_event(_decision())
        assert fired[0].severity == "high"
        assert "boom" in fired[0].description


# ── Local guards: the seam a plate-only credential needs ────────────


class TestLocalGuards:

    def test_confidence_floor_refuses_a_low_read(self):
        opener = FakeOpener()
        ctl, _ = _controller(opener, min_confidence=0.85)
        fired = ctl.handle_event(_decision(confidence=0.6))
        assert opener.pulses == 0
        assert fired[0].evidence["alert_type"] == "barrier_refused"
        assert "60%" in fired[0].description and "85%" in fired[0].description

    def test_confidence_floor_passes_a_good_read(self):
        opener = FakeOpener()
        ctl, _ = _controller(opener, min_confidence=0.85)
        ctl.handle_event(_decision(confidence=0.95))
        assert opener.pulses == 1

    def test_a_missing_confidence_is_not_treated_as_zero(self):
        """An older producer that sends no confidence must not be
        silently locked out by a floor meant for weak reads."""
        opener = FakeOpener()
        ctl, _ = _controller(opener, min_confidence=0.85)
        ctl.handle_event(_decision(confidence=None))
        assert opener.pulses == 1

    def test_allowed_reasons_narrows_what_lifts_the_boom(self):
        opener = FakeOpener()
        ctl, _ = _controller(opener, allowed_reasons=["registered"])
        ctl.handle_event(_decision(reason="allowlisted"))
        assert opener.pulses == 0
        ctl.handle_event(_decision(reason="registered"))
        assert opener.pulses == 1

    def test_repeat_plate_guard_catches_a_copied_plate(self):
        opener = FakeOpener()
        ctl, _ = _controller(opener, max_opens_per_plate=2,
                             max_opens_window_minutes=10.0,
                             pulse_cooldown_seconds=0.0)
        for _ in range(2):
            ctl.handle_event(_decision())
        assert opener.pulses == 2
        fired = ctl.handle_event(_decision())
        assert opener.pulses == 2
        assert "copied" in fired[0].description
        # A different plate is unaffected.
        ctl.handle_event(_decision(plate="MH01AA0001"))
        assert opener.pulses == 3


# ── Holds ───────────────────────────────────────────────────────────


class TestHolds:

    def test_a_momentary_wiring_is_never_faked_into_a_hold(self):
        """Rule 3. Re-pulsing a momentary input to simulate a hold is
        how a barrier closes on a car."""
        opener = FakeOpener(can_hold=False)
        ctl, _ = _controller(opener)
        result = ctl.on_action("hold_open", {"camera": "cam1", "minutes": 15})
        assert result["ok"] is False and "momentary" in result["error"]
        assert opener.pulses == 0 and opener.holds == []

    def test_latching_wiring_holds_and_releases(self):
        opener = FakeOpener(can_hold=True)
        ctl, _ = _controller(opener)
        result = ctl.on_action("hold_open", {"camera": "cam1", "minutes": 15})
        assert result["ok"] and opener.holds == [True]
        gate = ctl.state_snapshot()["gates"][0]
        assert gate["state"] == "held" and gate["held_until"]

        assert ctl.on_action("release_hold", {"camera": "cam1"})["ok"]
        assert opener.holds == [True, False]
        assert ctl.state_snapshot()["gates"][0]["state"] == "closed"

    def test_a_held_gate_does_not_re_pulse_for_every_car(self):
        opener = FakeOpener(can_hold=True)
        ctl, _ = _controller(opener)
        ctl.on_action("hold_open", {"camera": "cam1", "minutes": 15})
        ctl.handle_event(_decision())
        assert opener.pulses == 0
        assert ctl.state_snapshot()["events"][0]["note"] == \
            "Gate is already held open."

    def test_hold_expires_on_the_tick(self):
        opener = FakeOpener(can_hold=True)
        ctl, _ = _controller(opener)
        ctl.on_action("hold_open", {"camera": "cam1", "minutes": 15})
        ctl.tick(time.time() + 16 * 60)
        assert opener.holds == [True, False]
        assert ctl.state_snapshot()["gates"][0]["state"] == "closed"

    def test_until_released_is_still_capped(self):
        """minutes=0 means "until released" from the page — but a gate
        nobody releases must not stand open all night."""
        opener = FakeOpener(can_hold=True)
        ctl, _ = _controller(opener, max_hold_minutes=30.0)
        ctl.on_action("hold_open", {"camera": "cam1", "minutes": 0})
        held_until = ctl.state_snapshot()["gates"][0]["held_until"]
        assert held_until is not None
        assert held_until - time.time() <= 30 * 60 + 1

    def test_max_hold_zero_disables_holding_entirely(self):
        opener = FakeOpener(can_hold=True)
        ctl, _ = _controller(opener, max_hold_minutes=0)
        ctl.on_action("hold_open", {"camera": "cam1", "minutes": 15})
        assert opener.holds == []

    def test_a_latch_that_refuses_is_a_fault(self):
        opener = FakeOpener(can_hold=True, fail=True)
        ctl, _ = _controller(opener)
        ctl.on_action("hold_open", {"camera": "cam1", "minutes": 15})
        assert ctl.state_snapshot()["gates"][0]["state"] == "faulted"


# ── Hold-open schedules ─────────────────────────────────────────────


class TestSchedules:

    def test_parses_days_groups_and_lists(self):
        windows = parse_schedule([
            {"start": "08:00", "end": "09:00", "days": "weekdays"},
            {"start": "7:30", "end": "8:00", "days": "sat,sun"},
            {"start": "10:00", "end": "11:00", "days": ["mon", "wed"]},
        ])
        assert [w.days for w in windows] == [[0, 1, 2, 3, 4], [5, 6], [0, 2]]
        assert windows[1].start == 7 * 60 + 30

    def test_a_malformed_window_is_skipped_not_fatal(self):
        windows = parse_schedule([{"start": "nope", "end": "09:00"},
                                  {"start": "08:00", "end": "09:00"}])
        assert len(windows) == 1

    def test_window_spanning_midnight(self):
        window = HoldWindow(22 * 60, 6 * 60, [0])      # Mon 22:00 → 06:00
        assert window.active_at(datetime(2026, 9, 21, 23, 0))   # Mon night
        assert window.active_at(datetime(2026, 9, 22, 5, 0))    # Tue morning
        assert not window.active_at(datetime(2026, 9, 22, 7, 0))

    def test_schedule_holds_the_gate_then_releases_it(self):
        opener = FakeOpener(can_hold=True)
        ctl, _ = _controller(opener)
        ctl._gates["cam1"].schedule = parse_schedule(
            [{"start": "08:00", "end": "09:00", "days": "daily"}])
        ctl.tick(datetime(2026, 9, 21, 8, 30).timestamp())
        gate = ctl.state_snapshot()["gates"][0]
        assert gate["state"] == "held" and "08:00–09:00" in gate["hold_reason"]

        ctl.tick(datetime(2026, 9, 21, 9, 30).timestamp())
        assert ctl.state_snapshot()["gates"][0]["state"] == "closed"
        assert opener.holds == [True, False]


class TestScheduleDoesNotSpam:
    """The tick runs once a second. Anything it reports unconditionally
    is 3,600 rows an hour — enough to evict every real event from the
    log and drown the page."""

    def _scheduled(self, opener):
        ctl, _ = _controller(opener)
        ctl._gates["cam1"].schedule = parse_schedule(
            [{"start": "08:00", "end": "09:00", "days": "daily"}])
        return ctl

    def test_a_window_on_momentary_wiring_is_reported_once(self):
        opener = FakeOpener(can_hold=False)
        ctl = self._scheduled(opener)
        base = datetime(2026, 9, 21, 8, 30).timestamp()
        for offset in range(10):
            ctl.tick(base + offset)
        assert len(ctl.state_snapshot()["events"]) == 1

    def test_a_latch_that_refuses_faults_once_per_window(self):
        opener = FakeOpener(can_hold=True, fail=True)
        ctl = self._scheduled(opener)
        base = datetime(2026, 9, 21, 8, 30).timestamp()
        for offset in range(10):
            ctl.tick(base + offset)
        state = ctl.state_snapshot()
        assert state["today"]["faults"] == 1
        assert sum(1 for e in state["events"] if e["action"] == "fault") == 1

    def test_the_next_window_is_tried_again(self):
        """Reported once per window, not once ever — tomorrow's rush
        must still be attempted."""
        opener = FakeOpener(can_hold=False)
        ctl = self._scheduled(opener)
        ctl.tick(datetime(2026, 9, 21, 8, 30).timestamp())
        ctl.tick(datetime(2026, 9, 21, 10, 0).timestamp())     # window over
        ctl.tick(datetime(2026, 9, 22, 8, 30).timestamp())     # next day
        assert len(ctl.state_snapshot()["events"]) == 2


class TestScheduledHoldKeepsItsPromise:

    def test_a_window_longer_than_the_cap_is_not_silently_cut(self):
        """max_hold_minutes is a backstop for "until released", not a
        limit on a window the operator configured. A four-hour delivery
        window must not become two — and, more importantly, the page
        must not display an end time the gate will not honour."""
        opener = FakeOpener(can_hold=True)
        ctl, _ = _controller(opener, max_hold_minutes=30.0)
        ctl._gates["cam1"].schedule = parse_schedule(
            [{"start": "08:00", "end": "12:00", "days": "daily"}])
        base = datetime(2026, 9, 21, 8, 30).timestamp()
        ctl.tick(base)
        gate = ctl.state_snapshot()["gates"][0]
        assert gate["state"] == "held"
        assert (gate["held_until"] - base) / 60 == pytest.approx(210, abs=1)

        # Still held well past the cap, because the window says so.
        ctl.tick(base + 45 * 60)
        assert ctl.state_snapshot()["gates"][0]["state"] == "held"
        # And released when the window actually ends.
        ctl.tick(datetime(2026, 9, 21, 12, 30).timestamp())
        assert ctl.state_snapshot()["gates"][0]["state"] == "closed"

    def test_minutes_left_handles_a_window_across_midnight(self):
        window = HoldWindow(22 * 60, 6 * 60, [0])       # Mon 22:00 → 06:00
        assert window.minutes_left(datetime(2026, 9, 21, 23, 0)) == pytest.approx(420)
        assert window.minutes_left(datetime(2026, 9, 22, 5, 0)) == pytest.approx(60)


# ── Knowing vs assuming ─────────────────────────────────────────────


class TestGateState:

    def test_an_unmonitored_gate_says_so_and_settles_closed(self):
        """Rule 4. No limit switch means no truth about the barrier —
        the page must be told, and 'open' must not stick forever."""
        opener = FakeOpener(state=None)
        ctl, _ = _controller(opener, assumed_open_seconds=20.0)
        assert ctl.state_snapshot()["gates"][0]["monitored"] is False
        ctl.handle_event(_decision())
        assert ctl.state_snapshot()["gates"][0]["state"] == "open"
        ctl.tick(time.time() + 25)
        assert ctl.state_snapshot()["gates"][0]["state"] == "closed"

    def test_a_monitored_gate_reports_the_real_position(self):
        opener = FakeOpener(state="closed")
        ctl, _ = _controller(opener)
        assert ctl.state_snapshot()["gates"][0]["monitored"] is True
        opener._state = "open"
        ctl.poll_states()
        assert ctl.state_snapshot()["gates"][0]["state"] == "open"
        opener._state = "closed"
        ctl.poll_states()
        assert ctl.state_snapshot()["gates"][0]["state"] == "closed"

    def test_a_fault_is_not_cleared_by_a_closed_reading(self):
        """A barrier that failed to open reads 'closed' — which is
        exactly the reading that must not look like all is well."""
        opener = FakeOpener(state="closed", fail=True)
        ctl, _ = _controller(opener)
        ctl.handle_event(_decision())
        assert ctl.state_snapshot()["gates"][0]["state"] == "faulted"
        ctl.poll_states()
        assert ctl.state_snapshot()["gates"][0]["state"] == "faulted"


class TestFailuresDoNotWedgeTheGate:
    """Every one of these was a real defect. A barrier that cannot be
    opened, or one that stays up while the page says otherwise, is worse
    than an app that crashes — nobody gets paged for a quiet lie."""

    def test_a_failed_release_clears_the_hold_instead_of_looping(self):
        """The hold state used to survive a failed release, so the tick
        retried every second — a high-severity alert per second forever
        — while handle_event still saw "held" and stopped pulsing for
        arriving cars. The gate became unopenable until a restart."""
        opener = FakeOpener(can_hold=True)
        ctl, _ = _controller(opener)
        ctl.on_action("hold_open", {"camera": "cam1", "minutes": 1})
        opener.fail = True                       # the relay drops off the net
        base = time.time() + 120
        for offset in range(5):
            ctl.tick(base + offset)

        state = ctl.state_snapshot()
        assert sum(1 for e in state["events"] if e["action"] == "fault") == 1
        assert state["gates"][0]["held_until"] is None
        assert state["gates"][0]["state"] == "faulted"
        assert state["today"]["faults"] == 1     # and it is actually counted

    def test_a_gate_whose_release_failed_can_still_be_opened(self):
        opener = FakeOpener(can_hold=True)
        ctl, _ = _controller(opener)
        ctl.on_action("hold_open", {"camera": "cam1", "minutes": 1})
        opener.fail = True
        ctl.tick(time.time() + 120)
        opener.fail = False
        ctl.handle_event(_decision())
        assert opener.pulses == 1                # not swallowed as "held"

    def test_a_rewire_does_not_strand_a_latched_gate(self):
        """on_config_update used to carry only four fields across, so a
        held gate came back with state='held' and held_until=None: no
        tick could expire it and Release returned ok without ever
        sending the release, while the latch stayed engaged."""
        ctl, _ = _controller(gates={"1": {"profile": "shelly_gen2",
                                          "host": "10.0.0.5"}})
        opener = FakeOpener(can_hold=True)
        ctl._gates["cam1"].opener = opener
        ctl.on_action("hold_open", {"camera": "cam1", "minutes": 60})
        assert opener.holds == [True]

        ctl.on_config_update({"gates": {"1": {"profile": "shelly_gen2",
                                              "host": "10.0.0.5"}}})
        gate = ctl.state_snapshot()["gates"][0]
        assert gate["state"] == "held" and gate["held_until"] is not None
        assert gate["hold_reason"]

    def test_a_rewire_closes_the_openers_it_replaced(self):
        closed: list[str] = []

        class _Tracking(FakeOpener):
            def close(self):
                closed.append("yes")

        ctl, _ = _controller(gates={"1": "http://old/open"})
        ctl._gates["cam1"].opener = _Tracking()
        ctl.on_config_update({"gates": {"1": "http://new/open"}})
        assert closed == ["yes"]                 # sockets/GPIO lines released

    @pytest.mark.parametrize("minutes", [float("nan"), float("inf"),
                                         float("-inf"), -5, "nonsense", None])
    def test_a_nonsense_hold_length_cannot_latch_the_gate_forever(self, minutes):
        """min(nan, cap) is nan, so held_until became nan, now >= nan was
        never true, and the gate latched open with an expiry that could
        not be reached — then crashed formatting it."""
        opener = FakeOpener(can_hold=True)
        ctl, _ = _controller(opener)
        result = ctl.on_action("hold_open", {"camera": "cam1",
                                             "minutes": minutes})
        assert result["ok"] is True
        held_until = ctl.state_snapshot()["gates"][0]["held_until"]
        assert held_until is not None and held_until == held_until   # not NaN
        assert 0 < held_until - time.time() <= 120 * 60
        ctl.tick(held_until + 1)                 # and it really does expire
        assert ctl.state_snapshot()["gates"][0]["state"] == "closed"

    def test_the_repeat_plate_memory_is_forgotten_after_its_window(self):
        """Pruning only ran inside the guard, for the one plate being
        checked — so with the guard off (the default) every plate ever
        admitted stayed in memory for the life of the process."""
        opener = FakeOpener()
        ctl, _ = _controller(opener, pulse_cooldown_seconds=0)
        gate = ctl._gates["cam1"]
        stale = time.monotonic() - 86400
        for index in range(500):
            gate.recent_plates[f"OLD{index:04d}"] = [stale]
        ctl.handle_event(_decision(plate="MH99ZZ9999"))
        assert len(gate.recent_plates) == 1

    def test_the_plate_memory_is_capped_inside_a_single_window(self):
        opener = FakeOpener()
        ctl, _ = _controller(opener)
        gate = ctl._gates["cam1"]
        now = time.monotonic()
        for index in range(gc._MAX_TRACKED_PLATES + 500):
            gate.recent_plates[f"P{index:05d}"] = [now]
        ctl._prune_plates(gate)
        assert len(gate.recent_plates) == gc._MAX_TRACKED_PLATES

    def test_a_contact_that_will_not_release_says_stuck_open(self):
        """The opposite failure from "did not open": the barrier is
        probably UP and staying up, not down with a car waiting, and the
        two need different things done about them."""
        opener = FakeOpener()
        opener.pulse = lambda: (_ for _ in ()).throw(
            transports.StuckClosed("contact closed but did not release"))
        ctl, _ = _controller(opener)
        fired = ctl.handle_event(_decision())
        assert fired[0].severity == "high"
        assert "STUCK OPEN" in fired[0].title
        assert fired[0].evidence["stuck_open"] is True

    def test_a_monitored_gate_does_not_sit_in_opening_forever(self):
        """OPENING is left via poll_states. A status endpoint that
        starts answering None (a 500, a reboot) left the gate OPENING
        permanently, counted as open by the page and by Home Assistant."""
        opener = FakeOpener(state="closed")
        ctl, _ = _controller(opener, assumed_open_seconds=20.0)
        ctl.handle_event(_decision())
        assert ctl.state_snapshot()["gates"][0]["state"] == "opening"
        opener._state = None                     # the probe goes dark
        ctl.poll_states()
        ctl.tick(time.time() + 25)
        assert ctl.state_snapshot()["gates"][0]["state"] == "closed"

    def test_a_second_car_restarts_the_settle_timer(self):
        """set_state is a no-op when the state is unchanged, so a second
        pulse into an already-open unmonitored gate left `since` at the
        first one and the display snapped to closed seconds after a real
        open."""
        opener = FakeOpener()
        ctl, _ = _controller(opener, assumed_open_seconds=20.0,
                             pulse_cooldown_seconds=0)
        start = time.time()
        ctl.handle_event(_decision())
        first_since = ctl.state_snapshot()["gates"][0]["since"]
        time.sleep(0.01)
        ctl.handle_event(_decision(plate="MH01AA0001"))
        assert ctl.state_snapshot()["gates"][0]["since"] > first_since
        assert ctl.state_snapshot()["gates"][0]["state"] == "open"


# ── Operator actions ────────────────────────────────────────────────


class TestActions:

    def test_open_now_pulses_and_is_attributed(self):
        opener = FakeOpener()
        ctl, _ = _controller(opener)
        assert ctl.on_action("open_now",
                             {"camera": "cam1", "by": "R. Kulkarni"})["ok"]
        assert opener.pulses == 1
        row = ctl.state_snapshot()["events"][0]
        assert row["action"] == "opened" and row["by"] == "R. Kulkarni"
        assert ctl.state_snapshot()["today"]["manual"] == 1

    def test_test_pulse_is_recorded_as_a_test_not_an_entry(self):
        opener = FakeOpener()
        ctl, _ = _controller(opener)
        ctl.on_action("test", {"camera": "cam1"})
        assert opener.pulses == 1
        state = ctl.state_snapshot()
        assert state["events"][0]["action"] == "test"
        assert state["today"]["opened"] == 0      # a test is not an admission

    def test_actions_on_a_missing_or_unwired_gate_answer_clearly(self):
        ctl, _ = _controller()
        assert ctl.on_action("open_now", {"camera": "cam7"})["ok"] is False
        ctl._gates["cam2"] = gc.Gate(camera_id="cam2", name="Rear", opener=None)
        ctl._wiring_errors["cam2"] = "unknown profile 'shelli'"
        result = ctl.on_action("open_now", {"camera": "cam2"})
        assert result["ok"] is False and "shelli" in result["error"]

    def test_unknown_action_is_refused(self):
        ctl, _ = _controller(FakeOpener())
        assert ctl.on_action("detonate", {"camera": "cam1"})["ok"] is False

    def test_camera_key_accepts_core_id_or_handle(self):
        opener = FakeOpener()
        ctl, _ = _controller(opener)
        assert ctl.on_action("open_now", {"camera": "1"})["ok"]
        assert opener.pulses == 1


# ── Wiring: profiles and transports ─────────────────────────────────


class TestWiring:

    def test_a_bare_url_still_works(self):
        """The 1.0 config shape — every value a URL — must keep loading."""
        opener = build_opener("http://192.168.1.50/relay/0?turn=on")
        assert opener.kind == "http" and opener.address == "192.168.1.50"

    def test_shelly_profile_fills_in_the_urls_and_the_timing(self):
        opener = build_opener({"profile": "shelly_gen2", "host": "10.0.0.5",
                               "channel": 1, "pulse_ms": 800})
        assert opener.kind == "http"
        assert opener.url == ("http://10.0.0.5/rpc/Switch.Set?id=1&on=true"
                              "&toggle_after=1")
        assert opener.can_hold is True          # off_url makes it latching
        assert opener.self_timed is True
        assert opener.vendor == "Shelly (Gen2+ / Plus / Pro)"

    def test_tasmota_channel_is_one_based(self):
        opener = build_opener({"profile": "tasmota", "host": "t.local",
                               "channel": 0})
        assert "Power1%20ON" in opener.url

    def test_modbus_profile_builds_a_coil_writer(self):
        opener = build_opener({"profile": "modbus_tcp", "host": "10.0.4.12",
                               "coil": 3, "sense_input": 2})
        assert opener.kind == "modbus" and opener.coil == 3
        assert "10.0.4.12:502" in opener.address

    def test_onvif_profile_needs_a_door_token(self):
        with pytest.raises(OpenerError, match="door_token"):
            build_opener({"profile": "onvif_door", "host": "10.0.0.9"})
        opener = build_opener({"profile": "onvif_door", "host": "10.0.0.9",
                               "door_token": "Door1", "username": "svc",
                               "password": "x"})
        assert opener.kind == "onvif" and opener.can_hold is True

    def test_unknown_profile_and_unknown_option_say_what_was_meant(self):
        with pytest.raises(OpenerError, match="shelly_gen2"):
            build_opener({"profile": "shelly", "host": "x"})
        with pytest.raises(OpenerError, match="Accepted"):
            build_opener({"transport": "http", "url": "http://x", "tumeout": 5})

    def test_a_profile_missing_its_host_says_which_key(self):
        with pytest.raises(OpenerError, match="host"):
            build_opener({"profile": "shelly_gen2"})

    def test_dry_contact_requires_a_line(self):
        with pytest.raises(OpenerError, match="line"):
            build_opener({"transport": "dry_contact", "chip": "gpiochip0"})

    def test_dry_contact_holds_only_with_a_hold_line(self):
        plain = build_opener({"transport": "dry_contact", "line": 17})
        assert plain.can_hold is False
        latching = build_opener({"transport": "dry_contact", "line": 17,
                                 "hold_line": 27})
        assert latching.can_hold is True

    def test_a_gate_whose_wiring_will_not_build_stays_visible(self):
        """A silently dropped gate is how a site finds out at 3am that
        nothing was ever wired."""
        ctl, _ = _controller(gates={"1": {"profile": "nonesuch", "host": "x"}})
        gate = ctl.state_snapshot()["gates"][0]
        assert gate["transport"] == "none"
        assert "nonesuch" in gate["address"]


class TestHttpOpener:

    def test_pulse_uses_the_relays_own_timer_when_it_has_one(self, monkeypatch):
        calls = []
        monkeypatch.setattr(transports.httpx, "get",
                            lambda url, **kw: calls.append(url) or _Resp())
        build_opener({"profile": "shelly_gen2", "host": "h"}).pulse()
        assert len(calls) == 1           # self-timed: no second off-call

    def test_a_non_2xx_answer_is_a_fault(self, monkeypatch):
        monkeypatch.setattr(transports.httpx, "get", lambda url, **kw: _Resp(503))
        with pytest.raises(OpenerError, match="503"):
            build_opener("http://relay/open").pulse()

    def test_status_url_makes_the_gate_monitored(self, monkeypatch):
        monkeypatch.setattr(transports.httpx, "get",
                            lambda url, **kw: _Resp(200, '{"output":true}'))
        opener = build_opener({"profile": "shelly_gen2", "host": "h"})
        assert opener.read_state() == "open"

    def test_tasmota_is_deliberately_unmonitored(self):
        """Querying Power during an active PulseTime window can defeat
        Tasmota's auto-off (arendst/Tasmota#7810). On a lamp that is a
        curiosity; on a barrier it holds the boom up. So the profile
        ships with no status probe, on purpose."""
        opener = build_opener({"profile": "tasmota", "host": "t.local"})
        assert opener.status_url == ""
        assert opener.read_state() is None

    def test_an_unreachable_relay_is_a_fault_not_a_traceback(self, monkeypatch):
        def boom(*_args, **_kw):
            raise OSError("no route to host")
        monkeypatch.setattr(transports.httpx, "get", boom)
        with pytest.raises(OpenerError, match="did not answer"):
            build_opener("http://relay/open").pulse()


class TestModbusOpener:

    def test_an_unreachable_board_names_the_likely_cause(self, monkeypatch):
        """Inside the shipped compose the apps network is internal and
        the egress proxy is HTTP CONNECT only, so raw TCP has no route.
        That is the overwhelmingly likely cause of this error, and the
        message should say so rather than making somebody bisect it."""
        def boom(*_args, **_kw):
            raise OSError("Network is unreachable")
        monkeypatch.setattr(transports.socket, "create_connection", boom)
        opener = build_opener({"transport": "modbus", "host": "10.0.4.12"})
        with pytest.raises(OpenerError, match="APPS_EGRESS_ENFORCED"):
            opener.pulse()

    def test_an_ordinary_refusal_does_not_get_the_egress_lecture(self, monkeypatch):
        def boom(*_args, **_kw):
            raise ConnectionRefusedError("Connection refused")
        monkeypatch.setattr(transports.socket, "create_connection", boom)
        opener = build_opener({"transport": "modbus", "host": "10.0.4.12"})
        with pytest.raises(OpenerError) as exc:
            opener.pulse()
        assert "APPS_EGRESS_ENFORCED" not in str(exc.value)

    def test_write_single_coil_builds_a_valid_modbus_frame(self, monkeypatch):
        """Function 0x05, 0xFF00 to close and 0x0000 to release, in a
        MBAP header whose length covers unit + PDU."""
        sent = _fake_modbus(monkeypatch)
        build_opener({"transport": "modbus", "host": "h", "coil": 3,
                      "pulse_ms": 20}).pulse()
        assert len(sent) == 2
        for frame, expected in zip(sent, (0xFF00, 0x0000)):
            _tid, proto, length, unit, fc, addr, value = struct.unpack(
                ">HHHBBHH", frame)
            assert proto == 0 and length == len(frame) - 6
            assert unit == 1 and fc == 0x05 and addr == 3 and value == expected

    def test_read_discrete_input_reports_the_barrier_position(self, monkeypatch):
        """The response-parsing path, which nothing exercised before."""
        _fake_modbus(monkeypatch, discrete=1)
        opener = build_opener({"transport": "modbus", "host": "h",
                               "sense_input": 2})
        assert opener.read_state() == "open"
        _fake_modbus(monkeypatch, discrete=0)
        assert build_opener({"transport": "modbus", "host": "h",
                             "sense_input": 2}).read_state() == "closed"

    def test_a_modbus_exception_response_is_a_typed_fault(self, monkeypatch):
        _fake_modbus(monkeypatch, exception_code=2)   # illegal data address
        with pytest.raises(OpenerError, match="Modbus exception 2"):
            build_opener({"transport": "modbus", "host": "h"}).pulse()

    def test_a_mismatched_reply_is_rejected(self, monkeypatch):
        """A frame for somebody else's transaction must not be read as
        an answer to ours."""
        _fake_modbus(monkeypatch, wrong_tid=True)
        with pytest.raises(OpenerError, match="does not match the request"):
            build_opener({"transport": "modbus", "host": "h"}).pulse()

    def test_an_implausible_length_is_refused_not_read(self, monkeypatch):
        """A peer advertising 0xFFFF would otherwise have us block for
        the whole socket timeout reading 65 KB."""
        _fake_modbus(monkeypatch, bad_length=True)
        with pytest.raises(OpenerError, match="implausible"):
            build_opener({"transport": "modbus", "host": "h"}).pulse()

    def test_a_connection_cut_mid_frame_is_a_fault(self, monkeypatch):
        _fake_modbus(monkeypatch, truncate=True)
        with pytest.raises(OpenerError, match="closed mid-frame"):
            build_opener({"transport": "modbus", "host": "h"}).pulse()

    def test_a_release_that_fails_reports_stuck_open(self, monkeypatch):
        """Set succeeded, clear failed: the barrier is probably UP, not
        down with a car waiting."""
        _fake_modbus(monkeypatch, fail_after=1)
        with pytest.raises(transports.StuckClosed, match="held open"):
            build_opener({"transport": "modbus", "host": "h",
                          "pulse_ms": 20}).pulse()


# ── Config ──────────────────────────────────────────────────────────


class TestOneShotUrlIsFlagged:

    def test_a_url_with_no_release_warns(self, caplog):
        """The 1.0 config shape. It must keep working, but a relay that
        latches on and is never turned off is a boom that stays up, so
        the risk has to be visible rather than discovered."""
        import logging
        with caplog.at_level(logging.WARNING):
            build_opener("http://relay/relay/0?turn=on")
        assert "may stay open" in caplog.text

    def test_a_url_carrying_its_own_timer_is_trusted(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            opener = build_opener("http://relay/relay/0?turn=on&timer=1")
        assert opener.self_timed is True
        assert "may stay open" not in caplog.text


class TestBackToBackWindows:

    def test_the_hold_follows_the_new_window(self):
        """08:00-09:00 then 09:00-12:00: the gate stays held, but under
        the SECOND window. Without this the page kept showing the first
        window's label and an end time already in the past."""
        opener = FakeOpener(can_hold=True)
        ctl, _ = _controller(opener)
        ctl._gates["cam1"].schedule = parse_schedule([
            {"start": "08:00", "end": "09:00", "days": "daily"},
            {"start": "09:00", "end": "12:00", "days": "daily"},
        ])
        ctl.tick(datetime(2026, 9, 21, 8, 30).timestamp())
        first = ctl.state_snapshot()["gates"][0]
        assert "08:00–09:00" in first["hold_reason"]

        later = datetime(2026, 9, 21, 9, 30).timestamp()
        ctl.tick(later)
        second = ctl.state_snapshot()["gates"][0]
        assert second["state"] == "held"
        assert "09:00–12:00" in second["hold_reason"]
        assert second["held_until"] > later        # not stranded in the past

    def test_the_absolute_ceiling_bounds_even_a_schedule(self):
        """A 00:00-24:00 window would otherwise hold a barrier open for
        ever, which the module docstring promises it cannot."""
        opener = FakeOpener(can_hold=True)
        ctl, _ = _controller(opener, max_hold_minutes=60.0)
        ctl._gates["cam1"].schedule = parse_schedule(
            [{"start": "00:00", "end": "24:00", "days": "daily"}])
        base = datetime(2026, 9, 21, 1, 0).timestamp()
        ctl.tick(base)
        held_until = ctl.state_snapshot()["gates"][0]["held_until"]
        assert (held_until - base) / 60 <= gc.ABSOLUTE_MAX_HOLD_MINUTES + 1
        ctl.tick(held_until + 1)
        assert ctl.state_snapshot()["gates"][0]["state"] == "closed"


class TestConfig:

    def test_nats_url_is_required(self, tmp_path: Path):
        path = tmp_path / "c.yml"
        path.write_text("gates: {}\n")
        with pytest.raises(ValueError, match="nats_url"):
            load_config(path)

    def test_the_1_0_relays_key_still_loads(self, tmp_path: Path):
        path = tmp_path / "c.yml"
        path.write_text('nats_url: "nats://x:4222"\n'
                        'relays:\n  "1": "http://r/open"\n')
        cfg = load_config(path)
        assert cfg.gates == {"1": "http://r/open"}

    def test_full_config_parses(self, tmp_path: Path):
        path = tmp_path / "c.yml"
        path.write_text(
            'nats_url: "nats://x:4222"\n'
            "gates:\n"
            '  "1":\n'
            '    name: "Main Gate"\n'
            "    profile: shelly_gen2\n"
            '    host: "10.0.0.5"\n'
            "    schedule:\n"
            '      - {start: "08:00", end: "09:00", days: weekdays}\n'
            "min_confidence: 0.8\n"
            "allowed_reasons: [Registered]\n"
            "max_hold_minutes: 45\n")
        cfg = load_config(path)
        assert cfg.min_confidence == 0.8
        assert cfg.allowed_reasons == ["registered"]      # normalised
        assert cfg.max_hold_minutes == 45

    def test_live_config_update_rewires_without_losing_counters(self):
        opener = FakeOpener()
        ctl, _ = _controller(opener)
        ctl.handle_event(_decision())
        assert ctl.state_snapshot()["gates"][0]["opened_today"] == 1
        ctl.on_config_update({"gates": {"1": "http://new-relay/open"},
                              "dry_run": True})
        gate = ctl.state_snapshot()["gates"][0]
        assert gate["transport"] == "http" and gate["opened_today"] == 1
        assert ctl._dry_run is True


# ── The contract the page and Home Assistant read ───────────────────


class TestContractSurface:

    def test_state_snapshot_matches_the_documented_shape(self):
        opener = FakeOpener(can_hold=True, state="closed")
        ctl, _ = _controller(opener)
        ctl.handle_event(_decision())
        state = ctl.state_snapshot()
        assert set(state) >= {"gates", "per_camera", "today", "needs_wiring",
                              "events", "recent", "dry_run", "safety_note",
                              "profiles", "since"}
        assert set(state["today"]) == {"opened", "denied", "faults", "manual",
                                       "open_now"}
        gate = state["gates"][0]
        assert set(gate) >= {"id", "name", "state", "since", "transport",
                             "address", "vendor", "monitored", "can_hold",
                             "held_until", "hold_reason", "schedule_note",
                             "dry_run", "opened_today", "faults_today",
                             "last_event", "fault_note"}
        row = state["events"][0]
        assert set(row) >= {"id", "time", "gate", "gate_name", "plate", "owner",
                            "unit", "decision", "reason", "action", "by",
                            "note", "confidence"}
        assert row["id"]          # the page keys on this

    def test_home_assistant_paths_resolve(self):
        """Every entity state_path must exist in the snapshot, or the
        integration renders an entity that is permanently unavailable."""
        opener = FakeOpener()
        ctl, _ = _controller(opener)
        ctl.handle_event(_decision())
        state = ctl.state_snapshot()
        for entity in gc.MANIFEST.entities:
            path = entity.state_path
            if not path:
                assert entity.action, f"{entity.key} has neither path nor action"
                continue
            if entity.per_camera:
                leaf = path.replace("[camera={camera}]", "").split(".")[-1]
                assert leaf in state["per_camera"]["cam1"], \
                    f"{entity.key}: per_camera.{leaf} missing"
            else:
                node: object = state
                for part in path.split("."):
                    assert isinstance(node, dict) and part in node, \
                        f"{entity.key}: {path} missing"
                    node = node[part]

    def test_every_entity_control_names_a_declared_action(self):
        actions = {a.name for a in gc.MANIFEST.actions}
        for entity in gc.MANIFEST.entities:
            if entity.action:
                assert entity.action in actions, entity.key

    def test_manifest_declares_the_contract(self):
        manifest = gc.MANIFEST
        assert manifest.subscribes == "opennvr.events.access.decided.v1.>"
        assert manifest.requires_scopes == ["events:access.decided"]
        assert manifest.requires_tasks == []       # no inference, ever
        assert manifest.provides == ["gates"]
        assert manifest.camera_picker is False
        assert {a.name for a in manifest.emits} == {
            "barrier_opened", "barrier_fault", "barrier_held_open",
            "barrier_refused"}
        # Anything that moves a barrier asks first.
        for action in manifest.actions:
            if action.name in ("open_now", "hold_open", "test"):
                assert action.confirm is True

    def test_ui_html_renders_without_state(self):
        ctl, _ = _controller()
        assert "No gates wired" in ctl.ui_html()

    def test_counters_reset_on_a_new_day(self):
        opener = FakeOpener()
        ctl, _ = _controller(opener)
        ctl.handle_event(_decision())
        assert ctl.state_snapshot()["today"]["opened"] == 1
        ctl._day = "1999-01-01"
        ctl._roll_day()
        state = ctl.state_snapshot()
        assert state["today"]["opened"] == 0
        assert state["gates"][0]["opened_today"] == 0
