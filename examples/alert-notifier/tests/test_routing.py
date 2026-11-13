# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Routing, grouping and suppression.

The riskiest code in the app, because every bug here is silent: a rule
that never fires, a window that skips the small hours, an inhibition
that swallows the one alert that mattered. Each of these tests asserts a
behaviour whose absence nobody would notice until the night it counted.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import routing
from routing import (
    Grouper,
    Incident,
    Inhibitor,
    Matcher,
    Muting,
    QuietHours,
    Rule,
    RuleSet,
    TimeWindow,
    parse_incident,
    parse_quiet_hours,
    parse_rules,
)

JPEG = b"\xff\xd8\xff" + b"\x00" * 32


def inc(severity="high", *, camera="cam1", title="Person at gate",
        alert_type="", source="", zones=(), at=1000.0, corr="") -> Incident:
    return Incident(title=title, severity=severity, camera_id=camera,
                    alert_type=alert_type, source=source, zones=list(zones),
                    at=at, correlation_id=corr)


def wire(severity="high", *, camera="cam1", title="Person at gate",
         envelope=False, **extra):
    body = {"alert_id": "al_1", "fired_at": "2026-09-21T02:14:03+00:00",
            "title": title, "description": "desc", "severity": severity,
            "camera_id": camera, "correlation_id": "c1",
            "source": {"kind": "app", "name": "intrusion-detection"},
            "evidence": {}, "tags": []}
    body.update(extra)
    if not envelope:
        return body
    return {"id": "evt_1", "schema": "alert.fired.v1", "camera_id": camera,
            "ts": body["fired_at"], "producer": "app:x", "payload": body}


def at(hour, minute=0, weekday=0):
    """A UTC datetime on a chosen weekday (0 = Monday)."""
    # 2026-09-21 is a Monday.
    return datetime(2026, 9, 21 + weekday, hour, minute, tzinfo=timezone.utc)


# ── Parsing the wire ────────────────────────────────────────────────


class TestParsing:
    def test_both_wire_shapes_are_understood(self):
        """Alerts travel bare on the plumbing subject and wrapped in the
        contracted envelope. Reading both costs three lines and means a
        future domain publisher needs no new app."""
        bare = parse_incident(wire())
        wrapped = parse_incident(wire(envelope=True))
        assert bare.title == wrapped.title == "Person at gate"
        assert bare.severity == wrapped.severity == "high"
        assert bare.camera_id == wrapped.camera_id == "cam1"

    def test_the_source_app_is_extracted_for_routing(self):
        assert parse_incident(wire()).source == "intrusion-detection"

    def test_a_string_source_is_tolerated(self):
        assert parse_incident(wire(source="legacy")).source == "legacy"

    def test_an_alert_type_can_come_from_a_tag(self):
        """Producers that tagged a kind before the column existed."""
        assert parse_incident(
            wire(tags=["type:no_scan"])).alert_type == "no_scan"

    def test_zones_are_read_for_routing(self):
        got = parse_incident(wire(evidence={"zones": ["Driveway", "Porch"]}))
        assert got.zones == ["Driveway", "Porch"]

    def test_a_camera_name_replaces_the_id_when_known(self):
        got = parse_incident(wire(camera="3"), camera_names={"3": "Front Door"})
        assert got.where == "Front Door"

    def test_an_unknown_camera_still_reads_sensibly(self):
        assert parse_incident(wire(camera="9")).where == "camera 9"

    def test_a_garbage_envelope_does_not_raise(self):
        got = parse_incident({})
        assert got.title == "Alert" and got.severity == "low"

    def test_the_dedup_key_follows_the_correlation_id(self):
        """That is what the field is for, and it ties an alert's updates
        together across a restart."""
        first = parse_incident(wire(correlation_id="abc", title="A"))
        second = parse_incident(wire(correlation_id="abc", title="B"))
        assert first.key == second.key

    def test_without_a_correlation_id_camera_and_title_identify_it(self):
        one = parse_incident(wire(correlation_id="", title="Person"))
        two = parse_incident(wire(correlation_id="", title="Person"))
        three = parse_incident(wire(correlation_id="", title="Car"))
        assert one.key == two.key != three.key


