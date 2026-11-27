# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
The host's addresses, as the launchers last saw them.

Core runs on the Docker bridge and cannot see the host's interfaces. The
launchers (start.sh / start.ps1, and lan-probe on Linux) write what they
see into ``./data/net-hints/``, which compose mounts read-only at
``/app/net-hints``. The files are re-read on every use, so a host that
moved to another network is followed without recreating the container.

Two files, for two different jobs:

``host-ips``
    The host's own LAN addresses (no VPN tunnels). ONVIF discovery turns
    each private one into a /24 to scan, so nothing but real camera-side
    LANs belongs here.

``advertise-ips``
    The addresses a viewer's browser may reach this server on, VPN tunnels
    included (WireGuard, Tailscale, FortiClient). MediaMTX advertises them
    as WebRTC ICE host candidates. On a dual-NIC host with nginx bound to
    one address, only that address: the media ports are published there
    alone, and the camera NIC must not be shown to browsers.
"""

from __future__ import annotations

import os

HINTS_DIR_DEFAULT = "/app/net-hints"


def _hints_dir() -> str:
    return os.environ.get("OPENNVR_NET_HINTS_DIR") or HINTS_DIR_DEFAULT


def _read_tokens(path: str) -> list[str] | None:
    """Whitespace- or comma-separated tokens, in file order, de-duplicated.
    None when the file does not exist (or cannot be read), [] when empty."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            raw = fh.read()
    except OSError:
        return None
    out: list[str] = []
    for token in raw.replace(",", " ").split():
        if token not in out:
            out.append(token)
    return out


def host_ips_path() -> str:
    return os.environ.get("OPENNVR_HOST_IPS_FILE") or os.path.join(_hints_dir(), "host-ips")


def advertise_ips_path() -> str:
    return os.environ.get("OPENNVR_ADVERTISE_IPS_FILE") or os.path.join(_hints_dir(), "advertise-ips")


def read_host_ips() -> list[str] | None:
    """The host's LAN addresses (``host-ips``), or None without the file."""
    return _read_tokens(host_ips_path())


def read_advertise_ips() -> list[str] | None:
    """The addresses to advertise to browsers (``advertise-ips``), or None
    without the file (a launcher from before the file existed)."""
    return _read_tokens(advertise_ips_path())
