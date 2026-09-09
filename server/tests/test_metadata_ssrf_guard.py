# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Cloud instance metadata is never "internal".

Reported by Kamal Sentassi (S9S Security Research), coordinated
disclosure, 2026: ``_ip_is_internal`` returned True for
169.254.0.0/16 via ``is_link_local``, and "internal" is precisely what
the SSRF guards permit — so 169.254.169.254 was reachable on any path
gated by that check. Reading instance metadata yields cloud
credentials.
"""
import os
import sys

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from core.config import _host_is_internal, _ip_is_internal  # noqa: E402
import ipaddress  # noqa: E402


@pytest.mark.parametrize("addr", [
    "169.254.169.254",      # AWS / GCP / Azure / DigitalOcean IMDS
    "169.254.170.2",        # ECS task metadata
    "fd00:ec2::254",        # AWS IPv6 IMDS
])
def test_metadata_endpoints_are_not_internal(addr):
    assert _ip_is_internal(ipaddress.ip_address(addr)) is False
    assert _host_is_internal(addr) is False


@pytest.mark.parametrize("addr", [
    "127.0.0.1", "10.0.0.5", "172.16.4.4", "192.168.1.50", "::1", "fd12::1",
])
def test_the_actual_trust_zone_still_resolves_internal(addr):
    """The point is to deny metadata, not to break LAN cameras."""
    assert _ip_is_internal(ipaddress.ip_address(addr)) is True


def test_other_link_local_addresses_are_still_internal():
    """169.254/16 at large is legitimate (APIPA on a camera VLAN); only
    the metadata addresses themselves are denied."""
    assert _ip_is_internal(ipaddress.ip_address("169.254.1.10")) is True
    assert _ip_is_internal(ipaddress.ip_address("fe80::1")) is True


@pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "0.0.0.0"])
def test_public_and_wildcard_still_refused(addr):
    assert _ip_is_internal(ipaddress.ip_address(addr)) is False
