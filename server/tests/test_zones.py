# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HA-109: camera zones, and visits tagged with the zones they crossed.

* the geometry (point in polygon, polygon validation, path cleaning);
* ingest stores ``zone_ids`` from the pipeline's path, honours a zone's
  label filter, and a bad or missing path never costs the visit;
* zones CRUD: seeing needs the camera, changing needs cameras.manage and
  the camera; names are unique per camera; everything is audited; a token
  may only read them.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from services import zones as zs
from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture

SQUARE = [[0.2, 0.2], [0.6, 0.2], [0.6, 0.6], [0.2, 0.6]]


# ── geometry ─────────────────────────────────────────────────────────


def test_point_in_polygon():
    assert zs.point_in_polygon(0.4, 0.4, SQUARE)
    assert not zs.point_in_polygon(0.1, 0.4, SQUARE)
    assert not zs.point_in_polygon(0.4, 0.7, SQUARE)
    # A concave "L": the notch is outside.
    ell = [[0, 0], [1, 0], [1, 0.5], [0.5, 0.5], [0.5, 1], [0, 1]]
    assert zs.point_in_polygon(0.25, 0.75, ell)
    assert not zs.point_in_polygon(0.75, 0.75, ell)


@pytest.mark.parametrize("poly", [
    [[0, 0], [1, 1]],                          # too few points
    [[0, 0], [1, 0], [2, 1]],                  # out of range
    [[0, 0], [0.5, 0.5], [1, 1]],              # no area
    [[0, 0], [1, 0], [True, 1]],               # bool is not a coordinate
    [[0, 0], [1, 0], [float("nan"), 1]],
    [[i / 40, (i % 2) / 2] for i in range(33)],  # too many points
])
def test_bad_polygons_are_refused(poly):
    with pytest.raises(ValueError):
        zs.valid_polygon(poly)


def test_clean_path_drops_bad_paths():
    assert zs.clean_path(None) is None and zs.clean_path([]) is None
    assert zs.clean_path([[0.1, 0.2], [1.5, 0.2]]) is None
    assert zs.clean_path([[0.1, 0.2]]) == [(0.1, 0.2)]
    assert len(zs.clean_path([[0.5, 0.5]] * 500)) == zs.MAX_PATH_POINTS


# ── ingest ───────────────────────────────────────────────────────────


def _ingest(env, **body):  # noqa: F811
    from fastapi import BackgroundTasks

    from routers import internal_camera_agent as ica

    payload = ica.TrackEventIn(camera_id=1, label=body.pop("label", "person"),
                               started_at=datetime.now(UTC) - timedelta(seconds=30),
                               ended_at=datetime.now(UTC), track_id=body.pop("track", "t1"),
                               **body)
    s = env.Session()
    try:
        out = asyncio.run(ica.ingest_track_event(payload, BackgroundTasks(), None, s))
        return s.get(env.models.TimelineEvent, out["id"]).zone_ids
    finally:
        s.close()


