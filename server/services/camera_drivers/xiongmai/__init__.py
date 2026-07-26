# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Xiongmai vendor package (the "Sofia"/DVRIP OEM family and its rebadges).

Xiongmai is one of the most rebadged camera platforms in the world (Sparsh and
countless generic brands), and its units almost always report a generic
manufacturer ("ONVIF"/"General"), so string ``matches`` is unreliable — real
selection is the fingerprint ``probe``. Two credential-free fingerprints (no
lockout risk):

  * the Sofia/DVRIP binary port (TCP 34567) answers a session-0 request with a
    DVRIP frame; and/or
  * the AppWeb2.0 web UI serves ``/CGI/Security/sessionLogin`` (returns a salt)
    over HTTPS (or HTTP) without auth.

The driver itself inherits the deep-ONVIF baseline for the rich surface these
boards expose over ONVIF, adding native Sofia fallbacks — see ``driver.py``.
"""

from __future__ import annotations

from . import cgi_session, sofia
from .driver import XiongmaiDriver

DRIVER = XiongmaiDriver
# Below the HTTP-native brands (hikvision 10 / cpplus 15 / dahua 20 / secureye
# 30): if an XM-branded unit is really one of those inside, prefer that.
PRIORITY = 40

_XM_HINTS = ("xiongmai", "xm", "sofia", "topsee", "general")


def matches(manufacturer: str) -> bool:
    m = manufacturer or ""
    return any(h in m for h in _XM_HINTS)


async def probe(
    ip: str, port: int, username: str | None, password: str | None
) -> bool:
    """True iff the device is a Xiongmai board. Ignores the passed HTTP ``port``
    (that's the ONVIF control port) and fingerprints the XM-specific interfaces
    on their own ports — sending NO credentials, so a wrong guess never trips the
    account lockout.
    """
    if await sofia.probe(ip):
        return True
    if await cgi_session.probe(ip, "https"):
        return True
    return await cgi_session.probe(ip, "http")


__all__ = ["DRIVER", "PRIORITY", "XiongmaiDriver", "matches", "probe"]
