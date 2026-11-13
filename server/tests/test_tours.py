# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Live View tours (routers/tours.py).

* CRUD is audited; a bad camera or layout is a 422 naming what is wrong;
* every read is filtered to the caller's cameras, binned cameras drop out,
  and a layout deleted or disabled later is flagged, never an error;
* running needs live.view, changing needs cameras.manage; a manager who
  cannot see every camera in a tour may not overwrite it; tokens get 403;
* a stale revision is a 409; a corrupt window-settings row degrades to the
  predefined layouts instead of failing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture

URL = "/api/v1/tours"


def _body(**kw):
    body = {"name": "Ops wall", "pages": [
        {"label": "North", "layout_id": "2x2", "duration_s": 30, "camera_ids": [1, 2]},
        {"label": "South", "layout_id": "1x1", "duration_s": 60, "camera_ids": [3]},
    ]}
    body.update(kw)
    return body


def _create(env, **kw):  # noqa: F811
    r = env.client.post(URL, headers=env.jwt("admin"), json=_body(**kw))
    assert r.status_code == 201, r.text
    return r.json()


def _window_settings(env, value):  # noqa: F811
    s = env.Session()
    s.add(env.models.SecuritySetting(key="window_division_settings",
                                     json_value=value if isinstance(value, str)
                                     else json.dumps(value)))
    s.commit()
    s.close()


def _operator(env, *, view=(1,), manage=(1,)):  # noqa: F811
    """olga: cameras.manage + live.view, scoped to the given cameras."""
    m = env.models
    s = env.Session()
    perms = {p.name: p for p in s.query(m.Permission).all()}
    role = m.Role(name="operator")
    s.add(role)
    s.commit()
    for n in ("cameras.view", "cameras.manage", "live.view"):
        s.add(m.RolePermission(role_id=role.id, permission_id=perms[n].id))
    u = m.User(mfa_enabled=True, username="olga", email="o@x", hashed_password="h", is_active=True,
               is_superuser=False, role_id=role.id)
    s.add(u)
    s.commit()
    for cid in set(view) | set(manage):
        s.add(m.CameraPermission(user_id=u.id, camera_id=cid, can_view=cid in view,
                                 can_manage=cid in manage))
    s.commit()
    s.close()
    return env.jwt("olga")


# ── CRUD ─────────────────────────────────────────────────────────────


def test_tour_crud_is_audited(env):  # noqa: F811
    A = env.jwt("admin")
    t = _create(env, name="  Ops wall ")
    assert t["name"] == "Ops wall" and t["revision"] == 1 and t["editable"]
    assert [s["camera_ids"] for s in t["pages"]] == [[1, 2], [3]]
    assert env.client.get(URL, headers=A).json()["tours"][0]["id"] == t["id"]

    r = env.client.put(f"{URL}/{t['id']}", headers=A,
                       json=_body(name="Night", expected_revision=1))
    assert r.status_code == 200 and r.json()["name"] == "Night" and r.json()["revision"] == 2
    assert env.client.delete(f"{URL}/{t['id']}", headers=A).status_code == 200
    assert env.client.get(f"{URL}/{t['id']}", headers=A).status_code == 404

    s = env.Session()
    actions = [a for (a,) in s.query(env.models.AuditLog.action)
               .filter(env.models.AuditLog.action.like("tour.%"))
               .order_by(env.models.AuditLog.id).all()]
    s.close()
    assert actions == ["tour.create", "tour.update", "tour.delete"]


def test_page_keeps_order_and_drops_duplicates(env):  # noqa: F811
    # The second copy leaves its tile empty: no later camera moves up.
    t = _create(env, pages=[{"layout_id": "2x2", "camera_ids": [3, 1, 3, 2]}])
    assert t["pages"][0]["camera_ids"] == [3, 1, None, 2]
    assert t["pages"][0]["duration_s"] == 60


