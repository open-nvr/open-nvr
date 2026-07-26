# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""ONVIF vendor package — the universal baseline driver.

This is the terminal fallback: it never wins by manufacturer string or probe,
so the registry only lands here when no native vendor package matched. Any
ONVIF-conformant camera (Dahua, CP Plus, Uniview, unknown brands) is driven
from here until a native package is added.
"""

from __future__ import annotations

from .driver import OnvifDriver

# The registry uses DRIVER / matches / PRIORITY on every vendor package. ONVIF
# is special-cased as the fallback, so matches() is always False and there is
# no probe(); PRIORITY is set very high to reflect "last resort".
DRIVER = OnvifDriver
PRIORITY = 1000


def matches(manufacturer: str) -> bool:
    """Never matched by string — ONVIF is the explicit fallback, not a vendor."""
    return False


__all__ = ["DRIVER", "PRIORITY", "OnvifDriver", "matches"]
