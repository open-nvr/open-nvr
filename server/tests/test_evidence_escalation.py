# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The escalation proposal (services/evidence_escalation.py): a camera
whose visits got few full-resolution crops, or whose subjects were small
on the detect frame, is proposed for main-stream detection; the operator
accepts (detect_stream becomes main, the previous value remembered),
dismisses for a while, or resets. The camera list carries the state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.test_api_tokens import env  # noqa: F401 - shared fixture

URL = "/api/v1/cameras/{cam}/escalation"


def _esc():
    # Imported per test, never at module level: conftest swaps the core.*
    # modules between test modules, and a service bound at collection time
    # would keep a stale core (see tests/conftest.py).
    from services import evidence_escalation as esc
    return esc


def _visits(env, cam, n, *, source="detect", box_px=120, hours_ago=1.0):  # noqa: F811
    s = env.Session()
    t0 = datetime.now(UTC) - timedelta(hours=hours_ago)
    for i in range(n):
        s.add(env.models.TimelineEvent(
            camera_id=cam, source="tier0", event_type="track", label="person",
            track_id=f"t{cam}-{source}-{box_px}-{i}", started_at=t0 + timedelta(seconds=i),
            payload={"evidence": {"source": source, "box_px": box_px, "frame_px": 576}}))
    s.commit()
    s.close()
    _esc().invalidate()


@pytest.fixture(autouse=True)
def _fresh(env):  # noqa: F811
    _esc().invalidate()
    yield
    _esc().invalidate()


def test_too_few_visits_or_good_evidence_is_no_proposal(env):  # noqa: F811
    _visits(env, 1, 5, source="detect")                       # under MIN_VISITS
    _visits(env, 2, 30, source="playback")                    # full-res, big
    r = env.client.get(URL.format(cam=1), headers=env.jwt("admin")).json()
    assert r["escalation"] is None and r["evaluation"]["visits"] == 5
    r = env.client.get(URL.format(cam=2), headers=env.jwt("admin")).json()
    assert r["escalation"] is None
    assert r["evaluation"]["full_res_share"] == 1.0 and r["evaluation"]["qualifies"] is False


def test_low_res_evidence_over_a_day_is_proposed_and_small_subjects_too(env):  # noqa: F811
    _visits(env, 1, 16, source="detect")
    _visits(env, 1, 8, source="playback")
    _visits(env, 2, 12, source="playback", box_px=30)         # full-res but tiny
    _visits(env, 2, 10, source="playback", box_px=200)
    _visits(env, 3, 40, source="detect", hours_ago=30)        # outside the window
    r = env.client.get(URL.format(cam=1), headers=env.jwt("admin")).json()
    assert r["escalation"]["status"] == "proposed" and r["escalation"]["reasons"] == ["low_res_evidence"]
    assert r["evaluation"]["full_res_share"] == round(8 / 24, 3)
    assert "cost_note" in r["escalation"] and r["evaluation"]["cost_note"]
    r = env.client.get(URL.format(cam=2), headers=env.jwt("admin")).json()
    assert r["escalation"]["reasons"] == ["small_subjects"]
    r = env.client.get(URL.format(cam=3), headers=env.jwt("admin")).json()
    assert r["escalation"] is None and r["evaluation"]["visits"] == 0


def test_a_camera_already_on_main_is_never_proposed(env):  # noqa: F811
    env.client.put("/api/v1/cameras/1/detection-settings", headers=env.jwt("admin"),
                   json={"detect_stream": "main", "fps": 5})
    _visits(env, 1, 30, source="detect")
    r = env.client.get(URL.format(cam=1), headers=env.jwt("admin")).json()
    assert r["escalation"] is None and r["evaluation"]["detect_stream"] == "main"


def test_accept_switches_to_main_keeps_the_other_overrides_and_reset_puts_it_back(env):  # noqa: F811
    A = env.jwt("admin")
    env.client.put("/api/v1/cameras/1/detection-settings", headers=A, json={"detect_stream": "sub", "fps": 5})
    _visits(env, 1, 30, source="detect")
    r = env.client.post(URL.format(cam=1) + "/accept", headers=A)
    assert r.status_code == 200, r.text
    assert r.json()["escalation"]["status"] == "accepted" and r.json()["escalation"]["previous"] == "sub"
    assert r.json()["escalation"]["review_due"] is False
    d = env.client.get("/api/v1/cameras/1/detection-settings", headers=A).json()["detection"]
    assert d == {"detect_stream": "main", "fps": 5}
    # the camera list carries the state
    cams = {c["id"]: c for c in env.client.get("/api/v1/cameras/", headers=A).json()["cameras"]}
    assert cams[1]["escalation"] == {"status": "accepted", "reasons": ["low_res_evidence"], "review_due": False}
    assert cams[2]["escalation"] is None
    r = env.client.post(URL.format(cam=1) + "/reset", headers=A)
    assert r.status_code == 200
    d = env.client.get("/api/v1/cameras/1/detection-settings", headers=A).json()["detection"]
    assert d == {"detect_stream": "sub", "fps": 5}
    assert r.json()["escalation"]["status"] == "proposed", "still qualifies, so proposed again"


def test_dismiss_holds_for_its_days_then_the_proposal_returns(env, monkeypatch):  # noqa: F811
    A = env.jwt("admin")
    _visits(env, 1, 30, source="detect")
    r = env.client.post(URL.format(cam=1) + "/dismiss", headers=A, json={"days": 3})
    assert r.status_code == 200 and r.json()["escalation"]["status"] == "dismissed"
    until = datetime.fromisoformat(r.json()["escalation"]["dismissed_until"])
    assert timedelta(days=2, hours=23) < until - datetime.now(UTC) <= timedelta(days=3)
    s = env.Session()
    esc = _esc()
    ev = esc.evaluations(s)[1]
    assert esc.status_for(s, 1, ev, now=datetime.now(UTC) + timedelta(days=4))["status"] == "proposed"
    s.close()


def test_deciding_needs_cameras_manage_and_the_camera(env):  # noqa: F811
    _visits(env, 3, 30, source="detect")
    V = env.jwt("vera")
    assert env.client.get(URL.format(cam=3), headers=V).status_code == 200
    assert env.client.get(URL.format(cam=1), headers=V).status_code == 404
    assert env.client.post(URL.format(cam=3) + "/accept", headers=V).status_code == 403


def test_ingest_keeps_the_evidence_facts_in_the_payload():
    from services.timeline_service import record_track_visit
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import models
    eng = create_engine("sqlite://")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    row = record_track_visit(s, camera_id=1, label="person", started_at=datetime.now(UTC),
                             stationary=False, evidence={"source": "playback", "box_px": 60})
    assert row.payload == {"stationary": False, "evidence": {"source": "playback", "box_px": 60}}
    row = record_track_visit(s, camera_id=1, label="car", started_at=datetime.now(UTC))
    assert row.payload is None