def test_empty_tiles_stay_where_they_are(env):  # noqa: F811
    t = _create(env, pages=[{"layout_id": "2x2", "camera_ids": [None, 1, None, 2, None, None]}])
    assert t["pages"][0]["camera_ids"] == [None, 1, None, 2]  # trailing gaps trimmed
    got = env.client.get(f"{URL}/{t['id']}", headers=env.jwt("admin")).json()
    assert got["pages"][0]["camera_ids"] == [None, 1, None, 2]


def test_a_page_with_more_cameras_than_tiles_is_saved_as_several(env):  # noqa: F811
    """What the wall would have paged through becomes pages you can see:
    same label, layout and time, cut at the layout's tile count."""
    t = _create(env, pages=[
        {"label": "North", "layout_id": "2x2", "duration_s": 45, "camera_ids": [1, 3, None, None, None, None, None, None, None, 2]},
        {"label": "South", "layout_id": "1x1", "camera_ids": [3]},
    ])
    assert [(p["label"], p["layout_id"], p["duration_s"], p["camera_ids"]) for p in t["pages"]] == [
        ("North", "2x2", 45, [1, 3]),  # the middle slice was empty tiles only: dropped
        ("North", "2x2", 45, [None, 2]),
        ("South", "1x1", 60, [3]),
    ]
    s = env.Session()
    assert [p["camera_ids"] for p in s.get(env.models.CameraTour, t["id"]).pages] == [[1, 3], [None, 2], [3]]
    s.close()


def test_a_tour_stored_as_steps_reads_as_pages(env):  # noqa: F811
    """A row saved when a step paged through its cameras plays exactly as
    before, and the old name is still served for a wall running that app."""
    _window_settings(env, {"custom_layouts": [
        {"id": "wall", "name": "Wall", "grid_columns": 3, "grid_rows": 1}]})
    t = _create(env)
    s = env.Session()
    s.get(env.models.CameraTour, t["id"]).pages = [
        {"label": "North", "layout_id": "wall", "duration_s": 30, "camera_ids": [1, 2, 3, 1, None, 2, 3]},
        {"label": "Gone", "layout_id": "nope", "duration_s": 30, "camera_ids": [3] * 30},
    ]
    s.commit()
    s.close()
    got = env.client.get(f"{URL}/{t['id']}", headers=env.jwt("admin")).json()
    assert [(p["label"], p["camera_ids"]) for p in got["pages"]] == [
        ("North", [1, 2, 3]), ("North", [1, None, 2]), ("North", [3]),
        # A missing layout falls back to an automatic grid of at most 25.
        ("Gone", [3] * 25), ("Gone", [3] * 5),
    ]
    assert got["pages"][3]["layout_missing"] is True
    assert got["steps"] == got["pages"]


def test_a_custom_layout_made_smaller_splits_its_pages(env):  # noqa: F811
    _window_settings(env, {"custom_layouts": [
        {"id": "wall", "name": "Wall", "grid_columns": 2, "grid_rows": 2}]})
    t = _create(env, pages=[{"layout_id": "wall", "camera_ids": [1, 2, 3]}])
    assert [p["camera_ids"] for p in t["pages"]] == [[1, 2, 3]]
    s = env.Session()
    row = s.query(env.models.SecuritySetting).filter_by(key="window_division_settings").one()
    row.json_value = json.dumps({"custom_layouts": [
        {"id": "wall", "name": "Wall", "grid_columns": 2, "grid_rows": 2,
         "tiles": [{"row": 0, "col": 0, "rowSpan": 2, "colSpan": 1},
                   {"row": 0, "col": 1, "rowSpan": 2, "colSpan": 1},
                   {"row": 5, "col": 0}]}]})  # outside the grid: not a tile
    s.commit()
    s.close()
    got = env.client.get(f"{URL}/{t['id']}", headers=env.jwt("admin")).json()
    assert [p["camera_ids"] for p in got["pages"]] == [[1, 2], [3]]


