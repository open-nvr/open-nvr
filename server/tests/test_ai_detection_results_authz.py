# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Per-camera authorization on /ai-detection-results (GHSA-fpp6-xfgg-f4h3).

The routes authenticated the caller and scoped nothing: any logged-in user
could list every camera's detections and captions, read or delete any result
by id, write results onto any camera, and wipe the whole table with
DELETE /bulk/older-than/0. These run the real handlers against a real
(SQLite) database and the real camera_scope rules, as five people:

  admin     superuser
  owner     owns camera 10
  viewer    can_view grant on camera 10
  manager   can_view + can_manage grant on camera 10
  outsider  nothing

Camera 20 belongs to the admin; one result has no camera at all.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_aidr_authz_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base  # noqa: E402
from models import AIDetectionResult, AIModel, Camera, CameraPermission, Role, User  # noqa: E402
from routers import ai_detection_results as r  # noqa: E402

ADMIN, OWNER, VIEWER, MANAGER, OUTSIDER = 1, 2, 3, 4, 5


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    s.add(Role(id=1, name="admin", description="t"))
    s.add(Role(id=2, name="viewer", description="t"))
    s.commit()
    for uid, name, su in [(ADMIN, "admin", True), (OWNER, "owner", False), (VIEWER, "viewer", False),
                          (MANAGER, "manager", False), (OUTSIDER, "outsider", False)]:
        s.add(User(id=uid, username=name, email=f"{name}@x.t", hashed_password="x",
                   role_id=1 if su else 2, is_superuser=su, is_active=True))
    s.commit()
    s.add(Camera(id=10, name="gate", ip_address="10.0.0.10", owner_id=OWNER))
    s.add(Camera(id=20, name="vault", ip_address="10.0.0.20", owner_id=ADMIN))
    s.commit()
    s.add(CameraPermission(user_id=VIEWER, camera_id=10, can_view=True, can_manage=False))
    s.add(CameraPermission(user_id=MANAGER, camera_id=10, can_view=True, can_manage=True))
    s.add(AIModel(id=1, name="det", model_name="yolov8", task="person_detection", source_type="live"))
    s.commit()
    old = datetime.now(timezone.utc) - timedelta(days=30)
    for rid, cam in [(100, 10), (101, 10), (200, 20), (300, None)]:
        s.add(AIDetectionResult(id=rid, model_id=1, camera_id=cam, task="person_detection",
                                label="person", caption=f"caption on {cam}", created_at=old))
    s.commit()
    yield s
    s.close()


def _user(db, uid):
    return db.get(User, uid)


def _run(coro):
    return asyncio.run(coro)


def _list(db, uid, **kw):
    params = {"skip": 0, "limit": 100, "model_id": None, "camera_id": None, "task": None, **kw}
    return {x.id for x in _run(r.get_detection_results(current_user=_user(db, uid), db=db, **params))}


def _remaining(db):
    db.expire_all()
    return {x.id for x in db.query(AIDetectionResult).all()}


# --- list -------------------------------------------------------------------

@pytest.mark.parametrize("uid,expected", [
    (ADMIN, {100, 101, 200, 300}),       # everything, including the camera-less row
    (OWNER, {100, 101}),
    (VIEWER, {100, 101}),
    (MANAGER, {100, 101}),
    (OUTSIDER, set()),
])
def test_list_is_scoped_to_visible_cameras(db, uid, expected):
    assert _list(db, uid) == expected


def test_asking_for_someone_elses_camera_returns_nothing(db):
    assert _list(db, OUTSIDER, camera_id=10) == set()
    assert _list(db, OWNER, camera_id=20) == set()


# --- read by id -------------------------------------------------------------

@pytest.mark.parametrize("uid,rid", [(OUTSIDER, 100), (OWNER, 200), (OWNER, 300), (VIEWER, 200)])
def test_reading_an_invisible_result_is_404(db, uid, rid):
    with pytest.raises(HTTPException) as e:
        _run(r.get_detection_result(result_id=rid, current_user=_user(db, uid), db=db))
    assert e.value.status_code == 404


