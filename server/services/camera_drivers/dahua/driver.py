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
DahuaCgiDriver — ONVIF baseline plus Dahua's HTTP CGI API for the richer bits.

Subclasses OnvifDriver so imaging/encoder/time (which Dahua's ONVIF handles
well) fall back to the standard ONVIF path; overrides OSD, motion, events,
users, storage, and network via ``/cgi-bin/*.cgi``.

Response formats are Dahua's ``key=value`` text (see ``cgi.parse_kv``). Write
paths patch only the keys supplied and confirm on the ``OK`` body Dahua returns.

NOT yet verified against real Dahua hardware — mock-tested only; write paths are
deliberately conservative.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException

from ..base import (
    Capabilities,
    DeviceInfo,
    MotionInfo,
    NetworkInfo,
    OsdInfo,
    StorageInfo,
    StorageSlot,
    UsersInfo,
)
from ..onvif.driver import OnvifDriver
from .cgi import dahua_request, parse_kv

_CONFIG_PATH = "/cgi-bin/configManager.cgi"
_MAGICBOX_PATH = "/cgi-bin/magicBox.cgi"
_STORAGE_PATH = "/cgi-bin/storageDevice.cgi"
_USER_PATH = "/cgi-bin/userManager.cgi"
_EVENT_PATH = (
    "/cgi-bin/eventManager.cgi?action=attach"
    "&codes=[VideoMotion,VideoBlind,VideoLoss,AlarmLocal]&heartbeat=5"
)
# Dahua user groups map onto OpenNVR's level vocabulary.
_GROUP_BY_LEVEL = {"Administrator": "admin", "Operator": "user", "Viewer": "user"}
_CH = 0  # single-channel IP camera


def parse_dahua_event(chunk: str) -> dict | None:
    """Parse one Dahua alarm line (``Code=..;action=..;index=..``) into the same
    normalized shape as the Hikvision alert parser, or None for a heartbeat."""
    m = re.search(r"Code=([^;]+);\s*action=([^;]+)", chunk)
    if not m:
        return None
    code = m.group(1).strip()
    action = m.group(2).strip()
    if code.lower() == "heartbeat":  # Dahua keepalive — drop
        return None
    return {
        "event_type": code,
        "event_state": "active" if action.lower() == "start" else "inactive",
        "description": f"{code} {action}",
        "occurred_at": datetime.now(UTC).isoformat(),
    }


def _dahua_ok(status: int, text: str, op: str) -> None:
    """Dahua setConfig/addUser/... answer 200 with an ``OK`` body on success."""
    if status != 200:
        raise HTTPException(status_code=502, detail=f"{op} failed: HTTP {status}")
    if "OK" not in text and "ok" not in text:
        snippet = text.strip().splitlines()[0] if text.strip() else "error"
        raise HTTPException(status_code=502, detail=f"{op} failed: {snippet}")


def _as_bool(v: str | None) -> bool | None:
    if v is None:
        return None
    return v.strip().lower() == "true"


def _bytes_to_mb(v: str | None) -> int | None:
    try:
        return int(float(v)) // (1024 * 1024) if v else None
    except (ValueError, TypeError):
        return None


class DahuaCgiDriver(OnvifDriver):
    driver_name = "dahua"

    # --- identity: magicBox, ONVIF fallback ---

    async def get_info(self) -> DeviceInfo:
        try:
            st, text = await dahua_request(
                self.ip, f"{_MAGICBOX_PATH}?action=getSystemInfo",
                self.username, self.password, port=self.http_port,
            )
            st2, ver = await dahua_request(
                self.ip, f"{_MAGICBOX_PATH}?action=getSoftwareVersion",
                self.username, self.password, port=self.http_port,
            )
        except Exception:
            return await super().get_info()
        if st != 200:
            return await super().get_info()
        kv = parse_kv(text)
        vkv = parse_kv(ver) if st2 == 200 else {}
        return DeviceInfo(
            manufacturer="Dahua",
            model=kv.get("deviceType") or kv.get("type"),
            firmware_version=vkv.get("version"),
            serial_number=kv.get("serialNumber"),
            hardware_id=kv.get("hardwareVersion") or kv.get("processor"),
        )

    # --- capabilities: ONVIF baseline + CGI extras ---

    async def get_capabilities(self) -> Capabilities:
        caps = await super().get_capabilities()
        caps.driver_name = self.driver_name
        for area in ("storage", "motion", "osd", "users", "events", "network"):
            caps.supported_areas[area] = True
        # Signal that these write paths have not been exercised on real hardware.
        caps.detail["hardware_verified"] = False
        return caps

    # --- network (read only, via CGI — richer than ONVIF) ---

    async def get_network(self) -> NetworkInfo:
        try:
            st, text = await dahua_request(
                self.ip, f"{_CONFIG_PATH}?action=getConfig&name=Network",
                self.username, self.password, port=self.http_port,
            )
        except Exception:
            return await super().get_network()
        if st != 200:
            return await super().get_network()
        kv = parse_kv(text)
        iface = kv.get("table.Network.DefaultInterface", "eth0")
        p = f"table.Network.{iface}."

        def g(suffix: str) -> str | None:
            return kv.get(p + suffix)

        return NetworkInfo(
            supported=True,
            source="dahua-cgi",
            mac_address=g("PhysicalAddress"),
            ip_address=g("IPAddress"),
            subnet_mask=g("SubnetMask"),
            gateway=g("DefaultGateway"),
            dns_primary=g("DnsServers[0]"),
            dns_secondary=g("DnsServers[1]"),
            dhcp=_as_bool(g("DhcpEnable")),
            mtu=int(g("MTU")) if (g("MTU") or "").isdigit() else None,
        )

    # --- storage (read only, via CGI) ---

    async def get_storage(self) -> StorageInfo:
        try:
            st, text = await dahua_request(
                self.ip, f"{_STORAGE_PATH}?action=getDeviceAllInfo",
                self.username, self.password, port=self.http_port,
            )
        except Exception:
            return StorageInfo(supported=False, source="dahua-cgi")
        if st != 200:
            return StorageInfo(supported=False, source="dahua-cgi")
        kv = parse_kv(text)
        # Rows look like list.info[0].Name / .State and
        # list.info[0].Detail[0].TotalBytes / .UsedBytes.
        idxs = sorted(
            {
                int(m.group(1))
                for k in kv
                if (m := re.match(r"list\.info\[(\d+)\]\.", k))
            }
        )
        slots: list[StorageSlot] = []
        for i in idxs:
            base = f"list.info[{i}]."
            total = kv.get(f"{base}Detail[0].TotalBytes")
            used = kv.get(f"{base}Detail[0].UsedBytes")
            cap_mb = _bytes_to_mb(total)
            used_mb = _bytes_to_mb(used)
            slots.append(
                StorageSlot(
                    name=kv.get(f"{base}Name") or str(i),
                    status=kv.get(f"{base}State"),
                    capacity_mb=cap_mb,
                    free_mb=(cap_mb - used_mb)
                    if (cap_mb is not None and used_mb is not None)
                    else None,
                )
            )
        return StorageInfo(
            supported=True, present=bool(slots), slots=slots, source="dahua-cgi"
        )

    # --- OSD overlays (read + write via VideoWidget) ---

    async def get_osd(self) -> OsdInfo:
        try:
            st, text = await dahua_request(
                self.ip, f"{_CONFIG_PATH}?action=getConfig&name=VideoWidget",
                self.username, self.password, port=self.http_port,
            )
        except Exception:
            return OsdInfo(supported=False, source="dahua-cgi")
        if st != 200:
            return OsdInfo(supported=False, source="dahua-cgi")
        kv = parse_kv(text)
        p = f"table.VideoWidget[{_CH}]."
        return OsdInfo(
            supported=True,
            source="dahua-cgi",
            datetime_enabled=_as_bool(kv.get(p + "TimeTitle.EncodeBlend")),
            channel_name_enabled=_as_bool(kv.get(p + "ChannelTitle.EncodeBlend")),
            text_enabled=_as_bool(kv.get(p + "CustomTitle[0].EncodeBlend")),
            text=kv.get(p + "CustomTitle[0].Text"),
        )

    async def set_osd(self, patch: dict) -> OsdInfo:
        p = f"VideoWidget[{_CH}]."
        params: dict[str, str] = {"action": "setConfig"}
        if patch.get("datetime_enabled") is not None:
            params[p + "TimeTitle.EncodeBlend"] = _bstr(patch["datetime_enabled"])
        if patch.get("channel_name_enabled") is not None:
            params[p + "ChannelTitle.EncodeBlend"] = _bstr(patch["channel_name_enabled"])
        if patch.get("text_enabled") is not None:
            params[p + "CustomTitle[0].EncodeBlend"] = _bstr(patch["text_enabled"])
        if patch.get("text") is not None:
            params[p + "CustomTitle[0].Text"] = str(patch["text"])
        if len(params) == 1:  # nothing to change
            return await self.get_osd()
        st, resp = await dahua_request(
            self.ip, _CONFIG_PATH, self.username, self.password,
            port=self.http_port, params=params,
        )
        _dahua_ok(st, resp, "SetOSD")
        return await self.get_osd()

    # --- motion detection (read + write via MotionDetect) ---

    async def get_motion(self) -> MotionInfo:
        try:
            st, text = await dahua_request(
                self.ip, f"{_CONFIG_PATH}?action=getConfig&name=MotionDetect",
                self.username, self.password, port=self.http_port,
            )
        except Exception:
            return MotionInfo(supported=False, source="dahua-cgi")
        if st != 200:
            return MotionInfo(supported=False, source="dahua-cgi")
        kv = parse_kv(text)
        p = f"table.MotionDetect[{_CH}]."
        level = kv.get(p + "Level")
        return MotionInfo(
            supported=True,
            source="dahua-cgi",
            enabled=_as_bool(kv.get(p + "Enable")),
            sensitivity=int(level) if (level or "").isdigit() else None,
            sensitivity_max=6,  # Dahua Level is 1..6
        )

    async def set_motion(self, patch: dict) -> MotionInfo:
        p = f"MotionDetect[{_CH}]."
        params: dict[str, str] = {"action": "setConfig"}
        if patch.get("enabled") is not None:
            params[p + "Enable"] = _bstr(patch["enabled"])
        if patch.get("sensitivity") is not None:
            level = max(1, min(6, int(patch["sensitivity"])))
            params[p + "Level"] = str(level)
        if len(params) == 1:
            return await self.get_motion()
        st, resp = await dahua_request(
            self.ip, _CONFIG_PATH, self.username, self.password,
            port=self.http_port, params=params,
        )
        _dahua_ok(st, resp, "SetMotion")
        return await self.get_motion()

    # --- event stream (via eventManager attach) ---

    async def subscribe_events(self):
        """Yield normalized camera events from the Dahua alarm stream
        (multipart of ``Code=..;action=..`` lines). Long-lived; the caller
        (camera_event_manager) owns reconnect/backoff and teardown."""
        url = f"http://{self.ip}:{self.http_port}{_EVENT_PATH}"
        auth = httpx.DigestAuth(self.username, self.password)
        async with httpx.AsyncClient(timeout=None) as client, client.stream(
            "GET", url, auth=auth
        ) as resp:
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502, detail=f"eventManager HTTP {resp.status_code}"
                )
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if "Code=" in line:
                        ev = parse_dahua_event(line)
                        if ev is not None:
                            yield ev
                if len(buf) > 65536:
                    buf = buf[-8192:]

    # --- on-camera accounts + reboot (gated) ---

    async def get_users(self) -> UsersInfo:
        try:
            st, text = await dahua_request(
                self.ip, f"{_USER_PATH}?action=getUserInfoAll",
                self.username, self.password, port=self.http_port,
            )
        except Exception:
            return UsersInfo(supported=False, source="dahua-cgi")
        if st != 200:
            return UsersInfo(supported=False, source="dahua-cgi")
        kv = parse_kv(text)
        idxs = sorted(
            {int(m.group(1)) for k in kv if (m := re.match(r"users\[(\d+)\]\.", k))}
        )
        users = []
        for i in idxs:
            name = kv.get(f"users[{i}].Name")
            if not name:
                continue
            users.append({
                "id": str(i),
                "name": name,
                "level": kv.get(f"users[{i}].Group"),
                "is_current": name == self.username,
            })
        return UsersInfo(supported=True, users=users, source="dahua-cgi")

    async def create_user(self, username: str, password: str, level: str) -> UsersInfo:
        if level not in _GROUP_BY_LEVEL:
            raise HTTPException(
                status_code=400,
                detail=f"level must be one of {tuple(_GROUP_BY_LEVEL)}",
            )
        if not username or not password:
            raise HTTPException(status_code=400, detail="username and password required")
        current = await self.get_users()
        if any(u["name"] == username for u in current.users):
            raise HTTPException(status_code=409, detail=f"User '{username}' exists")
        params = {
            "action": "addUser",
            "user.Name": username,
            "user.Password": password,
            "user.Group": _GROUP_BY_LEVEL[level],
            "user.Memo": "Created by OpenNVR",
        }
        st, resp = await dahua_request(
            self.ip, _USER_PATH, self.username, self.password,
            port=self.http_port, params=params,
        )
        _dahua_ok(st, resp, "CreateUser")
        return await self.get_users()

    async def delete_user(self, username: str) -> UsersInfo:
        # Hard guardrail: never remove the account OpenNVR authenticates with, or
        # the built-in admin — that's the way to lock OpenNVR (and you) out.
        if username == self.username:
            raise HTTPException(
                status_code=400,
                detail="Refusing to delete the account OpenNVR uses to reach this camera",
            )
        if username == "admin":
            raise HTTPException(
                status_code=400, detail="Refusing to delete the built-in admin account"
            )
        current = await self.get_users()
        if not any(u["name"] == username for u in current.users):
            raise HTTPException(status_code=404, detail=f"User '{username}' not found")
        st, resp = await dahua_request(
            self.ip, _USER_PATH, self.username, self.password, port=self.http_port,
            params={"action": "deleteUser", "name": username},
        )
        _dahua_ok(st, resp, "DeleteUser")
        return await self.get_users()

    async def reboot(self) -> dict:
        st, resp = await dahua_request(
            self.ip, f"{_MAGICBOX_PATH}?action=reboot",
            self.username, self.password, port=self.http_port,
        )
        _dahua_ok(st, resp, "Reboot")
        return {"status": "rebooting"}


def _bstr(v: bool) -> str:
    return "true" if v else "false"
