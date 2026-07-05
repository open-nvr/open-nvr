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
HikvisionIsapiDriver — ONVIF baseline plus ISAPI for the richer bits.

Subclasses OnvifDriver so anything not overridden falls back to the standard
ONVIF path. Phase 0 overrides network (ISAPI is fuller and more reliable than
ONVIF on Hikvision) and storage (SD status, which the ONVIF baseline can't
read). Response shapes verified against a real DS-2CD204WFWD-I.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException

from .base import (
    Capabilities,
    MotionInfo,
    NetworkInfo,
    OsdInfo,
    StorageInfo,
    StorageSlot,
    UsersInfo,
)
from .isapi_service import isapi_request
from .onvif_driver import OnvifDriver

_OVERLAY_PATH = "/ISAPI/System/Video/inputs/channels/1/overlays"
_MOTION_PATH = "/ISAPI/System/Video/inputs/channels/1/motionDetection"
_ALERT_PATH = "/ISAPI/Event/notification/alertStream"
_USERS_PATH = "/ISAPI/Security/users"
_REBOOT_PATH = "/ISAPI/System/reboot"
_USER_LEVELS = ("Administrator", "Operator", "Viewer")


def parse_event_alert(block: str) -> dict | None:
    """Parse one Hikvision <EventNotificationAlert> block into a normalized
    event dict, or None for a keepalive heartbeat (videoloss/inactive)."""
    et = re.search(r"<eventType>([^<]+)</eventType>", block)
    if not et:
        return None
    event_type = et.group(1).strip()
    state_m = re.search(r"<eventState>([^<]+)</eventState>", block)
    state = state_m.group(1).strip() if state_m else None
    # The camera streams videoloss/inactive as a periodic keepalive — drop it.
    if event_type == "videoloss" and state == "inactive":
        return None
    desc = re.search(r"<eventDescription>([^<]*)</eventDescription>", block)
    dt = re.search(r"<dateTime>([^<]+)</dateTime>", block)
    return {
        "event_type": event_type,
        "event_state": state,
        "description": desc.group(1).strip() if desc else None,
        "occurred_at": dt.group(1).strip() if dt else datetime.now(UTC).isoformat(),
    }


def _tag(name: str, text: str) -> str | None:
    m = re.search(rf"<{name}>([^<]+)</{name}>", text)
    return m.group(1).strip() if m else None


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _isapi_ok(status: int, text: str, op: str) -> None:
    """Hikvision write endpoints answer 200 + <statusCode>1</statusCode> on OK."""
    if status != 200:
        raise HTTPException(status_code=502, detail=f"{op} failed: HTTP {status}")
    m = re.search(r"<statusCode>(\d+)</statusCode>", text)
    if m and m.group(1) != "1":
        ss = re.search(r"<statusString>([^<]+)</statusString>", text)
        raise HTTPException(
            status_code=502, detail=f"{op} failed: {ss.group(1) if ss else 'error'}"
        )


def _nested_enabled(block: str, xml: str) -> bool | None:
    m = re.search(rf"<{block}[^>]*>(.*?)</{block}>", xml, re.DOTALL)
    if not m:
        return None
    e = re.search(r"<enabled>(true|false)</enabled>", m.group(1))
    return (e.group(1) == "true") if e else None


def _set_nested_enabled(xml: str, block: str, value: bool) -> str:
    v = "true" if value else "false"
    return re.sub(
        rf"(<{block}[^>]*>.*?<enabled>)(?:true|false)(</enabled>)",
        rf"\g<1>{v}\g<2>",
        xml,
        count=1,
        flags=re.DOTALL,
    )