def test_a_client_sending_steps_is_still_understood(env):  # noqa: F811
    body = _body()
    body["steps"] = body.pop("pages")
    r = env.client.post(URL, headers=env.jwt("admin"), json=body)
    assert r.status_code == 201, r.text
    assert [p["camera_ids"] for p in r.json()["pages"]] == [[1, 2], [3]]


def test_a_tour_has_no_page_limit(env):  # noqa: F811
    t = _create(env, pages=[{"layout_id": "1x1", "camera_ids": [1, 2, 3]}] * 400)
    assert len(t["pages"]) == 1200


def test_a_runaway_body_is_refused_before_it_is_read(env, monkeypatch):  # noqa: F811
    from routers import tours

    monkeypatch.setattr(tours, "MAX_BODY_BYTES", 2000)
    A = env.jwt("admin")
    r = env.client.post(URL, headers=A, json=_body(pages=[{"layout_id": "1x1", "camera_ids": [1]}] * 100))
    assert r.status_code == 413
    assert env.client.get(URL, headers=A).json()["tours"] == []
    assert env.client.post(URL, headers=A, json=_body()).status_code == 201


def test_a_page_of_only_empty_tiles_is_422(env):  # noqa: F811
    r = env.client.post(URL, headers=env.jwt("admin"),
                        json=_body(pages=[{"layout_id": "2x2", "camera_ids": [None, None]}]))
    assert r.status_code == 422


def test_bad_bodies_are_422(env):  # noqa: F811
    A = env.jwt("admin")
    for body in (_body(name="  "),
                 _body(pages=[]),
                 _body(pages=[{"layout_id": "2x2", "camera_ids": []}]),
                 _body(pages=[{"layout_id": "2x2", "duration_s": 5, "camera_ids": [1]}])):
        assert env.client.post(URL, headers=A, json=body).status_code == 422


def test_unknown_or_binned_cameras_are_named(env):  # noqa: F811
    s = env.Session()
    s.get(env.models.Camera, 2).deleted_at = datetime.now(UTC)
    s.commit()
    s.close()
    r = env.client.post(URL, headers=env.jwt("admin"),
                        json=_body(pages=[{"layout_id": "2x2", "camera_ids": [1, 2, 99]}]))
    assert r.status_code == 422
    assert r.json()["detail"]["camera_ids"] == [2, 99]


def test_unknown_and_disabled_layouts_are_422(env):  # noqa: F811
    _window_settings(env, {"layouts_enabled": {"3x3": False}, "custom_layouts": [
        {"id": "wall", "name": "Wall", "grid_columns": 5, "grid_rows": 4, "enabled": True},
        {"id": "old", "name": "Old", "grid_columns": 2, "grid_rows": 1, "enabled": False},
    ]})
    A = env.jwt("admin")
    for bad in ("nope", "3x3", "old"):
        r = env.client.post(URL, headers=A,
                            json=_body(pages=[{"layout_id": bad, "camera_ids": [1]}]))
        assert r.status_code == 422 and r.json()["detail"]["layout_ids"] == [bad]
    assert env.client.post(URL, headers=A, json=_body(
        pages=[{"layout_id": "wall", "camera_ids": [1]}])).status_code == 201


def test_a_plain_grid_needs_no_window_settings(env):  # noqa: F811
    A = env.jwt("admin")
    t = _create(env, pages=[{"layout_id": "grid:5x4", "camera_ids": [1]}])
    assert t["pages"][0]["layout_missing"] is False
    for bad in ("grid:9x1", "grid:0x2", "grid:5x4x", "grid:"):
        assert env.client.post(URL, headers=A, json=_body(
            pages=[{"layout_id": bad, "camera_ids": [1]}])).status_code == 422


# ── reads degrade, never fail ────────────────────────────────────────


