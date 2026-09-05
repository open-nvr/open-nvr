# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Tests for the App Store index — the "discover" half of the App Catalog
(``GET /api/v1/apps/index``).

Run with:

    cd server && pytest tests/test_apps_index.py -v

Coverage:

* ``server/config/apps_index.yml`` loads and every seeded entry
  validates against the ``IndexEntry`` pydantic model (id/name/summary/
  category/version/image/requires_tasks/emits/docs_url/install) — the
  same curated-yaml → lru_cache → validated-pydantic pattern as
  ``ai_models._load_use_case_map``.
* ``GET /apps/index`` returns one ``apps`` entry per index row and the
  response shape matches the frontend contract exactly.
* the installed cross-reference flips ``installed:true`` /
  ``enabled:<bool>`` for an app present in ``installed_apps`` and leaves
  every other entry ``installed:false, enabled:null``.
* ``enabled`` tracks the stored flag (enable then re-read).
* the route requires an authenticated user.
"""

from __future__ import annotations

# Python 3.10 sandbox polyfill — pyproject requires 3.11+ where
# datetime.UTC exists. No-op on 3.11+.
import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017 — only runs where UTC is absent

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

# Settings need to be valid for the modules under test to import.
from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault(
    "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode()
)

# Stub ``core.logging_config`` — same pattern as test_apps_registry.py.
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


# ─── App under test: the real router on an in-memory SQLite DB ──────────
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.auth import get_current_active_user  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import AuditLog, InstalledApp, Role, User  # noqa: E402
from routers import apps as apps_router  # noqa: E402
from routers.apps import (  # noqa: E402
    IndexEntry,
    _load_apps_index,
    get_register_principal,
)


class _StubUser:
    id = 1
    username = "tester"


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
            AuditLog.__table__,
            Role.__table__,
            User.__table__,
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


@pytest.fixture
def client():
    app, session_factory, engine = _make_app()
    app.dependency_overrides[get_current_active_user] = lambda: _StubUser()
    app.dependency_overrides[get_register_principal] = lambda: _StubUser()

    with TestClient(app) as test_client:
        # Stash the session factory so tests can seed installed rows.
        test_client.session_factory = session_factory
        yield test_client
    engine.dispose()


@pytest.fixture
def no_auth_client():
    """A client WITHOUT the auth override — exercises the real
    ``get_current_active_user`` dependency so we can prove the route
    is protected."""
    app, _session_factory, engine = _make_app()
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def _seed_installed(client, app_id: str, *, enabled: bool):
    """Insert one installed_apps row directly (bypasses register so the
    test isn't coupled to the SSRF/manifest validation there)."""
    session = client.session_factory()
    session.add(
        InstalledApp(
            id=app_id,
            name=app_id,
            version="1.0.0",
            category="perimeter",
            url="http://x:9200",
            enabled=enabled,
            config_json={},
            manifest_json={"id": app_id},
            status="registered",
        )
    )
    session.commit()
    session.close()


# ─── apps_index.yml loads + validates against IndexEntry ────────────────

# The installable apps — every listed entry MUST have a service block in
# docker-compose.apps.yml (a review found the original ten listed eight
# apps with no service block, so 8/10 install attempts failed with "no
# such service" on every path; all eight were then restored WITH their
# service blocks). scripts/validate_apps_index.py enforces the
# cross-check so this can't drift again. camera-agent is deliberately
# excluded — it's the conversational front door, not a catalog app.
_EXPECTED_IDS = {
    "loitering-detection",
    "occupancy-counting",
    "line-crossing",
    "abandoned-object",
    "intrusion-detection",
    "license-plate-recognition",
    "gate-controller",
    "alert-notifier",
    "smart-doorbell",
    "package-delivery",
    "footage-search",
    "home-assistant-relay",
}


def test_index_yaml_loads_and_validates():
    """Every seeded entry parses as an IndexEntry (curated yaml →
    lru_cache → validated pydantic, mirroring _load_use_case_map)."""
    entries = _load_apps_index()
    assert all(isinstance(e, IndexEntry) for e in entries)
    assert {e.id for e in entries} == _EXPECTED_IDS


def test_index_yaml_entries_are_well_formed():
    """Each entry carries the store fields the catalog renders, and the
    install block is a real copy-paste (compose + command, both
    non-empty)."""
    for e in _load_apps_index():
        assert e.name and e.summary and e.category and e.version
        assert e.image.startswith("ghcr.io/open-nvr/")
        # https GitHub URLs — repo-relative paths 404ed in the SPA and
        # were an unconstrained href sink (review M5).
        assert e.docs_url.startswith("https://github.com/open-nvr/")
        assert e.install.compose.strip()
        assert e.install.command.strip().startswith("docker compose")


def test_index_yaml_has_no_secrets():
    """No literal secret ever ships in the index — the compose snippets
    reference ${INTERNAL_API_KEY} from the operator's .env, never a
    baked value."""
    for e in _load_apps_index():
        assert "${INTERNAL_API_KEY}" in e.install.compose
        # The effective test-env secret must not have leaked into the file.
        assert os.environ["INTERNAL_API_KEY"] not in e.install.compose


# ─── GET /apps/index ────────────────────────────────────────────────────


def test_index_returns_all_entries(client):
    resp = client.get("/apps/index")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"apps"}
    assert {a["id"] for a in body["apps"]} == _EXPECTED_IDS