def _set_text_overlay(
    xml: str, tid: int, enabled: bool | None, text: str | None
) -> str:
    m = re.search(
        rf"(<TextOverlay>\s*<id>{tid}</id>.*?</TextOverlay>)", xml, re.DOTALL
    )
    if not m:
        return xml
    block = m.group(1)
    nb = block
    if enabled is not None:
        nb = re.sub(
            r"<enabled>(?:true|false)</enabled>",
            f"<enabled>{'true' if enabled else 'false'}</enabled>",
            nb,
            count=1,
        )
    if text is not None:
        rep = f"<displayText>{_esc(text)}</displayText>"
        if re.search(r"<displayText\s*/>", nb):
            nb = re.sub(r"<displayText\s*/>", rep, nb, count=1)
        else:
            nb = re.sub(r"<displayText>[^<]*</displayText>", rep, nb, count=1)
    return xml.replace(block, nb, 1)


def _nested_ip(block_name: str, text: str) -> str | None:
    """First <ipAddress> inside a named block (DefaultGateway/PrimaryDNS/…)."""
    m = re.search(rf"<{block_name}>.*?<ipAddress>([0-9.]+)</ipAddress>", text, re.DOTALL)
    ip = m.group(1) if m else None
    return ip if ip and ip != "0.0.0.0" else None


