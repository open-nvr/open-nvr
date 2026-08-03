# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Hikvision vendor package — native control via ISAPI over HTTP Digest.

Covers the areas ONVIF can't reach well on Hikvision: OSD, motion, alert-stream
events, camera-user CRUD, SD-card storage, and richer network info. Everything
else falls back to the ONVIF baseline (the driver subclasses OnvifDriver).
"""

from __future__ import annotations

from .._probe import fingerprint_get
from .driver import HikvisionIsapiDriver

DRIVER = HikvisionIsapiDriver
# Below the OEM-family drivers so a device that both string-matches a rebrand and
# actually speaks ISAPI is confirmed here; well above the ONVIF fallback (1000).
PRIORITY = 10

# ISAPI signature path — present on every Hikvision-family device, 404 on Dahua.
_SIG_PATH = "/ISAPI/System/deviceInfo"
# Hikvision stamps this namespace on its ISAPI XML; ISAPI-alike OEMs do not.
_HIK_NS = "hikvision.com"


def matches(manufacturer: str) -> bool:
    return "hikvision" in manufacturer


async def probe(
    ip: str, port: int, username: str | None, password: str | None
) -> bool:
    """True iff the device serves *Hikvision's* ISAPI, not merely an ISAPI-alike.

    Several OEMs (Secureye SP-C2QN, verified) implement a partial ISAPI subset
    and answer this path with 200 + ``<DeviceInfo``, but with no Hikvision
    namespace and a different schema — most of the driver's endpoints then 400.
    So a 200 must additionally carry the Hikvision XML namespace to count.

    A 401 Digest challenge is still accepted: the endpoint exists but creds were
    rejected, and we cannot read the body to check further. That is deliberately
    weaker, and the manufacturer pass is what disambiguates in practice.
    """
    status, text = await fingerprint_get(ip, port, _SIG_PATH, username, password)
    if status == 200:
        return "<DeviceInfo" in text and _HIK_NS in text
    return status == 401


__all__ = ["DRIVER", "PRIORITY", "HikvisionIsapiDriver", "matches", "probe"]