def test_index_response_shape_matches_contract(client):
    """The per-app shape is exactly the frontend contract:
    {id, name, summary, category, version, kind, image, external_url,
     pricing, price_note, entitlement, author, requires_tasks[], emits[],
     docs_url, install{compose,command}, installed, enabled}.
    build_context is index-only and must NOT leak into the response."""
    app = next(
        a
        for a in client.get("/apps/index").json()["apps"]
        if a["id"] == "loitering-detection"
    )
    assert set(app) == {
        "id",
        "name",
        "summary",
        "category",
        "kind",
        "external_url",
        "pricing",
        "price_note",
        "entitlement",
        "author",
        "version",
        "image",
        "requires_tasks",
        "emits",
        "docs_url",
        "install",
        "installed",
        "enabled",
    }
    assert set(app["install"]) == {"compose", "command"}
    assert isinstance(app["requires_tasks"], list)
    assert isinstance(app["emits"], list)
    assert app["requires_tasks"] == ["object_detection"]
    assert app["emits"] == ["loitering"]


def test_index_uninstalled_apps_are_available(client):
    """With an empty registry every entry is available-to-install:
    installed=false, enabled=null."""
    for a in client.get("/apps/index").json()["apps"]:
        assert a["installed"] is False
        assert a["enabled"] is None


def test_index_flips_installed_for_registered_app(client):
    """An installed_apps row flips exactly its matching entry to
    installed=true; every other entry stays available."""
    _seed_installed(client, "loitering-detection", enabled=False)

    apps = {a["id"]: a for a in client.get("/apps/index").json()["apps"]}

    installed_entry = apps["loitering-detection"]
    assert installed_entry["installed"] is True
    assert installed_entry["enabled"] is False  # installed but not enabled

    # Every other entry is untouched.
    for app_id, a in apps.items():
        if app_id == "loitering-detection":
            continue
        assert a["installed"] is False
        assert a["enabled"] is None


def test_index_enabled_tracks_stored_flag(client):
    """enabled reflects the installed_apps row's flag — an enabled app
    reads enabled=true."""
    _seed_installed(client, "occupancy-counting", enabled=True)

    entry = next(
        a
        for a in client.get("/apps/index").json()["apps"]
        if a["id"] == "occupancy-counting"
    )
    assert entry["installed"] is True
    assert entry["enabled"] is True


def test_index_requires_auth(no_auth_client):
    """No credential ⇒ the dependency rejects the call (401/403), never
    an anonymous listing."""
    resp = no_auth_client.get("/apps/index")
    assert resp.status_code in (401, 403)
