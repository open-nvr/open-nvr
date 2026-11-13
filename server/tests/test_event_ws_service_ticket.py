# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Service tickets for /events/ws — and proof that user tickets are
unchanged by their existence.

The camera agent consumes core's overlay tracks over this socket (one
source of truth for the site switch, the per-app permission and the
box maths) and re-scopes them per viewer itself. It authenticates with
the deployment's INTERNAL_API_KEY, which mints an UNSCOPED ticket.
Everything a user ticket did before — bound to a row, active only,
scoped to visible cameras, single use, expiring — must still hold, and
an app key must not be able to open the site-wide stream at all.
"""
import os
import sys
import time

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("INTERNAL_API_KEY", "x" * 48)
os.environ.setdefault("SECRET_KEY", "s" * 64)
os.environ.setdefault("MEDIAMTX_SECRET", "m" * 48)

from core.database import Base  # noqa: E402
from models import Role, User  # noqa: E402
# `routers/__init__` re-exports the ROUTER under the name `events`, so
# `from routers import events` would hand back an APIRouter; take the
# module itself.
import importlib  # noqa: E402

ev = importlib.import_module("routers.events")
from routers.apps import get_read_principal  # noqa: E402
from services.app_keys import AppPrincipal  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    # Every table, not a subset: _ws_scope_for walks cameras and
    # camera_permissions, and users.role_id is NOT NULL.
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    role = Role(name="viewer")
    s.add(role)
    s.commit()
    s.info["role_id"] = role.id
    yield s
    s.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _fresh_tickets():
    ev._ws_tickets.clear()
    yield
    ev._ws_tickets.clear()


def _user(db, name="alice", active=True, superuser=False):
    u = User(username=name, email=f"{name}@x", hashed_password="h",
             is_active=active, is_superuser=superuser,
             role_id=db.info["role_id"])
    db.add(u)
    db.commit()
    return u


# ─── tickets ────────────────────────────────────────────────────────────


def test_user_ticket_resolves_to_the_user_row(db):
    _user(db, "alice")
    t, _ = ev._mint_ws_ticket("alice")
    who = ev._authenticate_ws(t, db)
    assert isinstance(who, User) and who.username == "alice"


def test_service_ticket_resolves_without_a_user_row(db):
    t, _ = ev._mint_ws_ticket(None)
    assert ev._authenticate_ws(t, db) is ev.SERVICE


def test_service_ticket_is_not_mistaken_for_a_missing_one(db):
    """The consume step has THREE outcomes now; the service one must not
    collapse into 'unknown ticket'."""
    t, _ = ev._mint_ws_ticket(None)
    assert ev._consume_ws_ticket(t) is ev._SERVICE_TICKET
    assert ev._consume_ws_ticket(t) is None          # single use, like before
    assert ev._consume_ws_ticket("nope") is None


def test_tickets_are_single_use_and_expire(db):
    _user(db, "alice")
    t, _ = ev._mint_ws_ticket("alice")
    assert ev._authenticate_ws(t, db) is not None
    assert ev._authenticate_ws(t, db) is None        # replay refused
    t2, _ = ev._mint_ws_ticket(None)
    ev._ws_tickets[t2] = (None, time.time() - 1)     # force expiry
    assert ev._authenticate_ws(t2, db) is None


def test_inactive_user_ticket_is_refused(db):
    _user(db, "bob", active=False)
    t, _ = ev._mint_ws_ticket("bob")
    assert ev._authenticate_ws(t, db) is None


def test_unknown_username_ticket_is_refused(db):
    t, _ = ev._mint_ws_ticket("ghost")
    assert ev._authenticate_ws(t, db) is None


# ─── scope ──────────────────────────────────────────────────────────────


def test_service_is_unrestricted_but_a_user_is_scoped(db):
    """The whole point, and the whole risk: the service identity gets
    None (everything), an ordinary user gets exactly their cameras."""
    alice = _user(db, "alice")
    assert ev._ws_scope_for(ev.SERVICE, db) is None
    scoped = ev._ws_scope_for(alice, db)
    assert scoped == set()        # no cameras owned or granted → nothing


def test_superuser_stays_unrestricted(db):
    root = _user(db, "root", superuser=True)
    assert ev._ws_scope_for(root, db) is None


# ─── the mint endpoint ──────────────────────────────────────────────────


def _client(principal_factory):
    app = FastAPI()
    app.include_router(ev.router)
    app.dependency_overrides[get_read_principal] = principal_factory
    return TestClient(app)


def test_user_jwt_mints_a_user_ticket(db):
    _user(db, "alice")
    c = _client(lambda: SimpleNamespace(username="alice"))
    body = c.post("/events/ws-ticket").json()
    assert body["kind"] == "user"
    assert ev._authenticate_ws(body["ticket"], db).username == "alice"


def test_internal_key_mints_a_service_ticket(db):
    c = _client(lambda: None)                      # None = service identity
    body = c.post("/events/ws-ticket").json()
    assert body["kind"] == "service"
    assert ev._authenticate_ws(body["ticket"], db) is ev.SERVICE


def test_app_key_cannot_mint_a_ticket_at_all():
    """An installed app is not a platform service. Its view of the bus is
    what the SDK gives it, not the operator's site-wide stream."""
    c = _client(lambda: AppPrincipal(app_id="loitering-detection"))
    r = c.post("/events/ws-ticket")
    assert r.status_code == 403
    assert ev._ws_tickets == {}


def test_response_shape_is_backward_compatible(db):
    """Existing browser clients read `ticket` and `expires_in`; `kind` is
    additive."""
    _user(db, "alice")
    c = _client(lambda: SimpleNamespace(username="alice"))
    body = c.post("/events/ws-ticket").json()
    assert {"ticket", "expires_in"} <= set(body)
    assert body["expires_in"] == ev._WS_TICKET_TTL_SECONDS
