# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HA-108: pausing recording, behind a site flag that is off by default.

Pinned here:

* with the flag off, pausing is refused before anything touches MediaMTX,
  whoever asks; resuming is always allowed;
* with it on, pausing needs recordings.pause and the camera (tokens: the
  scope and the allow-list);
* a pause is durable (CameraConfig.recording_enabled=False, which startup
  provisioning honours) and listed with who/why/when-to-resume;
* automatic resume survives a restart: overdue ones resume at boot;
* turning the flag off resumes every paused camera, at once or at boot;
* a token may turn a camera off (which also stops recording) only while
  the flag is on.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture


@pytest.fixture()
def mtx(env, monkeypatch):  # noqa: F811
    from services.mediamtx_admin_service import MediaMtxAdminService
    import services.recording_pause as rp

    calls = []

    async def enable(camera_id, **kw):
        calls.append(("enable", camera_id))
        return {"status": "ok"}

    async def disable(camera_id):
        calls.append(("disable", camera_id))
        return {"status": "ok"}

    async def unprovision(camera_id, ip):
        calls.append(("unprovision", camera_id))
        return {"status": "ok"}

    monkeypatch.setattr(MediaMtxAdminService, "enable_recording", staticmethod(enable))
    monkeypatch.setattr(MediaMtxAdminService, "disable_recording", staticmethod(disable))
    monkeypatch.setattr(MediaMtxAdminService, "unprovision_path", staticmethod(unprovision))
    # No real background timers in tests.
    monkeypatch.setattr(rp, "_schedule_resume", lambda cid, at: calls.append(("schedule", cid)))
    return calls


def _allow(env, on=True):  # noqa: F811
    r = env.client.put("/api/v1/system/settings/recording-pause",
                       headers=env.jwt("admin"), json={"enabled": on})
    assert r.status_code == 200, r.text
    return r.json()


def _pause(env, headers, cam=1, **body):  # noqa: F811
    return env.client.post(f"/api/v1/cameras/{cam}/recording", headers=headers,
                           json={"enabled": False, **body})


def _state(env):  # noqa: F811
    from services.recording_pause import paused

    s = env.Session()
    try:
        cfg = {c.camera_id: c.recording_enabled for c in s.query(env.models.CameraConfig).all()}
        return paused(s), cfg
    finally:
        s.close()


def test_off_by_default_and_refused_before_touching_the_media_server(env, mtx):  # noqa: F811
    r = _pause(env, env.jwt("admin"))
    assert r.status_code == 403 and "not allowed" in r.json()["detail"]
    assert mtx == []
    assert env.client.get("/api/v1/system/info", headers=env.jwt("admin")).json()[
        "recording_pause_enabled"] is False


def test_pause_is_durable_listed_and_audited_then_resumes(env, mtx):  # noqa: F811
    _allow(env)
    r = _pause(env, env.jwt("admin"), reason="someone home", resume_after_s=3600)
    assert r.status_code == 200, r.text
    assert r.json()["paused"]["by"] == "user:admin"
    paused, cfg = _state(env)
    assert set(paused) == {"1"} and paused["1"]["reason"] == "someone home"
    assert paused["1"]["resume_at"] is not None
    assert cfg[1] is False
    assert ("disable", 1) in mtx and ("schedule", 1) in mtx

    s = env.Session()
    row = (s.query(env.models.AuditLog).filter_by(action="recording.pause").one())
    assert json.loads(row.details)["reason"] == "someone home"
    s.close()

    r = env.client.post("/api/v1/cameras/1/recording", headers=env.jwt("admin"),
                        json={"enabled": True})
    assert r.status_code == 200
    paused, cfg = _state(env)
    assert paused == {} and cfg[1] is True and ("enable", 1) in mtx


def test_pausing_needs_the_permission(env, mtx):  # noqa: F811
    _allow(env)
    s = env.Session()
    s.get(env.models.Camera, 3).owner_id = env.ids["viewer"]
    s.commit()
    s.close()
    r = _pause(env, env.jwt("vera"), cam=3)
    assert r.status_code == 403 and "recordings.pause" in r.json()["detail"]
    assert mtx == []


def test_a_token_pauses_with_the_scope_on_its_cameras_only(env, mtx):  # noqa: F811
    _allow(env)
    tok = _mint(env, scopes=["cameras.view", "recordings.pause"], camera_ids=[1])["token"]
    assert _pause(env, _as(tok)).status_code == 200
    assert _state(env)[0]["1"]["by"].startswith("token:")
    assert _pause(env, _as(tok), cam=2).status_code == 403
    viewer = _mint(env, name="noscope", scopes=["cameras.view"])["token"]
    assert _pause(env, _as(viewer)).status_code == 403


def test_turning_the_flag_off_resumes_everything(env, mtx):  # noqa: F811
    _allow(env)
    assert _pause(env, env.jwt("admin"), cam=1).status_code == 200
    assert _pause(env, env.jwt("admin"), cam=2).status_code == 200
    out = _allow(env, on=False)
    assert sorted(out["resumed_cameras"]) == [1, 2] and out["paused"] == {}
    paused, cfg = _state(env)
    assert paused == {} and cfg[1] is True and cfg[2] is True
    # And pausing is refused again.
    assert _pause(env, env.jwt("admin")).status_code == 403


