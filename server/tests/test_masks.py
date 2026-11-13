# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Camera masks: what Tier-0 must ignore in a camera's picture.

* masks CRUD: seeing needs the camera, changing needs cameras.manage and
  the camera; names are unique per camera; a motion mask keeps no labels;
  everything is audited; a token may only read them;
* the camera-agent sources listing carries every camera's masks, split by
  kind, with empty lists for a camera that has none.
"""

from __future__ import annotations

import pytest

from services import masks as ms
from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture

SQUARE = [[0.2, 0.2], [0.6, 0.2], [0.6, 0.6], [0.2, 0.6]]
STRIP = [[0.7, 0.0], [1.0, 0.0], [1.0, 0.1], [0.7, 0.1]]


def _mask(env, cam=1, **body):  # noqa: F811
    body.setdefault("name", "clock")
    body.setdefault("polygon", STRIP)
    r = env.client.post(f"/api/v1/cameras/{cam}/masks", headers=env.jwt("admin"), json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ── CRUD ─────────────────────────────────────────────────────────────


def test_masks_crud_and_audit(env):  # noqa: F811
    A = env.jwt("admin")
    created = _mask(env, name="OSD clock")
    assert created["kind"] == "motion" and created["labels"] is None
    mid = created["id"]
    # A motion mask keeps no labels even when some are sent.
    r = env.client.put(f"/api/v1/cameras/1/masks/{mid}", headers=A,
                       json={"name": "OSD clock", "polygon": STRIP, "labels": ["car"]})
    assert r.status_code == 200 and r.json()["labels"] is None
    # Turned into an object mask for cars only.
    r = env.client.put(f"/api/v1/cameras/1/masks/{mid}", headers=A,
                       json={"name": "street", "kind": "object", "polygon": SQUARE,
                             "labels": ["Car", "truck", "car"]})
    assert r.status_code == 200
    assert r.json()["kind"] == "object" and r.json()["labels"] == ["car", "truck"]
    listed = env.client.get("/api/v1/cameras/1/masks", headers=A).json()["masks"]
    assert [m["name"] for m in listed] == ["street"]
    assert env.client.delete(f"/api/v1/cameras/1/masks/{mid}", headers=A).status_code == 200
    assert env.client.get("/api/v1/cameras/1/masks", headers=A).json()["masks"] == []
    assert env.client.delete(f"/api/v1/cameras/1/masks/{mid}", headers=A).status_code == 404
    s = env.Session()
    actions = [a for (a,) in s.query(env.models.AuditLog.action)
               .filter(env.models.AuditLog.action.like("mask.%"))
               .order_by(env.models.AuditLog.id).all()]
    s.close()
    assert actions == ["mask.create", "mask.update", "mask.update", "mask.delete"]


def test_mask_names_are_unique_per_camera(env):  # noqa: F811
    _mask(env, name="clock")
    r = env.client.post("/api/v1/cameras/1/masks", headers=env.jwt("admin"),
                        json={"name": "clock", "polygon": STRIP})
    assert r.status_code == 409
    _mask(env, cam=2, name="clock")  # another camera may reuse it


def test_bad_mask_bodies_are_422(env):  # noqa: F811
    A = env.jwt("admin")
    for body in ({"name": "x", "polygon": [[0, 0], [1, 1]]},
                 {"name": "  ", "polygon": STRIP},
                 {"name": "x", "polygon": STRIP, "kind": "privacy"},
                 {"name": "x", "polygon": STRIP, "kind": "object", "labels": ["<b>"]}):
        assert env.client.post("/api/v1/cameras/1/masks", headers=A, json=body).status_code == 422


def test_who_may_see_and_change_masks(env):  # noqa: F811
    _mask(env)
    V = env.jwt("vera")  # sees camera 3 only, has no cameras.manage
    assert env.client.get("/api/v1/cameras/1/masks", headers=V).status_code == 404
    assert env.client.get("/api/v1/cameras/3/masks", headers=V).status_code == 200
    assert env.client.post("/api/v1/cameras/3/masks", headers=V,
                           json={"name": "x", "polygon": STRIP}).status_code == 403


def test_a_token_reads_masks_on_its_cameras_and_never_writes(env):  # noqa: F811
    _mask(env)
    tok = _mint(env, scopes=["cameras.view", "cameras.manage"], camera_ids=[1])["token"]
    assert env.client.get("/api/v1/cameras/1/masks", headers=_as(tok)).status_code == 200
    assert env.client.get("/api/v1/cameras/2/masks", headers=_as(tok)).status_code == 403
    assert env.client.post("/api/v1/cameras/1/masks", headers=_as(tok),
                           json={"name": "t", "polygon": STRIP}).status_code == 403


# ── the sources listing the detect-pipeline reads ────────────────────


def test_sources_listing_carries_the_masks_by_kind(env, monkeypatch):  # noqa: F811
    from routers import internal_camera_agent as ica

    monkeypatch.setattr(ica, "_mint_mediamtx_jwt", lambda *a, **k: None)
    _mask(env, name="clock", polygon=STRIP)
    _mask(env, name="tree", polygon=SQUARE)
    _mask(env, name="street", kind="object", polygon=SQUARE, labels=["car"])
    _mask(env, name="poster", kind="object", polygon=STRIP)
    s = env.Session()
    try:
        body = ica.list_camera_agent_sources(principal=None, db=s, x_detect_hwaccel=None)
    finally:
        s.close()
    cams = {c["open_nvr_camera_id"]: c for c in body["cameras"]}
    assert cams["1"]["motion_masks"] == [STRIP, SQUARE]
    assert cams["1"]["object_masks"] == [
        {"polygon": SQUARE, "labels": ["car"]},
        {"polygon": STRIP, "labels": None},
    ]
    # A camera with nothing drawn still says so, so the pipeline never has
    # to tell "no masks" from "an older core".
    assert cams["2"]["motion_masks"] == [] and cams["2"]["object_masks"] == []


def test_masks_for_cameras_is_one_query_per_fleet(env):  # noqa: F811
    s = env.Session()
    try:
        assert ms.masks_for_cameras(s, []) == {}
        assert ms.masks_for_cameras(s, [7]) == {7: {"motion_masks": [], "object_masks": []}}
    finally:
        s.close()


@pytest.mark.parametrize("kind", ["motion", "object"])
def test_serialize_hides_labels_on_motion_masks(kind):
    class _M:
        id, camera_id, name, polygon, labels = 1, 2, "m", SQUARE, ["car"]
        created_at = updated_at = None
    m = _M()
    m.kind = kind
    out = ms.serialize(m)
    assert out["labels"] == (["car"] if kind == "object" else None)