class TestInlineImages:
    def test_a_base64_jpeg_is_taken(self):
        import base64
        raw = base64.b64encode(JPEG).decode()
        got = parse_incident(wire(evidence={"snapshot_b64": raw}))
        assert got.image == JPEG

    def test_a_data_url_prefix_is_stripped(self):
        import base64
        raw = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
        assert parse_incident(wire(evidence={"snapshot_b64": raw})).image == JPEG

    def test_something_that_is_not_a_jpeg_is_refused(self):
        """A channel that uploads a non-image gets a 400 at 3am. The
        magic bytes are checked rather than the field name trusted."""
        import base64
        raw = base64.b64encode(b"<html>nope</html>").decode()
        assert parse_incident(wire(evidence={"snapshot_b64": raw})).image is None

    def test_undecodable_base64_is_survived(self):
        assert parse_incident(
            wire(evidence={"snapshot_b64": "!!!not base64!!!"})).image is None

    def test_an_absurdly_large_image_is_refused(self):
        big = "A" * (routing.MAX_INLINE_IMAGE_BYTES * 2 + 10)
        assert parse_incident(wire(evidence={"snapshot_b64": big})).image is None

    def test_an_evidence_path_is_not_mistaken_for_an_image(self):
        """Most apps upload to the evidence store and send a PATH, which
        this app cannot resolve. It must travel without a photo, not
        with a corrupt one."""
        got = parse_incident(wire(evidence={"images": {"scene": "2026/09/a.jpg"}}))
        assert got.image is None


# ── Matching ────────────────────────────────────────────────────────


class TestMatcher:
    def test_an_empty_matcher_matches_everything(self):
        assert Matcher().matches(inc()) is True
        assert Matcher().is_catch_all() is True

    def test_severity_is_a_floor_not_an_equality(self):
        m = Matcher(min_severity="high")
        assert m.matches(inc("critical")) is True
        assert m.matches(inc("high")) is True
        assert m.matches(inc("medium")) is False

    def test_cameras_match_by_id_or_name(self):
        m = Matcher(cameras=["Front Door"])
        got = Incident(camera_id="3", camera_name="Front Door")
        assert m.matches(got) is True
        assert Matcher(cameras=["3"]).matches(got) is True

    def test_a_glob_covers_the_camera_added_next_month(self):
        """Typing eight camera names is how a ninth ends up unwatched."""
        m = Matcher(cameras=["gate-*"])
        assert m.matches(Incident(camera_id="gate-north")) is True
        assert m.matches(Incident(camera_id="lobby")) is False

    def test_matching_ignores_case(self):
        assert Matcher(cameras=["FRONT door"]).matches(
            Incident(camera_name="Front Door")) is True

    def test_zones_match_if_any_zone_does(self):
        m = Matcher(zones=["Driveway"])
        assert m.matches(inc(zones=["Porch", "Driveway"])) is True
        assert m.matches(inc(zones=["Porch"])) is False

    def test_title_contains_searches_the_description_too(self):
        got = Incident(title="Alert", description="plate XX99 on the watchlist")
        assert Matcher(title_contains="watchlist").matches(got) is True

    def test_every_condition_must_hold(self):
        m = Matcher(cameras=["cam1"], min_severity="high")
        assert m.matches(inc("high", camera="cam1")) is True
        assert m.matches(inc("low", camera="cam1")) is False
        assert m.matches(inc("high", camera="cam2")) is False

    def test_a_camera_condition_cannot_match_an_alert_with_no_camera(self):
        assert Matcher(cameras=["*"]).matches(Incident(camera_id="")) is False


# ── Time windows ────────────────────────────────────────────────────


class TestTimeWindow:
    def test_a_plain_window(self):
        w = TimeWindow([], "09:00", "17:00")
        assert w.contains(at(12)) is True
        assert w.contains(at(8)) is False
        assert w.contains(at(17)) is False  # end is exclusive

    def test_crossing_midnight_covers_both_legs(self):
        """The first thing hand-rolled schedules get wrong."""
        w = TimeWindow([], "22:00", "07:00")
        assert w.crosses_midnight is True
        assert w.contains(at(23)) is True
        assert w.contains(at(3)) is True
        assert w.contains(at(12)) is False

    def test_the_day_is_the_day_the_window_STARTED(self):
        """'Monday 22:00-07:00' means Monday night — which is mostly
        Tuesday's small hours. A naive implementation checks today and
        silently skips them."""
        w = TimeWindow(["mon"], "22:00", "07:00")
        assert w.contains(at(23, weekday=0)) is True   # Monday evening
        assert w.contains(at(3, weekday=1)) is True    # Tuesday 03:00
        assert w.contains(at(23, weekday=1)) is False  # Tuesday evening
        assert w.contains(at(3, weekday=2)) is False   # Wednesday 03:00

    def test_days_with_no_clock_means_all_day(self):
        w = TimeWindow(["sat", "sun"], "", "")
        assert w.contains(at(3, weekday=5)) is True   # Saturday
        assert w.contains(at(3, weekday=0)) is False  # Monday

    def test_an_unparseable_clock_is_rejected_at_load(self):
        for bad in ("25:00", "9:70", "noon", "09.00"):
            with pytest.raises(ValueError):
                TimeWindow([], bad, "17:00")

    def test_unknown_days_are_dropped_rather_than_matching_everything(self):
        assert TimeWindow(["mon", "funday"], "", "").days == ["mon"]

    def test_it_describes_itself_for_the_page(self):
        assert "overnight" in TimeWindow([], "22:00", "07:00").describe()