def test_a_binned_camera_drops_out_of_a_running_tour(env):  # noqa: F811
    t = _create(env)
    s = env.Session()
    s.get(env.models.Camera, 2).deleted_at = datetime.now(UTC)
    s.get(env.models.Camera, 1).is_active = False
    s.commit()
    s.close()
    page = env.client.get(f"{URL}/{t['id']}", headers=env.jwt("admin")).json()["pages"][0]
    assert page["camera_ids"] == [1]
    assert page["paused_camera_ids"] == [1]
    assert page["hidden_count"] == 1


def test_a_binned_camera_leaves_its_tile_empty(env):  # noqa: F811
    t = _create(env, pages=[{"layout_id": "2x2", "camera_ids": [1, 2, 3]}])
    s = env.Session()
    s.get(env.models.Camera, 2).deleted_at = datetime.now(UTC)
    s.commit()
    s.close()
    page = env.client.get(f"{URL}/{t['id']}", headers=env.jwt("admin")).json()["pages"][0]
    assert page["camera_ids"] == [1, None, 3]
    assert page["hidden_count"] == 1


def test_a_layout_removed_later_is_flagged(env):  # noqa: F811
    _window_settings(env, {"custom_layouts": [
        {"id": "wall", "name": "Wall", "grid_columns": 5, "grid_rows": 4}]})
    t = _create(env, pages=[{"layout_id": "wall", "camera_ids": [1]}])
    assert t["pages"][0]["layout_missing"] is False
    s = env.Session()
    row = s.query(env.models.SecuritySetting).filter_by(key="window_division_settings").one()
    row.json_value = json.dumps({"custom_layouts": [
        {"id": "wall", "name": "Wall", "grid_columns": 5, "grid_rows": 4, "enabled": False}]})
    s.commit()
    s.close()
    page = env.client.get(f"{URL}/{t['id']}", headers=env.jwt("admin")).json()["pages"][0]
    assert page["layout_missing"] is True and page["camera_ids"] == [1]


def test_corrupt_window_settings_fall_back_to_predefined_layouts(env):  # noqa: F811
    _window_settings(env, '{"custom_layouts": [{"id": 1}]}')  # fails validation
    t = _create(env)  # predefined layouts still accepted
    got = env.client.get(f"{URL}/{t['id']}", headers=env.jwt("admin"))
    assert got.status_code == 200
    assert [s["layout_missing"] for s in got.json()["pages"]] == [False, False]


# ── who may do what ──────────────────────────────────────────────────


def test_a_viewer_runs_a_tour_with_only_their_cameras(env):  # noqa: F811
    t = _create(env)
    V = env.jwt("vera")  # live.view, sees camera 3 only, no cameras.manage
    got = env.client.get(f"{URL}/{t['id']}", headers=V).json()
    assert [s["camera_ids"] for s in got["pages"]] == [[], [3]]
    assert [s["hidden_count"] for s in got["pages"]] == [2, 0]
    assert env.client.get(URL, headers=V).status_code == 200
    assert env.client.post(URL, headers=V, json=_body()).status_code == 403
    assert env.client.put(f"{URL}/{t['id']}", headers=V, json=_body()).status_code == 403
    assert env.client.delete(f"{URL}/{t['id']}", headers=V).status_code == 403


def test_a_manager_cannot_overwrite_a_tour_with_cameras_they_cannot_see(env):  # noqa: F811
    t = _create(env)
    O = _operator(env, view=(1,), manage=(1,))
    got = env.client.get(f"{URL}/{t['id']}", headers=O).json()
    assert got["editable"] is False
    assert env.client.put(f"{URL}/{t['id']}", headers=O, json=_body(
        pages=[{"layout_id": "1x1", "camera_ids": [1]}])).status_code == 403
    assert env.client.delete(f"{URL}/{t['id']}", headers=O).status_code == 403
    # …and may only put cameras they manage into a tour of their own.
    r = env.client.post(URL, headers=O, json=_body(
        pages=[{"layout_id": "1x1", "camera_ids": [1, 2]}]))
    assert r.status_code == 422 and r.json()["detail"]["camera_ids"] == [2]
    assert env.client.post(URL, headers=O, json=_body(
        pages=[{"layout_id": "1x1", "camera_ids": [1]}])).status_code == 201


