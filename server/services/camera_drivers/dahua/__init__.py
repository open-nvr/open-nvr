# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Dahua vendor package — native control via the HTTP CGI API over Digest.

Covers OSD, motion, alarm-stream events, camera-user CRUD, SD-card storage, and
richer network info via ``configManager.cgi`` / ``magicBox.cgi`` / etc.
Everything else falls back to the ONVIF baseline (the driver subclasses
OnvifDriver). Also the native driver for CP Plus and many OEM rebadges — see
``docs/adding-a-camera-vendor.md``.

NOTE: the write paths here are implemented conservatively (patch-only, degrade to
``supported=False`` on parse failure) but have NOT yet been exercised against
real Dahua hardware — only mock-tested. ``capabilities.detail.hardware_verified``
is False for this driver so the UI can flag it.
"""

from __future__ import annotations

from .._probe import fingerprint_get
from .driver import DahuaCgiDriver

DRIVER = DahuaCgiDriver
# Below hikvision (10) and cpplus (15) so a more specific brand wins the string
# pass; well above the ONVIF fallback (1000).
PRIORITY = 20

# CGI signature path — present on Dahua-family devices, 404 on Hikvision.
_SIG_PATH = "/cgi-bin/magicBox.cgi?action=getDeviceType"


def matches(manufacturer: str) -> bool:
    return "dahua" in manufacturer


async def probe(
    ip: str, port: int, username: str | None, password: str | None
) -> bool:
    """True iff the device serves the Dahua CGI namespace.

    Accepts a 200 whose body looks like Dahua's ``key=value`` reply (``type=``),
    or a 401 Digest challenge (endpoint exists, creds rejected — still
    Dahua-family). A Hikvision/ONVIF-only device 404s this path.
    """
    status, text = await fingerprint_get(ip, port, _SIG_PATH, username, password)
    if status == 200 and ("type=" in text or "=" in text.split("\n", 1)[0]):
        return True
    return status == 401


__all__ = ["DRIVER", "PRIORITY", "DahuaCgiDriver", "matches", "probe"]
