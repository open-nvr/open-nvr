# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Integration tests for the A2.4 v2 endpoints + correlation_id wiring.

Stubs out a contract-compliant adapter via httpx.MockTransport so the
tests don't need a real adapter container running. Verifies the
registry + audit + sovereignty + correlation_id paths end-to-end.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import httpx
import pytest


def _base_caps(*, fingerprint: str = "sha256:zzz", egress: list[str] | None = None) -> dict:
    return {
        "adapter": {
            "name": "stub", "version": "1.0.0", "vendor": "open-nvr",
            "license": "AGPL-3.0", "supported_contract_versions": ["1"],
        },
        "model": {"name": "stub-model", "version": "v1", "framework": "f", "fingerprint": fingerprint},
        "endpoints": {
            "infer": {"supported": True, "input_content_types": ["application/json"]},
            "infer_stream": {"supported": False},
        },
        "tasks_advertised": ["echo"],
        "permissions": {
            "gpu": False, "network_egress": egress or [],
            "host_filesystem": [], "shared_memory_paths": [], "host_metadata": False,
        },
        "scheduling": {"max_inflight": 1, "preferred_batch_size": 1, "fair_queuing": "none"},
        "cost": {"currency": "USD", "estimated_per_call": 0.0, "estimated_per_hour": 0.0,
                 "rate_limit_per_minute": None, "is_metered": False},
    }


class _StubAdapter:
    """Pretends to be a contract-compliant adapter. Captures the
    correlation_id header on /infer so tests can assert it gets
    threaded through."""

    def __init__(self) -> None:
        self.last_correlation_id: str | None = None
        self.last_payload: dict | None = None
        self.infer_response: dict = {
            "status": "ok",
            "model_name": "stub-model",
            "model_version": "v1",
            "inference_ms": 5,
            "result": {"echoed": True},
        }
        self.infer_status_code: int = 200

    async def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/capabilities":
            return httpx.Response(200, json=_base_caps())
        if path == "/health":
            return httpx.Response(200, json={
                "status": "ok",
                "adapter_name": "stub", "adapter_version": "1.0.0",
                "model_name": "stub-model", "model_version": "v1",
                "started_at": "2026-05-19T00:00:00Z", "uptime_seconds": 1,
            })
        if path == "/infer":
            self.last_correlation_id = request.headers.get("x-correlation-id")
            body = bytes(request.read())
            import json as _json
            try:
                self.last_payload = _json.loads(body) if body else None
            except _json.JSONDecodeError:
                self.last_payload = None
            return httpx.Response(self.infer_status_code, json=self.infer_response)
        return httpx.Response(404)


@pytest.fixture
def kaic_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AI_SOVEREIGNTY", "local_only")
    monkeypatch.setenv("ADAPTER_URL", "http://127.0.0.1:9100")
    monkeypatch.setenv("KAI_C_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("INTERNAL_API_KEY", "")
    # The internal surface fails closed on an unset key (see c6d7b1f). These
    # functional tests exercise endpoint behaviour, not auth, so run them as an
    # authorised local-dev box via the explicit opt-in. Auth is covered on its
    # own: test_v2_endpoints_require_internal_api_key_when_set (key set) and
    # test_v2_endpoints_fail_closed_when_key_unset (key unset, no opt-in).
    monkeypatch.setenv("KAI_C_ALLOW_ANONYMOUS", "true")
    return {"audit_path": tmp_path / "audit.jsonl"}


@pytest.fixture
def kaic_app(kaic_test_env, monkeypatch: pytest.MonkeyPatch):
    """Build the FastAPI app with the stub adapter wired in.

    The startup hook tries to register adapters from the env-derived
    ADAPTER_REGISTRY dict — we let that fail silently (the stub
    adapter URL won't resolve on real network), then register the
    stub explicitly via the v2 endpoint inside each test.
    """
    if "main" in sys.modules:
        importlib.reload(sys.modules["main"])
    import main as kaic_main

    stub = _StubAdapter()
    transport = httpx.MockTransport(stub.respond)

    from fastapi.testclient import TestClient

    # Patch AdapterRegistry to use the mock transport.
    original_init = kaic_main.AdapterRegistry.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=transport)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(kaic_main.AdapterRegistry, "__init__", patched_init)

    with TestClient(kaic_main.app) as client:
        yield client, stub


# ── Registry endpoints ─────────────────────────────────────────────