def test_a_manager_who_can_only_view_a_camera_may_not_change_its_tour(env):  # noqa: F811
    """Sees every camera, manages one: Edit would only ever end in a 422,
    so the tour is read-only for them, and they may not delete it either."""
    t = _create(env, pages=[{"layout_id": "2x2", "camera_ids": [1, 2]}])
    O = _operator(env, view=(1, 2), manage=(1,))
    got = env.client.get(f"{URL}/{t['id']}", headers=O).json()
    assert got["pages"][0]["camera_ids"] == [1, 2] and got["editable"] is False
    assert env.client.put(f"{URL}/{t['id']}", headers=O, json=_body(
        pages=[{"layout_id": "2x2", "camera_ids": [1]}])).status_code == 403
    assert env.client.patch(f"{URL}/{t['id']}", headers=O, json={"enabled": False}).status_code == 403
    assert env.client.delete(f"{URL}/{t['id']}", headers=O).status_code == 403


def test_turning_a_tour_off_rewrites_nothing_else(env):  # noqa: F811
    """The switch must work on a tour whose layout went away and whose page
    lost every camera to the bin, and must not drop those cameras or the
    description (re-saving the viewer's filtered copy did all three)."""
    _window_settings(env, {"custom_layouts": [
        {"id": "wall", "name": "Wall", "grid_columns": 5, "grid_rows": 4}]})
    t = _create(env, description="Ops wall, screen 2", pages=[
        {"layout_id": "wall", "camera_ids": [1]},
        {"layout_id": "2x2", "camera_ids": [2, 3]}])
    s = env.Session()
    s.get(env.models.Camera, 1).deleted_at = datetime.now(UTC)
    row = s.query(env.models.SecuritySetting).filter_by(key="window_division_settings").one()
    row.json_value = json.dumps({"custom_layouts": []})
    s.commit()
    s.close()

    A = env.jwt("admin")
    r = env.client.patch(f"{URL}/{t['id']}", headers=A, json={"enabled": False, "expected_revision": 1})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False and r.json()["revision"] == 2
    s = env.Session()
    stored = s.get(env.models.CameraTour, t["id"])
    assert stored.description == "Ops wall, screen 2"
    assert [x["camera_ids"] for x in stored.pages] == [[1], [2, 3]]
    assert stored.pages[0]["layout_id"] == "wall"
    s.close()
    # A stale revision is refused, and a viewer may not switch it.
    assert env.client.patch(f"{URL}/{t['id']}", headers=A,
                            json={"enabled": True, "expected_revision": 1}).status_code == 409
    assert env.client.patch(f"{URL}/{t['id']}", headers=env.jwt("vera"),
                            json={"enabled": True}).status_code == 403


def test_a_stale_revision_is_409(env):  # noqa: F811
    t = _create(env)
    A = env.jwt("admin")
    assert env.client.put(f"{URL}/{t['id']}", headers=A,
                          json=_body(expected_revision=1)).status_code == 200
    r = env.client.put(f"{URL}/{t['id']}", headers=A, json=_body(expected_revision=1))
    assert r.status_code == 409


def test_tokens_cannot_reach_tours(env):  # noqa: F811
    _create(env)
    tok = _mint(env, scopes=["live.view", "cameras.manage"])["token"]
    assert env.client.get(URL, headers=_as(tok)).status_code == 403
    assert env.client.post(URL, headers=_as(tok), json=_body()).status_code == 403


# ── settings ─────────────────────────────────────────────────────────


