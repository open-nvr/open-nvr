# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Registry tests — registration, polling, drift detection, deregistration."""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from kai_c.audit import AuditEventType, AuditStore
from kai_c.contract_types import CapabilitiesResponse
from kai_c.registry import AdapterRegistry
from kai_c.sovereignty import SovereigntyViolation


def _base_caps(*, fingerprint: str = "sha256:aaa", egress: list[str] | None = None, gpu: bool = False) -> dict:
    return {
        "adapter": {
            "name": "test-adapter", "version": "1.0.0", "vendor": "open-nvr",
            "license": "AGPL-3.0", "supported_contract_versions": ["1"],
        },
        "model": {
            "name": "m1", "version": "v1", "framework": "f", "fingerprint": fingerprint,
        },
        "endpoints": {
            "infer": {"supported": True, "input_content_types": ["application/json"]},
            "infer_stream": {"supported": False},
        },
        "tasks_advertised": ["echo"],
        "permissions": {
            "gpu": gpu, "network_egress": egress or [],
            "host_filesystem": [], "shared_memory_paths": [], "host_metadata": False,
        },
        "scheduling": {"max_inflight": 1, "preferred_batch_size": 1, "fair_queuing": "none"},
        "cost": {"currency": "USD", "estimated_per_call": 0.0, "estimated_per_hour": 0.0,
                 "rate_limit_per_minute": None, "is_metered": False},
    }


