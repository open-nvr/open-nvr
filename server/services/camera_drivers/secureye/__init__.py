# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Secureye vendor package.

Secureye rebadges several OEM platforms, so the manufacturer string alone is not
trustworthy — detection leans on the probe. The unit this driver was built and
verified against (SP-C2QN, ``CGI_V3.0.0`` platform) serves a *partial* ISAPI
subset with no Hikvision namespace, which is exactly why the Hikvision probe was
tightened to require that namespace: without it, this camera was being claimed by
the Hikvision driver and most of its endpoints answered HTTP 400.

A Secureye unit built on different internals (e.g. a genuine Hikvision or Dahua
OEM) will fail this probe, fall through the fingerprint pass, and land on the
right native driver — or on the ONVIF baseline. That is the intended behavior.
"""

from __future__ import annotations

from .._probe import fingerprint_get
from .driver import SecureyeDriver

DRIVER = SecureyeDriver
# After hikvision (10) / cpplus (15) / dahua (20): if a Secureye-branded unit is
# genuinely one of those inside, the specific native probe should win first.
PRIORITY = 30

_SIG_PATH = "/ISAPI/System/deviceInfo"
# Hikvision stamps this on its ISAPI XML; the Secureye ISAPI-alike does not.
_HIK_NS = "hikvision.com"


def matches(manufacturer: str) -> bool:
    return "secureye" in manufacturer


async def probe(
    ip: str, port: int, username: str | None, password: str | None
) -> bool:
    """True iff the device serves an ISAPI-style API that is *not* Hikvision's.

    Requires a readable 200 body: a ``<DeviceInfo`` payload without the Hikvision
    namespace. A 401 is deliberately NOT accepted here — unlike the Hikvision
    probe we cannot inspect the body to tell the two apart, and guessing would
    reintroduce the false-positive this package exists to prevent.
    """
    status, text = await fingerprint_get(ip, port, _SIG_PATH, username, password)
    if status != 200:
        return False
    return "<DeviceInfo" in text and _HIK_NS not in text


__all__ = ["DRIVER", "PRIORITY", "SecureyeDriver", "matches", "probe"]