def _zone(env, cam=1, **body):  # noqa: F811
    body.setdefault("name", "drive")
    body.setdefault("polygon", SQUARE)
    r = env.client.post(f"/api/v1/cameras/{cam}/zones", headers=env.jwt("admin"), json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_ingest_tags_the_zones_the_path_crossed(env):  # noqa: F811
    drive = _zone(env, name="drive")
    lawn = _zone(env, name="lawn", polygon=[[0.7, 0.7], [0.9, 0.7], [0.9, 0.9], [0.7, 0.9]])
    cars_only = _zone(env, name="bay", polygon=SQUARE, labels=["car"])
    _zone(env, cam=2, name="elsewhere")

    # Walks through the drive and ends on the lawn.
    assert _ingest(env, path=[[0.3, 0.3], [0.5, 0.5], [0.8, 0.8]]) == sorted([drive, lawn])
    # A car in the same place also counts in the cars-only bay.
    assert _ingest(env, label="car", track="t2", path=[[0.3, 0.3]]) == sorted([drive, cars_only])
    # Crossed no zone: computed, and empty.
    assert _ingest(env, track="t3", path=[[0.05, 0.05]]) == []


def test_no_path_or_a_bad_path_still_stores_the_visit(env):  # noqa: F811
    _zone(env)
    assert _ingest(env, track="a") is None
    assert _ingest(env, track="b", path=[[0.3, 7.0]]) is None


def test_zone_filter_matches_the_stored_json_text_form(env):  # noqa: F811
    """``zone_filter`` is four LIKE patterns written against ``json.dumps``
    with its default separators. Pin the text the JSON column actually
    stores (through ``record_track_visit``, the real writer) so a changed
    serializer fails here rather than silently emptying zone filters."""
    from sqlalchemy import String, cast

    from services.timeline_service import record_track_visit, zone_filter

    s = env.Session()
    now = datetime.now(UTC)
    row = record_track_visit(s, camera_id=1, label="person", started_at=now, zone_ids=[1, 4])
    record_track_visit(s, camera_id=1, label="person", started_at=now, zone_ids=[14])
    record_track_visit(s, camera_id=1, label="person", started_at=now, zone_ids=[41, 1])
    TE = env.models.TimelineEvent
    stored = s.query(cast(TE.zone_ids, String)).filter(TE.id == row.id).scalar()
    assert stored == "[1, 4]" == json.dumps([1, 4])

    def ids(zone_id):
        return sorted(r.zone_ids for r in s.query(TE).filter(zone_filter(zone_id)).all())

    assert ids(4) == [[1, 4]]              # last item
    assert ids(1) == [[1, 4], [41, 1]]     # first and last, never inside 14 or 41
    assert ids(14) == [[14]]               # only item; not a prefix of "1, 4"
    assert ids(41) == [[41, 1]]
    assert ids(2) == []
    s.close()


def test_events_api_returns_zone_ids(env):  # noqa: F811
    zid = _zone(env)
    _ingest(env, path=[[0.3, 0.3]])
    events = env.client.get("/api/v1/events", headers=env.jwt("admin")).json()["events"]
    assert events[0]["zone_ids"] == [zid]


# ── CRUD ─────────────────────────────────────────────────────────────


def test_zone_crud_is_audited(env):  # noqa: F811
    A = env.jwt("admin")
    zid = _zone(env, name=" Front drive ", labels=["Person", "car", "car"])
    listed = env.client.get("/api/v1/cameras/1/zones", headers=A).json()["zones"]
    assert listed[0]["name"] == "Front drive" and listed[0]["labels"] == ["car", "person"]
    r = env.client.put(f"/api/v1/cameras/1/zones/{zid}", headers=A,
                       json={"name": "drive", "polygon": SQUARE})
    assert r.status_code == 200 and r.json()["labels"] is None
    assert env.client.delete(f"/api/v1/cameras/1/zones/{zid}", headers=A).status_code == 200
    assert env.client.get("/api/v1/cameras/1/zones", headers=A).json()["zones"] == []
    s = env.Session()
    actions = [a for (a,) in s.query(env.models.AuditLog.action)
               .filter(env.models.AuditLog.action.like("zone.%"))
               .order_by(env.models.AuditLog.id).all()]
    s.close()
    assert actions == ["zone.create", "zone.update", "zone.delete"]


def test_zone_names_are_unique_per_camera(env):  # noqa: F811
    _zone(env, name="drive")
    r = env.client.post("/api/v1/cameras/1/zones", headers=env.jwt("admin"),
                        json={"name": "drive", "polygon": SQUARE})
    assert r.status_code == 409
    _zone(env, cam=2, name="drive")  # another camera may reuse it


def test_bad_zone_bodies_are_422(env):  # noqa: F811
    A = env.jwt("admin")
    for body in ({"name": "x", "polygon": [[0, 0], [1, 1]]},
                 {"name": "  ", "polygon": SQUARE},
                 {"name": "x", "polygon": SQUARE, "labels": ["<b>"]}):
        assert env.client.post("/api/v1/cameras/1/zones", headers=A, json=body).status_code == 422


def test_who_may_see_and_change_zones(env):  # noqa: F811
    _zone(env)
    V = env.jwt("vera")  # sees camera 3 only, has no cameras.manage
    assert env.client.get("/api/v1/cameras/1/zones", headers=V).status_code == 404
    assert env.client.get("/api/v1/cameras/3/zones", headers=V).status_code == 200
    assert env.client.post("/api/v1/cameras/3/zones", headers=V,
                           json={"name": "x", "polygon": SQUARE}).status_code == 403


def test_a_token_reads_zones_on_its_cameras_and_never_writes(env):  # noqa: F811
    _zone(env)
    tok = _mint(env, scopes=["cameras.view", "cameras.manage"], camera_ids=[1])["token"]
    assert env.client.get("/api/v1/cameras/1/zones", headers=_as(tok)).status_code == 200
    assert env.client.get("/api/v1/cameras/2/zones", headers=_as(tok)).status_code == 403
    assert env.client.post("/api/v1/cameras/1/zones", headers=_as(tok),
                           json={"name": "t", "polygon": SQUARE}).status_code == 403
