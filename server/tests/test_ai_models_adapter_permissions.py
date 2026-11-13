# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Tests for the adapter permission-approval proxy routes (§8 / §11):

    GET  /ai-models/adapters/{name}/permissions
    POST /ai-models/adapters/{name}/permissions/grant
    POST /ai-models/adapters/{name}/permissions/revoke
    POST /ai-models/adapters/{name}/permissions/approve-all

Run with:

    cd server && pytest tests/test_ai_models_adapter_permissions.py -v

Coverage:

* Happy path: KAI-C's permission view is proxied through verbatim.
* Mutations write an audit log (action + adapter + keys + grant_id).
* Unknown adapter: KAI-C's 404 maps to a backend 404.
* KAI-C down / 5xx: graceful 502, never a raw 500.
* The service half sends X-Internal-Api-Key + X-Actor.
"""

from __future__ import annotations

# Python 3.10 sandbox polyfill (see test_ai_models_adapter_metrics.py).
import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

import os
import secrets
import sys
import types as _types
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def info(self, *a, **kw):
        pass

    def warning(self, *a, **kw):
        pass

    def error(self, *a, **kw):
        pass

    def debug(self, *a, **kw):
        pass

    def critical(self, *a, **kw):
        pass


for _name in (
    "main_logger", "auth_logger", "camera_logger",
    "recording_logger", "cloud_logger", "ai_logger",
):
    setattr(_lm, _name, _L())
sys.modules["core.logging_config"] = _lm


from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import get_current_active_user  # noqa: E402
from core.database import get_db  # noqa: E402
from routers import ai_models as ai_models_router  # noqa: E402

VIEW = {
    "adapter": "yolov8",
    "approval_status": "pending",
    "declared": [
        {"key": "gpu", "label": "GPU access", "kind": "gpu",
         "sovereignty_conflict": False},
    ],
    "granted": [],
    "pending": ["gpu"],
}

GRANT_RESULT = {**VIEW, "approval_status": "approved", "granted": ["gpu"],
                "pending": [], "grant_id": "deadbeef"}


class _StubUser:
    id = 1
    username = "tester"
    # Grant/revoke/approve-all are gated on ai.manage (superusers hold
    # every permission); the gate itself is covered structurally in
    # test_user_superuser_flag.py — these tests exercise the proxying.
    is_superuser = True


class _StubKaiCService:
    """Stands in for KaiCService — only the permission methods are used.
    Records the audit-relevant call args so tests can assert on them."""

    def __init__(self, *, response: dict | None = None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.calls: list[tuple] = []

    async def get_adapter_permissions(self, adapter_name: str) -> dict:
        self.calls.append(("get", adapter_name))
        if self._error is not None:
            raise self._error
        return self._response or {}

    async def grant_adapter_permissions(self, adapter_name, keys, actor=None) -> dict:
        self.calls.append(("grant", adapter_name, keys, actor))
        if self._error is not None:
            raise self._error
        return self._response or {}

    async def revoke_adapter_permissions(self, adapter_name, keys, actor=None) -> dict:
        self.calls.append(("revoke", adapter_name, keys, actor))
        if self._error is not None:
            raise self._error
        return self._response or {}

    async def approve_all_adapter_permissions(self, adapter_name, actor=None) -> dict:
        self.calls.append(("approve", adapter_name, actor))
        if self._error is not None:
            raise self._error
        return self._response or {}


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request(
        "GET", "http://localhost:8100/api/v1/adapters/yolov8/permissions"
    )
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"KAI-C returned {status_code}", request=request, response=response
    )


@pytest.fixture
def audit_calls(monkeypatch):
    """Capture write_audit_log calls without a real DB."""
    calls: list[dict] = []

    def _fake(db, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(ai_models_router, "write_audit_log", _fake)
    return calls


@pytest.fixture
def make_client(monkeypatch):
    def _make(service: _StubKaiCService) -> TestClient:
        monkeypatch.setattr(
            ai_models_router, "get_kai_c_service", lambda: service
        )
        app = FastAPI()
        app.include_router(ai_models_router.router)
        app.dependency_overrides[get_db] = lambda: iter([None])
        app.dependency_overrides[get_current_active_user] = lambda: _StubUser()
        return TestClient(app)

    return _make


# ── GET permissions ─────────────────────────────────────────────────


def test_get_permissions_proxied_verbatim(make_client):
    service = _StubKaiCService(response=VIEW)
    client = make_client(service)
    resp = client.get("/ai-models/adapters/yolov8/permissions")
    assert resp.status_code == 200
    assert resp.json() == VIEW
    assert service.calls == [("get", "yolov8")]


def test_get_permissions_unknown_adapter_404(make_client):
    client = make_client(_StubKaiCService(error=_http_status_error(404)))
    resp = client.get("/ai-models/adapters/ghost/permissions")
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]


def test_get_permissions_kai_c_down_502(make_client):
    client = make_client(
        _StubKaiCService(error=httpx.ConnectError("connection refused"))
    )
    resp = client.get("/ai-models/adapters/yolov8/permissions")
    assert resp.status_code == 502


# ── grant ───────────────────────────────────────────────────────────


def test_grant_returns_view_and_audits(make_client, audit_calls):
    service = _StubKaiCService(response=GRANT_RESULT)
    client = make_client(service)
    resp = client.post(
        "/ai-models/adapters/yolov8/permissions/grant", json={"keys": ["gpu"]}
    )
    assert resp.status_code == 200
    assert resp.json() == GRANT_RESULT
    # service called with actor = current user's username.
    assert service.calls == [("grant", "yolov8", ["gpu"], "tester")]
    # audit log recorded action + keys + grant_id.
    assert len(audit_calls) == 1
    a = audit_calls[0]
    assert a["action"] == "adapter.permission.grant"
    assert a["entity_id"] == "yolov8"
    assert a["details"]["keys"] == ["gpu"]
    assert a["details"]["grant_id"] == "deadbeef"


def test_grant_unknown_adapter_404(make_client):
    client = make_client(_StubKaiCService(error=_http_status_error(404)))
    resp = client.post(
        "/ai-models/adapters/ghost/permissions/grant", json={"keys": ["gpu"]}
    )
    assert resp.status_code == 404


def test_grant_kai_c_5xx_502(make_client):
    client = make_client(_StubKaiCService(error=_http_status_error(500)))
    resp = client.post(
        "/ai-models/adapters/yolov8/permissions/grant", json={"keys": ["gpu"]}
    )
    assert resp.status_code == 502


# ── revoke ──────────────────────────────────────────────────────────


def test_revoke_returns_view_and_audits(make_client, audit_calls):
    revoked = {**VIEW, "grant_id": "cafe"}
    service = _StubKaiCService(response=revoked)
    client = make_client(service)
    resp = client.post(
        "/ai-models/adapters/yolov8/permissions/revoke", json={"keys": ["gpu"]}
    )
    assert resp.status_code == 200
    assert service.calls == [("revoke", "yolov8", ["gpu"], "tester")]
    assert audit_calls[0]["action"] == "adapter.permission.revoke"
    assert audit_calls[0]["details"]["grant_id"] == "cafe"


# ── approve-all ─────────────────────────────────────────────────────


def test_approve_all_returns_view_and_audits(make_client, audit_calls):
    service = _StubKaiCService(response=GRANT_RESULT)
    client = make_client(service)
    resp = client.post("/ai-models/adapters/yolov8/permissions/approve-all")
    assert resp.status_code == 200
    assert service.calls == [("approve", "yolov8", "tester")]
    a = audit_calls[0]
    assert a["action"] == "adapter.permission.approve"
    assert a["details"]["grant_id"] == "deadbeef"
    assert a["details"]["keys"] == ["gpu"]  # from result["granted"]


def test_approve_all_unknown_adapter_404(make_client):
    client = make_client(_StubKaiCService(error=_http_status_error(404)))
    resp = client.post("/ai-models/adapters/ghost/permissions/approve-all")
    assert resp.status_code == 404


# ── service half: headers ───────────────────────────────────────────


@pytest.mark.anyio
async def test_service_grant_sends_key_and_actor(monkeypatch):
    from services.kai_c_service import KaiCService

    seen: dict = {}

    async def _respond(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        seen["key"] = request.headers.get("x-internal-api-key")
        seen["actor"] = request.headers.get("x-actor")
        import json as _json
        seen["body"] = _json.loads(request.read())
        return httpx.Response(200, json=GRANT_RESULT)

    service = KaiCService.__new__(KaiCService)
    service.kai_c_url = "http://localhost:8100"
    service.http_client = httpx.AsyncClient(transport=httpx.MockTransport(_respond))
    monkeypatch.setattr(KaiCService, "_internal_api_key", lambda self: "secret-key")

    body = await service.grant_adapter_permissions("yolov8", ["gpu"], actor="alice")
    await service.http_client.aclose()

    assert body == GRANT_RESULT
    assert seen["path"] == "/api/v1/adapters/yolov8/permissions/grant"
    assert seen["method"] == "POST"
    assert seen["key"] == "secret-key"
    assert seen["actor"] == "alice"
    assert seen["body"] == {"keys": ["gpu"]}


@pytest.mark.anyio
async def test_service_approve_all_no_body(monkeypatch):
    from services.kai_c_service import KaiCService

    seen: dict = {}

    async def _respond(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["actor"] = request.headers.get("x-actor")
        return httpx.Response(200, json=GRANT_RESULT)

    service = KaiCService.__new__(KaiCService)
    service.kai_c_url = "http://localhost:8100"
    service.http_client = httpx.AsyncClient(transport=httpx.MockTransport(_respond))
    monkeypatch.setattr(KaiCService, "_internal_api_key", lambda self: "")

    await service.approve_all_adapter_permissions("yolov8", actor="bob")
    await service.http_client.aclose()
    assert seen["path"] == "/api/v1/adapters/yolov8/permissions/approve-all"
    assert seen["actor"] == "bob"


@pytest.fixture
def anyio_backend():
    return "asyncio"