class TestLocalNow:
    def test_an_unknown_timezone_falls_back_rather_than_raising(self):
        """A typo must make the schedule visibly wrong on the page, not
        stop alerts going out."""
        got = routing.local_now("Mars/Olympus", 0.0)
        assert got.tzinfo is not None

    def test_a_real_zone_is_applied(self):
        utc = routing.local_now("UTC", 0.0)
        kolkata = routing.local_now("Asia/Kolkata", 0.0)
        assert (kolkata.hour, kolkata.minute) == (5, 30)
        assert utc.hour == 0


# ── The rule list ───────────────────────────────────────────────────


class TestRuleSet:
    def test_a_catch_all_is_always_present_and_last(self):
        """A hidden default is how an operator ends up unable to answer
        'what happens to everything else?'"""
        rules = RuleSet([Rule("A", ["x"], Matcher(min_severity="critical"))])
        assert rules.rules[-1].catch_all is True
        assert rules.rules[-1].matcher.is_catch_all()

    def test_first_match_wins(self):
        rules = RuleSet([
            Rule("critical", ["pager"], Matcher(min_severity="critical")),
            Rule("rest", ["chat"], Matcher()),
        ])
        assert rules.match(inc("critical")).name == "critical"
        assert rules.match(inc("medium")).name == "rest"

    def test_a_disabled_rule_is_skipped_not_matched(self):
        rules = RuleSet([
            Rule("off", ["pager"], Matcher(), enabled=False),
            Rule("on", ["chat"], Matcher()),
        ])
        assert rules.match(inc()).name == "on"

    def test_an_unmatched_alert_still_lands_on_the_catch_all(self):
        rules = RuleSet([Rule("narrow", ["x"], Matcher(cameras=["nope"]))])
        assert rules.match(inc()).catch_all is True

    def test_a_declared_catch_all_is_widened_not_duplicated(self):
        rules = RuleSet([
            Rule("mine", ["x"], Matcher(cameras=["cam9"]), catch_all=True)])
        assert sum(1 for r in rules.rules if r.catch_all) == 1
        assert rules.rules[-1].matcher.is_catch_all()
        assert rules.rules[-1].channels == ["x"]


class TestShadowing:
    """A rule that can never fire is a coverage gap the operator
    believes they closed. The one affordance a flat list has over a
    tree, and worth more than the tree's expressiveness."""

    def test_a_broad_rule_above_hides_a_narrow_one_below(self):
        rules = RuleSet([
            Rule("everything", ["chat"], Matcher()),
            Rule("gate only", ["pager"], Matcher(cameras=["gate-1"])),
        ])
        assert rules.shadowed() == [(1, 0)]

    def test_a_narrow_rule_above_hides_nothing(self):
        rules = RuleSet([
            Rule("gate only", ["pager"], Matcher(cameras=["gate-1"])),
            Rule("everything", ["chat"], Matcher()),
        ])
        assert rules.shadowed() == []

    def test_a_lower_severity_floor_above_covers_a_higher_one_below(self):
        rules = RuleSet([
            Rule("medium+", ["chat"], Matcher(min_severity="medium")),
            Rule("critical only", ["pager"],
                 Matcher(min_severity="critical")),
        ])
        assert (1, 0) in rules.shadowed()

    def test_a_higher_floor_above_does_not_cover_a_lower_one(self):
        rules = RuleSet([
            Rule("critical", ["pager"], Matcher(min_severity="critical")),
            Rule("medium+", ["chat"], Matcher(min_severity="medium")),
        ])
        assert rules.shadowed() == []

    def test_a_glob_above_covers_the_names_it_matches(self):
        rules = RuleSet([
            Rule("all gates", ["chat"], Matcher(cameras=["gate-*"])),
            Rule("north gate", ["pager"], Matcher(cameras=["gate-north"])),
        ])
        assert (1, 0) in rules.shadowed()

    def test_a_disabled_rule_shadows_nothing(self):
        rules = RuleSet([
            Rule("everything", ["chat"], Matcher(), enabled=False),
            Rule("gate", ["pager"], Matcher(cameras=["gate-1"])),
        ])
        assert rules.shadowed() == []

    def test_the_pinned_catch_all_is_never_badged_as_shadowed(self):
        """A broad rule above it DOES make it unreachable, but it
        cannot be deleted or reordered, so the badge would be a warning
        nobody can act on — and those teach people to ignore warnings.
        Its zero match count says it without the alarm."""
        rules = RuleSet([Rule("everything", ["chat"], Matcher())])
        assert rules.shadowed() == []
        assert rules.rules[-1].catch_all is True


