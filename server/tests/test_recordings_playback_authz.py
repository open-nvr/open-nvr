# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Authorization on the playback listing/URL endpoints.

Regression cover: /playback/list, /playback/cameras, /playback/url and
/sessions-for-ai authenticated the caller but did not check per-camera view
permission, so any logged-in user could enumerate or get playback URLs for
cameras they don't own. These exercise the decision helpers those endpoints
now call: _can_view_camera, _viewable_cameras, _camera_for_playback_path.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

# A sibling test (test_segment_end_time) registers a stub 'core.logging_config'
# in sys.modules via setdefault; drop it so the REAL module loads before we
# import server code that does `from core.logging_config import recording_logger`.
sys.modules.pop("core.logging_config", None)


from core.config import settings  # noqa: E402
from models import Camera  # noqa: E402
from routers.recordings import (  # noqa: E402
    _camera_for_playback_path,
    _can_view_camera,
    _viewable_cameras,
)
from services.stream_service import _build_stream_name  # noqa: E402


class FakeUser:
    def __init__(self, uid, superuser=False):
        self.id = uid
        self.is_superuser = superuser
        self.is_active = True


class FakeCamera:
    def __init__(self, cid, owner_id, ip="10.0.0.1"):
        self.id = cid
        self.owner_id = owner_id
        self.ip_address = ip
        self.is_active = True


class FakeQuery:
    def __init__(self, entity, session):
        self.entity = entity
        self.session = session

    def filter(self, *a, **k):
        return self

    def all(self):
        return self.session.cameras if self.entity is Camera else []

    def first(self):
        if self.entity is Camera:
            return self.session.cameras[0] if self.session.cameras else None
        # CameraPermission grant lookup
        return self.session.grant_result


class FakeSession:
    def __init__(self, cameras, grant_result=None):
        self.cameras = cameras
        self.grant_result = grant_result

    def query(self, *entities):
        return FakeQuery(entities[0], self)


# ---- _can_view_camera decision matrix ----

def test_superuser_sees_any_camera():
    db = FakeSession(cameras=[], grant_result=None)
    assert _can_view_camera(FakeUser(1, superuser=True), FakeCamera(9, owner_id=2), db)


def test_owner_sees_own_camera():
    db = FakeSession(cameras=[], grant_result=None)
    assert _can_view_camera(FakeUser(5, ), FakeCamera(9, owner_id=5), db)


def test_null_owner_camera_visible_to_any_user():
    db = FakeSession(cameras=[], grant_result=None)
    assert _can_view_camera(FakeUser(5), FakeCamera(9, owner_id=None), db)


def test_non_owner_without_grant_denied():
    db = FakeSession(cameras=[], grant_result=None)
    assert not _can_view_camera(FakeUser(5), FakeCamera(9, owner_id=2), db)


def test_non_owner_with_grant_allowed():
    db = FakeSession(cameras=[], grant_result=object())  # a CameraPermission row
    assert _can_view_camera(FakeUser(5), FakeCamera(9, owner_id=2), db)


# ---- _viewable_cameras filtering ----

def test_viewable_filters_out_foreign_cameras():
    mine = FakeCamera(1, owner_id=5)
    shared = FakeCamera(2, owner_id=None)   # legacy null-owner: visible
    foreign = FakeCamera(3, owner_id=99)    # not mine, no grant: hidden
    db = FakeSession(cameras=[mine, shared, foreign], grant_result=None)
    got = {c.id for c in _viewable_cameras(db, FakeUser(5))}
    assert got == {1, 2}


def test_viewable_superuser_sees_all():
    cams = [FakeCamera(1, 5), FakeCamera(2, 99), FakeCamera(3, None)]
    db = FakeSession(cameras=cams, grant_result=None)
    got = {c.id for c in _viewable_cameras(db, FakeUser(1, superuser=True))}
    assert got == {1, 2, 3}


# ---- _camera_for_playback_path resolution ----

def test_path_resolves_to_correct_camera():
    cam = FakeCamera(57, owner_id=5, ip="192.168.1.9")
    db = FakeSession(cameras=[cam])
    path = _build_stream_name(settings.mediamtx_stream_prefix, cam.id, cam.ip_address)
    assert _camera_for_playback_path(db, path) is cam


def test_unknown_path_resolves_to_none():
    cam = FakeCamera(57, owner_id=5, ip="192.168.1.9")
    db = FakeSession(cameras=[cam])
    assert _camera_for_playback_path(db, "cam-does-not-exist") is None
