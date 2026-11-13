# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Open past the threshold alerts once, closing clears it, unsure looks are
tolerated, the crop follows the drawn gate — with a scripted model, no
broker, no core."""
from __future__ import annotations

import io

import pytest
from PIL import Image

import datetime as _dt

from gate_watch import ActiveHours, AppConfig, GateWatch, crop_to_gate, normalise_answer
from opennvr_app_sdk.testing import RecorderChannel, app_config

W, H = 640, 480


def _jpeg(im: Image.Image) -> bytes:
    b = io.BytesIO()
    im.save(b, "JPEG", quality=85)
    return b.getvalue()


FRAME = _jpeg(Image.new("RGB", (W, H), (120, 130, 140)))


class Source:
    def get_frame(self, camera_id: str) -> bytes | None:
        return FRAME


class Clock:
    def __init__(self) -> None:
        self.t = 5000.0

    def __call__(self) -> float:
        return self.t


def make_app(answers: list[str | None], **overrides):
    cfg = app_config(**{**AppConfig().__dict__, **overrides})
    rec = RecorderChannel()
    app = GateWatch(cfg, rec.dispatcher(), frame_source=Source(), cameras=["cam1"],
                    poll_interval_seconds=30.0)
    clock = Clock()
    app._clock = clock
    script = list(answers)
    asked: list[tuple[str, bytes]] = []

    def asker(camera_id: str, jpeg: bytes) -> str | None:
        asked.append((camera_id, jpeg))
        return script.pop(0) if script else None

    app._asker = asker
    return app, rec, clock, asked


def ticks(app, clock, n: int, step: float = 30.0) -> None:
    for _ in range(n):
        app.handle_tick()
        clock.t += step


@pytest.mark.parametrize("answer, verdict", [
    ("open", "open"), ("The gate is open.", "open"), ("Opened", "open"), ("ajar", "open"),
    ("closed", "closed"), ("It is shut.", "closed"), ("Closed.", "closed"),
    ("I cannot tell", "unsure"), ("", "unsure"), ("open and closed", "unsure"), (None, "unsure"),
])
def test_answers_reduce_to_one_of_three_words(answer, verdict):
    assert normalise_answer(answer) == verdict


def test_open_past_the_threshold_alerts_once_and_closing_clears_it():
    app, rec, clock, asked = make_app(["open"] * 6 + ["closed", "closed"],
                                      open_seconds=120, cooldown_seconds=600, attach_snapshot=False)
    ticks(app, clock, 4)                      # open for 0, 30, 60, 90 s: under the threshold
    assert rec.alerts == []
    ticks(app, clock, 1)                      # 120 s open
    assert [a.alert_type for a in rec.alerts] == ["gate_left_open"]
    assert rec.alerts[0].camera_id == "cam1" and rec.alerts[0].severity == "medium"
    assert "120 s" in rec.alerts[0].description
    ticks(app, clock, 1)                      # still open, within cooldown: quiet
    assert len(rec.alerts) == 1
    ticks(app, clock, 2)                      # closed: cleared once
    assert [a.alert_type for a in rec.alerts] == ["gate_left_open", "gate_closed"]
    assert rec.alerts[-1].severity == "low"
    assert len(asked) == 8 and all(cam == "cam1" for cam, _ in asked)


def test_a_gate_that_closes_in_time_never_alerts():
    app, rec, clock, _ = make_app(["open", "open", "closed", "open", "closed"], open_seconds=120)
    ticks(app, clock, 5)
    assert rec.alerts == []


def test_unsure_looks_neither_extend_nor_reset_the_timer_until_tolerated():
    app, rec, clock, _ = make_app(["open", "unclear", "open", "open", "open"],
                                  open_seconds=100, unsure_tolerance=2, attach_snapshot=False)
    ticks(app, clock, 5)                      # open at 0 s, unsure at 30, open at 60, 90, 120
    assert [a.alert_type for a in rec.alerts] == ["gate_left_open"]


def test_repeated_unsure_raises_unknown_once():
    app, rec, clock, _ = make_app([None, None, None, "closed"], unsure_tolerance=2)
    ticks(app, clock, 4)
    assert [a.alert_type for a in rec.alerts] == ["gate_unknown"]


def test_the_crop_follows_the_drawn_gate():
    app, rec, clock, asked = make_app(["closed"])
    app.on_config_update({"gate": {"cam1": [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]}})
    ticks(app, clock, 1)
    with Image.open(io.BytesIO(asked[0][1])) as crop:
        # Half the frame, plus the 15 % margin on each side.
        assert 380 <= crop.width <= 420 and 280 <= crop.height <= 320
    assert app.state_snapshot()["cameras"]["cam1"]["gate_drawn"] is True


def test_no_gate_drawn_means_the_whole_frame():
    assert crop_to_gate(FRAME, None) == FRAME
    assert crop_to_gate(FRAME, [[0, 0], [1, 1]]) == FRAME       # not a polygon


def test_catalog_config_applies_live():
    app, rec, clock, _ = make_app([])
    app.on_config_update({"open_seconds": 10, "poll_interval_seconds": 5, "vqa_adapter": "moondream",
                          "question": "Open or closed?"})
    assert app.cfg.open_seconds == 10.0 and app._interval == 5.0 and app._adapter == "moondream"
    app.on_config_update({"unsure_tolerance": 0})                  # rejected, keeps the old value
    assert app.cfg.unsure_tolerance == 2


def test_config_rejects_nonsense():
    with pytest.raises(ValueError):
        AppConfig(poll_interval_seconds=0)
    with pytest.raises(ValueError):
        AppConfig(unsure_tolerance=0)
    with pytest.raises(ValueError):
        AppConfig(question="  ")


def test_still_open_escalates_once_and_closing_resets_it():
    app, rec, clock, asked = make_app(["open"] * 40 + ["closed"], open_seconds=60, cooldown_seconds=3600,
                                      escalate_after_seconds=120)
    ticks(app, clock, 3)                                    # open 60 s: left-open alert
    assert [a.alert_type for a in rec.alerts] == ["gate_left_open"]
    ticks(app, clock, 4)                                    # 120 s after the alert: escalation
    assert [a.alert_type for a in rec.alerts] == ["gate_left_open", "gate_still_open"]
    assert rec.alerts[1].severity == "high"
    ticks(app, clock, 10)                                   # still open: nothing more
    assert len(rec.alerts) == 2
    ticks(app, clock, 25)                                   # closes
    assert rec.alerts[-1].alert_type == "gate_closed"
    assert app.state_snapshot()["cameras"]["cam1"]["escalated"] is False


def test_outside_active_hours_the_gate_is_counted_but_quiet():
    app, rec, clock, asked = make_app(["open"] * 10, open_seconds=30,
                                      active_hours={"start": "08:00", "end": "18:00"})
    app._wall = lambda: _dt.datetime(2026, 10, 24, 22, 0).astimezone()
    ticks(app, clock, 5)
    assert rec.alerts == []
    st = app.state_snapshot()
    assert st["open_now"] == 1 and st["today"]["opens"] == 1 and st["in_hours"] is False
    app._wall = lambda: _dt.datetime(2026, 10, 24, 9, 0).astimezone()
    ticks(app, clock, 1)
    assert [a.alert_type for a in rec.alerts] == ["gate_left_open"]


def test_dashboard_state_has_the_views_the_manifest_declares():
    app, rec, clock, asked = make_app(["open"] * 6, open_seconds=30)
    ticks(app, clock, 3)
    st = app.state_snapshot()
    for view in GateWatch.manifest.state_schema:
        head = view.path.split(".")[0]
        assert head in st, view.path
    assert st["per_camera"][0]["camera"] == "cam1" and st["per_camera"][0]["alerts_today"] == 1
    assert st["recent"][0]["message"] == "Gate left open at cam1" and st["recent"][0]["level"] == "alert"
    assert st["gallery"][0]["image"].startswith("data:image/jpeg;base64,")


def test_active_hours_parse_and_cross_midnight():
    hours = ActiveHours.parse({"start": "22:00", "end": "06:00"})
    assert hours.contains(_dt.datetime(2026, 1, 1, 23, 30))
    assert hours.contains(_dt.datetime(2026, 1, 1, 5, 59))
    assert not hours.contains(_dt.datetime(2026, 1, 1, 12, 0))
    assert ActiveHours.parse(None) is None and ActiveHours.parse({"start": "", "end": ""}) is None
    with pytest.raises(ValueError):
        ActiveHours.parse({"start": "25:00", "end": "06:00"})