def test_settings_default_validate_and_survive_a_save_without_them(env):  # noqa: F811
    A = env.jwt("admin")
    t = _create(env)
    assert t["settings"]["fit"] == "fit"
    assert (t["settings"]["transition"], t["settings"]["preload"]) == ("fade", True)
    assert t["settings"]["alerts"] == {"enabled": False, "sources": ["app_alert", "camera_event"],
                                       "min_severity": "medium", "scope": "tour",
                                       "hold_s": 30, "cooldown_s": 60}
    wanted = {"fit": "fill", "transition": "cut", "preload": False, "alerts": {"enabled": True, "sources": ["app_alert", "app_alert"],
                                        "min_severity": "high", "scope": "visible",
                                        "hold_s": 20, "cooldown_s": 0}}
    r = env.client.put(f"{URL}/{t['id']}", headers=A, json=_body(settings=wanted))
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["fit"] == "fill"
    assert (r.json()["settings"]["transition"], r.json()["settings"]["preload"]) == ("cut", False)
    assert r.json()["settings"]["alerts"]["sources"] == ["app_alert"]
    # An update that does not send settings keeps them.
    r = env.client.put(f"{URL}/{t['id']}", headers=A, json=_body(name="Renamed"))
    assert r.json()["settings"]["alerts"]["scope"] == "visible"
    for bad in ({"fit": "stretch"}, {"transition": "spin"}, {"alerts": {"hold_s": 1}}, {"alerts": {"sources": []}},
                {"alerts": {"min_severity": "urgent"}}):
        assert env.client.put(f"{URL}/{t['id']}", headers=A,
                              json=_body(settings=bad)).status_code == 422


def test_unreadable_stored_settings_fall_back_to_defaults(env):  # noqa: F811
    t = _create(env)
    s = env.Session()
    s.get(env.models.CameraTour, t["id"]).settings = {"fit": 42}
    s.commit()
    s.close()
    got = env.client.get(f"{URL}/{t['id']}", headers=env.jwt("admin"))
    assert got.status_code == 200 and got.json()["settings"]["fit"] == "fit"


# ── thumbnails ───────────────────────────────────────────────────────


def _fake_grab(monkeypatch, result=b"\xff\xd8jpeg"):
    from services import camera_thumbnail as ct

    ct.clear()
    calls = []

    async def grab(url):
        calls.append(url)
        return result

    monkeypatch.setattr(ct, "grab_frame", grab)
    monkeypatch.setattr(ct, "_source_url", lambda cid, url: f"tap://{cid}")
    return calls


def test_a_thumbnail_is_grabbed_once_and_cached(env, monkeypatch):  # noqa: F811
    calls = _fake_grab(monkeypatch)
    A = env.jwt("admin")
    for _ in range(3):
        r = env.client.get(f"{URL}/thumbnails/1", headers=A)
        assert r.status_code == 200 and r.content == b"\xff\xd8jpeg"
        assert r.headers["content-type"] == "image/jpeg"
    assert calls == ["tap://1"]


def test_a_failed_thumbnail_is_503_and_not_retried_at_once(env, monkeypatch):  # noqa: F811
    calls = _fake_grab(monkeypatch, result=None)
    A = env.jwt("admin")
    assert env.client.get(f"{URL}/thumbnails/2", headers=A).status_code == 503
    assert env.client.get(f"{URL}/thumbnails/2", headers=A).status_code == 503
    assert len(calls) == 1


def test_thumbnails_follow_camera_visibility(env, monkeypatch):  # noqa: F811
    calls = _fake_grab(monkeypatch)
    V = env.jwt("vera")  # sees camera 3 only
    assert env.client.get(f"{URL}/thumbnails/1", headers=V).status_code == 404
    assert env.client.get(f"{URL}/thumbnails/3", headers=V).status_code == 200
    s = env.Session()
    s.get(env.models.Camera, 3).is_active = False
    s.get(env.models.Camera, 2).deleted_at = datetime.now(UTC)
    s.commit()
    s.close()
    from services import camera_thumbnail as ct
    ct.clear()
    assert env.client.get(f"{URL}/thumbnails/3", headers=V).status_code == 503
    assert env.client.get(f"{URL}/thumbnails/2", headers=env.jwt("admin")).status_code == 404
    assert calls == ["tap://3"]