def test_register_adapter_returns_ok(kaic_app):
    client, stub = kaic_app
    # Default startup registers "default" → http://127.0.0.1:9100;
    # because we mocked it, registration should succeed.
    response = client.post(
        "/api/v1/adapters/register",
        json={"name": "stub-x", "url": "http://127.0.0.1:9100"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["adapter"]["name"] == "stub-x"
    assert body["adapter"]["fingerprint"] == "sha256:zzz"


def test_register_refuses_non_loopback_under_local_only(kaic_app):
    client, _ = kaic_app
    response = client.post(
        "/api/v1/adapters/register",
        json={"name": "bad", "url": "http://192.168.1.50:9100"},
    )
    assert response.status_code == 403
    detail = response.json()["detail"].lower()
    # The host is a LAN peer (not loopback, not in the Docker bridge
    # subnet), so local_only refuses it as "not on this machine".
    assert "local_only" in detail
    assert "not on this machine" in detail


def test_list_adapters_after_register(kaic_app):
    client, _ = kaic_app
    client.post("/api/v1/adapters/register", json={"name": "stub-x", "url": "http://127.0.0.1:9100"})
    response = client.get("/api/v1/adapters")
    body = response.json()
    names = [a["name"] for a in body["adapters"]]
    assert "stub-x" in names


def test_aggregated_capabilities(kaic_app):
    client, _ = kaic_app
    client.post("/api/v1/adapters/register", json={"name": "stub-x", "url": "http://127.0.0.1:9100"})
    response = client.get("/api/v1/ai/capabilities")
    body = response.json()
    assert body["sovereignty_mode"] == "local_only"
    assert body["contract_version"] == "1"
    assert "stub-x" in body["adapters"]


def test_deregister(kaic_app):
    client, _ = kaic_app
    client.post("/api/v1/adapters/register", json={"name": "stub-x", "url": "http://127.0.0.1:9100"})
    response = client.delete("/api/v1/adapters/stub-x")
    assert response.status_code == 200
    # Now /adapters should not include stub-x
    listing = client.get("/api/v1/adapters").json()
    assert not any(a["name"] == "stub-x" for a in listing["adapters"])


def test_refresh_unknown_adapter_returns_404(kaic_app):
    client, _ = kaic_app
    response = client.post("/api/v1/adapters/refresh?name=missing")
    assert response.status_code == 404


# ── Inference proxy + correlation_id ──────────────────────────────


def test_v1_infer_threads_correlation_id_to_adapter(kaic_app):
    client, stub = kaic_app
    client.post("/api/v1/adapters/register", json={"name": "stub-x", "url": "http://127.0.0.1:9100"})

    response = client.post(
        "/api/v1/infer/stub-x",
        json={"camera_id": "cam-1", "text": "hello"},
        headers={"X-Correlation-Id": "corr-abc-123"},
    )
    assert response.status_code == 200, response.text
    # Adapter saw the threaded correlation_id
    assert stub.last_correlation_id == "corr-abc-123"
    # Response echoed the id back
    assert response.headers.get("X-Correlation-Id") == "corr-abc-123"


def test_v1_infer_mints_correlation_id_when_absent(kaic_app):
    client, stub = kaic_app
    client.post("/api/v1/adapters/register", json={"name": "stub-x", "url": "http://127.0.0.1:9100"})
    response = client.post("/api/v1/infer/stub-x", json={"camera_id": "cam-1"})
    assert response.status_code == 200
    # Adapter still got A correlation_id (minted server-side)
    assert stub.last_correlation_id
    assert len(stub.last_correlation_id) >= 16


def test_v1_infer_unknown_adapter_returns_404(kaic_app):
    client, _ = kaic_app
    response = client.post("/api/v1/infer/nonexistent", json={})
    assert response.status_code == 404


# ── Audit emission via inference ───────────────────────────────────


def test_inference_completed_audit_event_after_success(kaic_app, kaic_test_env):
    client, _ = kaic_app
    client.post("/api/v1/adapters/register", json={"name": "stub-x", "url": "http://127.0.0.1:9100"})
    client.post(
        "/api/v1/infer/stub-x",
        json={"camera_id": "cam-audit-test"},
        headers={"X-Correlation-Id": "corr-audit-test"},
    )
    # Query the audit log
    response = client.get("/api/v1/audit?adapter=stub-x&event_type=inference.completed")
    body = response.json()
    matching = [
        e for e in body["events"]
        if e.get("camera_id") == "cam-audit-test"
    ]
    assert matching, "expected inference.completed audit event"
    assert matching[-1]["correlation_id"] == "corr-audit-test"


def test_inference_failed_audit_event_on_adapter_error(kaic_app):
    client, stub = kaic_app
    client.post("/api/v1/adapters/register", json={"name": "stub-x", "url": "http://127.0.0.1:9100"})
    # Make the stub return a failure envelope
    stub.infer_status_code = 400
    stub.infer_response = {
        "status": "error",
        "error": {
            "category": "transport_error", "code": "malformed_input",
            "message": "bad", "transient": False, "details": {},
        },
    }
    response = client.post("/api/v1/infer/stub-x", json={}, headers={"X-Correlation-Id": "corr-fail"})
    assert response.status_code == 400
    # Audit event recorded with error category
    audit_response = client.get("/api/v1/audit?event_type=inference.failed")
    matching = [e for e in audit_response.json()["events"] if e.get("correlation_id") == "corr-fail"]
    assert matching
    assert matching[-1]["error_category"] == "transport_error"
    assert matching[-1]["error_code"] == "malformed_input"


# ── Audit query filters ────────────────────────────────────────────


def test_audit_query_limit_validation(kaic_app):
    client, _ = kaic_app
    response = client.get("/api/v1/audit?limit=0")
    assert response.status_code == 400
    response = client.get("/api/v1/audit?limit=99999")
    assert response.status_code == 400


def test_audit_query_filters_by_camera(kaic_app):
    client, _ = kaic_app
    client.post("/api/v1/adapters/register", json={"name": "stub-x", "url": "http://127.0.0.1:9100"})
    client.post("/api/v1/infer/stub-x", json={"camera_id": "cam-a"})
    client.post("/api/v1/infer/stub-x", json={"camera_id": "cam-b"})
    response = client.get("/api/v1/audit?camera_id=cam-a")
    body = response.json()
    assert all(e.get("camera_id") == "cam-a" for e in body["events"] if "camera_id" in e)


# ── Auth on v2 endpoints (regression for self-review SR-NEW-7) ────


def test_v2_endpoints_require_internal_api_key_when_set(kaic_test_env, monkeypatch, tmp_path):
    """Regression for self-review SR-NEW-7: when INTERNAL_API_KEY is set,
    every v2 endpoint must reject anonymous calls."""
    monkeypatch.setenv("INTERNAL_API_KEY", "production-secret-token")
    if "main" in sys.modules:
        importlib.reload(sys.modules["main"])
    import main as kaic_main

    stub = _StubAdapter()
    transport = httpx.MockTransport(stub.respond)
    original_init = kaic_main.AdapterRegistry.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=transport)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(kaic_main.AdapterRegistry, "__init__", patched_init)

    from fastapi.testclient import TestClient
    with TestClient(kaic_main.app) as client:
        # Without the header → 401 on every v2 endpoint
        for method, path, body in [
            ("POST",   "/api/v1/adapters/register", {"name": "x", "url": "http://127.0.0.1:9100"}),
            ("DELETE", "/api/v1/adapters/x",        None),
            ("GET",    "/api/v1/adapters",          None),
            ("GET",    "/api/v1/ai/capabilities",   None),
            ("POST",   "/api/v1/adapters/refresh",  None),
            ("GET",    "/api/v1/adapters/x/metrics", None),
            ("POST",   "/api/v1/infer/x",           {}),
            ("GET",    "/api/v1/audit",             None),
        ]:
            request_kwargs = {"json": body} if body is not None else {}
            response = client.request(method, path, **request_kwargs)
            assert response.status_code == 401, f"{method} {path} returned {response.status_code} (should be 401 without auth)"

        # With the correct header → registration succeeds
        response = client.post(
            "/api/v1/adapters/register",
            json={"name": "auth-test", "url": "http://127.0.0.1:9100"},
            headers={"X-Internal-Api-Key": "production-secret-token"},
        )
        assert response.status_code == 200, response.text


def test_v2_endpoints_fail_closed_when_key_unset(kaic_test_env, monkeypatch):
    """Regression for c6d7b1f: with INTERNAL_API_KEY unset and no explicit
    KAI_C_ALLOW_ANONYMOUS opt-in, the internal surface fails CLOSED — every v2
    endpoint returns 401 rather than silently allowing anonymous calls."""
    monkeypatch.setenv("INTERNAL_API_KEY", "")
    monkeypatch.setenv("KAI_C_ALLOW_ANONYMOUS", "")
    if "main" in sys.modules:
        importlib.reload(sys.modules["main"])
    import main as kaic_main

    stub = _StubAdapter()
    transport = httpx.MockTransport(stub.respond)
    original_init = kaic_main.AdapterRegistry.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=transport)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(kaic_main.AdapterRegistry, "__init__", patched_init)

    from fastapi.testclient import TestClient
    with TestClient(kaic_main.app) as client:
        for method, path, body in [
            ("POST", "/api/v1/adapters/register", {"name": "x", "url": "http://127.0.0.1:9100"}),
            ("GET",  "/api/v1/adapters",          None),
            ("POST", "/api/v1/infer/x",           {}),
            ("GET",  "/api/v1/audit",             None),
        ]:
            request_kwargs = {"json": body} if body is not None else {}
            response = client.request(method, path, **request_kwargs)
            assert response.status_code == 401, (
                f"{method} {path} returned {response.status_code} (should fail closed)"
            )


def test_register_rejects_malformed_url(kaic_app):
    """Regression for self-review SR-NEW-8: malformed URLs produce a
    422 validation error (Pydantic) before they reach the sovereignty
    layer — operator sees 'bad URL', not a confusing 'sovereignty refused'."""
    client, _ = kaic_app
    response = client.post(
        "/api/v1/adapters/register",
        json={"name": "bad", "url": "this is not a url at all"},
    )
    assert response.status_code == 422, response.text