class TestBacktest:
    def test_it_counts_what_each_rule_would_catch(self):
        """The trust mechanism: an operator cannot reason about a
        predicate, but can absolutely reason about 'this would have
        fired 47 times yesterday'."""
        rules = RuleSet([
            Rule("gate", ["pager"], Matcher(cameras=["gate-1"])),
            Rule("rest", ["chat"], Matcher()),
        ])
        history = [inc(camera="gate-1"), inc(camera="gate-1"),
                   inc(camera="lobby")]
        counts = rules.backtest(history)
        assert counts["gate"] == 2
        assert counts["rest"] == 1

    def test_it_runs_the_same_matcher_the_live_path_runs(self):
        """A preview that reimplements the engine eventually disagrees
        with it, and a lying preview is worse than none."""
        rules = RuleSet([Rule("night", ["p"],
                              Matcher(from_time="22:00", to_time="07:00"))])
        night = inc(at=datetime(2026, 9, 21, 23, tzinfo=timezone.utc).timestamp())
        day = inc(at=datetime(2026, 9, 21, 12, tzinfo=timezone.utc).timestamp())
        counts = rules.backtest([night, day], tz_name="UTC")
        assert counts["night"] == 1
        # And the live path agrees on the same two.
        assert rules.match(
            night, local=routing.local_now("UTC", night.at)).name == "night"
        assert rules.match(
            day, local=routing.local_now("UTC", day.at)).catch_all is True


# ── Config parsing ──────────────────────────────────────────────────


class TestParseRules:
    def test_a_rule_naming_a_missing_channel_is_refused(self):
        """Not a warning: a rule pointing at nothing delivers nowhere,
        which is exactly the silent failure this app exists to stop."""
        with pytest.raises(ValueError, match="no such channel"):
            parse_rules([{"name": "x", "to": ["ghost"]}], {"real"})

    def test_duplicate_names_are_refused(self):
        """Names key the backtest counts and the delivery log."""
        with pytest.raises(ValueError, match="duplicate"):
            parse_rules([{"name": "a", "to": []}, {"name": "a", "to": []}],
                        set())

    def test_an_unknown_matcher_key_is_refused(self):
        with pytest.raises(ValueError, match="camers"):
            parse_rules([{"name": "a", "match": {"camers": ["x"]}}], set())

    def test_an_unknown_rule_key_is_refused(self):
        with pytest.raises(ValueError, match="sevrity"):
            parse_rules([{"name": "a", "sevrity": "high"}], set())

    def test_a_bad_severity_names_the_valid_ones(self):
        with pytest.raises(ValueError, match="urgent"):
            parse_rules([{"name": "a", "match": {"min_severity": "urgent"}}],
                        set())

    def test_a_bad_day_names_the_valid_ones(self):
        with pytest.raises(ValueError, match="munday"):
            parse_rules([{"name": "a", "match": {"days": ["munday"]}}], set())

    def test_a_bad_clock_is_caught_at_load(self):
        with pytest.raises(ValueError):
            parse_rules([{"name": "a", "match": {"from": "26:00"}}], set())

    def test_channels_accepts_a_comma_separated_string(self):
        rules = parse_rules([{"name": "a", "to": "x, y"}], {"x", "y"})
        assert rules.rules[0].channels == ["x", "y"]

    def test_the_legacy_channels_key_is_accepted(self):
        rules = parse_rules([{"name": "a", "channels": ["x"]}], {"x"})
        assert rules.rules[0].channels == ["x"]


class TestParseQuietHours:
    def test_an_unknown_mode_is_refused(self):
        with pytest.raises(ValueError, match="mode"):
            parse_quiet_hours({"mode": "ignore"})

    def test_an_unknown_breakthrough_is_refused(self):
        with pytest.raises(ValueError, match="breakthrough"):
            parse_quiet_hours({"breakthrough": "extremely"})

    def test_hold_is_the_default(self):
        assert parse_quiet_hours({}).mode == "hold"

    def test_critical_breaks_through_by_default(self):
        assert parse_quiet_hours({}).breakthrough == "critical"

    def test_breakthrough_can_be_turned_off_explicitly(self):
        assert parse_quiet_hours({"breakthrough": ""}).breakthrough == ""


# ── Quiet hours ─────────────────────────────────────────────────────