def test_reading_a_visible_result_works(db):
    res = _run(r.get_detection_result(result_id=100, current_user=_user(db, VIEWER), db=db))
    assert res.id == 100


# --- delete by id -----------------------------------------------------------

def test_outsider_cannot_delete_and_cannot_tell_it_exists(db):
    with pytest.raises(HTTPException) as e:
        _run(r.delete_detection_result(result_id=200, current_user=_user(db, OUTSIDER), db=db))
    assert e.value.status_code == 404
    assert 200 in _remaining(db)


def test_view_only_grant_cannot_delete(db):
    with pytest.raises(HTTPException) as e:
        _run(r.delete_detection_result(result_id=100, current_user=_user(db, VIEWER), db=db))
    assert e.value.status_code == 403
    assert 100 in _remaining(db)


@pytest.mark.parametrize("uid", [OWNER, MANAGER, ADMIN])
def test_owner_manager_and_admin_can_delete(db, uid):
    _run(r.delete_detection_result(result_id=100, current_user=_user(db, uid), db=db))
    assert 100 not in _remaining(db)


# --- bulk delete --------------------------------------------------------------

def test_bulk_delete_only_touches_cameras_the_caller_manages(db):
    _run(r.delete_old_results(days=7, current_user=_user(db, MANAGER), db=db))
    assert _remaining(db) == {200, 300}


@pytest.mark.parametrize("uid", [OUTSIDER, VIEWER])
def test_bulk_delete_without_manage_deletes_nothing(db, uid):
    _run(r.delete_old_results(days=7, current_user=_user(db, uid), db=db))
    assert _remaining(db) == {100, 101, 200, 300}


def test_bulk_delete_by_admin_covers_every_camera(db):
    _run(r.delete_old_results(days=7, current_user=_user(db, ADMIN), db=db))
    assert _remaining(db) == set()


def test_bulk_delete_refuses_zero_days():
    # FastAPI enforces ge=1 on the path parameter: "older than 0 days" was "everything".
    import inspect
    days = inspect.signature(r.delete_old_results).parameters["days"].default
    assert any(getattr(m, "ge", None) == 1 for m in getattr(days, "metadata", []))


# --- create -------------------------------------------------------------------

def _create(db, uid, camera_id):
    body = r.AIDetectionResultCreate(model_id=1, camera_id=camera_id, task="person_detection",
                                     label="person")
    return _run(r.create_detection_result(result_data=body, current_user=_user(db, uid), db=db))


@pytest.mark.parametrize("uid,camera_id", [(OUTSIDER, 10), (VIEWER, 10), (OWNER, 20), (OWNER, None)])
def test_writing_results_needs_manage_on_the_camera(db, uid, camera_id):
    with pytest.raises(HTTPException) as e:
        _create(db, uid, camera_id)
    assert e.value.status_code == 403


@pytest.mark.parametrize("uid,camera_id", [(OWNER, 10), (MANAGER, 10), (ADMIN, 20), (ADMIN, None)])
def test_managers_can_write_results(db, uid, camera_id):
    assert _create(db, uid, camera_id).camera_id == camera_id


# --- found while triaging the report: the same class, two more surfaces -------
#
# GET /camera-config/{id} skipped the view check that every other route on that
# router (and every camera surface) applies, and the config carries the
# camera's source URL - credentials included. The AI recording listings
# (/ai-models/recording-sessions, /ai-models/recordings) listed every camera's
# recordings to anyone signed in.

from models import CameraConfig  # noqa: E402
from routers import ai_model_management as aim  # noqa: E402
from routers import camera_config as cc  # noqa: E402

SECRET_URL = "rtsp://admin:hunter2@10.0.0.10:554/Streaming/101"


def _with_config(db):
    db.add(CameraConfig(camera_id=10, source_url=SECRET_URL)); db.commit()


