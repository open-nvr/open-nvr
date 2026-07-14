# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Contract §8.5 — startup-seeded adapters are config-as-consent.

An adapter seeded from ADAPTER_REGISTRY (the operator's own startup
configuration) that declares permissions must be auto-granted at seed
time, with the grant audited as actor ``system:startup-config`` and a
first-class ``adapter_grant_id``. Adapters registered later via
``POST /api/v1/adapters/register`` keep the human approval gate.

Follows the ``kaic_app`` fixture pattern from test_main_v2.py, but the
stub adapter here DECLARES permissions (gpu + a host_filesystem path)
so the startup seed exercises the auto-grant path instead of the
trivial no-permissions auto-approve.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import httpx
import pytest

from test_main_v2 import _StubAdapter, _base_caps

DECLARED_KEYS = {"gpu", "host_filesystem:/models/yolov8"}


class _PermissionedStub(_StubAdapter):
    """Contract-compliant stub that declares permissions, so it
    registers into the §8 approval gate rather than trivially
    auto-approving."""

    async def respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/capabilities":
            caps = _base_caps()
            caps["permissions"]["gpu"] = True
            caps["permissions"]["host_filesystem"] = ["/models/yolov8"]
            return httpx.Response(200, json=caps)
        return await super().respond(request)


@pytest.fixture
def kaic_app_permissioned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build the FastAPI app with a PERMISSIONED stub adapter wired in.

    The startup seed registers "default" → the stub URL, which declares
    gpu + host_filesystem, so lifespan's config-as-consent auto-grant
    fires for it."""
    monkeypatch.setenv("AI_SOVEREIGNTY", "local_only")
    monkeypatch.setenv("ADAPTER_URL", "http://127.0.0.1:9100")
    monkeypatch.setenv("KAI_C_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("INTERNAL_API_KEY", "")
    # Internal surface fails closed on an unset key (c6d7b1f); run these
    # functional tests as an authorised local-dev box via the explicit opt-in.
    monkeypatch.setenv("KAI_C_ALLOW_ANONYMOUS", "true")

    if "main" in sys.modules:
        importlib.reload(sys.modules["main"])
    import main as kaic_main

    stub = _PermissionedStub()
    transport = httpx.MockTransport(stub.respond)

    from fastapi.testclient import TestClient

    original_init = kaic_main.AdapterRegistry.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=transport)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(kaic_main.AdapterRegistry, "__init__", patched_init)

    with TestClient(kaic_main.app) as client:
        yield client, stub


# ── §8.5 startup seed → auto-grant ─────────────────────────────────


def test_startup_seeded_adapter_with_perms_is_approved(kaic_app_permissioned):
    """The seeded "default" adapter declares gpu + a weights path, yet
    must come up APPROVED — the operator wrote it into the startup
    config, which is the consent act."""
    client, _ = kaic_app_permissioned
    response = client.get("/api/v1/adapters/default/permissions")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approval_status"] == "approved"
    assert body["pending"] == []
    assert set(body["granted"]) == DECLARED_KEYS


def test_startup_auto_grant_is_audited_with_system_actor(kaic_app_permissioned):
    """Config-as-consent is still a first-class grant: audited as
    adapter.permission_granted with actor system:startup-config and an
    adapter_grant_id, so the receipt chain stays intact."""
    client, _ = kaic_app_permissioned
    response = client.get(
        "/api/v1/audit?adapter=default&event_type=adapter.permission_granted"
    )
    events = response.json()["events"]
    assert events, "expected an adapter.permission_granted audit event"
    grant = events[-1]
    assert grant["actor"] == "system:startup-config"
    assert grant["adapter_grant_id"]
    assert set(grant["keys"]) == DECLARED_KEYS
    assert grant["approval_status"] == "approved"


def test_startup_seeded_adapter_serves_immediately(kaic_app_permissioned):
    """End-to-end: the auto-granted seed adapter passes the §8 serving
    gate on the governed infer path without any operator click."""
    client, _ = kaic_app_permissioned
    response = client.post("/api/v1/infer/default", json={"camera_id": "cam-1"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"


# ── runtime registration keeps the human gate ──────────────────────


def test_api_registered_adapter_stays_pending(kaic_app_permissioned):
    """The SAME permissioned adapter registered at runtime via
    POST /api/v1/adapters/register must NOT be auto-granted — the §8.3
    pending flow (human approval) still applies."""
    client, _ = kaic_app_permissioned
    response = client.post(
        "/api/v1/adapters/register",
        json={"name": "runtime-x", "url": "http://127.0.0.1:9100"},
    )
    assert response.status_code == 200, response.text

    perms = client.get("/api/v1/adapters/runtime-x/permissions").json()
    assert perms["approval_status"] == "pending"
    assert perms["granted"] == []
    assert set(perms["pending"]) == DECLARED_KEYS

    # And it is refused on the governed serving path until granted.
    refused = client.post("/api/v1/infer/runtime-x", json={"camera_id": "cam-1"})
    assert refused.status_code == 403

    # No system:startup-config grant may exist for a runtime-registered
    # adapter.
    audit = client.get(
        "/api/v1/audit?adapter=runtime-x&event_type=adapter.permission_granted"
    ).json()["events"]
    assert not [e for e in audit if e.get("actor") == "system:startup-config"]
