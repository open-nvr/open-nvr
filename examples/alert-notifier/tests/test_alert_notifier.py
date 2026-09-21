# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The app: what gets suppressed, what gets sent, and what it admits.

The unit tests next door prove the pieces. These prove the assembly —
the ORDER of the suppression stack (a pause must beat a rule, a rule
must beat quiet hours), that a failing channel is reported somewhere
other than itself, and that nothing here can silently stop delivering
while reporting healthy.
"""
from __future__ import annotations

import queue
import time
from pathlib import Path

import pytest

import alert_notifier as an
import channels as ch
import routing
from alert_notifier import AlertNotifier, AppConfig, load_config


# ── A channel we can interrogate ────────────────────────────────────


class FakeChannel(ch.Channel):
    kind = "fake"
    can_edit = True
    can_attach = True

    def __init__(self, name, spec):
        super().__init__(name, spec)
        self.sent: list[ch.Message] = []
        self.edited: list[tuple[ch.Message, str]] = []
        self.probes = 0
        self.raise_on_send: Exception | None = None
        self.raise_on_probe: Exception | None = None

    @property
    def address(self):
        return f"fake://{self.name}"

    def send(self, msg):
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append(msg)
        return f"handle-{len(self.sent)}"

    def edit(self, msg, handle):
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.edited.append((msg, handle))

    def probe(self):
        self.probes += 1
        if self.raise_on_probe is not None:
            raise self.raise_on_probe


@pytest.fixture(autouse=True)
def fake_type(monkeypatch):
    monkeypatch.setitem(ch.CHANNEL_TYPES, "fake", FakeChannel)
    monkeypatch.setitem(ch.CHANNEL_FIELDS, "fake", ("note",))


def _config(**over) -> AppConfig:
    base = AppConfig(
        nats_url="nats://test:4222",
        channels={"phone": {"type": "fake"}},
        group_wait_seconds=0.0,
        probe_hours=0.0,
        respect_site_mode=False,
    )
    for key, value in over.items():
        setattr(base, key, value)
    return base


def make(**over) -> AlertNotifier:
    """An app with no background delivery thread, so an assertion never
    races one."""
    notifier = AlertNotifier(_config(**over))
    # The sentinel first, so the worker wakes and exits at once instead
    # of sitting out its poll timeout on every one of these tests.
    notifier._queue.put_nowait(None)
    if notifier._worker is not None:
        notifier._worker.join(timeout=5.0)
    notifier._stopping.set()
    notifier._worker = None
    return notifier


@pytest.fixture()
def app():
    return make()


def alert(severity="high", *, camera="cam1", title="Person at gate", **extra):
    body = {"alert_id": "al_1", "fired_at": "2026-09-21T02:14:03+00:00",
            "title": title, "description": "loitering 8s",
            "severity": severity, "camera_id": camera,
            "correlation_id": "", "evidence": {}, "tags": [],
            "source": {"kind": "app", "name": "intrusion-detection"}}
    body.update(extra)
    return body


def drain(app) -> list[an.Job]:
    """Deliver everything queued, synchronously."""
    jobs = []
    while True:
        try:
            job = app._queue.get_nowait()
        except queue.Empty:
            break
        if job is None:
            continue
        jobs.append(job)
        app._deliver(job)
    return jobs


def chan(app, name="phone") -> FakeChannel:
    return app._channels[name]


# ── The bug that made 1.0 useless ───────────────────────────────────


class TestTheSubject:
    def test_it_subscribes_where_alerts_actually_are(self):
        """1.0 subscribed to opennvr.events.alert.fired.v1.> because the
        contract doc said the SDK dual-publishes there. It does not, and
        nothing in the platform ever has — so 1.0 installed cleanly,
        reported healthy, and delivered zero notifications for ever."""
        assert an.ALERT_SUBJECT_PATTERN == "opennvr.alerts.>"
        assert an.MANIFEST.subscribes == "opennvr.alerts.>"

    def test_the_subject_a_real_alert_is_published_on_matches_it(self):
        """Proven against the SDK's own subject builder rather than a
        string in a doc."""
        from opennvr_app_sdk.alerts import Alert, AlertSource, alert_subject

        subject = alert_subject(Alert(
            title="t", description="d", camera_id="cam1",
            source=AlertSource(kind="app", name="intrusion-detection")))
        prefix = an.ALERT_SUBJECT_PATTERN.rstrip(">")
        assert subject.startswith(prefix), (
            f"{subject} would not reach a subscriber on "
            f"{an.ALERT_SUBJECT_PATTERN}")

    def test_the_domain_subject_would_NOT_have_matched(self):
        """The test that would have caught this in 1.0."""
        from opennvr_app_sdk.alerts import Alert, AlertSource, alert_subject

        subject = alert_subject(Alert(
            title="t", description="d", camera_id="cam1",
            source=AlertSource(kind="app", name="x")))
        assert not subject.startswith("opennvr.events.alert.fired.v1.")


# ── The suppression stack, in order ─────────────────────────────────


class TestSuppression:
    def test_an_alert_above_the_bar_is_delivered(self, app):
        app.on_alert(alert("high"), "opennvr.alerts.app.x.cam1")
        drain(app)
        assert len(chan(app).sent) == 1
        assert chan(app).sent[0].title == "Person at gate"

    def test_below_the_bar_nothing_is_sent(self, app):
        app.on_alert(alert("low"), "s")
        drain(app)
        assert chan(app).sent == []

    def test_the_reason_is_recorded_not_just_the_count(self, app):
        """'Why didn't I get that alert' is the question this product
        exists to answer."""
        app.on_alert(alert("low"), "s")
        assert "below the severity bar" in app._recent[0]["detail"]

    def test_a_pause_beats_a_rule(self, app):
        app.action_mute({"minutes": 30})
        app.on_alert(alert("critical"), "s")
        drain(app)
        assert chan(app).sent == []
        assert "paused" in app._recent[0]["detail"]

    def test_pausing_one_camera_leaves_the_others_alone(self, app):
        app.action_mute({"minutes": 30, "camera": "cam1"})
        app.on_alert(alert(camera="cam1"), "s")
        app.on_alert(alert(camera="cam2"), "s")
        drain(app)
        assert [m.camera for m in chan(app).sent] == ["camera cam2"]

    def test_a_rule_routing_nowhere_sends_nothing_and_says_so(self):
        app = make(rules=[{"name": "ignore vehicles", "to": [],
                           "match": {"sources": ["*plate*"]}}])
        app.on_alert(alert(source={"kind": "app",
                                   "name": "license-plate-recognition"}), "s")
        drain(app)
        assert chan(app).sent == []
        assert "routes nowhere" in app._recent[0]["detail"]

    def test_quiet_hours_hold_rather_than_drop(self):
        app = make(quiet_hours={
            "enabled": True, "windows": [{"from": "00:00", "to": "23:59"}]},
            timezone="UTC")
        app.on_alert(alert("high"), "s")
        drain(app)
        assert chan(app).sent == []
        assert app._held, "a held alert must not be lost"
        assert "held" in app._recent[0]["detail"]

    def test_critical_breaks_through_quiet_hours(self):
        app = make(quiet_hours={
            "enabled": True, "windows": [{"from": "00:00", "to": "23:59"}]},
            timezone="UTC")
        app.on_alert(alert("critical"), "s")
        drain(app)
        assert len(chan(app).sent) == 1

    def test_a_rule_can_opt_out_of_quiet_hours(self):
        """A barrier fault at 2am still has a car sitting at it."""
        app = make(rules=[{"name": "faults", "to": ["phone"],
                           "ignore_quiet_hours": True,
                           "match": {"alert_types": ["barrier_fault"]}},
                          {"name": "rest", "to": ["phone"]}],
                   quiet_hours={"enabled": True,
                                "windows": [{"from": "00:00", "to": "23:59"}]},
                   timezone="UTC")
        app.on_alert(alert("high", alert_type="barrier_fault"), "s")
        drain(app)
        assert len(chan(app).sent) == 1

    def test_a_lesser_alert_behind_a_serious_one_is_hushed(self):
        # The bar is lowered deliberately: at the default "high" the
        # severity floor already drops a medium alert, so inhibition
        # only earns its keep for an operator who WANTS medium alerts
        # but not the three duplicates of each one.
        app = make(inhibit_seconds=60.0, min_severity="medium")
        app.on_alert(alert("critical", title="Intruder"), "s")
        app.on_alert(alert("medium", title="Motion"), "s")
        drain(app)
        assert [m.title for m in chan(app).sent] == ["Intruder"]
        assert any("inhibited" in e["detail"] for e in app._recent)

    def test_inhibition_never_swallows_something_more_serious(self):
        app = make(inhibit_seconds=60.0, min_severity="medium")
        app.on_alert(alert("medium", title="Motion"), "s")
        app.on_alert(alert("critical", title="Intruder"), "s")
        drain(app)
        # A fresh SEND, not an edit: an edit buzzes nobody, so the one
        # alert that mattered would reach a phone that never rings.
        assert "Intruder" in [m.title for m in chan(app).sent]
        assert chan(app).edited == []

    def test_every_suppressed_alert_still_counts_toward_the_backtest(self, app):
        """The rules are tuned against what actually arrived, including
        what was held back."""
        app.on_alert(alert("low"), "s")
        assert app.action_backtest()["alerts_considered"] == 1


class TestSiteMode:
    def test_a_disarmed_site_does_not_buzz(self):
        app = make(respect_site_mode=True)
        app._site_mode = "disarmed"
        app.on_alert(alert("high"), "s")
        drain(app)
        assert chan(app).sent == []
        assert "disarmed" in app._recent[0]["detail"]

    def test_critical_gets_out_even_disarmed(self):
        app = make(respect_site_mode=True)
        app._site_mode = "disarmed"
        app.on_alert(alert("critical"), "s")
        drain(app)
        assert len(chan(app).sent) == 1

    def test_an_armed_site_delivers_normally(self):
        app = make(respect_site_mode=True)
        app._site_mode = "armed_away"
        app.on_alert(alert("high"), "s")
        drain(app)
        assert len(chan(app).sent) == 1

    def test_an_UNKNOWN_site_mode_is_not_treated_as_disarmed(self):
        """A core we cannot reach must never be able to silence the
        alarms. This is the difference between 'quiet because the family
        is home' and 'quiet because a health check failed'."""
        app = make(respect_site_mode=True)
        app._site_mode = ""
        app.on_alert(alert("high"), "s")
        drain(app)
        assert len(chan(app).sent) == 1

    def test_a_core_that_cannot_be_reached_leaves_the_mode_alone(self):
        app = make(respect_site_mode=True)
        app._site_mode = "armed_away"

        class Boom:
            def site_mode(self):
                raise RuntimeError("core down")

            def roster(self):
                return []

        app._nvr = Boom()
        app._nvr_tried = True
        app._site_mode_at = 0.0
        app.refresh_context(now=time.time())
        assert app._site_mode == "armed_away"

    def test_it_can_be_turned_off_entirely(self):
        app = make(respect_site_mode=False)
        app._site_mode = "disarmed"
        app.on_alert(alert("high"), "s")
        drain(app)
        assert len(chan(app).sent) == 1


