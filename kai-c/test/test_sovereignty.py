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
    host_is_on_this_machine,
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
    # A LAN peer host (not loopback, not in the Docker bridge subnet) is
    # still refused under local_only.
    with pytest.raises(SovereigntyViolation, match="not on this machine"):
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


# ── ISSUE-70: local_only must accept on-box Docker-bridge adapters ──


def _mock_resolver(monkeypatch, mapping: dict[str, str]):
    """Patch socket.getaddrinfo so service names resolve to fixed IPs
    without touching real DNS."""
    import socket as _socket

    real = _socket.getaddrinfo

    def fake(host, *args, **kwargs):
        if host in mapping:
            return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", (mapping[host], 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(_socket, "getaddrinfo", fake)


def test_local_only_accepts_docker_service_name_on_bridge(monkeypatch):
    """The reported bug: ``yolov8-adapter:9002`` resolves to a Docker
    bridge IP (inside OPENNVR_DOCKER_SUBNET). It is on this machine, so
    local_only must accept it instead of returning a sovereignty 400."""
    _mock_resolver(monkeypatch, {"yolov8-adapter": "172.28.0.7"})
    check_adapter(
        sovereignty_mode="local_only",
        adapter_url="http://yolov8-adapter:9002",
        capabilities=_caps(egress=[]),
    )


def test_local_only_accepts_direct_bridge_ip(monkeypatch):
    check_adapter(
        sovereignty_mode="local_only",
        adapter_url="http://172.28.0.7:9002",
        capabilities=_caps(egress=[]),
    )


def test_local_only_still_refuses_lan_peer_service_name(monkeypatch):
    """A service name that resolves OUTSIDE the bridge subnet (a peer VM
    on the LAN) must still be refused — the fix must not become a
    blanket allow."""
    _mock_resolver(monkeypatch, {"adapter-vm.internal": "192.168.1.50"})
    with pytest.raises(SovereigntyViolation, match="not on this machine"):
        check_adapter(
            sovereignty_mode="local_only",
            adapter_url="http://adapter-vm.internal:9002",
            capabilities=None,
        )


def test_local_only_bridge_adapter_with_egress_still_refused(monkeypatch):
    """Being on the bridge does NOT exempt an adapter from the
    network_egress (cloud-proxy) check."""
    _mock_resolver(monkeypatch, {"yolov8-adapter": "172.28.0.7"})
    with pytest.raises(SovereigntyViolation, match="network_egress"):
        check_adapter(
            sovereignty_mode="local_only",
            adapter_url="http://yolov8-adapter:9002",
            capabilities=_caps(egress=["api.openai.com"]),
        )


def test_host_is_on_this_machine_table(monkeypatch):
    _mock_resolver(
        monkeypatch,
        {"yolov8-adapter": "172.28.0.7", "adapter-vm.internal": "192.168.1.50"},
    )
    assert host_is_on_this_machine("localhost")
    assert host_is_on_this_machine("127.0.0.1")
    assert host_is_on_this_machine("::1")
    assert host_is_on_this_machine("172.28.0.7")
    assert host_is_on_this_machine("yolov8-adapter")
    assert not host_is_on_this_machine("adapter-vm.internal")
    assert not host_is_on_this_machine("8.8.8.8")
    assert not host_is_on_this_machine("0.0.0.0")
    assert not host_is_on_this_machine(None)


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
