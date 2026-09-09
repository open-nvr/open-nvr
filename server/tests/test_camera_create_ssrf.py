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


def test_the_guard_covers_both_probe_paths():
    """A static check with teeth: the guard must be reachable for the
    fetch_identity path too, i.e. NOT nested inside the
    `if not camera_create.rtsp_url:` branch that only covers
    resolve_source. Indentation is the whole difference here."""
    src = (
        os.path.join(os.path.dirname(__file__), "..", "routers", "cameras.py")
    )
    with open(src, encoding="utf-8") as fh:
        lines = fh.readlines()

    guard = [i for i, l in enumerate(lines)
             if "_host_is_internal(camera_create.ip_address)" in l]
    assert len(guard) == 1, "expected exactly one camera-create host guard"
    gi = guard[0]
    guard_indent = len(lines[gi]) - len(lines[gi].lstrip())

    derive = [i for i, l in enumerate(lines)
              if "if not camera_create.rtsp_url:" in l]
    assert derive, "derive branch not found — test needs updating"
    derive_indent = len(lines[derive[0]]) - len(lines[derive[0]].lstrip())

    assert gi < derive[0], "guard must run before the derive branch"
    assert guard_indent <= derive_indent, (
        "guard is nested inside the derive branch — the fetch_identity "
        "probe on the explicit-rtsp_url path would be unguarded"
    )
