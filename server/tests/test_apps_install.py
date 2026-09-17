# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Tests for the opt-in one-click App install endpoints.

    POST /apps/index/{id}/install
    POST /apps/index/{id}/uninstall
    GET  /apps/index/{id}/install-status

Security surface under test — this is the sovereignty moat:

* OPT-IN: install/uninstall 403 when APPS_INSTALL_ENABLED is false (the
  copy-paste command path via GET /apps/index stays available).
* RBAC: install/uninstall 403 without the ``apps.install`` permission.
* INDEX-ONLY: an id not in the curated apps_index.yml is 404 (no
  arbitrary images / user input reach the desired state).
* DESIRED-STATE ONLY: a successful install writes ONE
  app_install_intents row (desired=installed, status=pending, image +
  digest copied FROM the index) and an audit row — no docker, no
  subprocess in the server process.
* uninstall flips desired -> absent (+ audit).
* install-status reflects the written intent; "none" before any request.

Run with:

    cd server && pytest tests/test_apps_install.py -v
"""

from __future__ import annotations

import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault(
    "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode()
)

# Stub core.logging_config (same pattern as the other apps tests).
_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, name):
        return lambda *a, **kw: None


for _name in (
    "main_logger",
    "auth_logger",
    "camera_logger",
    "recording_logger",
    "cloud_logger",
    "ai_logger",
):
    setattr(_lm, _name, _L())
sys.modules["core.logging_config"] = _lm


from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core import config as config_module  # noqa: E402
from core.auth import get_current_active_user  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import (  # noqa: E402
    AppInstallIntent,
    AuditLog,
    Camera,
    InstalledApp,
    Permission,
    Role,
    RolePermission,
    SkillAssignment,
    User,
)
from routers import apps as apps_router  # noqa: E402

# A real curated id (present in apps_index.yml) and a bogus one.
REAL_APP_ID = "loitering-detection"
UNKNOWN_APP_ID = "definitely-not-a-real-app"


class _User:
    """Stub authenticated principal; ``permissions`` drives the RBAC gate
    via core.permissions.RequirePermission which reads user.role.permissions.
    """

    def __init__(self, *, perms: list[str], superuser: bool = False):
        self.id = 1
        self.username = "tester"
        self.is_active = True
        self.is_superuser = superuser
        self.role = _Role(perms)


class _Role:
    def __init__(self, perms: list[str]):
        self.permissions = [_Perm(p) for p in perms]


class _Perm:
    def __init__(self, name: str):
        self.name = name


def _make_app():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            InstalledApp.__table__,
            AppInstallIntent.__table__,
            AuditLog.__table__,
            Role.__table__,
            User.__table__,
            Permission.__table__,
            RolePermission.__table__,
            # Uninstall releases the app's camera picks.
            Camera.__table__,
            SkillAssignment.__table__,
        ],
    )
    session_factory = sessionmaker(bind=engine)

    app = FastAPI()
    app.include_router(apps_router.router)

    def _override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    return app, session_factory, engine


@pytest.fixture(autouse=True)
def _enable_install(monkeypatch):
    """Default the opt-in ON for most tests; the OFF case toggles it."""
    monkeypatch.setattr(
        config_module.settings, "apps_install_enabled", True, raising=False
    )
    yield


def _client(user: _User):
    app, session_factory, engine = _make_app()
    app.dependency_overrides[get_current_active_user] = lambda: user
    tc = TestClient(app)
    tc.session_factory = session_factory
    tc._engine = engine
    return tc


@pytest.fixture
def admin_client():
    """A caller holding apps.install."""
    tc = _client(_User(perms=["apps.install"]))
    with tc:
        yield tc
    tc._engine.dispose()


@pytest.fixture
def unprivileged_client():
    """Authenticated, but WITHOUT apps.install."""
    tc = _client(_User(perms=["cameras.view"]))
    with tc:
        yield tc
    tc._engine.dispose()


def _intents(client) -> list[AppInstallIntent]:
    s = client.session_factory()
    try:
        return s.query(AppInstallIntent).all()
    finally:
        s.close()


def _audits(client) -> list[AuditLog]:
    s = client.session_factory()
    try:
        return s.query(AuditLog).all()
    finally:
        s.close()


# ── OPT-IN gate ────────────────────────────────────────────────────────


def test_install_403_when_disabled(admin_client, monkeypatch):
    monkeypatch.setattr(
        config_module.settings, "apps_install_enabled", False, raising=False
    )
    resp = admin_client.post(f"/apps/index/{REAL_APP_ID}/install")
    assert resp.status_code == 403
    assert "disabled" in resp.json()["detail"].lower()
    # Nothing written.
    assert _intents(admin_client) == []


def test_uninstall_403_when_disabled(admin_client, monkeypatch):
    monkeypatch.setattr(
        config_module.settings, "apps_install_enabled", False, raising=False
    )
    resp = admin_client.post(f"/apps/index/{REAL_APP_ID}/uninstall")
    assert resp.status_code == 403


# ── RBAC gate ──────────────────────────────────────────────────────────


def test_install_403_without_permission(unprivileged_client):
    resp = unprivileged_client.post(f"/apps/index/{REAL_APP_ID}/install")
    assert resp.status_code == 403
    assert "apps.install" in resp.json()["detail"]
    assert _intents(unprivileged_client) == []


def test_uninstall_403_without_permission(unprivileged_client):
    resp = unprivileged_client.post(f"/apps/index/{REAL_APP_ID}/uninstall")
    assert resp.status_code == 403


def test_superuser_bypasses_permission(monkeypatch):
    tc = _client(_User(perms=[], superuser=True))
    monkeypatch.setattr(
        config_module.settings, "apps_install_enabled", True, raising=False
    )
    with tc:
        resp = tc.post(f"/apps/index/{REAL_APP_ID}/install")
    assert resp.status_code == 200
    tc._engine.dispose()


# ── INDEX-ONLY gate ────────────────────────────────────────────────────


def test_install_404_for_unknown_id(admin_client):
    resp = admin_client.post(f"/apps/index/{UNKNOWN_APP_ID}/install")
    assert resp.status_code == 404
    assert _intents(admin_client) == []


def test_status_404_for_unknown_id(admin_client):
    resp = admin_client.get(f"/apps/index/{UNKNOWN_APP_ID}/install-status")
    assert resp.status_code == 404


# ── Success writes desired-state + audit + digest ──────────────────────


def test_install_writes_intent_audit_and_copies_image(admin_client):
    resp = admin_client.post(f"/apps/index/{REAL_APP_ID}/install")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == REAL_APP_ID
    assert body["desired"] == "installed"
    assert body["status"] == "pending"
    assert body["requested_by"] == "tester"
    # image is copied FROM the curated index, never from the request.
    assert body["image"].startswith("ghcr.io/open-nvr/")

    rows = _intents(admin_client)
    assert len(rows) == 1
    assert rows[0].id == REAL_APP_ID
    assert rows[0].desired == "installed"
    assert rows[0].status == "pending"

    audits = [a for a in _audits(admin_client) if a.action == "app.install.request"]
    assert len(audits) == 1
    assert audits[0].entity_id == REAL_APP_ID


def test_install_is_idempotent_upsert(admin_client):
    admin_client.post(f"/apps/index/{REAL_APP_ID}/install")
    admin_client.post(f"/apps/index/{REAL_APP_ID}/install")
    # Still one row (upsert on the curated id, not a second insert).
    assert len(_intents(admin_client)) == 1


def test_install_pins_digest_when_index_has_one(admin_client, monkeypatch):
    """When the curated index entry carries an image_digest it is copied
    onto the intent for the reconciler to pin."""
    from routers import apps as apps_mod

    real = apps_mod._load_apps_index()

    def _with_digest():
        out = []
        for e in real:
            if e.id == REAL_APP_ID:
                e = e.model_copy(update={"image_digest": "sha256:" + "a" * 64})
            out.append(e)
        return out

    monkeypatch.setattr(apps_mod, "_load_apps_index", _with_digest)

    resp = admin_client.post(f"/apps/index/{REAL_APP_ID}/install")
    assert resp.status_code == 200
    assert resp.json()["image_digest"] == "sha256:" + "a" * 64


# ── Uninstall flips desired to absent ──────────────────────────────────


def test_uninstall_flips_desired_absent(admin_client):
    admin_client.post(f"/apps/index/{REAL_APP_ID}/install")
    resp = admin_client.post(f"/apps/index/{REAL_APP_ID}/uninstall")
    assert resp.status_code == 200
    assert resp.json()["desired"] == "absent"
    assert resp.json()["status"] == "pending"

    rows = _intents(admin_client)
    assert len(rows) == 1  # same row, flipped
    assert rows[0].desired == "absent"

    audits = [
        a for a in _audits(admin_client) if a.action == "app.uninstall.request"
    ]
    assert len(audits) == 1


# ── Status endpoint ────────────────────────────────────────────────────


def test_status_none_before_any_request(admin_client):
    resp = admin_client.get(f"/apps/index/{REAL_APP_ID}/install-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["desired"] is None
    assert body["status"] == "none"


def test_status_reflects_intent(admin_client):
    admin_client.post(f"/apps/index/{REAL_APP_ID}/install")
    resp = admin_client.get(f"/apps/index/{REAL_APP_ID}/install-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["desired"] == "installed"
    assert body["status"] == "pending"


def test_status_readable_without_install_permission(unprivileged_client):
    """install-status is a read; any authenticated user may poll it."""
    resp = unprivileged_client.get(
        f"/apps/index/{REAL_APP_ID}/install-status"
    )
    assert resp.status_code == 200


# ── review fixes: service-key boundary + zombie-card cleanup ───────────


def test_internal_api_key_alone_cannot_install():
    """The service-key boundary the router comment stakes out (review
    finding: it was never asserted): X-Internal-Api-Key — the read
    principal the OpenNVR Agent holds — must NEVER satisfy
    install/uninstall. Those are user-JWT + apps.install only. Guards
    against a future refactor swapping the dependency to
    get_read_principal and silently passing the suite."""
    app, session_factory, engine = _make_app()
    # Deliberately NO get_current_active_user override — auth runs for
    # real, and the only credential presented is the internal key.
    with TestClient(app) as tc:
        for verb in ("install", "uninstall"):
            resp = tc.post(
                f"/apps/index/{REAL_APP_ID}/{verb}",
                headers={
                    "X-Internal-Api-Key": os.environ["INTERNAL_API_KEY"]
                },
            )
            assert resp.status_code in (401, 403), (verb, resp.status_code)
    s = session_factory()
    try:
        assert s.query(AppInstallIntent).count() == 0  # nothing written
    finally:
        s.close()
    engine.dispose()


def test_uninstall_deletes_registration_row(admin_client):
    """Zombie-card regression: uninstall must also drop the
    installed_apps registration row, or the catalog shows an immortal
    'installed' card drifting to unreachable — nothing else ever
    deletes that row (the reconciler only stops the container)."""
    from models import InstalledApp as _IA

    s = admin_client.session_factory()
    try:
        s.add(_IA(
            id=REAL_APP_ID, name="Loitering", version="1.0.0",
            url="http://loitering:9200", manifest_json={}, config_json={},
        ))
        s.commit()
    finally:
        s.close()

    resp = admin_client.post(f"/apps/index/{REAL_APP_ID}/uninstall")
    assert resp.status_code == 200

    s = admin_client.session_factory()
    try:
        assert s.query(_IA).filter(_IA.id == REAL_APP_ID).first() is None
    finally:
        s.close()
    # The intent row still exists with desired=absent (the reconciler
    # tears the container down from it).
    rows = _intents(admin_client)
    assert len(rows) == 1 and rows[0].desired == "absent"