class HikvisionIsapiDriver(OnvifDriver):
    driver_name = "hikvision"

    # --- capabilities: ONVIF baseline + ISAPI extras ---

    async def get_capabilities(self) -> Capabilities:
        caps = await super().get_capabilities()
        caps.driver_name = self.driver_name
        # ISAPI exposes SD storage, OSD overlays, and granular motion that the
        # ONVIF baseline doesn't implement.
        caps.supported_areas["storage"] = True
        caps.supported_areas["motion"] = True
        caps.supported_areas["osd"] = True
        caps.supported_areas["users"] = True
        return caps

    # --- OSD overlays (read + write via ISAPI) ---

    async def get_osd(self) -> OsdInfo:
        try:
            st, xml = await isapi_request(
                self.ip, _OVERLAY_PATH, self.username, self.password,
                port=self.http_port,
            )
        except Exception:
            return OsdInfo(supported=False, source="isapi")
        if st != 200:
            return OsdInfo(supported=False, source="isapi")
        text_enabled = text = None
        t1 = re.search(r"<TextOverlay>\s*<id>1</id>(.*?)</TextOverlay>", xml, re.DOTALL)
        if t1:
            e = re.search(r"<enabled>(true|false)</enabled>", t1.group(1))
            text_enabled = (e.group(1) == "true") if e else None
            dt = re.search(r"<displayText>([^<]*)</displayText>", t1.group(1))
            text = dt.group(1) if dt else ""
        return OsdInfo(
            supported=True,
            datetime_enabled=_nested_enabled("DateTimeOverlay", xml),
            channel_name_enabled=_nested_enabled("channelNameOverlay", xml),
            text_enabled=text_enabled,
            text=text,
            source="isapi",
        )

    async def set_osd(self, patch: dict) -> OsdInfo:
        st, xml = await isapi_request(
            self.ip, _OVERLAY_PATH, self.username, self.password, port=self.http_port
        )
        if st != 200:
            raise HTTPException(status_code=502, detail="Could not read OSD config")
        if patch.get("datetime_enabled") is not None:
            xml = _set_nested_enabled(xml, "DateTimeOverlay", patch["datetime_enabled"])
        if patch.get("channel_name_enabled") is not None:
            xml = _set_nested_enabled(
                xml, "channelNameOverlay", patch["channel_name_enabled"]
            )
        if patch.get("text_enabled") is not None or patch.get("text") is not None:
            xml = _set_text_overlay(xml, 1, patch.get("text_enabled"), patch.get("text"))
        st2, resp = await isapi_request(
            self.ip, _OVERLAY_PATH, self.username, self.password,
            method="PUT", port=self.http_port, body=xml,
        )
        _isapi_ok(st2, resp, "SetOSD")
        return await self.get_osd()

    # --- motion detection (read + write via ISAPI) ---

    async def get_motion(self) -> MotionInfo:
        try:
            st, xml = await isapi_request(
                self.ip, _MOTION_PATH, self.username, self.password,
                port=self.http_port,
            )
        except Exception:
            return MotionInfo(supported=False, source="isapi")
        if st != 200:
            return MotionInfo(supported=False, source="isapi")
        en = re.search(r"<enabled>(true|false)</enabled>", xml)  # first = motion enable
        sl = re.search(r"<sensitivityLevel>(\d+)</sensitivityLevel>", xml)
        return MotionInfo(
            supported=True,
            enabled=(en.group(1) == "true") if en else None,
            sensitivity=int(sl.group(1)) if sl else None,
            sensitivity_max=100,
            source="isapi",
        )

    async def set_motion(self, patch: dict) -> MotionInfo:
        st, xml = await isapi_request(
            self.ip, _MOTION_PATH, self.username, self.password, port=self.http_port
        )
        if st != 200:
            raise HTTPException(status_code=502, detail="Could not read motion config")
        if patch.get("enabled") is not None:
            v = "true" if patch["enabled"] else "false"
            xml = re.sub(
                r"<enabled>(?:true|false)</enabled>",
                f"<enabled>{v}</enabled>",
                xml,
                count=1,
            )
        if patch.get("sensitivity") is not None:
            s = max(0, min(100, int(patch["sensitivity"])))
            xml = re.sub(
                r"<sensitivityLevel>\d+</sensitivityLevel>",
                f"<sensitivityLevel>{s}</sensitivityLevel>",
                xml,
                count=1,
            )
        st2, resp = await isapi_request(
            self.ip, _MOTION_PATH, self.username, self.password,
            method="PUT", port=self.http_port, body=xml,
        )
        _isapi_ok(st2, resp, "SetMotion")
        return await self.get_motion()

    # --- event stream (motion/tamper/etc via ISAPI alertStream) ---

    async def subscribe_events(self):
        """Yield normalized camera events from the Hikvision alert stream
        (multipart/mixed of <EventNotificationAlert> blocks). Long-lived; the
        caller (camera_event_manager) owns reconnect/backoff and teardown."""
        url = f"http://{self.ip}:{self.http_port}{_ALERT_PATH}"
        auth = httpx.DigestAuth(self.username, self.password)
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url, auth=auth) as resp:
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=502,
                        detail=f"alertStream HTTP {resp.status_code}",
                    )
                buf = ""
                async for chunk in resp.aiter_text():
                    buf += chunk
                    while True:
                        m = re.search(
                            r"<EventNotificationAlert.*?</EventNotificationAlert>",
                            buf,
                            re.DOTALL,
                        )
                        if not m:
                            break
                        block = m.group(0)
                        buf = buf[m.end():]
                        ev = parse_event_alert(block)
                        if ev is not None:
                            yield ev
                    # Bound the buffer so a partial trailing block can't grow.
                    if len(buf) > 65536:
                        buf = buf[-8192:]

    # --- on-camera accounts + reboot (gated) ---

    async def get_users(self) -> UsersInfo:
        try:
            st, xml = await isapi_request(
                self.ip, _USERS_PATH, self.username, self.password,
                port=self.http_port,
            )
        except Exception:
            return UsersInfo(supported=False, source="isapi")
        if st != 200:
            return UsersInfo(supported=False, source="isapi")
        users = []
        for block in re.findall(r"<User\b.*?</User>", xml, re.DOTALL):
            name = _tag("userName", block)
            if not name:
                continue
            users.append({
                "id": _tag("id", block),
                "name": name,
                "level": _tag("userLevel", block),
                "is_current": name == self.username,
            })
        return UsersInfo(supported=True, users=users, source="isapi")

    async def create_user(self, username: str, password: str, level: str) -> UsersInfo:
        if level not in _USER_LEVELS:
            raise HTTPException(
                status_code=400, detail=f"level must be one of {_USER_LEVELS}"
            )
        if not username or not password:
            raise HTTPException(status_code=400, detail="username and password required")
        current = await self.get_users()
        if any(u["name"] == username for u in current.users):
            raise HTTPException(status_code=409, detail=f"User '{username}' exists")
        ids = [int(u["id"]) for u in current.users if (u.get("id") or "").isdigit()]
        next_id = (max(ids) + 1) if ids else 1
        body = (
            f'<User version="2.0"><id>{next_id}</id>'
            f"<userName>{_esc(username)}</userName>"
            f"<password>{_esc(password)}</password>"
            f"<userLevel>{level}</userLevel></User>"
        )
        st, resp = await isapi_request(
            self.ip, _USERS_PATH, self.username, self.password,
            method="POST", port=self.http_port, body=body,
        )
        _isapi_ok(st, resp, "CreateUser")
        return await self.get_users()

    async def delete_user(self, username: str) -> UsersInfo:
        # Hard guardrail: never remove the account OpenNVR authenticates with —
        # that's the exact way to lock OpenNVR (and possibly you) out.
        if username == self.username:
            raise HTTPException(
                status_code=400,
                detail="Refusing to delete the account OpenNVR uses to reach this camera",
            )
        current = await self.get_users()
        match = next((u for u in current.users if u["name"] == username), None)
        if not match:
            raise HTTPException(status_code=404, detail=f"User '{username}' not found")
        st, resp = await isapi_request(
            self.ip, f"{_USERS_PATH}/{match.get('id')}", self.username, self.password,
            method="DELETE", port=self.http_port,
        )
        _isapi_ok(st, resp, "DeleteUser")
        return await self.get_users()

    async def reboot(self) -> dict:
        st, resp = await isapi_request(
            self.ip, _REBOOT_PATH, self.username, self.password,
            method="PUT", port=self.http_port,
        )
        _isapi_ok(st, resp, "Reboot")
        return {"status": "rebooting"}

    # --- network (read only, via ISAPI — richer than ONVIF here) ---

    async def get_network(self) -> NetworkInfo:
        try:
            status, text = await isapi_request(
                self.ip,
                "/ISAPI/System/Network/interfaces",
                self.username,
                self.password,
                port=self.http_port,
            )
        except Exception:
            # Fall back to the ONVIF network read.
            return await super().get_network()
        if status != 200:
            return await super().get_network()

        addressing = _tag("addressingType", text)  # "static" | "dhcp"
        mtu = _tag("MTU", text)
        return NetworkInfo(
            supported=True,
            source="isapi",
            mac_address=_tag("MACAddress", text),
            ip_address=_tag("ipAddress", text),  # first = interface IP
            subnet_mask=_tag("subnetMask", text),
            gateway=_nested_ip("DefaultGateway", text),
            dns_primary=_nested_ip("PrimaryDNS", text),
            dns_secondary=_nested_ip("SecondaryDNS", text),
            dhcp=(addressing == "dhcp") if addressing else None,
            mtu=int(mtu) if mtu and mtu.isdigit() else None,
        )

    # --- storage (read only, via ISAPI) ---

    async def get_storage(self) -> StorageInfo:
        try:
            status, text = await isapi_request(
                self.ip,
                "/ISAPI/ContentMgmt/Storage",
                self.username,
                self.password,
                port=self.http_port,
            )
        except Exception:
            return StorageInfo(supported=False, source="isapi")
        if status != 200:
            return StorageInfo(supported=False, source="isapi")

        slots: list[StorageSlot] = []
        for block in re.findall(r"<hdd>(.*?)</hdd>", text, re.DOTALL):
            cap = _tag("capacity", block)
            free = _tag("freeSpace", block)
            slots.append(
                StorageSlot(
                    name=_tag("hddName", block) or _tag("id", block),
                    status=_tag("status", block),
                    capacity_mb=int(cap) if cap and cap.isdigit() else None,
                    free_mb=int(free) if free and free.isdigit() else None,
                )
            )
        return StorageInfo(
            supported=True, present=bool(slots), slots=slots, source="isapi"
        )
