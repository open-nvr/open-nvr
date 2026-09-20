# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Correlation ids and the per-request context (HA-002).

Pinned here:

* a valid client ``X-Correlation-Id`` is kept, echoed back, and stamped on
  every audit row the request writes; an invalid one is replaced by the
  request's own id (never trusted into logs or headers);
* every request gets one, including the quiet media paths;
* a non-user actor set from a SYNC dependency reaches the endpoint and the
  audit row. This is the trap the design calls out: FastAPI runs sync
  dependencies in a threadpool on a copied context, so a ``ContextVar.set``
  there would be lost. The context object is mutated instead;
* outside a request (background work) write_audit_log still works, with
  no correlation id.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_correlation_test.db")
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

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import models  # noqa: E402
from core import request_context  # noqa: E402
from middleware.request_logging import RequestLoggingMiddleware  # noqa: E402
from services.audit_service import write_audit_log  # noqa: E402


@pytest.fixture()
def session_factory():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)


@pytest.fixture()
def client(session_factory):
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    def get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def token_principal():
        # Stand-in for the API-token resolver (HA-101): a SYNC dependency
        # that names the non-user actor by mutating the shared context.
        ctx = request_context.current()
        assert ctx is not None
        ctx.actor = "token:ha-main"
        return "principal"

    @app.post("/api/v1/async-audit")
    async def async_audit(db=Depends(get_db)):
        write_audit_log(db, action="test.async")
        return {"correlation_id": request_context.current().correlation_id}

    @app.post("/api/v1/sync-audit-as-token")
    def sync_audit(_p=Depends(token_principal), db=Depends(get_db)):
        write_audit_log(db, action="test.sync", details={"x": 1})
        return {"actor": request_context.current().actor}

    @app.post("/api/v1/async-audit-as-token")
    async def async_audit_token(_p=Depends(token_principal), db=Depends(get_db)):
        write_audit_log(db, action="test.async_token")
        return {"actor": request_context.current().actor}

    @app.get("/api/v1/recordings/playback/hls/x")
    def quiet():
        return {"ok": True}

    return TestClient(app)


def _rows(session_factory):
    db = session_factory()
    try:
        return db.query(models.AuditLog).order_by(models.AuditLog.id).all()
    finally:
        db.close()


def test_valid_inbound_id_is_kept_echoed_and_audited(client, session_factory):
    r = client.post("/api/v1/async-audit",
                    headers={"X-Correlation-Id": "ha-01J9ZK:ctx.42"})
    assert r.status_code == 200
    assert r.json()["correlation_id"] == "ha-01J9ZK:ctx.42"
    assert r.headers["X-Correlation-Id"] == "ha-01J9ZK:ctx.42"
    [row] = _rows(session_factory)
    assert row.correlation_id == "ha-01J9ZK:ctx.42"


@pytest.mark.parametrize("bad", [
    "has space", "semi;colon", "x" * 65, "<script>", "",
])
def test_invalid_inbound_id_is_replaced_by_request_id(client, session_factory, bad):
    r = client.post("/api/v1/async-audit", headers={"X-Correlation-Id": bad})
    assert r.status_code == 200
    corr = r.headers["X-Correlation-Id"]
    assert corr != bad
    assert corr == r.headers["X-Request-ID"]
    assert _rows(session_factory)[-1].correlation_id == corr


def test_generated_id_when_none_sent(client, session_factory):
    r = client.post("/api/v1/async-audit")
    corr = r.headers["X-Correlation-Id"]
    assert corr == r.headers["X-Request-ID"] and len(corr) == 36
    assert _rows(session_factory)[-1].correlation_id == corr


def test_each_request_gets_its_own_context(client, session_factory):
    a = client.post("/api/v1/async-audit").headers["X-Correlation-Id"]
    b = client.post("/api/v1/async-audit").headers["X-Correlation-Id"]
    assert a != b
    assert [r.correlation_id for r in _rows(session_factory)] == [a, b]


def test_actor_from_sync_dependency_reaches_sync_endpoint_and_audit(
    client, session_factory,
):
    r = client.post("/api/v1/sync-audit-as-token",
                    headers={"X-Correlation-Id": "corr-sync"})
    assert r.json() == {"actor": "token:ha-main"}
    [row] = _rows(session_factory)
    assert json.loads(row.details) == {"x": 1, "actor": "token:ha-main"}
    assert row.correlation_id == "corr-sync"


def test_actor_from_sync_dependency_reaches_async_endpoint_and_audit(
    client, session_factory,
):
    r = client.post("/api/v1/async-audit-as-token")
    assert r.json() == {"actor": "token:ha-main"}
    [row] = _rows(session_factory)
    assert json.loads(row.details) == {"actor": "token:ha-main"}


