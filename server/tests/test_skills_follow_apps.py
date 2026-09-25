"""Skills follow apps: all-cameras apps, startup reconcile, the skill view."""
from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_sfa_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base  # noqa: E402
from models import Camera, InstalledApp, Role, User  # noqa: E402
from services import skill_assignments as sa  # noqa: E402


@pytest.fixture()
def db():
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng, expire_on_commit=False)()
    s.add(Role(id=1, name="admin", description="t")); s.commit()
    s.add(User(id=1, username="o", email="o@x.t", hashed_password="x", role_id=1)); s.commit()
    for cid, name in ((1, "Gate"), (2, "Yard")):
        s.add(Camera(id=cid, name=name, ip_address=f"10.0.0.{cid}",
                     rtsp_url=f"rtsp://x/{cid}", owner_id=1, is_active=True))
    s.commit()
    yield s
    s.close()


def _install(s, app_id, enabled=True, **manifest):
    s.add(InstalledApp(id=app_id, name=app_id, version="1.0.0", url=f"http://{app_id}:9200",
                       enabled=enabled, manifest_json={"id": app_id, **manifest}, config_json={}))
    s.commit()


def _skills(s, cam_id):
    s.expire_all()
    return sa.camera_skills(s.get(Camera, cam_id))


def test_an_all_cameras_app_is_picked_on_every_camera_and_brings_its_skills(db):
    _install(db, "camera-agent", camera_picker=False, all_cameras=True,
             enrich_tasks=["image_captioning", "visual_qa"])
    assert sa.sync_all_camera_picks(db, "camera-agent") == 2
    db.commit()
    for cam in (1, 2):
        assert {"camera_agent", "image_captioning", "vqa"} <= _skills(db, cam)
    assert sa.sync_all_camera_picks(db, "camera-agent") == 0          # idempotent


def test_an_ordinary_app_is_not_given_all_cameras(db):
    _install(db, "smart-doorbell", requires_tasks=["face_recognition"])
    assert sa.sync_all_camera_picks(db, "smart-doorbell") == 0
    assert _skills(db, 1) == set()


def test_a_new_camera_joins_every_all_cameras_app(db):
    _install(db, "camera-agent", all_cameras=True, enrich_tasks=["vqa"])
    sa.sync_all_camera_picks(db, "camera-agent"); db.commit()
    db.add(Camera(id=3, name="Lobby", ip_address="10.0.0.3", rtsp_url="rtsp://x/3",
                  owner_id=1, is_active=True))
    db.commit()
    assert sa.adopt_new_camera(db, 3) == 1
    db.commit()
    assert "vqa" in _skills(db, 3)


def test_disabling_an_all_cameras_app_empties_its_skills_everywhere(db):
    _install(db, "camera-agent", all_cameras=True, enrich_tasks=["vqa"])
    sa.sync_all_camera_picks(db, "camera-agent"); db.commit()
    db.query(InstalledApp).filter_by(id="camera-agent").update({"enabled": False}); db.commit()
    sa.reproject_app_cameras(db, "camera-agent"); db.commit()
    assert _skills(db, 1) == set() and _skills(db, 2) == set()


def test_startup_reconcile_retires_operator_rows_and_projects_derived_skills(db):
    """Rows the retired editor wrote go; manifests registered before this
    code existed get their tasks into the projection; a second run is a
    no-op."""
    _install(db, "smart-doorbell", requires_tasks=["face_recognition"])
    _install(db, "camera-agent", all_cameras=True, enrich_tasks=["vqa"])
    sa.declare(db, skill="embed", camera_id=1, consumer=sa.OPERATOR_CONSUMER)
    sa.declare(db, skill=sa.app_pick_skill("smart-doorbell"), camera_id=1,
               consumer=sa.app_consumer("smart-doorbell"))
    db.commit()
    # simulate a projection written by older code: the pick without its task
    cam = db.get(Camera, 1)
    cam.assignments = [{"skill": "embed"}, {"skill": "smart_doorbell"}]
    db.commit()
    stats = sa.reconcile_on_startup(db)
    db.commit()
    assert stats == {"retired_operator_rows": 1, "all_camera_picks": 2, "cameras_projected": 2}
    assert _skills(db, 1) == {"smart_doorbell", "face_recognition", "camera_agent", "vqa"}
    assert _skills(db, 2) == {"camera_agent", "vqa"}
    assert sa.reconcile_on_startup(db) == {"retired_operator_rows": 0, "all_camera_picks": 0,
                                           "cameras_projected": 2}


def test_the_skill_view_shows_derived_claims_and_who_brings_them(db):
    _install(db, "smart-doorbell", requires_tasks=["face_recognition"])
    _install(db, "package-delivery", enabled=False, requires_tasks=["face_recognition"])
    for app in ("smart-doorbell", "package-delivery"):
        sa.declare(db, skill=sa.app_pick_skill(app), camera_id=1, consumer=sa.app_consumer(app))
    db.commit()
    view = sa.skill_view(db, "face_recognition")
    claims = {c["consumer"]: c for c in view["cameras"][0]["consumers"]}
    assert claims["app:smart-doorbell"]["derived"] is True
    assert claims["app:package-delivery"]["dormant"] is True
    assert view["union"] == [1]


def test_manifest_tasks_are_canonical_and_deduplicated():
    assert sa.app_tasks({"requires_tasks": ["scene_caption", "image-captioning"],
                         "enrich_tasks": ["visual_qa", "embed", ""]}) == \
        ["image_captioning", "vqa", "embed"]
    assert sa.app_tasks(None) == [] and sa.app_tasks({"requires_tasks": "x"}) == []