@pytest.mark.parametrize("uid", [OUTSIDER])
def test_camera_config_is_not_readable_without_view(db, uid):
    _with_config(db)
    with pytest.raises(HTTPException) as e:
        _run(cc.get_config(camera_id=10, db=db, current_user=_user(db, uid)))
    assert e.value.status_code in (403, 404)


@pytest.mark.parametrize("uid", [OWNER, VIEWER, MANAGER, ADMIN])
def test_camera_config_is_readable_with_view(db, uid):
    _with_config(db)
    cfg = _run(cc.get_config(camera_id=10, db=db, current_user=_user(db, uid)))
    assert cfg.camera_id == 10


def test_an_api_token_never_gets_the_source_urls_credentials(db):
    from core.request_context import begin_request, end_request
    from schemas import CameraConfigResponse

    _with_config(db)
    cfg = db.query(CameraConfig).filter(CameraConfig.camera_id == 10).first()
    assert CameraConfigResponse.model_validate(cfg).model_dump()["source_url"] == SECRET_URL  # a person
    ctx, token = begin_request("c-1")
    try:
        ctx.actor = "token:ha"
        dumped = CameraConfigResponse.model_validate(cfg).model_dump()["source_url"]
    finally:
        end_request(token)
    assert "hunter2" not in dumped and "admin" not in dumped


def _sessions(monkeypatch, cameras):
    class Svc:
        def list_recording_sessions(self, db, camera_id=None, date=None):
            return {"recordings_base": "/r", "cameras": [{"camera_id": c, "dates": []} for c in cameras]}
    # Stand in for the module: the real one imports OpenCV, which the
    # server's test environment does not install.
    import types
    fake = types.ModuleType("services.recording_session_service")
    fake.get_recording_session_service = lambda: Svc()
    monkeypatch.setitem(sys.modules, "services.recording_session_service", fake)


@pytest.mark.parametrize("uid,expected", [(ADMIN, [10, 20]), (OWNER, [10]), (OUTSIDER, [])])
def test_ai_recording_sessions_list_only_visible_cameras(db, monkeypatch, uid, expected):
    _sessions(monkeypatch, [10, 20])
    out = _run(aim.list_recording_sessions_for_ai(current_user=_user(db, uid), db=db, camera_id=None, date=None))
    assert [c["camera_id"] for c in out["cameras"]] == expected


def test_ai_recording_sessions_for_someone_elses_camera_is_404(db, monkeypatch):
    _sessions(monkeypatch, [20])
    with pytest.raises(HTTPException) as e:
        _run(aim.list_recording_sessions_for_ai(current_user=_user(db, OWNER), db=db, camera_id=20, date=None))
    assert e.value.status_code == 404


@pytest.mark.parametrize("mode", ["ip", "id"])
@pytest.mark.parametrize("uid,expected_cams", [(ADMIN, {10, 20, None}), (OWNER, {10}), (OUTSIDER, set())])
def test_ai_recordings_list_only_visible_cameras(db, monkeypatch, mode, uid, expected_cams):
    # Directories are the cameras' stream names, which depend on the path mode.
    from core.config import settings
    from services.stream_service import _build_stream_name, substream_name

    monkeypatch.setattr(settings, "mediamtx_path_mode", mode)
    name = {cid: _build_stream_name(settings.mediamtx_stream_prefix, cid, ip)
            for cid, ip in ((10, "10.0.0.10"), (20, "10.0.0.20"))}
    dirs = {name[10]: 10, substream_name(name[10]): 10, name[20]: 20, "scratch": None}
    items = [{"camera": d, "relpath": f"{d}/x.mp4"} for d in dirs]
    monkeypatch.setattr(aim.storage_service, "list_recordings",
                        lambda db, camera_id=None, limit=100, offset=0: {"items": [dict(i) for i in items], "total": len(items)})
    out = _run(aim.list_recordings_for_ai(current_user=_user(db, uid), db=db, camera_id=None, limit=100, offset=0))
    assert {dirs[i["camera"]] for i in out["items"]} == expected_cams
    assert out["total"] == len(out["items"])


