# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The announcer's pure parts (HA-117). The multicast itself is Linux-only
and unverified on this project's Windows dev box; see the tracker."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from announcer import build_txt, parse_addresses  # noqa: E402


def test_txt_carries_only_public_facts():
    txt = build_txt(version="0.1.5", port=443)
    assert txt == {"txtvers": "1", "path": "/api/v1", "https": "1", "port": "443",
                   "version": "0.1.5"}
    assert "version" not in build_txt(version=None, port=8443)
    assert all(len(f"{k}={v}".encode()) < 255 for k, v in txt.items())


def test_addresses_are_valid_unique_and_not_loopback():
    assert parse_addresses("192.168.1.10", "10.0.0.5, 192.168.1.10,127.0.0.1,junk,fe80::1") == \
        ["192.168.1.10", "10.0.0.5", "fe80::1"]
    assert parse_addresses(None, "") == []
    assert parse_addresses("0.0.0.0", None) == []
