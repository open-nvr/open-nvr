# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""An app reads the site's arming state through the app door.

``GET /internal/app/site-mode`` answers the same body as the operator's
``GET /site-mode`` for any app or site key, deployment-wide (not roster
scoped — "is anyone home" is a property of the site). It is read-only:
arming stays the operator's verb.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("INTERNAL_API_KEY", "site_" + secrets.token_hex(16))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-app-site-mode")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.config import settings  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from routers import app_platform  # noqa: E402
from services import site_mode  # noqa: E402

#: Read at import, not set here: another module may have configured the
#: key first, and a key we merely wish were configured authenticates nothing.
SITE_KEY = settings.internal_api_key


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(app_platform.router)

    def _db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    with TestClient(app) as tc:
        yield tc, SessionLocal


def _get(tc, headers=None):
    return tc.get("/internal/app/site-mode",
                  headers=headers if headers is not None
                  else {"X-Internal-Api-Key": SITE_KEY})


def test_default_is_armed_away_with_the_operator_body_shape(client):
    tc, _ = client
    r = _get(tc)
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "armed_away"
    assert body["changed_at"] is None and body["changed_by"] is None
    assert body["modes"] == list(site_mode.MODES)


def test_a_change_made_by_the_operator_is_what_the_app_sees(client):
    tc, SessionLocal = client
    s = SessionLocal()
    site_mode.set_mode(s, "disarmed", "user:admin")
    s.commit()
    s.close()
    body = _get(tc).json()
    assert body["mode"] == "disarmed" and body["changed_by"] == "user:admin"


def test_no_key_no_answer(client):
    tc, _ = client
    assert _get(tc, headers={}).status_code in (401, 403)


def test_the_app_door_is_read_only(client):
    """Arming is an operator verb: the app door has no PUT."""
    tc, _ = client
    r = tc.put("/internal/app/site-mode", json={"mode": "disarmed"},
               headers={"X-Internal-Api-Key": SITE_KEY})
    assert r.status_code == 405
