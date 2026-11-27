# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Camera-create must not probe arbitrary hosts.

Reported by Kamal Sentassi (S9S Security Research), coordinated
disclosure, 2026: POST /cameras used the same ONVIF/RTSP probe services
as the onvif router but WITHOUT that router's ``_host_is_internal``
guard, so it would dial any caller-supplied address from the server.

Two probes reach the network from that handler and BOTH are covered:
``resolve_source`` (no rtsp_url given) and ``fetch_identity`` (rtsp_url
given) — guarding only the first would leave the second reachable by
supplying any URL at all.
"""
import os
import sys

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from core.config import _host_is_internal  # noqa: E402


@pytest.mark.parametrize("host", [
    "8.8.8.8",
    "attacker.example.com",
    "169.254.169.254",      # metadata, denied by the companion fix
    "0.0.0.0",
])
def test_hosts_camera_create_must_refuse(host):
    assert _host_is_internal(host) is False


@pytest.mark.parametrize("host", [
    "192.168.1.64", "10.10.0.7", "172.20.1.9", "127.0.0.1", "localhost",
])
def test_hosts_camera_create_must_still_accept(host):
    """Real cameras on the LAN keep working — the guard is a boundary,
    not a lockout."""
    assert _host_is_internal(host) is True


def test_the_rtsp_url_host_is_guarded_too():
    """ip_address and rtsp_url are SEPARATE caller-controlled hosts, and
    TransportProbeService opens a TCP + TLS connection to the rtsp_url
    host. A guard that only reads ip_address leaves that reachable."""
    from routers.cameras import _reject_external_camera_hosts

    class _C:
        ip_address = "192.168.1.10"
        rtsp_url = "rtsp://8.8.8.8:554/stream"

    with pytest.raises(Exception) as exc:
        _reject_external_camera_hosts(_C())
    assert "8.8.8.8" in str(getattr(exc.value, "detail", exc.value))


def test_both_hosts_internal_is_allowed():
    from routers.cameras import _reject_external_camera_hosts

    class _C:
        ip_address = "192.168.1.10"
        rtsp_url = "rtsp://192.168.1.10:554/stream"

    _reject_external_camera_hosts(_C())   # must not raise


def test_external_ip_address_is_refused_even_with_an_internal_url():
    from routers.cameras import _reject_external_camera_hosts

    class _C:
        ip_address = "8.8.8.8"
        rtsp_url = "rtsp://192.168.1.10/s"

    with pytest.raises(Exception):
        _reject_external_camera_hosts(_C())


def test_a_credential_less_create_is_still_guarded():
    """THE path the first cut of this fix missed. The guard used to sit
    inside `if username and password:`, but TransportProbeService runs
    from CameraService.create_camera regardless — so a camera posted with
    no credentials and a chosen rtsp_url was still dialled."""
    from routers.cameras import _reject_external_camera_hosts

    class _C:
        ip_address = None
        rtsp_url = "rtsp://attacker.example:9999/x"

    with pytest.raises(Exception):
        _reject_external_camera_hosts(_C())


def test_the_guard_runs_before_every_branch():
    """Structural: the guard must be at the handler's top level, not
    nested in the credentials or derive branch. Indentation is the whole
    difference between covering one probe and covering four."""
    src = os.path.join(os.path.dirname(__file__), "..", "routers", "cameras.py")
    with open(src, encoding="utf-8") as fh:
        lines = fh.readlines()

    # The helper's own `def` line contains the same text — match the CALL.
    call = [i for i, l in enumerate(lines)
            if "_reject_external_camera_hosts(camera_create)" in l
            and not l.lstrip().startswith("def ")]
    assert len(call) == 1, "expected exactly one guard call in create_camera"
    ci = call[0]
    indent = len(lines[ci]) - len(lines[ci].lstrip())
    assert indent == 4, (
        f"guard is nested (indent {indent}) — it must run at the handler's "
        "top level so a credential-less create cannot skip it"
    )

    for marker in ("if camera_create.username and camera_create.password:",
                   "if not camera_create.rtsp_url:"):
        hit = [i for i, l in enumerate(lines) if marker in l]
        assert hit, f"branch not found, test needs updating: {marker}"
        assert ci < hit[0], f"guard must run before: {marker}"


# ---------------------------------------------------------------------------
# Opt-in tunnel networks (CAMERA_EXTRA_INTERNAL_CIDRS): a cloud-hosted
# OpenNVR pulling cameras over Tailscale (100.64.0.0/10) or a similar tunnel.


@pytest.fixture
def extra_cidrs(monkeypatch):
    def _set(value: str):
        monkeypatch.setenv("CAMERA_EXTRA_INTERNAL_CIDRS", value)
    monkeypatch.delenv("CAMERA_EXTRA_INTERNAL_CIDRS", raising=False)
    return _set


def test_a_tailscale_address_is_refused_by_default(extra_cidrs):
    assert _host_is_internal("100.101.102.103") is False


def test_a_listed_tunnel_network_is_accepted(extra_cidrs):
    extra_cidrs("100.64.0.0/10")
    assert _host_is_internal("100.101.102.103") is True
    # Only what is listed: the rest of the public internet stays out.
    assert _host_is_internal("100.128.0.1") is False
    assert _host_is_internal("8.8.8.8") is False


@pytest.mark.parametrize("host", ["100.100.100.100", "100.100.100.200", "169.254.169.254"])
def test_metadata_and_tailscale_service_stay_refused_under_a_covering_cidr(extra_cidrs, host):
    extra_cidrs("100.64.0.0/10, 169.254.0.0/16")
    assert _host_is_internal(host) is False


@pytest.mark.parametrize("value", ["0.0.0.0/0", "64.0.0.0/2", "::/0", "not-a-network"])
def test_a_network_that_would_switch_the_guard_off_is_ignored(extra_cidrs, value):
    extra_cidrs(value)
    assert _host_is_internal("8.8.8.8") is False
    assert _host_is_internal("2001:4860:4860::8888") is False


def test_several_networks_comma_or_space_separated(extra_cidrs):
    # Public ranges on purpose: Python already counts the documentation
    # ranges (192.0.2.0/24, ...) as private.
    extra_cidrs("100.64.0.0/10,45.10.20.0/24 46.20.30.0/24")
    assert _host_is_internal("45.10.20.7") is True
    assert _host_is_internal("46.20.30.7") is True
    assert _host_is_internal("47.30.40.7") is False


def test_camera_create_accepts_a_listed_tunnel_camera(extra_cidrs):
    from routers.cameras import _reject_external_camera_hosts

    class _C:
        ip_address = "100.101.102.103"
        rtsp_url = "rtsp://100.101.102.103:554/stream"

    with pytest.raises(Exception):
        _reject_external_camera_hosts(_C())
    extra_cidrs("100.64.0.0/10")
    _reject_external_camera_hosts(_C())  # no exception