# ── Grouping through the app ────────────────────────────────────────


class TestGroupingEndToEnd:
    def test_a_burst_becomes_one_notification(self):
        app = make(group_wait_seconds=20.0)
        for title in ("Person", "Motion", "Line crossed"):
            app.on_alert(alert(title=title), "s")
        assert drain(app) == []           # nothing yet — still collapsing
        app.tick(now=time.time() + 30)
        drain(app)
        assert len(chan(app).sent) == 1
        assert chan(app).sent[0].group == 3

    def test_a_later_alert_edits_the_message_instead_of_stacking(self):
        app = make(group_wait_seconds=0.0)
        app.on_alert(alert(title="Person"), "s")
        drain(app)
        app.on_alert(alert(title="Car"), "s")
        drain(app)
        assert len(chan(app).sent) == 1
        assert len(chan(app).edited) == 1
        assert chan(app).edited[0][1] == "handle-1"

    def test_a_channel_that_cannot_edit_sends_a_fresh_message(self,
                                                              monkeypatch):
        """An update this channel cannot make must not be a LOST update."""
        monkeypatch.setattr(FakeChannel, "can_edit", False)
        app = make(group_wait_seconds=0.0)
        app.on_alert(alert(title="Person"), "s")
        drain(app)
        app.on_alert(alert(title="Car"), "s")
        drain(app)
        assert len(chan(app).sent) == 2

    def test_an_edit_that_raises_NotSupported_falls_back_to_a_send(
            self, monkeypatch):
        app = make(group_wait_seconds=0.0)
        app.on_alert(alert(title="Person"), "s")
        drain(app)

        def refuse(self, msg, handle):
            raise ch.NotSupported("no")

        monkeypatch.setattr(FakeChannel, "edit", refuse)
        app.on_alert(alert(title="Car"), "s")
        drain(app)
        assert len(chan(app).sent) == 2

    def test_shutdown_delivers_what_was_still_being_collapsed(self):
        app = make(group_wait_seconds=60.0)
        app.on_alert(alert(), "s")
        assert chan(app).sent == []
        app._shutdown()
        drain(app)
        assert len(chan(app).sent) == 1


