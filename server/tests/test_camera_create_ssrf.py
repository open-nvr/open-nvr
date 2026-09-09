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
