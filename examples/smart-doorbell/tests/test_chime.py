# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""When the door rings, and when it deliberately does not.

An alert and a chime are different events. Every face at the door is
worth recording; only some are worth interrupting somebody for. These
guard the three rules that separate the two — who it is decides the
sound, quiet hours silence the bell but not the alarm, and a bell that
rings twelve times is noise — plus the one that makes the feature
debuggable: a suppressed ring always says why.
"""
from __future__ import annotations

from datetime import datetime, time as _time
from unittest.mock import MagicMock

import pytest

from chime import (
    CHIME_TONES,
    DEFAULT_TONES,
    ChimePolicy,
    in_quiet_hours,
    parse_quiet_hours,
)
from face_recognition_pipeline import FaceRead
from smart_doorbell import AppConfig, CameraConfig, SmartDoorbell


def _at(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(2026, 9, 22, int(h), int(m))


# ── who it is decides the sound ──────────────────────────────────────


def test_the_people_who_live_here_are_announced_without_ringing():
    """A bell that rings when the family comes home is the bell people
    stop hearing — and then it does not work for the stranger either."""
    p = ChimePolicy()
    for who in ("family", "resident"):
        d = p.decide(key="k", category=who, recognized=True, now=0.0)
        assert d.ring is False
        assert d.tone == "none"
        assert who in d.reason


def test_someone_with_business_at_the_door_rings():
    p = ChimePolicy()
    for who in ("visitor", "contractor"):
        d = p.decide(key=who, category=who, recognized=True, now=0.0)
        assert d.ring is True
        assert d.tone == "ding_dong"


def test_a_stranger_rings():
    p = ChimePolicy()
    d = p.decide(key="k", category=None, recognized=False, now=0.0)
    assert (d.ring, d.tone) == (True, "ding_dong")


def test_a_watchlist_match_is_an_alarm_not_a_doorbell():
    """The one recognised face that must be louder than a stranger."""
    p = ChimePolicy()
    d = p.decide(key="k", category="watchlist", recognized=True, now=0.0)
    assert (d.ring, d.tone) == (True, "alarm")


def test_the_table_is_a_starting_point_an_operator_edits():
    p = ChimePolicy(tones={"family": "ding_dong", "visitor": "none"})
    assert p.decide(key="a", category="family", recognized=True,
                    now=0.0).ring is True
    assert p.decide(key="b", category="visitor", recognized=True,
                    now=0.0).ring is False


def test_a_tone_nobody_can_play_is_refused_not_stored(caplog):
    p = ChimePolicy(tones={"family": "foghorn"})
    assert p.tones["family"] == DEFAULT_TONES["family"], (
        "an unknown tone must leave the default in place")
    assert "foghorn" in caplog.text


def test_an_unknown_category_still_rings_something():
    """A category somebody invents in the adapter must not fall through
    to silence — a door that goes quiet for a face it half-recognises is
    worse than one that rings too much."""
    p = ChimePolicy()
    d = p.decide(key="k", category="gardener", recognized=True, now=0.0)
    assert d.ring is True
    assert d.tone in CHIME_TONES and d.tone != "none"


# ── quiet hours silence the bell, not the alarm ──────────────────────


@pytest.mark.parametrize("at,expected", [
    ("21:59", False), ("22:00", True), ("03:00", True),
    ("06:59", True), ("07:00", False),
])
def test_a_quiet_window_that_crosses_midnight(at, expected):
    """start > end means the window wraps, and 03:00 is inside it — the
    case an interval check gets wrong."""
    window = parse_quiet_hours("22:00-07:00")
    assert in_quiet_hours(_at(at), window) is expected


def test_a_delivery_at_three_in_the_morning_does_not_wake_the_house():
    p = ChimePolicy(quiet_hours="22:00-07:00")
    d = p.decide(key="k", category="contractor", recognized=True,
                 now=0.0, wall=_at("03:00"))
    assert d.ring is False
    assert "quiet hours" in d.reason
    assert d.tone == "ding_dong", "the tone it WOULD have played is still said"


def test_a_stranger_at_three_in_the_morning_still_does():
    """Suppressing this would be a burglar alarm that observes bedtime."""
    p = ChimePolicy(quiet_hours="22:00-07:00",
                    tones={"unknown": "alarm"})
    d = p.decide(key="k", category=None, recognized=False,
                 now=0.0, wall=_at("03:00"))
    assert d.ring is True


def test_a_watchlist_match_overrides_quiet_hours():
    p = ChimePolicy(quiet_hours="22:00-07:00")
    assert p.decide(key="k", category="watchlist", recognized=True,
                    now=0.0, wall=_at("03:00")).ring is True


def test_an_unparseable_window_is_ignored_loudly(caplog):
    """The operator set it expecting silence and would find out at
    03:00 otherwise."""
    p = ChimePolicy(quiet_hours="ten at night till seven")
    assert p.decide(key="k", category="visitor", recognized=True,
                    now=0.0, wall=_at("03:00")).ring is True
    assert "quiet_hours" in caplog.text


def test_a_zero_width_window_silences_nothing():
    assert in_quiet_hours(_at("03:00"), (_time(2, 0), _time(2, 0))) is False


def test_no_window_configured_never_silences():
    assert parse_quiet_hours("") is None
    assert parse_quiet_hours(None) is None
    assert in_quiet_hours(_at("03:00"), None) is False


# ── a bell that rings twelve times is noise ──────────────────────────


def test_the_same_caller_does_not_ring_again_inside_the_window():
    p = ChimePolicy(rechime_seconds=300)
    assert p.decide(key="door|bob", category="visitor", recognized=True,
                    now=0.0).ring is True
    again = p.decide(key="door|bob", category="visitor", recognized=True,
                     now=100.0)
    assert again.ring is False
    assert "rang 100s ago" in again.reason

    assert p.decide(key="door|bob", category="visitor", recognized=True,
                    now=400.0).ring is True


def test_two_callers_at_one_door_are_not_folded_into_each_other():
    p = ChimePolicy(rechime_seconds=300)
    assert p.decide(key="door|bob", category="visitor", recognized=True,
                    now=0.0).ring is True
    assert p.decide(key="door|eve", category="visitor", recognized=True,
                    now=1.0).ring is True


def test_a_suppressed_ring_does_not_push_the_next_one_further_away():
    """Counting a silenced ring would mean somebody walking past during
    quiet hours delays the ring that should have happened after it."""
    p = ChimePolicy(quiet_hours="22:00-07:00", rechime_seconds=300)
    p.decide(key="k", category="visitor", recognized=True, now=0.0,
             wall=_at("03:00"))                      # silenced
    d = p.decide(key="k", category="visitor", recognized=True, now=1.0,
                 wall=_at("09:00"))                  # morning
    assert d.ring is True


def test_a_zero_window_rings_every_time():
    p = ChimePolicy(rechime_seconds=0)
    assert p.decide(key="k", category="visitor", recognized=True,
                    now=0.0).ring is True
    assert p.decide(key="k", category="visitor", recognized=True,
                    now=0.1).ring is True


def test_switching_the_chime_off_still_says_so():
    p = ChimePolicy(enabled=False)
    d = p.decide(key="k", category="visitor", recognized=True, now=0.0)
    assert d.ring is False
    assert "switched off" in d.reason


# ── the app end ──────────────────────────────────────────────────────


def _config(**overrides) -> AppConfig:
    base = AppConfig(
        kaic_url="http://localhost:8100",
        kaic_api_key="test-key",
        cameras=[CameraConfig(camera_id="front-door",
                              frame_url="http://example.invalid/snap.jpg")],
        poll_interval_seconds=0.0,
        request_timeout_seconds=1.0,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class _Store:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class _Nvr:
    def __init__(self):
        self.state = _Store()

    def save_evidence(self, jpeg):
        return "evidence/1.jpg"


def _doorbell(reads, **cfg):
    pipeline = MagicMock()
    pipeline.process_frame.side_effect = list(reads)
    dispatcher = MagicMock()
    app = SmartDoorbell(_config(**cfg), pipeline, dispatcher)
    app.nvr = _Nvr()

    class _Stub:
        def fetch(self):
            return b"\xff\xd8jpeg"

    for cam_id in list(app._frame_sources):
        app._frame_sources[cam_id] = _Stub()
    return app, dispatcher


def _read(recognized: bool, category: str | None = None) -> FaceRead:
    return FaceRead(
        face_detected=True, recognized=recognized,
        person_id="alice" if recognized else None,
        name="Alice" if recognized else None, category=category,
        similarity=0.9 if recognized else 0.2, correlation_id="c",
        face_bbox=None)


def test_the_decision_rides_in_the_alert_envelope():
    """Whatever actually rings reads it from there — an app that owned
    the speaker would work on exactly one deployment."""
    app, dispatcher = _doorbell([_read(False)])
    app.on_frame("front-door", b"\xff\xd8jpeg")

    alert = dispatcher.dispatch.call_args[0][0]
    assert alert.evidence["chime"]["ring"] is True
    assert alert.evidence["chime"]["tone"] == "ding_dong"
    assert "chime:ding_dong" in alert.tags


def test_a_silent_visit_still_alerts_and_says_why_it_was_silent():
    """The feed, the history and the inbox are unaffected by the bell.
    A doorbell that silently chose not to ring is indistinguishable from
    a broken one."""
    app, dispatcher = _doorbell([_read(True, "family")])
    app.on_frame("front-door", b"\xff\xd8jpeg")

    assert dispatcher.dispatch.call_count == 1, "the alert still fires"
    alert = dispatcher.dispatch.call_args[0][0]
    assert alert.evidence["chime"]["ring"] is False
    assert alert.evidence["chime"]["reason"]
    assert "chime:silent" in alert.tags


def test_the_dashboard_can_answer_why_it_did_not_ring():
    app, _ = _doorbell([_read(True, "family")])
    app.on_frame("front-door", b"\xff\xd8jpeg")

    snap = app.state_snapshot()
    assert snap["rings"] == 0
    assert snap["last_chime"]["ring"] is False
    assert "silent:" in app.ui_html()


def test_rings_are_counted_for_the_dashboard():
    app, _ = _doorbell([_read(False), _read(False)],
                       dedup_window_seconds=0, rechime_seconds=0)
    app.on_frame("front-door", b"\xff\xd8jpeg")
    app.on_frame("front-door", b"\xff\xd8jpeg")
    assert app.state_snapshot()["rings"] == 2


def test_the_policy_is_rebuilt_whole_on_a_live_config_change():
    """Never a policy with a new quiet window and an old tone table."""
    app, _ = _doorbell([_read(True, "family")])
    app.on_config_update({"chime_tones": {"family": "alarm"},
                          "quiet_hours": "22:00-07:00",
                          "rechime_seconds": 10})

    assert app._chime.tones["family"] == "alarm"
    assert app.config.quiet_hours == "22:00-07:00"
    assert app._chime.rechime_seconds == 10


def test_changing_the_bell_does_not_make_the_next_caller_wait():
    """An operator who just changed the chime is entitled to hear it."""
    app, _ = _doorbell([_read(False), _read(False)], dedup_window_seconds=0)
    app.on_frame("front-door", b"\xff\xd8jpeg")
    assert app.state_snapshot()["rings"] == 1

    app.on_config_update({"rechime_seconds": 3600})
    app.on_frame("front-door", b"\xff\xd8jpeg")
    assert app.state_snapshot()["rings"] == 2


@pytest.mark.parametrize("field", ["chime_enabled", "quiet_hours",
                                   "rechime_seconds"])
def test_the_chime_settings_are_exposed_as_params(field):
    from smart_doorbell import MANIFEST

    assert any(p.name == field for p in MANIFEST.params), (
        f"{field} cannot be changed from the catalog")
