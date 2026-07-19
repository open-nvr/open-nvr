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
import socket
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException

from ..base import (
    Capabilities,
    ImagingInfo,
    MotionInfo,
    NetworkInfo,
    OsdInfo,
    SecurityInfo,
    ServiceEntry,
    ServicesInfo,
    SmartDetector,
    SmartInfo,
    StorageInfo,
    StorageSlot,
    UsersInfo,
)
from ..onvif.driver import OnvifDriver
from .isapi import isapi_request

_OVERLAY_PATH = "/ISAPI/System/Video/inputs/channels/1/overlays"
_MOTION_PATH = "/ISAPI/System/Video/inputs/channels/1/motionDetection"
_ALERT_PATH = "/ISAPI/Event/notification/alertStream"
_USERS_PATH = "/ISAPI/Security/users"
_REBOOT_PATH = "/ISAPI/System/reboot"
_IMAGE_FLIP_PATH = "/ISAPI/Image/channels/1/ImageFlip"
_NOISE_REDUCE_PATH = "/ISAPI/Image/channels/1/noiseReduce"
_IPFILTER_PATH = "/ISAPI/System/Network/ipFilter"
_LOGINLOCK_PATH = "/ISAPI/Security/illegalLoginLock"
_EZVIZ_PATH = "/ISAPI/System/Network/EZVIZ"
_ADMINACCESS_PATH = "/ISAPI/Security/adminAccesses"
_HIK_NS = "http://www.hikvision.com/ver20/XMLSchema"
_NAS_PATH = "/ISAPI/ContentMgmt/Storage/nas"
_SNAPSHOT_PATH = "/ISAPI/Streaming/channels/101/picture"
_CONFIG_EXPORT_PATH = "/ISAPI/System/configurationData"

# Network services. ``tag`` is the element carrying the on/off state (SNMP uses
# notificationEnabled, not enabled). ``writable=False`` means we display it but
# don't drive it — its config is richer than a single toggle.
_SERVICES: dict[str, dict] = {
    "upnp": {
        "label": "UPnP", "path": "/ISAPI/System/Network/UPnP",
        "tag": "enabled", "writable": True,
    },
    "snmp": {
        "label": "SNMP", "path": "/ISAPI/System/Network/SNMP",
        "tag": "notificationEnabled", "writable": True,
    },
    "ddns": {
        "label": "DDNS", "path": "/ISAPI/System/Network/DDNS",
        "tag": "enabled", "writable": True,
    },
    "ftp": {
        "label": "FTP upload", "path": "/ISAPI/System/Network/FTP",
        "tag": "enabled", "writable": True,
    },
    "email": {
        "label": "Email (SMTP)", "path": "/ISAPI/System/Network/mailing",
        "tag": "enabled", "writable": True,
    },
    "qos": {
        "label": "QoS / DSCP", "path": "/ISAPI/System/Network/QoS",
        "tag": "enabled", "writable": False,
    },
    "http_hosts": {
        "label": "Event HTTP push", "path": "/ISAPI/Event/notification/httpHosts",
        "tag": "enabled", "writable": False,
    },
}
_USER_LEVELS = ("Administrator", "Operator", "Viewer")
_NR_MODES = ("close", "general", "advanced")

# Analytics detectors. ``needs_region``: the detector is inert until a line or
# region is drawn on the device, so we report whether one exists.
_SMART_DETECTORS: dict[str, dict] = {
    "line_crossing": {
        "label": "Line crossing",
        "path": "/ISAPI/Smart/LineDetection/1",
        "max": 100,
        "needs_region": True,
    },
    "intrusion": {
        "label": "Intrusion",
        "path": "/ISAPI/Smart/FieldDetection/1",
        "max": 100,
        "needs_region": True,
    },
    "face": {
        "label": "Face detection",
        "path": "/ISAPI/Smart/FaceDetect/1",
        "max": 5,
        "needs_region": False,
    },
    "tamper": {
        "label": "Tamper / camera blocked",
        "path": "/ISAPI/System/Video/inputs/channels/1/tamperDetection",
        "max": 100,
        "needs_region": True,
    },
}