# ── Channel health ──────────────────────────────────────────────────


class TestHealth:
    def test_a_fresh_channel_is_unverified_not_healthy(self, app):
        """A channel that has never errored because it has never been
        used is not known-good."""
        state = app.state_snapshot()
        assert state["channels"][0]["state"] == "unverified"
        assert state["health"]["problem"] is True

    def test_HTTP_200_alone_does_not_make_it_confirmed(self, app):
        app.on_alert(alert(), "s")
        drain(app)
        entry = app.state_snapshot()["channels"][0]
        assert entry["state"] == "healthy"
        assert entry["confirmed"] is False
        assert "unconfirmed" in entry["status"]

    def test_a_human_saying_they_got_it_is_what_confirms_it(self, app):
        app.on_alert(alert(), "s")
        drain(app)
        app.action_confirm("")
        assert app.state_snapshot()["channels"][0]["confirmed"] is True

    def test_a_failure_marks_the_channel_failing(self, app):
        chan(app).raise_on_send = ch.AuthFailed("token revoked")
        app.on_alert(alert(), "s")
        drain(app)
        entry = app.state_snapshot()["channels"][0]
        assert entry["state"] == "failing"
        assert "revoked" in entry["last_error"]

    def test_an_auth_failure_is_not_retried(self, app, monkeypatch):
        """A revoked token does not heal, and hammering it is how an
        account gets locked."""
        calls = []
        original = FakeChannel.send

        def counting(self, msg):
            calls.append(msg)
            return original(self, msg)

        monkeypatch.setattr(FakeChannel, "send", counting)
        chan(app).raise_on_send = ch.AuthFailed("nope")
        app.on_alert(alert(), "s")
        drain(app)
        assert len(calls) == 1

    def test_a_transient_failure_IS_retried(self, app, monkeypatch):
        calls = []
        original = FakeChannel.send

        def flaky(self, msg):
            calls.append(msg)
            if len(calls) < 3:
                raise ch.DeliveryError("503")
            return original(self, msg)

        monkeypatch.setattr(FakeChannel, "send", flaky)
        monkeypatch.setattr(an, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
        app._stopping.clear()  # the retry sleep uses this as its timer
        try:
            app.on_alert(alert(), "s")
            drain(app)
        finally:
            app._stopping.set()
        assert len(calls) == 3
        assert app.state_snapshot()["channels"][0]["state"] == "healthy"

    def test_a_misconfigured_channel_is_visible_not_silently_absent(self):
        app = make(channels={"bad": {"type": "telegram", "bot_token": ""}})
        entry = app.state_snapshot()["channels"][0]
        assert entry["state"] == "misconfigured"
        assert "bot_token" in entry["last_error"]

    def test_one_broken_channel_does_not_stop_the_others_loading(self):
        app = make(channels={"bad": {"type": "telegram"},
                             "good": {"type": "fake"}})
        assert "good" in app._channels
        assert "bad" not in app._channels

    def test_a_channel_disabled_in_config_is_not_built(self):
        app = make(channels={"phone": {"type": "fake", "enabled": False}})
        assert app._channels == {}


class TestReportingItsOwnFailure:
    """A notifier reporting its own outage through the broken channel is
    exactly the failure it exists to prevent."""

    @staticmethod
    def _catcher(app):
        fired = []
        app._alerts = type("D", (), {"fire": lambda s, a: fired.append(a)})()
        return fired

    def test_the_failure_is_raised_onto_the_bus(self, app):
        fired = self._catcher(app)
        chan(app).raise_on_send = ch.AuthFailed("token revoked")
        app.on_alert(alert(), "s")
        drain(app)
        assert fired and fired[0].alert_type == "notification_channel_failing"
        assert fired[0].severity == "high"

    def test_it_is_ALSO_announced_on_a_different_healthy_channel(self):
        app = make(channels={"broken": {"type": "fake"},
                             "backup": {"type": "fake"}},
                   rules=[{"name": "only broken", "to": ["broken"]}])
        app._health["backup"].ok(time.time())
        app._channels["broken"].raise_on_send = ch.AuthFailed("revoked")
        app.on_alert(alert(), "s")
        drain(app)
        notices = [m.title for m in app._channels["backup"].sent]
        assert "A notification channel is failing" in notices

    def test_with_no_healthy_channel_left_it_still_raises_the_alert(self, app):
        fired = self._catcher(app)
        chan(app).raise_on_send = ch.AuthFailed("revoked")
        app.on_alert(alert(), "s")
        drain(app)
        assert fired  # the bus path does not depend on any channel

    def test_recovery_is_announced_too(self, app):
        fired = self._catcher(app)
        chan(app).raise_on_send = ch.AuthFailed("revoked")
        app.on_alert(alert(), "s")
        drain(app)
        chan(app).raise_on_send = None
        app.on_alert(alert(title="Another"), "s")
        drain(app)
        assert any(a.alert_type == "notification_channel_recovered"
                   for a in fired)

    def test_a_dispatcher_that_raises_does_not_break_delivery(self, app):
        class Boom:
            def fire(self, alert):
                raise RuntimeError("nats down")

        app._alerts = Boom()
        chan(app).raise_on_send = ch.AuthFailed("revoked")
        app.on_alert(alert(), "s")
        drain(app)  # must not raise


class TestProbing:
    def test_probing_verifies_without_notifying_anyone(self, app):
        assert app.probe_all(force=True) == {"phone": "ok"}
        assert chan(app).probes == 1
        assert chan(app).sent == []

    def test_a_successful_probe_clears_a_failing_state(self, app):
        app._health["phone"].bad("old error", time.time())
        app.probe_all(force=True)
        assert app._health["phone"].state == "healthy"

    def test_a_rejected_credential_found_by_probing_condemns_it(self, app):
        """The whole point: a token revoked on Tuesday is found on
        Tuesday, by a call that wakes nobody."""
        chan(app).raise_on_probe = ch.AuthFailed("401")
        app.probe_all(force=True)
        assert app._health["phone"].state == "failing"

    def test_a_transport_blip_does_NOT_condemn_the_channel(self, app):
        """Or every flaky minute of wifi becomes a 'channel failing'
        alert, and people stop reading those."""
        app._health["phone"].ok(time.time())
        chan(app).raise_on_probe = ch.DeliveryError("connection reset")
        app.probe_all(force=True)
        assert app._health["phone"].state == "healthy"

    def test_probing_off_means_off(self):
        app = make(probe_hours=0.0)
        assert app.probe_all() == {}
        assert chan(app).probes == 0

    def test_probing_respects_its_interval(self):
        app = make(probe_hours=12.0)
        app.probe_all()
        app.probe_all()
        assert chan(app).probes == 1

    def test_a_channel_with_no_silent_check_says_so_rather_than_guessing(
            self, monkeypatch):
        monkeypatch.setattr(FakeChannel, "can_probe", False)
        app = make()
        assert "no silent check" in app.probe_all(force=True)["phone"]


# ── Actions ─────────────────────────────────────────────────────────


class TestActions:
    def test_the_test_uses_a_real_alert_not_a_hello_world(self, app):
        """A test that does not exercise the photo path does not test
        the part that breaks."""
        app.on_alert(alert(title="Person at gate",
                           evidence={"zones": ["Driveway"]}), "s")
        drain(app)
        chan(app).sent.clear()
        result = app.action_test("")
        drain(app)
        assert result["ok"] is True
        assert "most recent alert" in result["using"]
        assert "Person at gate" in chan(app).sent[0].title

    def test_a_test_with_no_history_still_sends_something_honest(self, app):
        result = app.action_test("")
        drain(app)
        assert "synthetic" in result["using"]
        assert chan(app).sent

    def test_a_test_bypasses_dry_run(self):
        """The operator asked for it and is standing there waiting."""
        app = make(dry_run=True)
        app.action_test("")
        drain(app)
        assert chan(app).sent

    def test_a_test_bypasses_a_pause(self, app):
        app.action_mute({"minutes": 60})
        app.action_test("")
        drain(app)
        assert chan(app).sent

    def test_testing_an_unknown_channel_is_an_error_not_a_silent_no_op(
            self, app):
        assert app.action_test("ghost")["ok"] is False

    def test_testing_with_no_channels_says_so(self):
        app = make(channels={})
        assert "no channels" in app.action_test("")["error"]

    def test_a_pause_always_expires(self, app):
        result = app.action_mute({"minutes": 10 ** 9})
        assert result["minutes"] <= routing.MAX_MUTE_MINUTES
        assert "coverage gap" in result["note"]

    def test_a_nan_pause_cannot_latch_the_app_silent(self, app):
        """min(nan, cap) is nan, so an unchecked NaN lands in an expiry
        that can never be reached — 'silent for ever'."""
        result = app.action_mute({"minutes": float("nan")})
        assert result["minutes"] == 60.0
        assert app._muting.active(time.time() + 3601) == {}

    @pytest.mark.parametrize("bad", ["soon", None, -5, 0, float("inf")])
    def test_a_nonsense_pause_length_falls_back(self, app, bad):
        assert app.action_mute({"minutes": bad})["minutes"] == 60.0

    def test_resuming_reports_whether_anything_was_paused(self, app):
        assert app.action_unmute("")["was_paused"] is False
        app.action_mute({"minutes": 5})
        assert app.action_unmute("")["was_paused"] is True

    def test_the_backtest_counts_per_rule(self):
        app = make(rules=[
            {"name": "gate", "to": ["phone"], "match": {"cameras": ["cam1"]}},
            {"name": "rest", "to": ["phone"]}])
        for camera in ("cam1", "cam1", "cam2"):
            app.on_alert(alert(camera=camera), "s")
        result = app.action_backtest()
        assert result["matches"]["gate"] == 2
        assert result["matches"]["rest"] == 1

    def test_the_backtest_is_honest_about_where_its_numbers_came_from(
            self, app):
        assert "since it" in app.action_backtest()["note"]

    def test_check_runs_every_probe_now(self, app):
        assert app.on_action("check", {})["results"] == {"phone": "ok"}

    def test_an_unknown_action_is_refused(self, app):
        assert app.on_action("launch_missiles", {})["ok"] is False

    def test_every_declared_action_is_implemented(self, app):
        """The catalog builds a form for each one; a declared action
        with no handler is a button that 404s."""
        for action in an.MANIFEST.actions:
            result = app.on_action(action.name, {})
            assert isinstance(result, dict)
            assert "unknown action" not in str(result.get("error", ""))


# ── Quiet-hours summary ─────────────────────────────────────────────


class TestQuietSummary:
    @staticmethod
    def _app():
        app = make(quiet_hours={"enabled": True,
                                "windows": [{"from": "00:00", "to": "23:59"}]},
                   timezone="UTC", min_severity="low")
        app._quiet.windows = [routing.TimeWindow([], "00:00", "23:59")]
        return app

    def test_held_alerts_are_delivered_when_the_window_ends(self):
        app = self._app()
        app.on_alert(alert("high", title="Person"), "s")
        app.on_alert(alert("medium", title="Motion"), "s")
        assert chan(app).sent == []
        app._quiet.enabled = False        # the window closes
        app.tick()
        drain(app)
        assert len(chan(app).sent) == 1
        assert chan(app).sent[0].group == 2

    def test_the_summary_leads_with_the_most_serious_one(self):
        app = self._app()
        app.on_alert(alert("low", title="Motion"), "s")
        app.on_alert(alert("high", title="Intruder"), "s")
        app._quiet.enabled = False
        app.tick()
        drain(app)
        assert "Intruder" in chan(app).sent[0].body

    def test_nothing_is_delivered_while_the_window_is_still_open(self):
        app = self._app()
        app.on_alert(alert("high"), "s")
        app.tick()
        drain(app)
        assert chan(app).sent == []
        assert len(app._held) == 1


# ── Config, live ────────────────────────────────────────────────────


class TestConfigUpdate:
    def test_adding_a_channel_applies_without_a_restart(self, app):
        app.on_config_update({"channels": {"phone": {"type": "fake"},
                                           "backup": {"type": "fake"}}})
        assert sorted(app._channels) == ["backup", "phone"]

    def test_removing_a_channel_closes_it_rather_than_leaking_it(
            self, app, monkeypatch):
        closed = []
        monkeypatch.setattr(FakeChannel, "close",
                            lambda self: closed.append(self.name))
        app.on_config_update({"channels": {}})
        assert closed == ["phone"]

    def test_a_live_pause_survives_a_config_change(self, app):
        """A rewire must not silently un-pause a site somebody paused."""
        app.action_mute({"minutes": 60})
        app.on_config_update({"min_severity": "low"})
        assert app._muting.active(time.time())

    def test_channel_health_survives_a_config_change(self, app):
        app.on_alert(alert(), "s")
        drain(app)
        app.action_confirm("phone")
        app.on_config_update({"min_severity": "low"})
        entry = app.state_snapshot()["channels"][0]
        assert entry["confirmed"] is True
        assert entry["delivered"] == 1

    def test_broken_rules_fall_back_rather_than_stopping_delivery(self, app):
        """A rules edit that does not parse must not turn the notifier
        off — it must keep delivering on the simple shape."""
        app.on_config_update({"rules": [{"name": "x", "to": ["ghost"]}]})
        app.on_alert(alert(), "s")
        drain(app)
        assert chan(app).sent

    def test_broken_quiet_hours_fall_back_to_none(self, app):
        app.on_config_update({"quiet_hours": {"mode": "explode"}})
        assert app._quiet.enabled is False
        app.on_alert(alert(), "s")
        drain(app)
        assert chan(app).sent

    def test_zero_means_zero_not_the_default(self, app):
        """A collapse window of 0 means 'send immediately' and an
        inhibit window of 0 means 'off'. Silently restoring the default
        is how a setting appears not to work."""
        app.on_config_update({"group_wait_seconds": 0,
                              "inhibit_seconds": 0, "probe_hours": 0})
        assert app._grouper.wait == 0.0
        assert app._inhibitor.window == 0.0

    def test_a_nonsense_number_falls_back_to_the_default(self, app):
        app.on_config_update({"group_wait_seconds": "soon"})
        assert app._grouper.wait == 20.0


class TestMigration:
    def test_a_1_0_config_still_loads(self, tmp_path: Path):
        path = tmp_path / "config.yml"
        path.write_text(
            "nats_url: nats://x:4222\n"
            "telegram_bot_token: TOK\n"
            "telegram_chat_id: '42'\n"
            "notify_webhook_url: https://example.com/hook\n"
            "min_severity: medium\n")
        cfg = load_config(path)
        assert cfg.channels["telegram"]["bot_token"] == "TOK"
        assert cfg.channels["webhook"]["url"] == "https://example.com/hook"
        assert cfg.min_severity == "medium"

    def test_an_explicit_channel_wins_over_the_old_key(self):
        got = an.migrate_1_0({
            "telegram_bot_token": "OLD", "telegram_chat_id": "1",
            "channels": {"telegram": {"type": "telegram", "bot_token": "NEW",
                                      "chat_id": "2"}}})
        assert got["telegram"]["bot_token"] == "NEW"

    def test_a_half_filled_old_telegram_block_is_not_migrated(self):
        """A token with no chat id cannot deliver; building it would
        just produce a channel that fails."""
        assert an.migrate_1_0({"telegram_bot_token": "TOK"}) == {}

    def test_a_config_with_no_nats_url_is_refused(self, tmp_path: Path):
        path = tmp_path / "config.yml"
        path.write_text("min_severity: high\n")
        with pytest.raises(ValueError, match="nats_url"):
            load_config(path)

    def test_the_shipped_example_config_parses(self):
        cfg = load_config(Path(__file__).resolve().parent.parent
                          / "config.example.yml")
        assert cfg.nats_url


# ── Contract surface ────────────────────────────────────────────────


class TestContractSurface:
    def test_the_manifest_declares_what_the_page_needs(self):
        assert "notifications" in an.MANIFEST.provides
        assert an.MANIFEST.has_ui is True
        assert an.MANIFEST.version == "2.0.0"

    def test_it_asks_for_no_cameras_and_no_inference(self):
        assert an.MANIFEST.camera_picker is False
        assert an.MANIFEST.requires_tasks == []

    def test_every_entity_points_at_something_real(self):
        """An entity whose state_path does not exist shows 'unavailable'
        in Home Assistant for ever."""
        state = make().state_snapshot()
        for entity in an.MANIFEST.entities:
            if not entity.state_path:
                continue
            node = state
            for part in entity.state_path.split("."):
                assert isinstance(node, dict) and part in node, (
                    f"{entity.key}: no {entity.state_path} in state")
                node = node[part]

    def test_every_state_view_points_at_something_real(self):
        state = make().state_snapshot()
        for view in an.MANIFEST.state_schema:
            assert view.path in state, f"{view.name}: no {view.path}"

    def test_every_control_entity_names_a_declared_action(self):
        declared = {a.name for a in an.MANIFEST.actions}
        for entity in an.MANIFEST.entities:
            if entity.platform in ("button", "switch", "select", "number"):
                assert entity.action in declared, entity.key

    def test_the_state_snapshot_is_json_serialisable(self):
        import json

        app = make()
        app.on_alert(alert(), "s")
        json.dumps(app.state_snapshot())

    def test_the_ui_escapes_what_came_off_the_bus(self):
        """An alert title is attacker-influenced in exactly the way a
        plate or a filename is."""
        app = make()
        app.on_alert(alert(title="<script>alert(1)</script>"), "s")
        drain(app)
        page = app.ui_html()
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page

    def test_the_ui_renders_with_nothing_configured(self):
        assert "No channels configured" in make(channels={}).ui_html()

    def test_the_ui_says_when_it_is_paused(self):
        app = make()
        app.action_mute({"minutes": 30})
        assert "paused" in app.ui_html()

    def test_a_dry_run_is_declared_rather_than_looking_like_success(self):
        app = make(dry_run=True)
        app.on_alert(alert(), "s")
        drain(app)
        assert chan(app).sent == []
        assert "Dry run" in app.ui_html()
        assert "dry run" in app._recent[0]["detail"]


class TestTheQueue:
    def test_an_event_storm_drops_the_OLDEST_not_the_newest(self, app):
        """In a storm the most recent alert is the one somebody needs."""
        for n in range(an.QUEUE_DEPTH + 10):
            app._enqueue(an.Job(message=ch.Message(title=f"m{n}"),
                                channels=["phone"], group_key=f"k{n}"))
        assert app._queue.qsize() <= an.QUEUE_DEPTH
        assert app._queued_dropped >= 10
        titles = [j.message.title for j in drain(app)]
        assert f"m{an.QUEUE_DEPTH + 9}" in titles
        assert "m0" not in titles

    def test_delivery_does_not_happen_on_the_decision_path(self, app):
        """Ten channels at an 8s timeout is 80s. Doing that inline would
        stall the NATS subscription behind one unreachable webhook."""
        app.on_alert(alert(), "s")
        assert chan(app).sent == []      # queued, not sent
        assert app._queue.qsize() == 1


# ── Regressions found in review ─────────────────────────────────────


class TestReviewRegressions:
    """One test per defect a review pass found. Each reproduces the
    exact failure, and each fails without its fix."""

    def test_a_config_change_does_not_delete_an_alert_mid_collapse(self):
        """The worst defect in the first draft. Config is applied by
        rebuilding, the Grouper was rebuilt empty, and an alert inside
        a collapse window when the operator saved ANY change ceased to
        exist — not delivered, not suppressed, not logged, with the
        page showing no evidence it had ever arrived."""
        app = make(group_wait_seconds=20.0)
        app.on_alert(alert(), "s")
        app.on_config_update({"base_url": "https://nvr.example.com"})
        app.tick(now=time.time() + 60)
        drain(app)
        assert len(chan(app).sent) == 1

    def test_the_quiet_hours_hold_is_a_promise_that_is_kept(self):
        """Every held alert was logged as "held for the 07:00 summary".
        The list used to be emptied BEFORE checking there was anywhere
        to send it, so a config change that left no usable channel
        deleted the night's alerts and the page then read "0 holding"."""
        app = make(group_wait_seconds=0.0, min_severity="low",
                   timezone="UTC",
                   quiet_hours={"enabled": True,
                                "windows": [{"from": "00:00", "to": "23:59"}]})
        app._quiet.windows = [routing.TimeWindow([], "00:00", "23:59")]
        for n in range(5):
            app.on_alert(alert(title=f"P{n}"), "s")
        assert len(app._held) == 5
        app.on_config_update({"channels": {}})   # an operator's typo
        app._quiet.enabled = False
        app.tick()
        assert len(app._held) == 5, "held alerts must not be deleted"

    def test_the_summary_goes_where_the_RULES_say_not_everywhere(self):
        """An alert the rules sent only to the maintenance webhook was
        broadcast to the family's phones in the morning."""
        app = make(group_wait_seconds=0.0, min_severity="low",
                   timezone="UTC",
                   channels={"family": {"type": "fake"},
                             "maint": {"type": "fake"}},
                   rules=[{"name": "maint only", "to": ["maint"]}],
                   quiet_hours={"enabled": True,
                                "windows": [{"from": "00:00", "to": "23:59"}]})
        app._quiet.windows = [routing.TimeWindow([], "00:00", "23:59")]
        app.on_alert(alert(), "s")
        app._quiet.enabled = False
        app.tick()
        drain(app)
        assert app._channels["family"].sent == []
        assert len(app._channels["maint"].sent) == 1

    def test_a_single_blip_does_not_raise_a_high_severity_outage_alert(self):
        """Firing on the first failure made one 503 from Slack raise an
        alert AND post a notice to another channel — the alarm-fatigue
        pattern this whole app is written against."""
        fired = []
        app = make()
        app._alerts = type("D", (), {"fire": lambda s, a: fired.append(a)})()
        chan(app).raise_on_send = ch.DeliveryError("503")
        app.on_alert(alert(), "s")
        drain(app)
        assert fired == []
        app.on_alert(alert(title="Second"), "s")
        drain(app)
        assert len(fired) == 1
        app.on_alert(alert(title="Third"), "s")
        drain(app)
        assert len(fired) == 1, "and it does not re-spam after that"

    def test_a_rejected_credential_IS_reported_at_once(self):
        """It never heals, so waiting for a second one only delays the
        truth."""
        fired = []
        app = make()
        app._alerts = type("D", (), {"fire": lambda s, a: fired.append(a)})()
        chan(app).raise_on_send = ch.AuthFailed("revoked")
        app.on_alert(alert(), "s")
        drain(app)
        assert len(fired) == 1

    def test_an_UNVERIFIED_backup_still_gets_the_failure_notice(self):
        """Requiring "healthy" meant that on day one — when the
        primary's token is wrong and the backup has never been used —
        the notice went nowhere at all."""
        app = make(channels={"broken": {"type": "fake"},
                             "backup": {"type": "fake"}},
                   rules=[{"name": "only broken", "to": ["broken"]}])
        assert app._health["backup"].state == "unverified"
        app._channels["broken"].raise_on_send = ch.AuthFailed("revoked")
        app.on_alert(alert(), "s")
        drain(app)
        assert app._channels["backup"].sent

    def test_a_removed_channel_leaves_the_page_and_the_problem_sensor(self):
        """Its Health used to survive for ever, so a renamed channel
        held Home Assistant's problem sensor on for something nobody
        could fix because it no longer existed."""
        app = make()
        app.on_alert(alert(), "s")
        drain(app)
        chan(app).raise_on_send = ch.AuthFailed("revoked")
        app.on_alert(alert(title="B"), "s")
        drain(app)
        app.on_config_update({"channels": {"other": {"type": "fake"}}})
        names = [c["name"] for c in app.state_snapshot()["channels"]]
        assert names == ["other"]

    def test_a_MISCONFIGURED_channel_still_does_not_vanish(self):
        """Pruning must not hide the thing it is most important to
        show."""
        app = make()
        app.on_config_update({"channels": {"bad": {"type": "telegram"}}})
        entry = app.state_snapshot()["channels"][0]
        assert entry["name"] == "bad"
        assert entry["state"] == "misconfigured"

    def test_a_declared_catch_all_is_not_widened_to_everything(self):
        """One rule marked catch_all collapses to a single entry, which
        looked identical to "the operator wrote no rules" — and got
        replaced by "everything, to every channel"."""
        app = make(channels={"pager": {"type": "fake"},
                             "family": {"type": "fake"}},
                   rules=[{"name": "Only the pager", "to": ["pager"],
                           "catch_all": True}])
        tail = app._rules.rules[-1]
        assert tail.name == "Only the pager"
        assert tail.channels == ["pager"]

    def test_a_partial_delivery_failure_is_not_logged_as_success(self):
        """A rule routing to the pager and Slack where the pager 401s is
        not a delivered notification, and a green line is how that goes
        unnoticed."""
        app = make(channels={"ok": {"type": "fake"}, "bad": {"type": "fake"}})
        app._channels["bad"].raise_on_send = ch.AuthFailed("nope")
        app.on_alert(alert(), "s")
        drain(app)
        entry = next(e for e in app._recent
                     if e["kind"] in ("delivered", "failed"))
        assert entry["kind"] == "failed"
        assert "ok: sent" in entry["detail"]

    def test_the_backtest_history_does_not_retain_snapshots(self):
        """2000 incidents each holding a 2 MB inline JPEG is gigabytes
        retained to answer a question about metadata."""
        import base64

        jpeg = b"\xff\xd8\xff" + b"\x00" * 1000
        app = make()
        app.on_alert(alert(evidence={
            "snapshot_b64": base64.b64encode(jpeg).decode()}), "s")
        assert app._history[0].image is None
        assert app.action_backtest()["alerts_considered"] == 1

    def test_the_photo_still_reaches_the_channel(self):
        """Dropping it from HISTORY must not drop it from the
        notification."""
        import base64

        jpeg = b"\xff\xd8\xff" + b"\x00" * 1000
        app = make()
        app.on_alert(alert(evidence={
            "snapshot_b64": base64.b64encode(jpeg).decode()}), "s")
        drain(app)
        assert chan(app).sent[0].image == jpeg

    def test_the_hold_is_bounded_and_says_how_many_it_dropped(self):
        app = make(group_wait_seconds=0.0, min_severity="low",
                   timezone="UTC",
                   quiet_hours={"enabled": True,
                                "windows": [{"from": "00:00", "to": "23:59"}]})
        app._quiet.windows = [routing.TimeWindow([], "00:00", "23:59")]
        for n in range(an.MAX_HELD + 25):
            app.on_alert(alert(title=f"P{n}"), "s")
        assert len(app._held) == an.MAX_HELD
        app._quiet.enabled = False
        app.tick()
        drain(app)
        assert "dropped" in chan(app).sent[0].body

    def test_confirming_a_channel_that_does_not_exist_is_refused(self):
        """A typo used to permanently invent a phantom row nobody could
        remove."""
        app = make()
        assert app.action_confirm("typo")["ok"] is False
        assert [c["name"] for c in app.state_snapshot()["channels"]] == ["phone"]

    def test_confirming_does_not_paper_over_a_failing_channel(self):
        app = make()
        chan(app).raise_on_send = ch.AuthFailed("revoked")
        app.on_alert(alert(), "s")
        drain(app)
        result = app.action_confirm("phone")
        assert result["confirmed"] == []
        assert app.state_snapshot()["channels"][0]["state"] == "failing"

    def test_the_daily_counters_roll_at_the_SITE_midnight(self):
        app = make(timezone="Asia/Kolkata")
        assert app._today_key() == routing.local_now(
            "Asia/Kolkata").strftime("%Y-%m-%d")

    def test_a_pause_reports_its_end_in_the_SITE_zone(self):
        app = make(timezone="Asia/Kolkata")
        result = app.action_mute({"minutes": 60})
        assert result["until"] == routing.clock(
            "Asia/Kolkata", time.time() + 3600)


class TestConcurrency:
    """The threading design is the point of the delivery worker, and the
    rest of this file deliberately runs single-threaded. These do not."""

    def test_the_real_worker_delivers_what_the_loop_decides(self):
        app = AlertNotifier(_config(group_wait_seconds=0.0))
        try:
            for n in range(50):
                app.on_alert(alert(title=f"P{n}", camera=f"cam{n}"), "s")
            deadline = time.time() + 10
            while (app._queue.unfinished_tasks
                   and time.time() < deadline):
                time.sleep(0.02)
            assert len(chan(app).sent) == 50
        finally:
            app._shutdown()

    def test_a_snapshot_taken_while_delivering_does_not_explode(self):
        """state_snapshot walks channels, health, rules and the log
        while the worker is mutating all four."""
        import json
        import threading

        app = AlertNotifier(_config(group_wait_seconds=0.0))
        errors: list[Exception] = []

        def reader():
            for _ in range(200):
                try:
                    json.dumps(app.state_snapshot())
                    app.ui_html()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        try:
            thread = threading.Thread(target=reader)
            thread.start()
            for n in range(200):
                app.on_alert(alert(title=f"P{n}", camera=f"cam{n % 7}"), "s")
            thread.join(timeout=30)
            assert errors == []
        finally:
            app._shutdown()

    def test_a_config_change_while_delivering_does_not_explode(self):
        import threading

        app = AlertNotifier(_config(group_wait_seconds=0.0))
        errors: list[Exception] = []

        def rewirer():
            for n in range(60):
                try:
                    app.on_config_update({
                        "channels": {"phone": {"type": "fake"},
                                     f"extra{n % 3}": {"type": "fake"}}})
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        try:
            thread = threading.Thread(target=rewirer)
            thread.start()
            for n in range(200):
                app.on_alert(alert(title=f"P{n}", camera=f"cam{n % 5}"), "s")
            thread.join(timeout=30)
            assert errors == []
        finally:
            app._shutdown()

    def test_shutdown_terminates_even_with_the_worker_gone(self):
        """queue.join() has no timeout: a worker that died — or was
        never started — used to make shutdown hang for ever and the
        process need killing."""
        app = make(group_wait_seconds=0.0)   # make() removes the worker
        app._enqueue(an.Job(message=ch.Message(title="x"),
                            channels=["phone"], group_key="k"))
        started = time.time()
        app._shutdown()
        assert time.time() - started < an.SHUTDOWN_DRAIN_SECONDS + 5
