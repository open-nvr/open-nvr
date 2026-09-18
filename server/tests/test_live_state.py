# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HA-110: live state from synthetic Tier-0 frames.

* counts per label, total vs active (stationary is present, not active);
* the overlay's "present" rule and score floor apply;
* per-zone counts from the box's bottom-centre, honouring label filters;
* track start and end; a camera that goes quiet drops to zero (stale) and
  the sweep ends its tracks;
* motion turns on at once and off only after the off-window;
* the consumer updates live state even with the overlay off, and
  publishes a ``live_state`` event on change;
* ``GET /live-state`` is scoped to the caller's cameras and a token's
  allow-list.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from services.live_state import LiveState
from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture

W, H = 1000, 500


def _t(tid, label="person", box=(100, 100, 200, 400), stationary=False, score=0.9, **kw):
    return {"id": tid, "label": label, "box": list(box), "score": score,
            "stationary": stationary, "matched": True, **kw}


def _frame(*tracks):
    return {"frame": {"w": W, "h": H}, "tracks": list(tracks)}


def test_counts_total_and_active():
    ls = LiveState()
    ls.update(1, _frame(_t(1), _t(2), _t(3, "car", stationary=True)), now=100)
    s = ls.camera(1, now=100)
    assert s["objects"] == {"person": {"total": 2, "active": 2},
                            "car": {"total": 1, "active": 0}}
    assert s["stale"] is False and s["motion"] is True


def test_low_score_and_absent_tracks_do_not_count():
    ls = LiveState()
    ls.update(1, _frame(_t(1, score=0.1),
                        _t(2, matched=False, misses=2),   # looked for, not found
                        _t(3)), now=100)
    assert ls.camera(1, now=100)["objects"] == {"person": {"total": 1, "active": 1}}


def test_zone_counts_use_where_the_object_stands():
    ls = LiveState()
    zone = SimpleNamespace(id=7, name="drive",
                           polygon=[[0, 0.6], [0.5, 0.6], [0.5, 1], [0, 1]], labels=None)
    cars = SimpleNamespace(id=8, name="bay",
                           polygon=[[0, 0], [1, 0], [1, 1], [0, 1]], labels=["car"])
    ls.set_zones(1, [zone, cars])
    # Box top is outside the drive, but its feet (y2=400 → 0.8) are inside.
    ls.update(1, _frame(_t(1, box=(100, 100, 200, 400)),
                        _t(2, box=(800, 0, 900, 100))), now=100)
    zones = {z["zone_id"]: z for z in ls.camera(1, now=100)["zones"]}
    assert zones[7]["objects"] == {"person": {"total": 1, "active": 1}}
    assert zones[8]["objects"] == {}   # people don't count in a cars-only zone


def test_start_and_end_and_quiet_cameras_drop_to_zero():
    ls = LiveState(stale_s=5)
    d = ls.update(1, _frame(_t(1)), now=100)
    assert [s["track_id"] for s in d["started"]] == ["1"] and d["changed"]
    d = ls.update(1, _frame(_t(1), _t(2)), now=101)
    assert [s["track_id"] for s in d["started"]] == ["2"] and not d["ended"]
    d = ls.update(1, _frame(_t(2)), now=102)
    assert [e["track_id"] for e in d["ended"]] == ["1"]
    # No frames: Tier-0 sends none when nothing is detected.
    assert ls.camera(1, now=106)["objects"] == {"person": {"total": 1, "active": 1}}
    assert ls.camera(1, now=108)["objects"] == {} and ls.camera(1, now=108)["stale"]
    swept = ls.sweep(now=108)
    assert swept == [(1, [{"track_id": "2", "label": "person", "zones": [], "duration_s": 1.0}])]
    assert ls.sweep(now=109) == []


def test_frames_after_a_gap_do_not_resurrect_old_tracks():
    ls = LiveState(stale_s=5)
    ls.update(1, _frame(_t(1)), now=100)
    d = ls.update(1, _frame(_t(2)), now=120)
    assert d["ended"] == [] and [s["track_id"] for s in d["started"]] == ["2"]


def test_motion_is_debounced():
    ls = LiveState(motion_off_after_s=10)
    ls.update(1, _frame(_t(1)), now=100)
    assert ls.camera(1, now=105)["motion"] is True
    ls.update(1, _frame(_t(1, stationary=True)), now=106)   # stopped moving
    assert ls.camera(1, now=109)["motion"] is True
    assert ls.camera(1, now=111)["motion"] is False
    assert LiveState().camera(9, now=0)["motion"] is False


# ── consumer hook ─────────────────────────────────────────────────────


@pytest.fixture()
def consumer(monkeypatch):
    import services.live_state as ls_mod
    import services.tier0_track_consumer as tc
    from services import event_bus_service as bus

    fresh = LiveState()
    monkeypatch.setattr(ls_mod, "_instance", fresh)
    monkeypatch.setattr(ls_mod, "refresh_zones_if_due", lambda cid, now=None: None)
    sent = []

    async def fake_publish(event):
        sent.append(event)

    monkeypatch.setattr(bus.get_event_bus(), "publish", fake_publish)
    return tc, fresh, sent


def _msg(raw):
    import json

    return SimpleNamespace(data=json.dumps(raw).encode(),
                           subject="opennvr.inference.tier0.cam1.completed")


