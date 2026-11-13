# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Per-camera detection settings: a camera's overrides of the pipeline's
global knobs.

* the record is validated and stored compactly (only the fields that are
  set); an empty body clears every override; junk in the column reads as
  "no overrides";
* seeing needs the camera, changing needs cameras.manage and the camera;
  everything is audited; a token may only read;
* the camera-agent sources listing carries the record as ``detection``.
"""

from __future__ import annotations

import pytest

from services import detection_settings as ds
from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture

URL = "/api/v1/cameras/{cam}/detection-settings"


def _put(env, cam=1, who="admin", **body):  # noqa: F811
    return env.client.put(URL.format(cam=cam), headers=env.jwt(who), json=body)


# ── the record ───────────────────────────────────────────────────────


def test_only_set_fields_are_stored_and_an_empty_body_clears(env):  # noqa: F811
    A = env.jwt("admin")
    assert env.client.get(URL.format(cam=1), headers=A).json() == {"camera_id": 1, "detection": {}}
    r = _put(env, fps=5, motion_threshold=40, min_spawn_score=0.6,
             object_filters={"Person": {"min_area": 0.002, "min_score": None},
                             "car": {}})                     # an empty filter is dropped
    assert r.status_code == 200, r.text
    assert r.json()["detection"] == {
        "fps": 5, "motion_threshold": 40, "min_spawn_score": 0.6,
        "object_filters": {"person": {"min_area": 0.002}},
    }
    s = env.Session()
    stored = s.get(env.models.Camera, 1).detection_settings
    s.close()
    assert stored == r.json()["detection"], "compact on disk, no nulls"
    # The whole record is replaced, not merged.
    assert _put(env, stationary_interval=0).json()["detection"] == {"stationary_interval": 0}
    assert _put(env).json()["detection"] == {}
    s = env.Session()
    assert s.get(env.models.Camera, 1).detection_settings is None
    actions = [a for (a,) in s.query(env.models.AuditLog.action)
               .filter(env.models.AuditLog.action == "camera.detection_settings").all()]
    s.close()
    assert len(actions) == 3


@pytest.mark.parametrize("body", [
    {"fps": 0}, {"fps": 31}, {"motion_threshold": 256}, {"min_spawn_score": 1.5},
    {"stationary_interval": -1}, {"unknown": 1},
    {"object_filters": {"<b>": {"min_area": 0.1}}},
    {"object_filters": {"car": {"min_area": 0.5, "max_area": 0.1}}},
    {"object_filters": {"car": {"min_ratio": 3, "max_ratio": 1}}},
    {"object_filters": {"car": {"bogus": 1}}},
])
def test_bad_bodies_are_422(env, body):  # noqa: F811
    assert _put(env, **body).status_code == 422


def test_junk_in_the_column_reads_as_no_overrides():
    assert ds.normalize(None) == {} and ds.normalize("x") == {} and ds.normalize({}) == {}
    assert ds.normalize({"fps": 99}) == {}
    assert ds.normalize({"fps": 4, "object_filters": {"dog": {"max_area": 0.3}}}) == {
        "fps": 4, "object_filters": {"dog": {"max_area": 0.3}}}


# ── who ──────────────────────────────────────────────────────────────


def test_who_may_see_and_change(env):  # noqa: F811
    V = env.jwt("vera")  # sees camera 3 only, has no cameras.manage
    assert env.client.get(URL.format(cam=1), headers=V).status_code == 404
    assert env.client.get(URL.format(cam=3), headers=V).status_code == 200
    assert _put(env, cam=3, who="vera", fps=3).status_code == 403


def test_a_token_reads_on_its_cameras_and_never_writes(env):  # noqa: F811
    tok = _mint(env, scopes=["cameras.view", "cameras.manage"], camera_ids=[1])["token"]
    assert env.client.get(URL.format(cam=1), headers=_as(tok)).status_code == 200
    assert env.client.get(URL.format(cam=2), headers=_as(tok)).status_code == 403
    assert env.client.put(URL.format(cam=1), headers=_as(tok), json={"fps": 3}).status_code == 403


# ── the sources listing ──────────────────────────────────────────────


def test_sources_listing_carries_the_record(env, monkeypatch):  # noqa: F811
    from routers import internal_camera_agent as ica

    monkeypatch.setattr(ica, "_mint_mediamtx_jwt", lambda *a, **k: None)
    _put(env, fps=3, motion_enabled=False,
         object_filters={"person": {"min_area": 0.001, "max_ratio": 1.5}})
    s = env.Session()
    try:
        body = ica.list_camera_agent_sources(principal=None, db=s, x_detect_hwaccel=None)
    finally:
        s.close()
    cams = {c["open_nvr_camera_id"]: c for c in body["cameras"]}
    assert cams["1"]["detection"] == {
        "fps": 3, "motion_enabled": False,
        "object_filters": {"person": {"min_area": 0.001, "max_ratio": 1.5}},
    }
    assert cams["2"]["detection"] == {}
