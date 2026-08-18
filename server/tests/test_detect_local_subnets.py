# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Tests for detect_local_subnets() — the multi-NIC auto-detection used by ONVIF
discovery when no Camera LAN subnet is configured.

    cd server && pytest tests/test_detect_local_subnets.py -v
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
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

import pytest  # noqa: E402

import routers.network as net  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Detection reads OPENNVR_HOST_IP / OPENNVR_DOCKER_SUBNET — keep the host's
    real environment out of every test."""
    monkeypatch.delenv("OPENNVR_HOST_IP", raising=False)
    monkeypatch.delenv("OPENNVR_LAN_IPS", raising=False)
    monkeypatch.delenv("OPENNVR_DOCKER_SUBNET", raising=False)


def _stub_docker(monkeypatch, in_docker=True):
    """Make os.path.exists('/.dockerenv') report the given container state
    without disturbing other path checks."""
    real_exists = os.path.exists
    monkeypatch.setattr(
        net.os.path,
        "exists",
        lambda p: in_docker if p == "/.dockerenv" else real_exists(p),
    )


def _stub_hostname_ips(monkeypatch, ips):
    """Make getaddrinfo(hostname) return the given IPv4 addresses, and force the
    default-route probe to fail so the test controls the full input set."""
    def boom(*a, **k):
        raise OSError("no default route")

    monkeypatch.setattr(net.socket, "socket", boom)
    monkeypatch.setattr(net.socket, "gethostname", lambda: "testhost")
    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda host, port, family: [(None, None, None, None, (ip, 0)) for ip in ips],
    )


def test_detects_all_private_subnets_across_nics(monkeypatch):
    # A multi-NIC host: camera LAN, default/uplink LAN, and a second camera-LAN IP.
    _stub_hostname_ips(
        monkeypatch,
        ["192.168.1.10", "192.168.1.44", "192.168.31.63", "10.20.0.5"],
    )
    subs = net.detect_local_subnets()
    assert "192.168.1.0/24" in subs  # the camera's subnet, NOT on the default route
    assert "192.168.31.0/24" in subs
    assert "10.20.0.0/24" in subs
    # two IPs on 192.168.1.x collapse to a single /24
    assert subs.count("192.168.1.0/24") == 1


def test_filters_out_loopback_linklocal_and_public(monkeypatch):
    # 127.x loopback, 169.254.x link-local, and 8.8.8.8 (globally routable) all drop.
    _stub_hostname_ips(
        monkeypatch,
        ["127.0.0.1", "169.254.83.107", "8.8.8.8", "192.168.1.10"],
    )
    subs = net.detect_local_subnets()
    assert subs == ["192.168.1.0/24"]  # only the private LAN survives


def test_returns_empty_when_nothing_detectable(monkeypatch):
    _stub_hostname_ips(monkeypatch, [])
    assert net.detect_local_subnets() == []


# ── Docker-awareness ────────────────────────────────────────────────────────
# Inside a bridge container the socket probes only see the compose bridge
# (e.g. 172.28.0.x) — never a camera LAN. Detection must ignore those and use
# OPENNVR_HOST_IP (the host's LAN address, exported by the launchers) instead.


def test_docker_uses_host_ip_and_ignores_bridge(monkeypatch):
    _stub_docker(monkeypatch)
    _stub_hostname_ips(monkeypatch, ["172.28.0.5"])
    monkeypatch.setenv("OPENNVR_HOST_IP", "192.168.1.50")
    assert net.detect_local_subnets() == ["192.168.1.0/24"]


def test_docker_without_host_ip_returns_empty(monkeypatch):
    _stub_docker(monkeypatch)
    _stub_hostname_ips(monkeypatch, ["172.28.0.5"])
    assert net.detect_local_subnets() == []


def test_docker_keeps_probed_ips_outside_bridge(monkeypatch):
    # host-network container: /.dockerenv exists but probes see real host NICs
    _stub_docker(monkeypatch)
    _stub_hostname_ips(monkeypatch, ["192.168.31.63", "172.28.0.5"])
    assert net.detect_local_subnets() == ["192.168.31.0/24"]


def test_docker_custom_bridge_subnet_respected(monkeypatch):
    _stub_docker(monkeypatch)
    _stub_hostname_ips(monkeypatch, ["10.99.0.7"])
    monkeypatch.setenv("OPENNVR_DOCKER_SUBNET", "10.99.0.0/16")
    monkeypatch.setenv("OPENNVR_HOST_IP", "10.20.0.5")
    assert net.detect_local_subnets() == ["10.20.0.0/24"]


def test_docker_default_bridge_not_excluded_when_overridden(monkeypatch):
    # With a custom bridge subnet, 172.28.x is a legitimate LAN again.
    _stub_docker(monkeypatch)
    _stub_hostname_ips(monkeypatch, ["172.28.0.5"])
    monkeypatch.setenv("OPENNVR_DOCKER_SUBNET", "10.99.0.0/16")
    assert net.detect_local_subnets() == ["172.28.0.0/24"]


def test_host_ip_garbage_and_nonprivate_values_dropped(monkeypatch):
    _stub_docker(monkeypatch)
    _stub_hostname_ips(monkeypatch, [])
    # CGNAT (not RFC1918), IPv6, and garbage all drop → nothing detected.
    monkeypatch.setenv("OPENNVR_HOST_IP", "100.100.1.5, fd00::1, not-an-ip")
    assert net.detect_local_subnets() == []


def test_host_ip_multiple_values_first_listed_first(monkeypatch):
    _stub_docker(monkeypatch)
    _stub_hostname_ips(monkeypatch, [])
    monkeypatch.setenv("OPENNVR_HOST_IP", "192.168.1.50,10.20.0.5")
    assert net.detect_local_subnets() == ["192.168.1.0/24", "10.20.0.0/24"]


def test_lan_ips_multi_nic_all_subnets_detected(monkeypatch):
    # Multi-NIC host: default-route IP in OPENNVR_HOST_IP, full NIC list in
    # OPENNVR_LAN_IPS (overlap dedupes, order preserved: host IP first).
    _stub_docker(monkeypatch)
    _stub_hostname_ips(monkeypatch, ["172.28.0.5"])
    monkeypatch.setenv("OPENNVR_HOST_IP", "192.168.29.140")
    monkeypatch.setenv("OPENNVR_LAN_IPS", "192.168.29.140,192.168.1.105,192.168.0.200")
    assert net.detect_local_subnets() == [
        "192.168.29.0/24",
        "192.168.1.0/24",
        "192.168.0.0/24",
    ]


def test_lan_ips_alone_without_host_ip(monkeypatch):
    _stub_docker(monkeypatch)
    _stub_hostname_ips(monkeypatch, [])
    monkeypatch.setenv("OPENNVR_LAN_IPS", "192.168.1.105 192.168.0.200")
    assert net.detect_local_subnets() == ["192.168.1.0/24", "192.168.0.0/24"]


def test_host_ip_hint_also_works_outside_docker(monkeypatch):
    # Not Docker-gated: an operator-set hint is always honored and ordered first.
    _stub_hostname_ips(monkeypatch, ["10.20.0.5"])
    monkeypatch.setenv("OPENNVR_HOST_IP", "192.168.1.50")
    assert net.detect_local_subnets() == ["192.168.1.0/24", "10.20.0.0/24"]
