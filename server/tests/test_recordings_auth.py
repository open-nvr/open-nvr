# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#221 P0 security: recordings are owner-scoped, like every camera route."""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

import datetime as _dt  # noqa: E402
if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_rauth_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("MEDIAMTX_BASE_URL", "http://127.0.0.1:8889")
os.environ.setdefault("MEDIAMTX_ADMIN_API", "http://127.0.0.1:9997/v3")
os.environ.setdefault("MEDIAMTX_HLS_URL", "http://127.0.0.1:8888")
os.environ.setdefault("MEDIAMTX_PLAYBACK_URL", "http://127.0.0.1:9996")

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import routers.recordings as rec  # noqa: E402
from core.database import Base  # noqa: E402
from models import Camera, CameraPermission, Role, User  # noqa: E402

_ADMIN = SimpleNamespace(id=1, is_superuser=True)


def _db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    role = Role(name="admin")
    s.add(role)
    s.commit()
    ua = User(username="a", email="a@t.io", hashed_password="x", role_id=role.id)
    ub = User(username="b", email="b@t.io", hashed_password="x", role_id=role.id)
    s.add_all([ua, ub])
    s.commit()
    cam_a = Camera(name="a-cam", ip_address="10.0.0.1", port=80, owner_id=ua.id)
    cam_b = Camera(name="b-cam", ip_address="10.0.0.2", port=80, owner_id=ub.id)
    s.add_all([cam_a, cam_b])
    s.commit()
    return s, ua, ub, cam_a, cam_b


def test_owned_cameras_query_scopes_to_owner():
    s, ua, ub, cam_a, cam_b = _db()
    a_ids = [c.id for c in rec._viewable_cameras_query(s, ua).all()]
    assert a_ids == [cam_a.id]
    # superuser sees all
    su = SimpleNamespace(id=99, is_superuser=True)
    assert len(rec._viewable_cameras_query(s, su).all()) == 2


def test_authorize_camera_owner_and_superuser():
    s, ua, ub, cam_a, cam_b = _db()
    assert rec._authorize_camera(cam_a, ua, s) is True
    assert rec._authorize_camera(cam_a, ub, s) is False          # other owner
    assert rec._authorize_camera(cam_a, SimpleNamespace(id=0, is_superuser=True), s) is True
    assert rec._authorize_camera(None, ua, s) is False


def test_shared_camera_via_can_view_is_authorized():
    """A user granted can_view on someone else's camera must see its
    recordings too — recordings must not be stricter than live view (#221)."""
    s, ua, ub, cam_a, cam_b = _db()
    # grant user A view access to user B's camera
    s.add(CameraPermission(user_id=ua.id, camera_id=cam_b.id, can_view=True))
    s.commit()
    assert rec._authorize_camera(cam_b, ua, s) is True
    ids = sorted(c.id for c in rec._viewable_cameras_query(s, ua).all())
    assert ids == sorted([cam_a.id, cam_b.id])
    # a revoked / can_view=False grant does NOT authorize
    s.add(CameraPermission(user_id=ub.id, camera_id=cam_a.id, can_view=False))
    s.commit()
    assert rec._authorize_camera(cam_a, ub, s) is False


def test_camera_for_path_only_resolves_owned():
    s, ua, ub, cam_a, cam_b = _db()
    # Build the expected path from the EXACT references _camera_for_path uses
    # (rec's own settings + stream-name fn), not fresh imports — otherwise a
    # prior test that reloaded core.config makes the two settings instances
    # diverge and the paths won't match (test-ordering flake, not a bug).
    def _path(cam):
        return rec._build_stream_name(
            rec.settings.mediamtx_stream_prefix, cam.id, cam.ip_address
        )

    path_a = _path(cam_a)
    path_b = _path(cam_b)
    # owner A resolves their own path, not B's
    assert rec._camera_for_path(s, path_a, ua) is not None
    assert rec._camera_for_path(s, path_b, ua) is None
    # superuser resolves either
    su = SimpleNamespace(id=0, is_superuser=True)
    assert rec._camera_for_path(s, path_b, su) is not None


def test_media_cors_is_not_wildcard():
    h = rec._media_cors_headers()
    assert h.get("Access-Control-Allow-Origin") != "*"