class TestQuietHours:
    def _q(self, **over):
        spec = {"enabled": True,
                "windows": [{"from": "22:00", "to": "07:00"}]}
        spec.update(over)
        return parse_quiet_hours(spec)

    def test_outside_the_window_nothing_changes(self):
        assert self._q().verdict(inc("high"), at(12)) == ""

    def test_inside_the_window_an_ordinary_alert_is_held(self):
        assert self._q().verdict(inc("high"), at(3)) == "hold"

    def test_critical_always_breaks_through(self):
        """Alarm fatigue is the danger being designed against, but a
        fire at 3am is the reason the system exists."""
        assert self._q().verdict(inc("critical"), at(3)) == ""

    def test_the_breakthrough_bar_is_configurable(self):
        q = self._q(breakthrough="high")
        assert q.verdict(inc("high"), at(3)) == ""
        assert q.verdict(inc("medium"), at(3)) == "hold"

    def test_drop_is_available_but_is_not_the_default(self):
        assert self._q(mode="drop").verdict(inc("high"), at(3)) == "drop"
        assert self._q().mode == "hold"

    def test_disabled_quiet_hours_do_nothing(self):
        assert self._q(enabled=False).verdict(inc("high"), at(3)) == ""

    def test_it_knows_when_the_window_ends_for_the_page(self):
        assert self._q().ends_at(at(3)) == "07:00"


# ── Muting ──────────────────────────────────────────────────────────


class TestMuting:
    def test_a_mute_silences_and_then_expires(self):
        m = Muting()
        m.mute("*", 10, now=1000.0)
        assert m.muted(inc(at=1000.0), 1000.0) == "everything"
        assert m.muted(inc(at=2000.0), 2000.0) == ""

    def test_one_camera_can_be_muted_alone(self):
        m = Muting()
        m.mute("cam1", 10, now=1000.0)
        assert m.muted(inc(camera="cam1"), 1000.0) == "camera cam1"
        assert m.muted(inc(camera="cam2"), 1000.0) == ""

    def test_every_mute_expires_however_long_is_asked_for(self):
        """There is no 'mute for ever' in a security product."""
        m = Muting()
        until = m.mute("*", 10 ** 9, now=0.0)
        assert until <= routing.MAX_MUTE_MINUTES * 60.0

    def test_a_zero_or_negative_length_still_produces_a_real_pause(self):
        m = Muting()
        assert m.mute("*", 0, now=0.0) > 0
        assert m.mute("*", -5, now=0.0) > 0

    def test_unmuting_reports_whether_anything_was_muted(self):
        m = Muting()
        assert m.unmute("*") is False
        m.mute("*", 10, now=0.0)
        assert m.unmute("*") is True

    def test_the_page_can_show_a_live_countdown(self):
        m = Muting()
        m.mute("*", 30, now=0.0)
        assert 29 < m.active(0.0)["*"] <= 30

    def test_a_pause_survives_a_restart(self):
        """1.0 kept its flood state in memory only, so a restart re-sent
        everything it had just suppressed."""
        first = Muting()
        first.mute("cam1", 60, now=1000.0)
        second = Muting()
        second.restore(first.snapshot(), now=1000.0)
        assert second.muted(inc(camera="cam1"), 1000.0) == "camera cam1"

    def test_an_expired_pause_does_not_come_back_from_the_dead(self):
        second = Muting()
        second.restore({"*": 500.0}, now=1000.0)
        assert second.muted(inc(), 1000.0) == ""

    def test_corrupt_saved_state_is_ignored_not_fatal(self):
        m = Muting()
        m.restore({"*": "soon", "cam1": None}, now=0.0)
        m.restore("not a dict", now=0.0)
        assert m.active(0.0) == {}


# ── Inhibition ──────────────────────────────────────────────────────


class TestInhibition:
    def test_a_lesser_alert_behind_a_serious_one_is_hushed(self):
        """One person walks past: intrusion raises high, and motion,
        line-crossing and occupancy each raise their own. Four
        notifications, one event."""
        i = Inhibitor(60.0)
        i.observe(inc("high", camera="cam1", at=1000.0))
        assert i.inhibits(inc("medium", camera="cam1", at=1005.0)) != ""

    def test_it_can_only_ever_make_things_quieter(self):
        """A HIGHER severity is never suppressed by a lower one, so this
        can never lose the alert that mattered."""
        i = Inhibitor(60.0)
        i.observe(inc("medium", camera="cam1", at=1000.0))
        assert i.inhibits(inc("high", camera="cam1", at=1005.0)) == ""
        assert i.inhibits(inc("critical", camera="cam1", at=1005.0)) == ""

    def test_an_equal_severity_is_not_inhibited(self):
        i = Inhibitor(60.0)
        i.observe(inc("high", camera="cam1", at=1000.0))
        assert i.inhibits(inc("high", camera="cam1", at=1005.0)) == ""

    def test_it_does_not_reach_across_cameras(self):
        i = Inhibitor(60.0)
        i.observe(inc("high", camera="cam1", at=1000.0))
        assert i.inhibits(inc("medium", camera="cam2", at=1005.0)) == ""

    def test_it_stops_at_the_end_of_the_window(self):
        i = Inhibitor(60.0)
        i.observe(inc("high", camera="cam1", at=1000.0))
        assert i.inhibits(inc("medium", camera="cam1", at=1100.0)) == ""

    def test_zero_turns_it_off_entirely(self):
        i = Inhibitor(0.0)
        i.observe(inc("critical", camera="cam1", at=1000.0))
        assert i.inhibits(inc("low", camera="cam1", at=1000.0)) == ""

    def test_the_reason_names_what_is_hushing_it(self):
        i = Inhibitor(60.0)
        i.observe(inc("critical", camera="cam1", at=1000.0))
        assert "critical" in i.inhibits(inc("low", camera="cam1", at=1010.0))

    def test_an_alert_with_no_camera_is_never_inhibited(self):
        i = Inhibitor(60.0)
        i.observe(inc("critical", camera="", at=1000.0))
        assert i.inhibits(inc("low", camera="", at=1001.0)) == ""

    def test_memory_does_not_grow_without_bound(self):
        i = Inhibitor(60.0)
        for n in range(500):
            i.observe(inc(camera=f"cam{n}", at=1000.0 + n))
        i.prune(now=1000.0 + 500 + 60 * 4 + 1)
        assert len(i._live) < 50


