# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sovereignty enforcement v2 tests — including the network_egress
tightening that A2.4 adds vs. the M1a loopback-only check."""
from __future__ import annotations

import pytest

from kai_c.contract_types import (
    AdapterInfo,
    CapabilitiesResponse,
    EndpointsInfo,
    InferEndpointInfo,
    ModelInfo,
    Permissions,
    Scheduling,
    StreamEndpointInfo,
)
from kai_c.sovereignty import (
    SovereigntyViolation,
    check_adapter,
    host_is_loopback,
)


def _caps(*, egress: list[str] | None = None) -> CapabilitiesResponse:
    return CapabilitiesResponse(
        adapter=AdapterInfo(
            name="test-adapter", version="1.0.0", vendor="x", license="MIT",
            supported_contract_versions=["1"],
        ),
        model=ModelInfo(name="m", version="1", framework="f"),
        endpoints=EndpointsInfo(
            infer=InferEndpointInfo(supported=True),
            infer_stream=StreamEndpointInfo(supported=False),
        ),
        permissions=Permissions(network_egress=egress or []),
        scheduling=Scheduling(),
    )


# ── host_is_loopback ───────────────────────────────────────────────


def test_host_is_loopback_recognises_common_forms():
    assert host_is_loopback("localhost")
    assert host_is_loopback("127.0.0.1")
    assert host_is_loopback("::1")
    assert host_is_loopback("[::1]")  # bracketed IPv6


def test_host_is_loopback_rejects_routable():
    assert not host_is_loopback("8.8.8.8")
    assert not host_is_loopback("example.com")
    assert not host_is_loopback("0.0.0.0")  # wildcard bind, not loopback
    assert not host_is_loopback(None)
    assert not host_is_loopback("")


# ── local_only mode ────────────────────────────────────────────────


def test_local_only_accepts_loopback_url_no_egress():
    check_adapter(
        sovereignty_mode="local_only",
        adapter_url="http://127.0.0.1:9100",
        capabilities=_caps(egress=[]),
    )


def test_local_only_refuses_non_loopback_url():
    with pytest.raises(SovereigntyViolation, match="non-loopback"):
        check_adapter(
            sovereignty_mode="local_only",
            adapter_url="http://192.168.1.10:9100",
            capabilities=None,
        )


def test_local_only_refuses_wildcard_bind():
    with pytest.raises(SovereigntyViolation, match="0.0.0.0"):
        check_adapter(
            sovereignty_mode="local_only",
            adapter_url="http://0.0.0.0:9100",
            capabilities=None,
        )


def test_local_only_refuses_loopback_url_with_egress_declared():
    """A2.4 tightening: under local_only, even loopback adapters get
    refused if they advertise non-empty permissions.network_egress
    (because that means they're a cloud-proxy)."""
    with pytest.raises(SovereigntyViolation, match="network_egress"):
        check_adapter(
            sovereignty_mode="local_only",
            adapter_url="http://127.0.0.1:9100",
            capabilities=_caps(egress=["api.openai.com"]),
        )


# ── federated mode ─────────────────────────────────────────────────


def test_federated_accepts_explicit_egress_list():
    check_adapter(
        sovereignty_mode="federated",
        adapter_url="http://192.168.1.10:9100",
        capabilities=_caps(egress=["api-inference.huggingface.co", "models.example.com"]),
    )


def test_federated_refuses_wildcard_egress():
    with pytest.raises(SovereigntyViolation, match="wildcard"):
        check_adapter(
            sovereignty_mode="federated",
            adapter_url="http://192.168.1.10:9100",
            capabilities=_caps(egress=["*.example.com"]),
        )


# ── cloud_allowed mode ─────────────────────────────────────────────


def test_cloud_allowed_skips_all_checks():
    # Even wildcards under non-loopback URL — cloud_allowed lets it through.
    check_adapter(
        sovereignty_mode="cloud_allowed",
        adapter_url="https://external.example.com",
        capabilities=_caps(egress=["*"]),
    )


# ── Invalid mode ───────────────────────────────────────────────────


def test_invalid_mode_raises():
    with pytest.raises(SovereigntyViolation, match="invalid"):
        check_adapter(
            sovereignty_mode="banana",
            adapter_url="http://127.0.0.1:9100",
            capabilities=None,
        )