def test_boot_resumes_overdue_pauses_and_reschedules_the_rest(env, mtx):  # noqa: F811
    from services import recording_pause, site_settings

    s = env.Session()
    site_settings.set_json(s, site_settings.RECORDING_PAUSE_KEY, True)
    now = datetime.now(UTC)
    site_settings.set_json(s, recording_pause.PAUSED_KEY, {
        "1": {"since": now.isoformat(), "by": "x", "reason": None,
              "resume_at": (now - timedelta(minutes=1)).isoformat()},
        "2": {"since": now.isoformat(), "by": "x", "reason": None,
              "resume_at": (now + timedelta(hours=1)).isoformat()},
        "3": {"since": now.isoformat(), "by": "x", "reason": None, "resume_at": None},
    })
    s.close()
    asyncio.run(recording_pause.restore_on_startup())
    paused, _ = _state(env)
    assert set(paused) == {"2", "3"}
    assert ("enable", 1) in mtx and ("schedule", 2) in mtx
    s = env.Session()
    row = s.query(env.models.AuditLog).filter_by(action="recording.resume").one()
    assert json.loads(row.details)["actor"] == "system:auto-resume" and row.entity_id == "1"
    s.close()


def test_boot_with_the_flag_off_resumes_everything(env, mtx):  # noqa: F811
    from services import recording_pause, site_settings

    s = env.Session()
    site_settings.set_json(s, recording_pause.PAUSED_KEY, {
        "1": {"since": "x", "by": "x", "reason": None, "resume_at": None}})
    s.close()
    asyncio.run(recording_pause.restore_on_startup())
    assert _state(env)[0] == {} and ("enable", 1) in mtx


def test_a_token_may_turn_a_camera_off_only_while_pausing_is_allowed(env, mtx):  # noqa: F811
    tok = _mint(env, scopes=["cameras.view", "cameras.manage"], camera_ids=[1])["token"]
    r = env.client.put("/api/v1/cameras/1", headers=_as(tok), json={"is_active": False})
    assert r.status_code == 403 and "recording" in r.json()["detail"]
    _allow(env)
    r = env.client.put("/api/v1/cameras/1", headers=_as(tok), json={"is_active": False})
    assert r.status_code == 200, r.text
    # Still nothing else.
    assert env.client.put("/api/v1/cameras/1", headers=_as(tok),
                          json={"is_active": True, "name": "x"}).status_code == 403


def test_a_turned_off_camera_cannot_be_paused(env, mtx):  # noqa: F811
    _allow(env)
    s = env.Session()
    s.get(env.models.Camera, 1).is_active = False
    s.commit()
    s.close()
    assert _pause(env, env.jwt("admin")).status_code == 409


def test_the_flag_endpoints_are_superuser_only(env):  # noqa: F811
    assert env.client.get("/api/v1/system/settings/recording-pause",
                          headers=env.jwt("vera")).status_code == 403
    assert env.client.put("/api/v1/system/settings/recording-pause",
                          headers=env.jwt("vera"), json={"enabled": True}).status_code == 403
    tok = _mint(env, scopes=["settings.view"])["token"]
    assert env.client.put("/api/v1/system/settings/recording-pause",
                          headers=_as(tok), json={"enabled": True}).status_code == 403


def test_the_resume_timer_fires_and_finishes_cleanly(env, monkeypatch):  # noqa: F811
    """The real timer, with a short delay: it resumes, is audited, and ends
    as a completed task (resume() cancels the registered timer, which must
    not be the one running)."""
    from services import recording_pause as rp, site_settings
    from services.mediamtx_admin_service import MediaMtxAdminService

    async def ok(*a, **k):
        return {"status": "ok"}

    monkeypatch.setattr(MediaMtxAdminService, "enable_recording", staticmethod(ok))
    at = datetime.now(UTC) + timedelta(milliseconds=200)
    s = env.Session()
    site_settings.set_json(s, rp.PAUSED_KEY, {"1": {
        "since": "x", "by": "x", "reason": None, "resume_at": at.isoformat()}})
    s.close()

    async def main():
        rp._schedule_resume(1, at)
        task = rp._resume_tasks[1]
        await asyncio.wait_for(asyncio.shield(task), 5)
        return task

    task = asyncio.run(main())
    assert not task.cancelled() and task.exception() is None
    assert _state(env)[0] == {} and 1 not in rp._resume_tasks


def test_a_bad_resume_delay_never_stops_recording(env, mtx):  # noqa: F811
    """M1 review: a huge delay from the entity command raised after
    recording was already off, leaving an unlisted, unresumable pause."""
    import pytest as _pytest

    from services import recording_pause

    s = env.Session()
    try:
        for bad in (5, -1, 10**15):
            with _pytest.raises(ValueError):
                asyncio.run(recording_pause.pause(s, 1, actor="t", resume_after_s=bad))
    finally:
        s.close()
    assert ("disable", 1) not in mtx
    assert _state(env)[0] == {}