# ── Grouping ────────────────────────────────────────────────────────


class TestGrouping:
    """Alertmanager's ``group_wait``, which the whole NVR field
    substitutes a blunt cooldown for. A cooldown either spams (too
    short) or drops the second, different event (too long)."""

    rule = Rule("r", ["chat"], Matcher())

    def test_with_no_wait_an_alert_goes_out_at_once(self):
        g = Grouper(0.0)
        group, update = g.add(inc(at=1000.0), self.rule, ["chat"])
        assert group is not None and update is False

    def test_a_burst_on_one_camera_becomes_one_message(self):
        g = Grouper(20.0)
        for n, title in enumerate(["Person", "Motion", "Car"]):
            assert g.add(inc(title=title, at=1000.0 + n),
                         self.rule, ["chat"]) == (None, False)
        due = g.flush_due(1021.0)
        assert len(due) == 1
        assert len(due[0].members) == 3

    def test_two_cameras_at_once_are_two_situations(self):
        g = Grouper(20.0)
        g.add(inc(camera="cam1", at=1000.0), self.rule, ["chat"])
        g.add(inc(camera="cam2", at=1000.0), self.rule, ["chat"])
        assert len(g.flush_due(1021.0)) == 2

    def test_nothing_flushes_before_the_window_closes(self):
        g = Grouper(20.0)
        g.add(inc(at=1000.0), self.rule, ["chat"])
        assert g.flush_due(1010.0) == []
        assert g.flush_due(1020.0) != []

    def test_the_message_is_about_the_most_serious_alert(self):
        """Collapsing must never bury the worst one under whichever
        happened to arrive first."""
        g = Grouper(20.0)
        g.add(inc("low", title="Motion", at=1000.0), self.rule, ["chat"])
        g.add(inc("critical", title="Intruder", at=1001.0), self.rule, ["chat"])
        assert g.flush_due(1021.0)[0].lead.title == "Intruder"

    def test_a_later_alert_UPDATES_the_message_instead_of_stacking(self):
        g = Grouper(0.0)
        g.add(inc(title="Person", at=1000.0), self.rule, ["chat"])
        group, update = g.add(inc(title="Car", at=1005.0), self.rule, ["chat"])
        assert update is True
        assert len(group.members) == 2

    def test_the_same_alert_re_firing_sends_nothing_at_all(self):
        """It is already represented in the message on the phone."""
        g = Grouper(0.0)
        g.add(inc(corr="abc", at=1000.0), self.rule, ["chat"])
        assert g.add(inc(corr="abc", at=1005.0),
                     self.rule, ["chat"]) == (None, False)

    def test_an_ESCALATION_gets_a_new_message_not_a_silent_edit(self):
        """An edit re-alerts nobody — Telegram's editMessageText changes
        the message in place with no buzz. Folding a critical into the
        notification raised for a medium one is how the alert that
        mattered reaches a phone that never rings."""
        g = Grouper(0.0)
        g.add(inc("medium", title="Motion", at=1000.0), self.rule, ["chat"])
        group, update = g.add(inc("critical", title="Intruder", at=1005.0),
                              self.rule, ["chat"])
        assert group is not None
        assert update is False, "an escalation must be a fresh notification"
        assert group.lead.title == "Intruder"

    def test_a_DE_escalation_still_updates_in_place(self):
        """Quieter news about the same situation is exactly what the
        update path is for."""
        g = Grouper(0.0)
        g.add(inc("critical", title="Intruder", at=1000.0), self.rule, ["chat"])
        _group, update = g.add(inc("low", title="Motion", at=1005.0),
                               self.rule, ["chat"])
        assert update is True

    def test_an_equal_severity_still_updates_in_place(self):
        g = Grouper(0.0)
        g.add(inc("high", title="Person", at=1000.0), self.rule, ["chat"])
        _group, update = g.add(inc("high", title="Car", at=1005.0),
                               self.rule, ["chat"])
        assert update is True

    def test_past_the_update_window_a_new_message_is_sent(self):
        g = Grouper(0.0, update_window_seconds=300.0)
        g.add(inc(title="Person", at=1000.0), self.rule, ["chat"])
        group, update = g.add(inc(title="Car", at=2000.0), self.rule, ["chat"])
        assert group is not None and update is False

    def test_a_per_rule_wait_overrides_the_global_one(self):
        g = Grouper(60.0)
        urgent = Rule("urgent", ["pager"], Matcher(), group_wait_seconds=0)
        group, _ = g.add(inc(at=1000.0), urgent, ["pager"])
        assert group is not None

    def test_the_wait_is_capped_so_alerts_cannot_be_held_for_ever(self):
        assert Grouper(99999.0).wait == routing.MAX_GROUP_WAIT_SECONDS

    def test_a_negative_wait_is_treated_as_none(self):
        assert Grouper(-5.0).wait == 0.0

    def test_shutdown_flushes_what_is_still_held(self):
        """A collapse window in flight at shutdown is a real
        notification somebody is owed."""
        g = Grouper(60.0)
        g.add(inc(at=1000.0), self.rule, ["chat"])
        assert len(g.flush_all()) == 1
        assert g.pending == 0

    def test_sent_groups_do_not_accumulate_for_ever(self):
        g = Grouper(0.0, update_window_seconds=60.0)
        for n in range(200):
            g.add(inc(camera=f"cam{n}", at=1000.0), self.rule, ["chat"])
        g.prune(now=1000.0 + 60 * 2 + 1)
        assert len(g._sent) == 0


