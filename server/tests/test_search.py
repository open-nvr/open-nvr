# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HA-116: GET /search.

* structured filters over events (label, plate, zone by id or name, time,
  source, free text) and alerts (severity, source, free text), newest first;
* the zone filter matches the id anywhere in the JSON list, not a prefix;
* scope: each half needs its own permission, cameras are the caller's,
  tokens are limited to their allow-list.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture

T0 = datetime(2026, 9, 18, 8, 0, tzinfo=UTC)


@pytest.fixture()
def data(env):  # noqa: F811
    s = env.Session()
    Z = env.models.CameraZone
    drive = Z(camera_id=1, name="Driveway", polygon=[[0, 0], [1, 0], [1, 1]])
    porch = Z(camera_id=2, name="Porch", polygon=[[0, 0], [1, 0], [1, 1]])
    s.add_all([drive, porch])
    s.commit()
    E = env.models.TimelineEvent

    def ev(cam, label, minutes, zones=None, plate=None):
        s.add(E(camera_id=cam, source="tier0", event_type="track", label=label,
                started_at=T0 + timedelta(minutes=minutes),
                ended_at=T0 + timedelta(minutes=minutes, seconds=20),
                zone_ids=zones, plate_text=plate))

    ev(1, "car", 0, [drive.id], plate="KA01AB1234")
    ev(1, "person", 5, [3, drive.id])
    ev(1, "person", 10, [drive.id + 10])       # zone 1x, not zone x
    ev(2, "person", 15, [porch.id])
    ev(3, "dog", 20)
    s.add(env.models.AppAlert(alert_id="a1", fired_at=T0 + timedelta(minutes=7),
                              severity="critical", title="Person loitering at gate",
                              camera_id="cam1", source_name="loitering"))
    s.add(env.models.AppAlert(alert_id="a2", fired_at=T0 + timedelta(minutes=8),
                              severity="low", title="Plate read", camera_id="cam2",
                              source_name="anpr"))
    s.commit()
    ids = {"drive": drive.id, "porch": porch.id}
    s.close()
    return ids


def _q(env, headers=None, **params):  # noqa: F811
    r = env.client.get("/api/v1/search", headers=headers or env.jwt("admin"), params=params)
    assert r.status_code == 200, r.text
    return r.json()["results"]


def test_everything_newest_first(env, data):  # noqa: F811
    got = _q(env)
    assert [r["kind"] for r in got] == ["event", "event", "event", "alert", "alert", "event",
                                        "event"]
    assert got[0]["label"] == "dog" and got[3]["title"] == "Plate read"


def test_event_filters(env, data):  # noqa: F811
    assert [r["label"] for r in _q(env, type="events", label="person")] == ["person"] * 3
    assert [r["plate_text"] for r in _q(env, plate="ka01 ab")] == ["KA01AB1234"]
    drive = _q(env, zone=str(data["drive"]))
    # The car (only zone) and the person (last of two zones); NOT the event
    # in zone drive+10, which a prefix match would also have returned.
    assert [r["label"] for r in drive] == ["person", "car"]
    assert len(_q(env, zone="driveway")) == 2                       # by name, any case
    assert [r["camera_id"] for r in _q(env, zone="Porch")] == [2]
    assert [r["label"] for r in _q(env, type="events",
                                   **{"from": (T0 + timedelta(minutes=12)).isoformat()})] == \
        ["dog", "person"]
    assert [r["label"] for r in _q(env, q="dog")] == ["dog"]
    # Label/plate/zone filters are event-only: alerts drop out, never match loosely.
    assert all(r["kind"] == "event" for r in _q(env, zone="Porch"))


def test_alert_filters(env, data):  # noqa: F811
    assert [r["alert_id"] for r in _q(env, type="alerts", severity="critical")] == ["a1"]
    assert [r["alert_id"] for r in _q(env, type="alerts", q="loiter")] == ["a1"]
    assert [r["alert_id"] for r in _q(env, type="alerts", camera_id=2)] == ["a2"]


def test_unknown_or_ambiguous_zone(env, data):  # noqa: F811
    assert env.client.get("/api/v1/search", headers=env.jwt("admin"),
                          params={"zone": "garden"}).status_code == 404


def test_scope_and_permissions(env, data):  # noqa: F811
    # vera: camera 3 only, recordings.view but no alerts.view.
    got = _q(env, env.jwt("vera"))
    assert [(r["kind"], r["camera_id"]) for r in got] == [("event", 3)]
    assert env.client.get("/api/v1/search", headers=env.jwt("vera"),
                          params={"camera_id": 1}).status_code == 404
    tok = _mint(env, scopes=["cameras.view", "alerts.view"], camera_ids=[1])["token"]
    got = _q(env, _as(tok))
    assert [(r["kind"], r["camera_id"]) for r in got] == [("alert", 1)]
    nothing = _mint(env, name="n", scopes=["cameras.view"])["token"]
    assert _q(env, _as(nothing)) == []


def test_zone_names_are_exact_and_scoped(env, data):  # noqa: F811
    """M1 review: ilike took user wildcards and saw other users' zones."""
    assert env.client.get("/api/v1/search", headers=env.jwt("admin"),
                          params={"zone": "drive%"}).status_code == 404
    # vera (camera 3 only) cannot probe the Porch zone on camera 2.
    assert env.client.get("/api/v1/search", headers=env.jwt("vera"),
                          params={"zone": "Porch"}).status_code == 404


def test_alert_camera_filter_is_not_cut_by_the_limit(env, data):  # noqa: F811
    s = env.Session()
    for i in range(10):
        s.add(env.models.AppAlert(alert_id=f"n{i}", fired_at=T0 + timedelta(hours=1, minutes=i),
                                  severity="low", title="noise", camera_id="cam1"))
    s.commit()
    s.close()
    got = _q(env, type="alerts", camera_id=2, limit=2)
    assert [r["alert_id"] for r in got] == ["a2"]