class _StubAdapter:
    """Fake httpx response source backing a single adapter URL."""

    def __init__(self, url: str, capabilities: dict, health_ok: bool = True) -> None:
        self.url = url
        self._caps = capabilities
        self._health_ok = health_ok
        self.call_count = 0

    def update_capabilities(self, capabilities: dict) -> None:
        self._caps = capabilities

    def set_health(self, ok: bool) -> None:
        self._health_ok = ok

    async def respond(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        path = request.url.path
        if path == "/capabilities":
            return httpx.Response(200, json=self._caps)
        if path == "/health":
            if not self._health_ok:
                return httpx.Response(503)
            return httpx.Response(200, json={
                "status": "ok",
                "adapter_name": "test-adapter",
                "adapter_version": "1.0.0",
                "model_name": "m1",
                "model_version": "v1",
                "started_at": "2026-05-19T00:00:00Z",
                "uptime_seconds": 1,
            })
        return httpx.Response(404)


@pytest.fixture
def audit(tmp_path: Path) -> AuditStore:
    return AuditStore(path=str(tmp_path / "audit.jsonl"))


@pytest.fixture
def adapter_stub() -> _StubAdapter:
    return _StubAdapter(url="http://127.0.0.1:9100", capabilities=_base_caps())


@pytest.fixture
async def registry(audit, adapter_stub):
    """Build a registry whose http client is wired through the stub."""
    transport = httpx.MockTransport(adapter_stub.respond)
    client = httpx.AsyncClient(transport=transport)
    reg = AdapterRegistry(
        sovereignty_mode="local_only", audit=audit, http_client=client,
        poll_interval_seconds=999,  # disable auto-poll
    )
    yield reg
    await reg.aclose()


# ── Registration ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_stores_adapter_and_emits_audit_event(registry, adapter_stub, audit):
    adapter = await registry.register("yolov8", adapter_stub.url)
    assert adapter.name == "yolov8"
    assert adapter.fingerprint == "sha256:aaa"
    assert registry.get("yolov8") is not None

    rows = audit.read_all()
    assert any(r["type"] == "adapter.registered" and r["adapter"] == "yolov8" for r in rows)


@pytest.mark.asyncio
async def test_register_refuses_under_local_only_with_egress(audit):
    caps_with_egress = _base_caps(egress=["api.openai.com"])
    stub = _StubAdapter("http://127.0.0.1:9100", caps_with_egress)
    transport = httpx.MockTransport(stub.respond)
    client = httpx.AsyncClient(transport=transport)
    reg = AdapterRegistry(sovereignty_mode="local_only", audit=audit, http_client=client)
    try:
        with pytest.raises(SovereigntyViolation, match="network_egress"):
            await reg.register("bad", stub.url)
        assert reg.get("bad") is None
    finally:
        await reg.aclose()


# ── Refresh / drift ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_fingerprint_drift_emits_event(registry, adapter_stub, audit):
    await registry.register("yolov8", adapter_stub.url)
    # Rotate the weights — fingerprint changes
    adapter_stub.update_capabilities(_base_caps(fingerprint="sha256:bbb"))
    await registry.refresh("yolov8")
    rows = audit.read_all()
    drift = [r for r in rows if r["type"] == "adapter.fingerprint_mismatch"]
    assert len(drift) == 1
    assert drift[0]["previous_fingerprint"] == "sha256:aaa"
    assert drift[0]["current_fingerprint"] == "sha256:bbb"


@pytest.mark.asyncio
async def test_refresh_adds_permission_deregisters(registry, adapter_stub, audit):
    """§11.3 blocking change: adding a permission de-registers the adapter."""
    await registry.register("yolov8", adapter_stub.url)
    # Adapter now claims it needs GPU permission — under §11.3 this is a
    # blocking drift event.
    adapter_stub.update_capabilities(_base_caps(gpu=True))
    await registry.refresh("yolov8")
    assert registry.get("yolov8") is None  # de-registered
    rows = audit.read_all()
    drift = [r for r in rows if r["type"] == "adapter.capability_drift"]
    assert any("gpu" in r.get("added_permissions", []) for r in drift)


@pytest.mark.asyncio
async def test_refresh_adds_egress_under_local_only_deregisters(registry, adapter_stub, audit):
    """Adapter that ADDS network_egress at runtime under local_only
    gets refused on the next poll."""
    await registry.register("yolov8", adapter_stub.url)
    adapter_stub.update_capabilities(_base_caps(egress=["api.openai.com"]))
    await registry.refresh("yolov8")
    assert registry.get("yolov8") is None
    rows = audit.read_all()
    assert any(r["type"] == "inference.refused_sovereignty" for r in rows)


@pytest.mark.asyncio
async def test_refresh_health_failures_emit_unavailable(registry, adapter_stub, audit):
    """Three consecutive /health failures → adapter.unavailable audit."""
    await registry.register("yolov8", adapter_stub.url)
    adapter_stub.set_health(False)
    for _ in range(3):
        await registry.refresh("yolov8")
    rows = audit.read_all()
    assert any(r["type"] == "adapter.unavailable" for r in rows)


# ── Deregister ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deregister_removes_and_emits(registry, adapter_stub, audit):
    await registry.register("yolov8", adapter_stub.url)
    await registry.deregister("yolov8")
    assert registry.get("yolov8") is None
    rows = audit.read_all()
    assert any(r["type"] == "adapter.deregistered" for r in rows)


@pytest.mark.asyncio
async def test_deregister_unknown_is_noop(registry):
    # Should not raise
    await registry.deregister("does-not-exist")


# ── Listing / aggregation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_summaries_includes_registered_adapters(registry, adapter_stub):
    await registry.register("yolov8", adapter_stub.url)
    summaries = registry.list_summaries()
    assert len(summaries) == 1
    assert summaries[0]["name"] == "yolov8"
    assert summaries[0]["fingerprint"] == "sha256:aaa"
    assert "echo" in summaries[0]["tasks_advertised"]


@pytest.mark.asyncio
async def test_aggregated_capabilities_includes_sovereignty_mode(registry, adapter_stub):
    await registry.register("yolov8", adapter_stub.url)
    agg = registry.aggregated_capabilities()
    assert agg["sovereignty_mode"] == "local_only"
    assert agg["contract_version"] == "1"
    assert "yolov8" in agg["adapters"]


# ── Forward compatibility (peer-review PR-5) ───────────────────────


@pytest.mark.asyncio
async def test_register_tolerates_unknown_capability_fields(audit):
    """Regression for peer-review PR-5: if an adapter's /capabilities
    response includes a future contract field KAI-C doesn't recognize,
    KAI-C must still parse and register the adapter (extra='ignore'
    on vendored types). Strict-server, lenient-client per Postel."""
    caps_with_future_field = _base_caps()
    caps_with_future_field["future_extension"] = {"some_new_thing": True}
    caps_with_future_field["adapter"]["future_capability_signal"] = "x"
    caps_with_future_field["model"]["future_metric"] = 42.0

    stub = _StubAdapter("http://127.0.0.1:9100", caps_with_future_field)
    transport = httpx.MockTransport(stub.respond)
    client = httpx.AsyncClient(transport=transport)
    reg = AdapterRegistry(sovereignty_mode="local_only", audit=audit, http_client=client)
    try:
        adapter = await reg.register("future-adapter", stub.url)
        assert adapter is not None
        assert adapter.fingerprint == "sha256:aaa"
    finally:
        await reg.aclose()
