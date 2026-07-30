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
XiongmaiDriver — the Xiongmai/"Sofia" OEM family (Sparsh and many rebadges).

Xiongmai boards expose a full ONVIF surface (verified with ONVIF Device Manager
against a Sparsh SC-INA50B: Imaging, Analytics, Rules, Events, PTZ, Users,
Network, Time, Maintenance on the ONVIF control port), so this driver INHERITS
the deep-ONVIF baseline (``OnvifDriver``) for all of that and adds only what the
platform needs on top:

  * correct identification (badge + ``native_api``) so the fleet reads true;
  * ``users`` turned on (XM answers ONVIF GetUsers — ODM shows User management);
  * native fallbacks over the Sofia/DVRIP port (34567) for get_info + reboot,
    for the rare unit where the ONVIF equivalents are flaky or disabled.

Selection is by fingerprint (the manufacturer string is usually the generic
"ONVIF"/"General"), and both the probe and these fallbacks are credential-gentle
(cached session, no retry loop) because XM locks the admin account on repeated
failed logins.

Safety invariants inherited unchanged: no set_network/set_ip/factory_reset.
"""

from __future__ import annotations

from ..base import Capabilities, DeviceInfo
from ..onvif.driver import OnvifDriver
from . import cgi_session, sofia


class XiongmaiDriver(OnvifDriver):
    driver_name = "xiongmai"

    async def get_capabilities(self) -> Capabilities:
        caps = await super().get_capabilities()
        caps.driver_name = self.driver_name
        # XM answers ONVIF GetUsers (User management is shown by ODM), so turn it
        # on even though the pure baseline leaves it off for unknown ONVIF cams.
        caps.supported_areas["users"] = True
        caps.detail = {
            **(caps.detail or {}),
            "native_api": "sofia",
            "hardware_verified": True,  # login + control proven live on SC-INA50B
        }
        return caps

    async def get_info(self) -> DeviceInfo:
        # ONVIF GetDeviceInformation is preferred (it's what the fleet expects);
        # fall back to the native Sofia SystemInfo only if ONVIF is unavailable.
        try:
            info = await super().get_info()
            if info.manufacturer or info.model or info.serial_number:
                return info
        except Exception:
            info = None
        try:
            sess = await sofia.get_session(self.ip, self.username, self.password)
            sysinfo = (await sess.system_info()).get("SystemInfo", {})
            hw = sysinfo.get("HardWare") or {}
            return DeviceInfo(
                manufacturer="Xiongmai",
                model=(hw.get("DeviceType") if isinstance(hw, dict) else None)
                or sysinfo.get("DeviceModel"),
                firmware_version=sysinfo.get("SoftWareVersion"),
                serial_number=sysinfo.get("SerialNo"),
                hardware_id=sysinfo.get("HardWareVersion"),
            )
        except Exception:
            # Return whatever ONVIF gave (possibly empty) rather than raising.
            return info or DeviceInfo(manufacturer="Xiongmai")

    async def reboot(self) -> dict:
        try:
            return await super().reboot()  # ONVIF SystemReboot
        except Exception:
            pass
        # Native Sofia OPMachine reboot as a fallback.
        sess = await sofia.get_session(self.ip, self.username, self.password)
        await sess.reboot()
        await sofia.drop_session(self.ip, self.username)
        return {"status": "rebooting"}


__all__ = ["XiongmaiDriver", "cgi_session", "sofia"]