def test_consumer_updates_live_state_and_publishes_on_change(consumer, monkeypatch):
    tc, live, sent = consumer
    monkeypatch.setattr(tc, "_overlay_enabled", lambda: True)
    raw = {"camera_id": "cam1", **_frame(_t(1))}
    asyncio.run(tc._handle_message(_msg(raw)))
    kinds = [e["event_type"] for e in sent]
    assert "live_state" in kinds and "tracks" in kinds
    ev = next(e for e in sent if e["event_type"] == "live_state")
    assert ev["camera_id"] == 1 and ev["payload"]["started"][0]["track_id"] == "1"
    sent.clear()
    asyncio.run(tc._handle_message(_msg(raw)))  # same frame again: no change
    assert [e["event_type"] for e in sent] == ["tracks"]


def test_the_handler_never_draws_boxes_with_the_overlay_off(consumer, monkeypatch):
    tc, live, sent = consumer
    monkeypatch.setattr(tc, "_overlay_enabled", lambda: False)
    asyncio.run(tc._handle_message(_msg({"camera_id": "cam1", **_frame(_t(1))})))
    assert [e["event_type"] for e in sent] == ["live_state"]
    assert live.camera(1)["objects"] == {"person": {"total": 1, "active": 1}}


# ── endpoint ──────────────────────────────────────────────────────────


def test_live_state_endpoint_is_scoped(env, monkeypatch):  # noqa: F811
    import services.live_state as ls_mod

    fresh = LiveState()
    monkeypatch.setattr(ls_mod, "_instance", fresh)
    fresh.update(1, _frame(_t(1)))
    fresh.update(3, _frame(_t(5, "car")))
    body = env.client.get("/api/v1/live-state", headers=env.jwt("admin")).json()
    cams = {c["camera_id"]: c for c in body["cameras"]}
    assert set(cams) == {1, 2, 3}
    assert cams[1]["objects"] == {"person": {"total": 1, "active": 1}}
    assert cams[2]["objects"] == {} and cams[2]["last_object"] is None
    # vera sees camera 3 only.
    vera = env.client.get("/api/v1/live-state", headers=env.jwt("vera")).json()
    assert [c["camera_id"] for c in vera["cameras"]] == [3]
    assert env.client.get("/api/v1/live-state?camera_id=1",
                          headers=env.jwt("vera")).status_code == 404
    tok = _mint(env, scopes=["cameras.view"], camera_ids=[1])["token"]
    got = env.client.get("/api/v1/live-state", headers=_as(tok)).json()
    assert [c["camera_id"] for c in got["cameras"]] == [1]


def test_last_plate_and_last_object_come_from_the_event_store(env, monkeypatch):  # noqa: F811
    from datetime import UTC, datetime

    import services.live_state as ls_mod
    from services.timeline_service import record_track_visit

    monkeypatch.setattr(ls_mod, "_instance", LiveState())
    s = env.Session()
    row = record_track_visit(s, camera_id=1, label="car", started_at=datetime.now(UTC),
                             evidence_path="ab/cd.jpg", zone_ids=[4])
    row.plate_text = "KA01AB1234"
    s.commit()
    rid = row.id
    s.close()
    cam = env.client.get("/api/v1/live-state?camera_id=1",
                         headers=env.jwt("admin")).json()["cameras"][0]
    assert cam["last_plate"]["text"] == "KA01AB1234"
    assert cam["last_object"] == {**cam["last_object"], "label": "car", "event_id": rid,
                                  "zone_ids": [4],
                                  "evidence_url": f"/api/v1/events/{rid}/evidence"}


def test_plates_need_recordings_view(env, monkeypatch):  # noqa: F811
    """M1 review: a cameras.view-only token used to read plate text here."""
    from datetime import UTC, datetime

    import services.live_state as ls_mod
    from services.timeline_service import record_track_visit

    monkeypatch.setattr(ls_mod, "_instance", LiveState())
    s = env.Session()
    row = record_track_visit(s, camera_id=1, label="car", started_at=datetime.now(UTC))
    row.plate_text = "KA01AB1234"
    s.commit()
    s.close()
    view_only = _mint(env, scopes=["cameras.view"], camera_ids=[1])["token"]
    cam = env.client.get("/api/v1/live-state", headers=_as(view_only)).json()["cameras"][0]
    assert cam["last_plate"] is None and cam["last_object"] is None
    rec = _mint(env, name="r", scopes=["cameras.view", "recordings.view"],
                camera_ids=[1])["token"]
    cam = env.client.get("/api/v1/live-state", headers=_as(rec)).json()["cameras"][0]
    assert cam["last_plate"]["text"] == "KA01AB1234"


def test_overlay_off_means_no_consumer_at_all(monkeypatch):
    """M1 review: DETECTION_OVERLAY_ENABLED=false promised "no track data
    will reach any consumer"; the consumer must not start (no live counts)."""
    import services.tier0_track_consumer as tc

    monkeypatch.setattr(tc, "_overlay_enabled", lambda: False)
    connected = []

    class _Nats:
        @staticmethod
        async def connect(*a, **k):
            connected.append(1)

    monkeypatch.setitem(__import__("sys").modules, "nats", _Nats)
    asyncio.run(tc.run_consumer_loop())
    assert connected == []