def _has_region(xml: str) -> bool:
    """True if a usable line/region is actually drawn.

    An empty coordinate list means the detector does nothing even when
    enabled; a 'line' whose points are all identical is equally inert.
    """
    pts = re.findall(
        r"<positionX>(\d+)</positionX>\s*<positionY>(\d+)</positionY>", xml
    )
    return len(set(pts)) >= 2


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
        # Native ISAPI alertStream — no longer inherited from the ONVIF baseline.
        caps.supported_areas["events"] = True
        caps.supported_areas["smart"] = True
        caps.supported_areas["security"] = True
        caps.supported_areas["services"] = True
        return caps

    # --- imaging: ONVIF baseline + ISAPI-only extras (flip, noise reduction) ---

    async def get_imaging(self) -> ImagingInfo:
        """ONVIF imaging plus the controls ONVIF can't reach on Hikvision.

        ``flip`` is surfaced as an ON/OFF enum so it renders with the same
        control as the existing wdr/backlight selects.
        """
        info = await super().get_imaging()
        got = False

        try:
            st, xml = await isapi_request(
                self.ip, _IMAGE_FLIP_PATH, self.username, self.password,
                port=self.http_port,
            )
            if st == 200:
                en = re.search(r"<enabled>(true|false)</enabled>", xml)
                if en:
                    info.settings["flip"] = "ON" if en.group(1) == "true" else "OFF"
                    info.ranges["flip"] = {"options": ["ON", "OFF"]}
                    got = True
        except Exception:
            pass

        try:
            st, xml = await isapi_request(
                self.ip, _NOISE_REDUCE_PATH, self.username, self.password,
                port=self.http_port,
            )
            if st == 200:
                mode = _tag("mode", xml)
                if mode:
                    info.settings["noise_reduce_mode"] = mode
                    info.ranges["noise_reduce_mode"] = {"options": list(_NR_MODES)}
                    got = True
                lvl = re.search(r"<generalLevel>(\d+)</generalLevel>", xml)
                if lvl:
                    info.settings["noise_reduction"] = int(lvl.group(1))
                    info.ranges["noise_reduction"] = {"min": 0, "max": 100}
                    got = True
        except Exception:
            pass

        if got:
            info.supported = True
            info.source = "onvif+isapi" if info.source == "onvif" else "isapi"
        return info

    async def set_imaging(self, patch: dict) -> ImagingInfo:
        """Apply ISAPI-only keys here; delegate the rest to the ONVIF baseline
        (which rejects unknown keys, so they must be stripped first)."""
        patch = dict(patch)  # never mutate the caller's dict
        flip = patch.pop("flip", None)
        nr = patch.pop("noise_reduction", None)
        nr_mode = patch.pop("noise_reduce_mode", None)

        if patch:
            await super().set_imaging(patch)

        if flip is not None:
            st, xml = await isapi_request(
                self.ip, _IMAGE_FLIP_PATH, self.username, self.password,
                port=self.http_port,
            )
            if st != 200:
                raise HTTPException(status_code=502, detail="Could not read flip config")
            on = str(flip).strip().upper() in ("ON", "TRUE", "1")
            xml = re.sub(
                r"<enabled>(?:true|false)</enabled>",
                f"<enabled>{'true' if on else 'false'}</enabled>",
                xml,
                count=1,
            )
            st2, resp = await isapi_request(
                self.ip, _IMAGE_FLIP_PATH, self.username, self.password,
                method="PUT", port=self.http_port, body=xml,
            )
            _isapi_ok(st2, resp, "SetImageFlip")

        if nr is not None or nr_mode is not None:
            st, xml = await isapi_request(
                self.ip, _NOISE_REDUCE_PATH, self.username, self.password,
                port=self.http_port,
            )
            if st != 200:
                raise HTTPException(
                    status_code=502, detail="Could not read noise-reduction config"
                )
            if nr_mode is not None:
                if nr_mode not in _NR_MODES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"noise_reduce_mode must be one of {_NR_MODES}",
                    )
                xml = re.sub(r"<mode>[^<]*</mode>", f"<mode>{nr_mode}</mode>", xml, count=1)
            if nr is not None:
                lvl = max(0, min(100, int(nr)))
                xml = re.sub(
                    r"<generalLevel>\d+</generalLevel>",
                    f"<generalLevel>{lvl}</generalLevel>",
                    xml,
                    count=1,
                )
            st2, resp = await isapi_request(
                self.ip, _NOISE_REDUCE_PATH, self.username, self.password,
                method="PUT", port=self.http_port, body=xml,
            )
            _isapi_ok(st2, resp, "SetNoiseReduce")

        return await self.get_imaging()

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
        slots = []
        for blk in re.findall(r"<TextOverlay>(.*?)</TextOverlay>", xml, re.DOTALL):
            sid = _tag("id", blk)
            if sid is None:
                continue
            en = re.search(r"<enabled>(true|false)</enabled>", blk)
            dt = re.search(r"<displayText>([^<]*)</displayText>", blk)
            px, py = _tag("positionX", blk), _tag("positionY", blk)
            slots.append({
                "id": int(sid),
                "enabled": (en.group(1) == "true") if en else None,
                "text": dt.group(1) if dt else "",
                "x": int(px) if px and px.isdigit() else None,
                "y": int(py) if py and py.isdigit() else None,
            })
        return OsdInfo(
            supported=True,
            datetime_enabled=_nested_enabled("DateTimeOverlay", xml),
            channel_name_enabled=_nested_enabled("channelNameOverlay", xml),
            text_enabled=text_enabled,
            text=text,
            slots=slots,
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
        # Multi-slot form: [{id, enabled?, text?}, ...] — patches only the slots
        # supplied, leaving every other overlay untouched.
        for slot in patch.get("slots") or []:
            sid = slot.get("id")
            if sid is None:
                continue
            xml = _set_text_overlay(
                xml, int(sid), slot.get("enabled"), slot.get("text")
            )
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

    # --- smart analytics (line crossing / intrusion / face / tamper) ---

    async def get_smart(self) -> SmartInfo:
        """Read every analytics detector this model exposes.

        Detectors the model doesn't implement answer 403 and are reported as
        ``supported=False`` rather than omitted, so the UI can show why.
        """
        detectors: list[SmartDetector] = []
        any_supported = False
        for key, spec in _SMART_DETECTORS.items():
            det = SmartDetector(
                key=key, label=spec["label"], sensitivity_max=spec["max"]
            )
            try:
                st, xml = await isapi_request(
                    self.ip, spec["path"], self.username, self.password,
                    port=self.http_port,
                )
            except Exception:
                detectors.append(det)
                continue
            if st == 200:
                det.supported = True
                any_supported = True
                en = re.search(r"<enabled>(true|false)</enabled>", xml)
                det.enabled = (en.group(1) == "true") if en else None
                sl = re.search(r"<sensitivityLevel>(\d+)</sensitivityLevel>", xml)
                det.sensitivity = int(sl.group(1)) if sl else None
                if spec["needs_region"]:
                    det.configured = _has_region(xml)
            detectors.append(det)
        return SmartInfo(
            supported=any_supported, detectors=detectors, source="isapi"
        )

    async def set_smart(self, key: str, patch: dict) -> SmartInfo:
        spec = _SMART_DETECTORS.get(key)
        if spec is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown detector '{key}'"
            )
        st, xml = await isapi_request(
            self.ip, spec["path"], self.username, self.password, port=self.http_port
        )
        if st != 200:
            raise HTTPException(
                status_code=502,
                detail=f"{spec['label']} is not supported by this camera",
            )
        if patch.get("enabled") is not None:
            v = "true" if patch["enabled"] else "false"
            # Top-level <enabled> is the master switch; the nested per-region
            # <enabled> is flipped with it so the detector actually arms.
            xml = re.sub(
                r"<enabled>(?:true|false)</enabled>",
                f"<enabled>{v}</enabled>",
                xml,
                flags=0,
            )
        if patch.get("sensitivity") is not None:
            s = max(1, min(spec["max"], int(patch["sensitivity"])))
            xml = re.sub(
                r"<sensitivityLevel>\d+</sensitivityLevel>",
                f"<sensitivityLevel>{s}</sensitivityLevel>",
                xml,
            )
        st2, resp = await isapi_request(
            self.ip, spec["path"], self.username, self.password,
            method="PUT", port=self.http_port, body=xml,
        )
        _isapi_ok(st2, resp, f"Set{spec['label']}")
        return await self.get_smart()

    # --- network services (UPnP / SNMP / FTP / DDNS / email / QoS / hosts) ---

    async def get_services(self) -> ServicesInfo:
        entries: list[ServiceEntry] = []
        any_ok = False
        for key, spec in _SERVICES.items():
            e = ServiceEntry(
                key=key, label=spec["label"], writable=spec["writable"]
            )
            try:
                st, xml = await isapi_request(
                    self.ip, spec["path"], self.username, self.password,
                    port=self.http_port,
                )
            except Exception:
                entries.append(e)
                continue
            if st == 200:
                e.supported = True
                any_ok = True
                m = re.search(
                    rf"<{spec['tag']}>(true|false|TRUE|FALSE)</{spec['tag']}>", xml
                )
                e.enabled = (m.group(1).lower() == "true") if m else None
                if key == "http_hosts":
                    url = _tag("url", xml)
                    e.detail = url or "no destination configured"
                elif key == "ftp":
                    host = _tag("ipAddress", xml)
                    e.detail = None if host in (None, "0.0.0.0") else host
            entries.append(e)
        return ServicesInfo(supported=any_ok, services=entries, source="isapi")

    async def set_service(self, key: str, enabled: bool) -> ServicesInfo:
        spec = _SERVICES.get(key)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"Unknown service '{key}'")
        if not spec["writable"]:
            raise HTTPException(
                status_code=400,
                detail=f"{spec['label']} is read-only — configure it on the camera",
            )
        st, xml = await isapi_request(
            self.ip, spec["path"], self.username, self.password, port=self.http_port
        )
        if st != 200:
            raise HTTPException(
                status_code=502, detail=f"{spec['label']} not available on this camera"
            )
        tag = spec["tag"]
        xml = re.sub(
            rf"<{tag}>(?:true|false|TRUE|FALSE)</{tag}>",
            f"<{tag}>{'true' if enabled else 'false'}</{tag}>",
            xml,
            count=1,
        )
        st2, resp = await isapi_request(
            self.ip, spec["path"], self.username, self.password,
            method="PUT", port=self.http_port, body=xml,
        )
        _isapi_ok(st2, resp, f"Set{spec['label']}")
        return await self.get_services()

    # --- snapshot + configuration backup ---

    async def get_snapshot(self) -> bytes:
        url = f"http://{self.ip}:{self.http_port}{_SNAPSHOT_PATH}"
        auth = httpx.DigestAuth(self.username, self.password)
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, auth=auth)
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502, detail=f"Snapshot failed: HTTP {resp.status_code}"
            )
        return resp.content

    async def export_config(self, secret_key: str) -> bytes:
        """Download the encrypted configuration blob.

        Hikvision requires a ``secretkey`` and encrypts the backup with it —
        the SAME key is required to restore, so the caller must keep it. A
        request without the key is rejected by the camera with HTTP 400.
        """
        if not secret_key or not secret_key.strip():
            raise HTTPException(
                status_code=400,
                detail="A secret key is required — the camera encrypts the "
                       "backup with it and the same key is needed to restore",
            )
        url = f"http://{self.ip}:{self.http_port}{_CONFIG_EXPORT_PATH}"
        auth = httpx.DigestAuth(self.username, self.password)
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                url, auth=auth, params={"secretkey": secret_key.strip()}
            )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Config export failed: HTTP {resp.status_code}",
            )
        return resp.content

    # --- camera-side security (IP filter, cloud, login lock) ---

    def _local_source_ip(self) -> str | None:
        """The address the camera sees us as, taken from a real socket.

        Never guessed: an allowlist built from the wrong address locks OpenNVR
        out of the camera permanently (recovery = physical reset button).
        """
        try:
            with socket.create_connection((self.ip, self.http_port), timeout=3) as s:
                return s.getsockname()[0]
        except Exception:
            return None

    async def get_security(self) -> SecurityInfo:
        info = SecurityInfo(supported=True, source="isapi")
        info.server_ip = self._local_source_ip()

        try:
            st, xml = await isapi_request(
                self.ip, _IPFILTER_PATH, self.username, self.password,
                port=self.http_port,
            )
            if st == 200:
                info.ip_filter_supported = True
                en = re.search(r"<enabled>(true|false)</enabled>", xml)
                info.ip_filter_enabled = (en.group(1) == "true") if en else None
                info.ip_filter_mode = _tag("permissionType", xml)
                info.ip_filter_entries = re.findall(
                    r"<ipAddress>([0-9.]+)</ipAddress>", xml
                )
                cap = re.search(r'<IPFilterAddressList size="(\d+)"', xml)
                info.ip_filter_max = int(cap.group(1)) if cap else None
        except Exception:
            pass

        try:
            st, xml = await isapi_request(
                self.ip, _LOGINLOCK_PATH, self.username, self.password,
                port=self.http_port,
            )
            if st == 200:
                en = re.search(r"<enabled>(true|false)</enabled>", xml)
                info.illegal_login_lock = (en.group(1) == "true") if en else None
        except Exception:
            pass

        try:
            st, xml = await isapi_request(
                self.ip, _EZVIZ_PATH, self.username, self.password,
                port=self.http_port,
            )
            if st == 200:
                info.cloud_supported = True
                en = re.search(r"<enabled>(true|false)</enabled>", xml)
                info.cloud_enabled = (en.group(1) == "true") if en else None
                info.cloud_host = _tag("hostName", xml)
        except Exception:
            pass

        try:
            st, xml = await isapi_request(
                self.ip, _ADMINACCESS_PATH, self.username, self.password,
                port=self.http_port,
            )
            if st == 200:
                for block in re.findall(
                    r"<AdminAccessProtocol\b.*?</AdminAccessProtocol>", xml, re.DOTALL
                ):
                    proto = _tag("protocol", block)
                    if not proto:
                        continue
                    en = re.search(r"<enabled>(true|false)</enabled>", block)
                    port = _tag("portNo", block)
                    info.protocols.append({
                        "protocol": proto,
                        "enabled": (en.group(1) == "true") if en else None,
                        "port": int(port) if port and port.isdigit() else None,
                    })
        except Exception:
            pass

        return info

    async def set_ip_filter(
        self, *, enabled: bool, mode: str, entries: list[str]
    ) -> SecurityInfo:
        """Write the camera's IP filter — the one genuinely lockout-capable
        write in this driver. Guardrails (see feature.md §8.2):

        * ``allow`` mode must contain the address the camera sees us from,
          verified against a live socket — never a guess or a caller-supplied
          value;
        * an empty allowlist is refused outright;
        * the list is written with the filter DISABLED first and read back,
          so a bad list can never be armed;
        * after arming we re-read through the filter to prove we still have
          access.
        """
        mode = (mode or "").strip().lower()
        if mode not in ("allow", "deny"):
            raise HTTPException(
                status_code=400, detail="mode must be 'allow' or 'deny'"
            )
        entries = [e.strip() for e in entries if e and e.strip()]

        if enabled and mode == "allow":
            if not entries:
                raise HTTPException(
                    status_code=400,
                    detail="Refusing to enable an empty allowlist — that locks "
                           "OpenNVR out of this camera",
                )
            server_ip = self._local_source_ip()
            if not server_ip:
                raise HTTPException(
                    status_code=502,
                    detail="Cannot determine this server's address as seen by the "
                           "camera; refusing to arm an allowlist",
                )
            if server_ip not in entries:
                entries.insert(0, server_ip)

        addr_xml = "".join(
            f"<IPFilterAddress><id>{i + 1}</id>"
            f"<ipAddress>{_esc(a)}</ipAddress></IPFilterAddress>"
            for i, a in enumerate(entries)
        )

        def _body(is_enabled: bool) -> str:
            return (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<IPFilter version="2.0" xmlns="{_HIK_NS}">'
                f"<enabled>{'true' if is_enabled else 'false'}</enabled>"
                f"<permissionType>{mode}</permissionType>"
                f"<IPFilterAddressList>{addr_xml}</IPFilterAddressList>"
                "</IPFilter>"
            )

        # 1) stage the list with the filter OFF so a bad list is never armed
        st, resp = await isapi_request(
            self.ip, _IPFILTER_PATH, self.username, self.password,
            method="PUT", port=self.http_port, body=_body(False),
        )
        _isapi_ok(st, resp, "SetIPFilter")

        # 2) read back and confirm the list landed as intended
        staged = await self.get_security()
        if enabled and mode == "allow":
            if self._local_source_ip() not in staged.ip_filter_entries:
                raise HTTPException(
                    status_code=502,
                    detail="Camera did not store this server's address; "
                           "leaving the IP filter disabled",
                )

        if not enabled:
            return staged

        # 3) arm it, then prove we can still reach the camera through it
        st, resp = await isapi_request(
            self.ip, _IPFILTER_PATH, self.username, self.password,
            method="PUT", port=self.http_port, body=_body(True),
        )
        _isapi_ok(st, resp, "EnableIPFilter")
        after = await self.get_security()
        if not after.ip_filter_supported:
            raise HTTPException(
                status_code=502,
                detail="Lost access to the camera after enabling the IP filter",
            )
        return after

    async def set_cloud(self, enabled: bool) -> SecurityInfo:
        """Enable/disable the Hik-Connect / EZVIZ P2P cloud tunnel."""
        st, xml = await isapi_request(
            self.ip, _EZVIZ_PATH, self.username, self.password, port=self.http_port
        )
        if st != 200:
            raise HTTPException(
                status_code=502, detail="Cloud settings not available on this camera"
            )
        xml = re.sub(
            r"<enabled>(?:true|false)</enabled>",
            f"<enabled>{'true' if enabled else 'false'}</enabled>",
            xml,
            count=1,
        )
        st2, resp = await isapi_request(
            self.ip, _EZVIZ_PATH, self.username, self.password,
            method="PUT", port=self.http_port, body=xml,
        )
        _isapi_ok(st2, resp, "SetCloud")
        return await self.get_security()

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
        info = StorageInfo(
            supported=True, present=bool(slots), slots=slots, source="isapi"
        )
        try:
            nst, ntext = await isapi_request(
                self.ip, _NAS_PATH, self.username, self.password,
                port=self.http_port,
            )
            if nst == 200:
                info.nas_supported = True
                opt = re.search(r'<supportMountType opt="([^"]*)"', ntext)
                if opt:
                    info.nas_mount_types = [
                        t.strip() for t in opt.group(1).split(",") if t.strip()
                    ]
                for blk in re.findall(r"<nas.*?</nas>", ntext, re.DOTALL):
                    addr = _tag("ipAddress", blk) or _tag("hostName", blk)
                    if not addr:
                        continue
                    info.nas_mounts.append({
                        "address": addr,
                        "path": _tag("path", blk),
                        "type": _tag("mountType", blk),
                        "status": _tag("status", blk),
                    })
        except Exception:
            pass
        return info