def test_actor_does_not_leak_into_the_next_request(client, session_factory):
    client.post("/api/v1/sync-audit-as-token")
    client.post("/api/v1/async-audit")
    token_row, plain_row = _rows(session_factory)
    assert json.loads(token_row.details)["actor"] == "token:ha-main"
    assert plain_row.details is None


def test_quiet_paths_still_carry_a_correlation_id(client):
    r = client.get("/api/v1/recordings/playback/hls/x",
                   headers={"X-Correlation-Id": "quiet-1"})
    assert r.status_code == 200
    assert r.headers["X-Correlation-Id"] == "quiet-1"


def test_outside_a_request_there_is_no_context(session_factory):
    assert request_context.current() is None
    db = session_factory()
    try:
        row = write_audit_log(db, action="test.background")
        assert row.correlation_id is None
        assert row.details is None
    finally:
        db.close()


def test_explicit_correlation_id_wins(session_factory):
    db = session_factory()
    try:
        row = write_audit_log(db, action="t", correlation_id="given")
        assert row.correlation_id == "given"
    finally:
        db.close()


def test_existing_actor_in_details_is_not_clobbered():
    from services.audit_service import _with_actor

    assert _with_actor({"actor": "user:bob"}, "token:x") == {"actor": "user:bob"}
    assert _with_actor("plain text", "token:x") == {
        "actor": "token:x", "details": "plain text",
    }


def test_audit_log_api_returns_and_filters_by_correlation_id(session_factory):
    """Operators can see the id, and pull every row one external action caused."""
    import core.auth as auth_mod
    from core.database import get_db
    from routers import audit_logs

    db = session_factory()
    try:
        write_audit_log(db, action="ptz.move", correlation_id="ha-ctx-1")
        write_audit_log(db, action="recording.pause", correlation_id="ha-ctx-1")
        write_audit_log(db, action="camera.update", correlation_id="other")
    finally:
        db.close()

    app = FastAPI()
    app.include_router(audit_logs.router, prefix="/api/v1")

    def _db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[auth_mod.get_current_superuser] = lambda: object()
    c = TestClient(app)

    body = c.get("/api/v1/audit-logs/", params={"correlation_id": "ha-ctx-1"}).json()
    assert body["total"] == 2
    assert {row["action"] for row in body["logs"]} == {"ptz.move", "recording.pause"}
    assert all(row["correlation_id"] == "ha-ctx-1" for row in body["logs"])

    everything = c.get("/api/v1/audit-logs/").json()
    assert everything["total"] == 3


# ── signed media URLs never reach the request log (HA-112 review) ─────────

_TOKEN = "m1.eyJrIjoiZXZlbnQiLCJpIjoxfQ.c2lnbmF0dXJl"


def test_redact_signed_media_path_masks_the_token_only():
    from utils.url_redaction import redact_signed_media_path as r

    assert r(f"/api/v1/media/s/{_TOKEN}") == "/api/v1/media/s/<redacted>"
    assert r(f"http://nvr:8000/api/v1/media/s/{_TOKEN}?x=1") == \
        "http://nvr:8000/api/v1/media/s/<redacted>"
    assert r(f"/api/v1/media/s/{_TOKEN}/deeper") == "/api/v1/media/s/<redacted>"
    for untouched in ("/api/v1/media/sign", "/api/v1/cameras/", "", None):
        assert r(untouched) == untouched


def test_signed_media_urls_are_redacted_in_every_request_log_record(monkeypatch):
    """The token in the path IS the credential. Three records could carry
    it: the middleware's request_start / request_complete pair and, for a
    403 or 404 from the route, main.py's http.exception record. None of
    them may hold the token, or the log would double as a link store."""
    import importlib

    from fastapi import HTTPException

    import middleware.request_logging as rl

    main = importlib.import_module("main")
    records: list = []

    class _Capture:
        def log_action(self, action, **kw):
            records.append((action, kw))

        def error(self, msg, **kw):
            records.append(("error", {"message": msg, **kw}))

    monkeypatch.setattr(rl, "api_logger", _Capture())
    monkeypatch.setattr(main, "main_logger", _Capture())

    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    app.add_exception_handler(HTTPException, main.http_exception_handler)

    @app.get("/api/v1/media/s/{token}")
    def fetch(token: str):
        raise HTTPException(status_code=403, detail="Invalid or expired link")

    r = TestClient(app).get(f"/api/v1/media/s/{_TOKEN}")
    assert r.status_code == 403
    assert [a for a, _ in records] == [
        "api.request_start", "http.exception", "api.request_complete"]
    blob = repr(records)
    assert _TOKEN not in blob and _TOKEN[3:20] not in blob
    assert blob.count("/api/v1/media/s/<redacted>") >= 3
