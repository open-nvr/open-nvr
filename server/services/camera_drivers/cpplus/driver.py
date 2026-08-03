# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""CpPlusDriver — CP Plus cameras via the Dahua CGI API.

CP Plus hardware is Dahua OEM, so everything is inherited from DahuaCgiDriver.
Kept as its own class/package purely so CP-Plus-specific divergences (a renamed
CGI path, a different user-group vocabulary) can be overridden here later without
disturbing the Dahua driver. NOT yet verified against real CP Plus hardware.
"""

from __future__ import annotations

from ..dahua.driver import DahuaCgiDriver


class CpPlusDriver(DahuaCgiDriver):
    driver_name = "cpplus"

    async def get_capabilities(self):
        caps = await super().get_capabilities()
        caps.driver_name = self.driver_name
        return caps