# --- second review: two more surfaces of the same class ------------------------
#
# POST /ai-models/inference ran a live inference on any camera (a caption of a
# camera the caller cannot see is a view of it), and /inference/recording
# analysed any camera's footage and filed the results under a camera of the
# caller's choosing. The compliance pages listed every camera's address,
# vendor and credential weakness to any user, and the access audit every
# user's actions and addresses.

from routers import ai_models as aimod  # noqa: E402
from routers import compliance as comp  # noqa: E402


def test_live_inference_on_an_invisible_camera_is_404(db):
    req = aimod.InferenceRequest(camera_id=20, rtsp_url="rtsp://x", model_name="blip", task="caption")
    with pytest.raises(HTTPException) as e:
        _run(aimod.run_inference(request=req, current_user=_user(db, OWNER), db=db))
    assert e.value.status_code == 404


def test_recording_inference_on_an_invisible_camera_is_404(db):
    req = aimod.RecordingInferenceRequest(camera_id=20, segments=["cam-20/a.mp4"], model_name="blip", task="caption")
    with pytest.raises(HTTPException) as e:
        _run(aimod.run_recording_inference(request=req, current_user=_user(db, OWNER), db=db))
    assert e.value.status_code == 404


def test_recording_inference_cannot_read_another_cameras_footage(db, monkeypatch, tmp_path):
    # Owner of camera 10 names camera 20's folder as a segment, under their own camera.
    from core.config import settings
    from services.stream_service import _build_stream_name
    import services.storage_service as ss

    monkeypatch.setattr(settings, "mediamtx_path_mode", "id")
    other = _build_stream_name(settings.mediamtx_stream_prefix, 20, "10.0.0.20")
    (tmp_path / other).mkdir()
    (tmp_path / other / "a.mp4").write_bytes(b"x")
    monkeypatch.setattr(ss, "get_effective_recordings_base_path", lambda db: str(tmp_path))
    import types
    fake = types.ModuleType("cv2")
    monkeypatch.setitem(sys.modules, "cv2", fake)
    req = aimod.RecordingInferenceRequest(camera_id=10, segments=[f"{other}/a.mp4"], model_name="blip", task="caption")
    with pytest.raises(HTTPException) as e:
        _run(aimod.run_recording_inference(request=req, current_user=_user(db, OWNER), db=db))
    assert e.value.status_code == 404
    assert "Recording not found" in e.value.detail


def test_recording_dirs_follow_scope_and_path_mode(db, monkeypatch):
    from core.config import settings
    monkeypatch.setattr(settings, "mediamtx_path_mode", "ip")
    assert aimod._recording_dirs_for(db, None) is None
    assert aimod._recording_dirs_for(db, set()) == set()
    assert aimod._recording_dirs_for(db, {10}) == {"cam-10_0_0_10", "cam-10_0_0_10-sub"}


def _check(monkeypatch):
    import services.security_compliance as sc
    monkeypatch.setattr(sc, "check_cameras", lambda cams: {"cameras": [c.id for c in cams]})


@pytest.mark.parametrize("uid,expected", [(ADMIN, [10, 20]), (OWNER, [10]), (VIEWER, [10]), (OUTSIDER, [])])
def test_security_check_lists_only_visible_cameras(db, monkeypatch, uid, expected):
    _check(monkeypatch)
    out = _run(comp.get_security_check(db=db, current_user=_user(db, uid)))
    assert out["cameras"] == expected


def test_recording_coverage_for_an_invisible_camera_is_404(db):
    with pytest.raises(HTTPException) as e:
        _run(comp.get_recording_coverage(days=7, camera_id=20, db=db, current_user=_user(db, OWNER)))
    assert e.value.status_code == 404


def test_access_audit_requires_a_superuser():
    import inspect
    dep = inspect.signature(comp.get_access_audit).parameters["current_user"].default
    from core.auth import get_current_superuser
    assert dep.dependency is get_current_superuser