# ── Rendering ───────────────────────────────────────────────────────


class TestRender:
    def test_the_camera_appears_once_not_twice(self):
        """A title of 'Person at gate — Front Door' above a line reading
        'Front Door · 02:14' is what happens when both layers add it."""
        group = routing.Group(key="k", rule="r", channels=[],
                              members=[Incident(title="Person at gate",
                                                camera_name="Front Door",
                                                at=1000.0)])
        message = routing.render(group)
        assert message.title == "Person at gate"
        assert message.camera == "Front Door"
        assert message.text().count("Front Door") == 1

    def test_the_photo_comes_from_whichever_member_has_one(self):
        group = routing.Group(key="k", rule="r", channels=[], members=[
            Incident(title="Motion", severity="low", at=1000.0),
            Incident(title="Person", severity="high", image=JPEG, at=1001.0)])
        assert routing.render(group).image == JPEG

    def test_the_zone_is_in_the_body_because_it_decides_act_or_ignore(self):
        group = routing.Group(key="k", rule="r", channels=[], members=[
            Incident(title="Person", zones=["Restricted"], at=1000.0)])
        assert "Restricted" in routing.render(group).body

    def test_the_alerts_own_timestamp_wins_over_now(self):
        """By the time a phone shows it, 'now' is a lie."""
        group = routing.Group(key="k", rule="r", channels=[], members=[
            Incident(title="Person", fired_at="2026-09-21T02:14:03+00:00",
                     at=9e9)])
        assert routing.render(group).when.endswith(":03")

    def test_an_unparseable_timestamp_does_not_crash_the_message(self):
        group = routing.Group(key="k", rule="r", channels=[], members=[
            Incident(title="Person", fired_at="last tuesday", at=1000.0)])
        assert routing.render(group).when

    def test_no_base_url_means_no_dead_link(self):
        """An empty link is better than one that goes nowhere."""
        group = routing.Group(key="k", rule="r", channels=[],
                              members=[Incident(title="P", at=1000.0)])
        assert routing.render(group, base_url="").url == ""
        assert routing.render(group, base_url="").actions == []


# ── Regressions found in review ─────────────────────────────────────


