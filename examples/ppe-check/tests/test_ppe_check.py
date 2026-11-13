# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Every judgeable person is checked once from their best crop; kit missing
in two checks in a row alerts — with a scripted detector and crop store."""
from __future__ import annotations

import pytest

from opennvr_app_sdk.testing import RecorderChannel, app_config
import base64
import datetime as _dt

from ppe_check import DEFAULT_PHRASES, ActiveHours, AppConfig, PpeCheck, judge, judgeable

CROP = b"\xff\xd8crop"


def det(label: str, score: float = 0.7) -> dict:
    return {"label": label, "confidence": score, "bbox": {"x": 0.3, "y": 0.1, "w": 0.2, "h": 0.2}}


def person(track: str, *, score: float = 0.8, x: float = 0.3, y: float = 0.2, w: float = 0.2,
           h: float = 0.6, best: bool = True) -> dict:
    return {"label": "person", "score": score, "track_id": track, "best": best,
            "bbox": {"x": x, "y": y, "w": w, "h": h}}


def event(*people: dict, camera_id: str = "cam1") -> dict:
    return {"schema": "opennvr.tier0.v1", "adapter": "tier0", "camera_id": camera_id,
            "frame": {"w": 1920, "h": 1080},
            "tracks": [{"id": p["track_id"], "label": p["label"], "score": p["score"], "best": p["best"],
                        "box": [p["bbox"]["x"] * 1920, p["bbox"]["y"] * 1080,
                                (p["bbox"]["x"] + p["bbox"]["w"]) * 1920,
                                (p["bbox"]["y"] + p["bbox"]["h"]) * 1080]}
                       for p in people]}


FULL_KIT = [det("hard hat", 0.6), det("safety vest", 0.7)]
NO_HELMET = [det("safety vest", 0.7), det("hard hat", 0.12)]


class Clock:
    def __init__(self) -> None:
        self.t = 5000.0

    def __call__(self) -> float:
        return self.t


def make_app(looks: list[list[dict] | None], *, crops: dict | None = None, **overrides):
    cfg = app_config(**{**AppConfig().__dict__, "consume_tier0": True, **overrides})
    rec = RecorderChannel()
    app = PpeCheck(cfg, rec.dispatcher())
    clock = Clock()
    app._mono = clock
    script = list(looks)
    seen: list[tuple[str, bytes]] = []

    def detector(camera_id: str, jpeg: bytes):
        seen.append((camera_id, jpeg))
        return script.pop(0) if script else []

    app._detect = detector
    store = crops if crops is not None else {}
    app._fetch_best = lambda cam, track: store.get((cam, track), CROP)
    return app, rec, clock, seen


def test_judge_reads_any_phrase_of_an_item():
    missing, scores = judge([det("safety helmet", 0.55), det("safety vest", 0.2)], ["helmet", "vest"],
                            DEFAULT_PHRASES, 0.3)
    assert missing == ["vest"]
    assert scores == {"helmet": 0.55, "vest": 0.2}


def test_people_at_the_edge_or_too_small_are_not_judged():
    ok = dict(person_label="person", min_score=0.5, min_height=0.25, edge_margin=0.01)
    assert judgeable(person("a"), **ok)
    assert not judgeable(person("b", y=0.0), **ok)              # head above the frame
    assert not judgeable(person("c", x=0.85, w=0.2), **ok)      # off the right edge
    assert not judgeable(person("d", h=0.1), **ok)              # too small for the kit to show
    assert not judgeable(person("e", score=0.3), **ok)
    assert not judgeable({**person("f"), "label": "car"}, **ok)


def test_full_kit_is_counted_not_alerted():
    app, rec, clock, seen = make_app([FULL_KIT])
    app.handle_event(event(person("p1")))
    assert app.run_pending() == []
    assert len(seen) == 1 and seen[0] == ("cam1", CROP)
    assert app.state_snapshot()["cameras"]["cam1"] == {
        "checked": 1, "compliant": 1, "violations": 0, "suppressed": 0, "last_missing": []}
    assert rec.alerts == []


def test_missing_helmet_alerts_after_two_checks():
    app, rec, clock, seen = make_app([NO_HELMET, NO_HELMET, NO_HELMET])
    app.handle_event(event(person("p1")))
    assert app.run_pending() == []                           # first check: suspected, not alerted
    clock.t += 5
    app.handle_event(event(person("p1")))
    assert app.run_pending() == []                           # not due yet (recheck_seconds)
    clock.t += 15
    app.handle_event(event(person("p1")))
    fired = app.run_pending()
    assert [a.alert_type for a in fired] == ["ppe_missing"]
    assert fired[0].title == "Person without helmet at cam1"
    assert fired[0].evidence["missing"] == ["helmet"] and fired[0].evidence["track_id"] == "p1"
    assert fired[0].evidence["scores"]["vest"] == 0.7
    clock.t += 20
    app.handle_event(event(person("p1")))
    assert app.run_pending() == []                           # the same person does not alert twice
    assert len(rec.alerts) == 1


def test_a_flicker_does_not_alert():
    app, rec, clock, seen = make_app([NO_HELMET, FULL_KIT, NO_HELMET])
    for _ in range(3):
        app.handle_event(event(person("p1")))
        app.run_pending()
        clock.t += 20
    assert rec.alerts == []


def test_second_person_within_cooldown_is_counted_but_quiet():
    app, rec, clock, seen = make_app([NO_HELMET] * 4, recheck_seconds=0)
    app.handle_event(event(person("p1"), person("p2", x=0.6)))
    app.run_pending()
    app.handle_event(event(person("p1"), person("p2", x=0.6)))
    fired = app.run_pending()
    assert len(fired) == 1
    cam = app.state_snapshot()["cameras"]["cam1"]
    assert cam["violations"] == 2 and cam["suppressed"] == 1


def test_a_person_with_no_best_crop_yet_is_retried():
    app, rec, clock, seen = make_app([FULL_KIT], crops={("cam1", "p1"): None})
    app.handle_event(event(person("p1")))
    assert app.run_pending() == [] and seen == []
    assert app.state_snapshot()["failed_checks"] == 1
    app._fetch_best = lambda cam, track: CROP
    app.handle_event(event(person("p1")))                    # still due: no check has completed
    app.run_pending()
    assert len(seen) == 1


def test_a_person_who_left_before_their_turn_is_not_checked():
    app, rec, clock, seen = make_app([FULL_KIT, FULL_KIT], stale_after_seconds=30)
    app.handle_event(event(person("p1"), person("p2", x=0.6)))
    clock.t += 31                                            # the queue lagged half a minute
    app.handle_event(event(person("p2", x=0.6)))             # p2 is still here, p1 is not
    app.run_pending()
    assert len(seen) == 1
    assert app.state_snapshot()["skipped"] == 1


def test_a_compliant_person_is_rechecked_after_the_interval():
    app, rec, clock, seen = make_app([FULL_KIT, FULL_KIT], check_interval_seconds=60)
    app.handle_event(event(person("p1")))
    app.run_pending()
    clock.t += 30
    app.handle_event(event(person("p1")))
    assert app.run_pending() == [] and len(seen) == 1
    clock.t += 31
    app.handle_event(event(person("p1")))
    app.run_pending()
    assert len(seen) == 2


def test_live_config_changes_the_required_kit():
    app, rec, clock, seen = make_app([FULL_KIT, [det("hard hat", 0.6), det("safety vest", 0.7)]],
                                     recheck_seconds=0)
    app.handle_event(event(person("p1")))
    app.run_pending()
    app.on_config_update({"required_items": ["helmet", "vest", "gloves"], "confirm_checks": 1})
    assert app.cfg.required_items == ["helmet", "vest", "gloves"]
    app.handle_event(event(person("p1")))                    # judged afresh against the new list
    fired = app.run_pending()
    assert len(fired) == 1 and fired[0].evidence["missing"] == ["gloves"]
    app.on_config_update({"required_items": ["jetpack"]})    # no phrases for it: ignored
    assert app.cfg.required_items == ["helmet", "vest", "gloves"]


def test_config_validation():
    with pytest.raises(ValueError):
        AppConfig(required_items=[])
    with pytest.raises(ValueError):
        AppConfig(required_items=["cape"])
    with pytest.raises(ValueError):
        AppConfig(min_item_score=1.5)
    with pytest.raises(ValueError):
        AppConfig(confirm_checks=0)
    cfg = AppConfig(required_items=["Helmet", "cape"], item_phrases={"cape": ["cape", "cloak"]})
    assert cfg.required_items == ["helmet", "cape"]
    assert cfg.item_phrases["cape"] == ["cape", "cloak"] and cfg.item_phrases["vest"]


def test_off_shift_violations_are_counted_but_quiet():
    app, rec, clock, seen = make_app([NO_HELMET] * 3, recheck_seconds=0,
                                     active_hours={"start": "06:00", "end": "22:00"})
    app._wall = lambda: _dt.datetime(2026, 10, 24, 23, 0).astimezone()
    for _ in range(2):
        app.handle_event(event(person("p1")))
        app.run_pending()
    assert rec.alerts == []
    st = app.state_snapshot()
    assert st["today"]["violations"] == 1 and st["cameras"]["cam1"]["suppressed"] == 1 and st["in_hours"] is False


def test_dashboard_state_has_the_views_the_manifest_declares():
    app, rec, clock, seen = make_app([FULL_KIT, NO_HELMET, NO_HELMET], recheck_seconds=0)
    app.handle_event(event(person("p1")))
    app.run_pending()
    for _ in range(2):
        app.handle_event(event(person("p2", x=0.6)))
        app.run_pending()
    st = app.state_snapshot()
    for view in PpeCheck.manifest.state_schema:
        assert view.path.split(".")[0] in st, view.path
    assert st["today"]["checked"] == 3 and st["today"]["compliance_pct"] == 33.3 and st["today"]["violations"] == 1
    assert st["per_camera"][0]["compliance_pct"] == 33.3 and st["per_camera"][0]["last_missing"] == "helmet"
    assert st["by_item"] == [{"item": "helmet", "today": 1}]
    assert st["recent"][0]["message"] == "Person without helmet at cam1"
    assert st["gallery"][0]["image"] == "data:image/jpeg;base64," + base64.b64encode(CROP).decode()


def test_active_hours_parse():
    hours = ActiveHours.parse({"start": "22:00", "end": "06:00"})
    assert hours.contains(_dt.datetime(2026, 1, 1, 23, 30)) and not hours.contains(_dt.datetime(2026, 1, 1, 12, 0))
    assert ActiveHours.parse(None) is None
    with pytest.raises(ValueError):
        ActiveHours.parse({"start": "x", "end": "06:00"})
