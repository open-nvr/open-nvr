# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""What was there at the start is the baseline; more than that, seen twice,
is a dump; back to empty clears it — with a scripted detector."""
from __future__ import annotations

import io

import pytest
from PIL import Image

import datetime as _dt

from garbage_dumping import ActiveHours, AppConfig, GarbageDumping, count_garbage, crop_to_zone
from opennvr_app_sdk.testing import RecorderChannel, app_config

W, H = 640, 480


def _jpeg(im: Image.Image) -> bytes:
    b = io.BytesIO()
    im.save(b, "JPEG", quality=85)
    return b.getvalue()


FRAME = _jpeg(Image.new("RGB", (W, H), (100, 110, 90)))


def det(label: str, score: float = 0.6) -> dict:
    return {"label": label, "confidence": score, "bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}}


class Source:
    def get_frame(self, camera_id: str) -> bytes | None:
        return FRAME


class Clock:
    def __init__(self) -> None:
        self.t = 9000.0

    def __call__(self) -> float:
        return self.t


def make_app(looks: list[list[dict] | None], **overrides):
    cfg = app_config(**{**AppConfig().__dict__, **overrides})
    rec = RecorderChannel()
    app = GarbageDumping(cfg, rec.dispatcher(), frame_source=Source(), cameras=["cam1"],
                         poll_interval_seconds=60.0)
    clock = Clock()
    app._clock = clock
    script = list(looks)
    seen: list[bytes] = []

    def detector(camera_id: str, jpeg: bytes):
        seen.append(jpeg)
        return script.pop(0) if script else []

    app._detector = detector
    return app, rec, clock, seen


def ticks(app, clock, n: int, step: float = 60.0) -> None:
    for _ in range(n):
        app.handle_tick()
        clock.t += step


def test_counting_honours_the_score_floor():
    n, labels = count_garbage([det("garbage bag", 0.5), det("litter", 0.1), {"label": "sack"}], 0.2)
    assert n == 1 and labels == ["garbage bag"]


def test_a_dump_alerts_after_two_looks_and_clears_when_the_zone_is_empty():
    empty: list[dict] = []
    one = [det("garbage bag")]
    app, rec, clock, _ = make_app([empty, one, one, one, empty, empty], confirm_looks=2, attach_snapshot=False)
    ticks(app, clock, 1)                      # baseline: empty
    ticks(app, clock, 1)                      # one bag, first look: not yet
    assert rec.alerts == []
    ticks(app, clock, 1)                      # second look: dumped
    assert [a.alert_type for a in rec.alerts] == ["garbage_dumped"]
    assert rec.alerts[0].evidence["new"] == 1 and "garbage bag" in rec.alerts[0].description
    ticks(app, clock, 1)                      # still there: quiet
    assert len(rec.alerts) == 1
    ticks(app, clock, 2)                      # empty again: cleared once
    assert [a.alert_type for a in rec.alerts] == ["garbage_dumped", "garbage_cleared"]
    assert rec.alerts[-1].severity == "low"


def test_garbage_there_at_the_start_is_the_baseline_not_an_alert():
    two = [det("sack"), det("cardboard box")]
    app, rec, clock, _ = make_app([two] * 5, confirm_looks=2)
    ticks(app, clock, 5)
    assert rec.alerts == []
    st = app.state_snapshot()["cameras"]["cam1"]
    assert st["baseline"] == 2 and st["count"] == 2


def test_a_second_dump_on_top_of_the_first_alerts_again():
    one, two = [det("sack")], [det("sack"), det("garbage bag")]
    app, rec, clock, _ = make_app([[], one, one, two, two], confirm_looks=2, cooldown_seconds=0, attach_snapshot=False)
    ticks(app, clock, 5)
    assert [a.alert_type for a in rec.alerts] == ["garbage_dumped", "garbage_dumped"]
    assert rec.alerts[1].evidence["count"] == 2 and rec.alerts[1].evidence["new"] == 1


def test_a_single_flicker_does_not_alert():
    app, rec, clock, _ = make_app([[], [det("litter")], [], [], []], confirm_looks=2)
    ticks(app, clock, 5)
    assert rec.alerts == []


def test_a_failed_look_changes_nothing():
    app, rec, clock, _ = make_app([[], None, None, [det("sack")], [det("sack")]], confirm_looks=2, attach_snapshot=False)
    ticks(app, clock, 5)
    assert [a.alert_type for a in rec.alerts] == ["garbage_dumped"]
    assert app.state_snapshot()["cameras"]["cam1"]["looks"] == 3


def test_the_crop_follows_the_drawn_zone_and_a_new_zone_resets_the_baseline():
    app, rec, clock, seen = make_app([[det("sack")], [det("sack")], [det("sack")]])
    ticks(app, clock, 1)
    assert app.state_snapshot()["cameras"]["cam1"]["baseline"] == 1
    app.on_config_update({"zone": {"1": [[0.5, 0.5], [1.0, 0.5], [1.0, 1.0], [0.5, 1.0]]}})
    assert app.state_snapshot()["cameras"]["cam1"]["baseline"] is None
    ticks(app, clock, 1)
    with Image.open(io.BytesIO(seen[-1])) as crop:
        assert 330 <= crop.width <= 360 and 250 <= crop.height <= 270
    assert crop_to_zone(FRAME, None) == FRAME


def test_catalog_config_applies_live():
    app, rec, clock, _ = make_app([])
    app.on_config_update({"queries": ["rubbish", "bin bag"], "confirm_looks": 3, "poll_interval_seconds": 15})
    assert app.cfg.queries == ["rubbish", "bin bag"] and app.cfg.confirm_looks == 3 and app._interval == 15.0
    app.on_config_update({"min_score": 2})                           # rejected
    assert app.cfg.min_score == 0.2


def test_config_rejects_nonsense():
    with pytest.raises(ValueError):
        AppConfig(queries=[])
    with pytest.raises(ValueError):
        AppConfig(confirm_looks=0)
    with pytest.raises(ValueError):
        AppConfig(min_score=1.5)


def test_outside_active_hours_the_dump_is_counted_but_quiet():
    app, rec, clock, seen = make_app([[], [det("sack")], [det("sack")], [det("sack")]],
                                     active_hours={"start": "08:00", "end": "18:00"})
    app._wall = lambda: _dt.datetime(2026, 10, 24, 22, 0).astimezone()
    ticks(app, clock, 3)
    assert rec.alerts == []
    assert app.state_snapshot()["items_now"] == 1 and app.state_snapshot()["in_hours"] is False
    app._wall = lambda: _dt.datetime(2026, 10, 24, 9, 0).astimezone()
    ticks(app, clock, 1)
    assert [a.alert_type for a in rec.alerts] == ["garbage_dumped"]


def test_dashboard_state_has_the_views_the_manifest_declares_and_before_after_pictures():
    app, rec, clock, seen = make_app([[], [det("sack")], [det("sack")], []])
    ticks(app, clock, 4)
    assert [a.alert_type for a in rec.alerts] == ["garbage_dumped", "garbage_cleared"]
    st = app.state_snapshot()
    for view in GarbageDumping.manifest.state_schema:
        assert view.path.split(".")[0] in st, view.path
    assert st["today"] == {"dumps": 1, "cleared": 1, "since": st["today"]["since"]}
    assert st["per_camera"][0]["dumps_today"] == 1 and st["per_camera"][0]["last_look"]
    labels = [g["label"] for g in st["gallery"]]
    assert labels[:2] == ["Garbage dumped at cam1 — before", "Garbage dumped at cam1 — after"]
    assert all(g["image"].startswith("data:image/jpeg;base64,") for g in st["gallery"])
    assert st["recent"][0]["message"] == "Garbage cleared at cam1"


def test_active_hours_parse():
    hours = ActiveHours.parse({"start": "22:00", "end": "06:00"})
    assert hours.contains(_dt.datetime(2026, 1, 1, 23, 30)) and not hours.contains(_dt.datetime(2026, 1, 1, 12, 0))
    assert ActiveHours.parse(None) is None
    with pytest.raises(ValueError):
        ActiveHours.parse({"start": "x", "end": "06:00"})