class TestReviewRegressions:
    """One test per defect a review pass found in this code. Each
    reproduces the exact failure, and each fails without its fix."""

    rule = Rule("r", ["chat"], Matcher())

    def test_a_rebuilt_grouper_adopts_the_alerts_in_flight(self):
        """Config is applied by REBUILDING, and a fresh Grouper starts
        empty — so an alert inside a collapse window when the operator
        saved any config change simply ceased to exist: not delivered,
        not suppressed, not logged."""
        first = Grouper(20.0)
        first.add(inc(at=1000.0), self.rule, ["chat"])
        second = Grouper(20.0)
        second.adopt(first)
        assert len(second.flush_due(1021.0)) == 1

    def test_adopting_carries_the_handles_that_make_an_edit_an_edit(self):
        first = Grouper(0.0)
        group, _ = first.add(inc(at=1000.0), self.rule, ["chat"])
        group.handles["chat"] = "msg-1"
        second = Grouper(0.0)
        second.adopt(first)
        assert second.sent_group(group.key).handles["chat"] == "msg-1"

    def test_a_per_rule_window_is_honoured_at_FLUSH_time(self):
        """It used to be read only for the 'send immediately' shortcut,
        so a rule asking to collapse for 60s was flushed on the global
        5s schedule and the setting silently did nothing."""
        g = Grouper(5.0)
        slow = Rule("slow", ["chat"], Matcher(), group_wait_seconds=60.0)
        g.add(inc(at=1000.0), slow, ["chat"])
        assert g.flush_due(1010.0) == []
        assert len(g.flush_due(1061.0)) == 1

    def test_a_nonsense_per_rule_window_falls_back(self):
        g = Grouper(5.0)
        bad = Rule("bad", ["chat"], Matcher(), group_wait_seconds=float("nan"))
        g.add(inc(at=1000.0), bad, ["chat"])
        assert g.flush_due(1006.0), "a NaN window must not hold for ever"

    def test_an_escalation_goes_out_NOW_not_after_the_window(self):
        """A new message was right; holding it for the collapse window
        re-imposed the same silence the escalation path exists to
        break."""
        g = Grouper(20.0)
        g.add(inc("medium", title="Motion", at=1000.0), self.rule, ["chat"])
        g.flush_due(1021.0)
        group, update = g.add(inc("critical", title="Intruder", at=1025.0),
                              self.rule, ["chat"])
        assert group is not None and update is False
        assert g.pending == 0, "nothing should still be held"

    def test_an_escalation_carries_the_burst_it_escalated_out_of(self):
        g = Grouper(0.0)
        g.add(inc("medium", title="Motion", at=1000.0), self.rule, ["chat"])
        group, _ = g.add(inc("critical", title="Intruder", at=1005.0),
                         self.rule, ["chat"])
        assert {m.title for m in group.members} == {"Motion", "Intruder"}

    def test_a_window_whose_start_equals_its_end_matches_NOTHING(self):
        """Read as crossing midnight it meant all 24 hours — which for a
        quiet-hours window is 'silent for ever', the worst possible
        reading of somebody typing the same time twice."""
        w = TimeWindow([], "09:00", "09:00")
        assert w.empty is True
        assert w.contains(at(3)) is False
        assert w.contains(at(9)) is False
        assert w.contains(at(14)) is False
        assert "empty" in w.describe()

    def test_a_time_scoped_matcher_is_not_skipped_when_no_clock_is_given(self):
        """It used to return True, so a night-only rule fired at noon
        for any caller that did not pass a clock."""
        night = Matcher(from_time="22:00", to_time="23:00")
        noon = inc(at=datetime(2026, 9, 21, 12,
                               tzinfo=timezone.utc).timestamp())
        assert night.matches(noon, local=None) is False

    def test_a_restored_pause_is_re_capped_against_a_bad_clock(self):
        """A pause written while the clock was wrong — an appliance with
        no RTC, a container before NTP settles — is an absolute stamp
        that can sit months ahead. 'No mute for ever' has to hold on the
        way back in too."""
        m = Muting()
        m.restore({"*": 1000.0 + 10 ** 9}, now=1000.0)
        assert m.active(1000.0)["*"] <= routing.MAX_MUTE_MINUTES

    def test_the_displayed_time_is_in_the_SITE_zone(self):
        """astimezone() with no argument uses the PROCESS's zone, which
        in a container with no TZ is UTC — so a site in Asia/Kolkata got
        a headline time five and a half hours out."""
        group = routing.Group(key="k", rule="r", channels=[], members=[
            Incident(title="P", fired_at="2026-09-21T02:14:03+00:00", at=0)])
        assert routing.render(group, tz_name="UTC").when == "02:14:03"
        assert routing.render(group, tz_name="Asia/Kolkata").when == "07:44:03"

    def test_a_naive_timestamp_is_read_as_UTC_not_as_local(self):
        group = routing.Group(key="k", rule="r", channels=[], members=[
            Incident(title="P", fired_at="2026-09-21T02:14:03", at=0)])
        assert routing.render(group, tz_name="UTC").when == "02:14:03"

    def test_two_separate_alerts_get_two_different_message_ids(self):
        """dedup_key names the SITUATION and is reused for the next
        alert on the same camera. Seeding Matrix's transaction id from
        it made the server treat a brand-new alert as a replay: it
        returned the original event id, posted nothing, and this app
        recorded a delivery that never happened."""
        first = routing.Group(key="R|cam1", rule="r", channels=[],
                              members=[Incident(title="A", at=1.0)],
                              opened_at=1.0)
        second = routing.Group(key="R|cam1", rule="r", channels=[],
                               members=[Incident(title="B", at=99.0)],
                               opened_at=99.0)
        one, two = routing.render(first), routing.render(second)
        assert one.dedup_key == two.dedup_key      # same situation
        assert one.message_id != two.message_id    # different messages
