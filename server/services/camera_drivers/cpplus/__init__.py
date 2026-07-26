# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""CP Plus vendor package.

CP Plus is overwhelmingly a Dahua OEM — same HTTP CGI API — so the driver is a
thin subclass of DahuaCgiDriver and the probe reuses Dahua's fingerprint. It
lives in its own package (rather than a string alias in the Dahua package) so a
CP Plus quirk can be overridden here without touching the Dahua driver, and so a
CP Plus unit that is actually Hikvision-internally rebadged still routes
correctly: it matches cpplus by string, fails the Dahua probe, and the registry
fingerprint pass lands it on the Hikvision driver.
"""

from __future__ import annotations

from ..dahua import probe  # CP Plus speaks the Dahua CGI API — reuse the probe
from .driver import CpPlusDriver

DRIVER = CpPlusDriver
# Above dahua (20) so the specific brand wins the manufacturer string pass;
# below hikvision (10) so a genuine Hikvision string still wins.
PRIORITY = 15


def matches(manufacturer: str) -> bool:
    return "cp plus" in manufacturer or "cp-plus" in manufacturer or "cpplus" in manufacturer


__all__ = ["DRIVER", "PRIORITY", "CpPlusDriver", "matches", "probe"]
