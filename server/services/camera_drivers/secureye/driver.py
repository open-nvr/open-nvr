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
SecureyeDriver — ONVIF baseline plus Secureye's own native APIs.

Verified against a real **Secureye SP-C2QN** (firmware ``NVSS_V26.0.0``,
platform ``CGI_V3.0.0``). This device is **not** a Hikvision or Dahua rebadge:
it has its own ``/CGI/`` namespace *and* a partial, namespace-free ``/ISAPI/``
subset. Hikvision's standard ISAPI paths mostly return HTTP 400 here, which is
why the Hikvision probe was tightened to require the Hikvision XML namespace.

Where each area is actually served (all confirmed live):

    area       transport   path
    ---------  ----------  ------------------------------------------------
    info       ONVIF       GetDeviceInformation
    network    ONVIF       GetNetworkInterfaces (the ISAPI path 400s)
    encoder    ONVIF       GetVideoEncoderConfigurations
    time       ONVIF       GetSystemDateAndTime
    imaging    CGI         /CGI/Image/channels/1/{color,noiseReduce}/template/0
    osd        CGI         /CGI/System/Video/inputs/channels/1/overlays/type/1
    storage    ISAPI       /ISAPI/ContentMgmt/Storage/hdd/
    motion     ISAPI       /ISAPI/System/Video/.../motionDetection
    security   ISAPI       /ISAPI/System/Network/ipFilter

The CGI endpoints were found in the device's own web-UI endpoint manifest —
they are not discoverable by probing Hikvision-style paths.

**Users are deliberately NOT exposed.** The device returns them obfuscated —
``<userName>S/CdW5jKf1k=</userName>`` — so parsing would surface encrypted blobs
as account names. Reporting the area unsupported is the honest result.
"""

from __future__ import annotations

import re
import socket

from fastapi import HTTPException

from ..base import (
    Capabilities,
    ImagingInfo,
    MotionInfo,
    OsdInfo,
    SecurityInfo,
    ServiceEntry,
    ServicesInfo,
    SmartDetector,
    SmartInfo,
    StorageInfo,
    StorageSlot,
)
from ..onvif.driver import OnvifDriver
from .cgi import cgi_request

_MOTION_PATH = "/ISAPI/System/Video/inputs/channels/1/motionDetection"
_IPFILTER_PATH = "/ISAPI/System/Network/ipFilter"
# This firmware exposes imaging and OSD under its OWN /CGI/ namespace, not the
# Hikvision-style /ISAPI/ paths (which 400). Discovered from the device's own
# endpoint manifest in its web UI and confirmed live. Both are template- and
# channel-scoped; template 0 is the active one.
_COLOR_PATH = "/CGI/Image/channels/1/color/template/0"
_NR_PATH = "/CGI/Image/channels/1/noiseReduce/template/0"
_OVERLAY_PATH = "/CGI/System/Video/inputs/channels/1/overlays/type/1"
_HDD_PATH = "/ISAPI/ContentMgmt/Storage/hdd/"
_SMART_CAP_PATH = "/ISAPI/Smart/channels/1/capabilities"
_TELNET_PATH = "/CGI/System/TelnetCtrl"
_PLATFORM_PATH = "/CGI/System/Platform"
_AUTOREBOOT_PATH = "/CGI/System/AutoReboot"
_REBOOT_PATH = "/ISAPI/System/reboot"
_IMG = "/CGI/Image/channels/1"

# Every image setting this firmware serves, declared once. ``tag`` is the XML
# element holding the value; ``kind`` drives parsing and the UI control.
# NOTE: Defog's element is misspelled <enbaled> in the firmware — not a typo here.
_IMAGE_SPECS: tuple[dict, ...] = (
    {"key": "brightness", "path": f"{_IMG}/color/template/0",
     "tag": "brightnessLevel", "kind": "int", "range": (0, 100)},
    {"key": "contrast", "path": f"{_IMG}/color/template/0",
     "tag": "contrastLevel", "kind": "int", "range": (0, 100)},
    {"key": "saturation", "path": f"{_IMG}/color/template/0",
     "tag": "saturationLevel", "kind": "int", "range": (0, 100)},
    {"key": "hue", "path": f"{_IMG}/color/template/0",
     "tag": "hueLevel", "kind": "int", "range": (0, 100)},
    {"key": "sharpness", "path": f"{_IMG}/sharpness/template/0",
     "tag": "sharpnessLevel", "kind": "int", "range": (0, 100)},
    {"key": "gain", "path": f"{_IMG}/gain/template/0",
     "tag": "GainLevel", "kind": "int", "range": (0, 100)},
    {"key": "shutter", "path": f"{_IMG}/shutter/template/0",
     "tag": "ShutterLevel", "kind": "str"},
    {"key": "noise_reduce_mode", "path": f"{_IMG}/noiseReduce/template/0",
     "tag": "mode", "kind": "enum", "options": ("close", "general", "advanced")},
    {"key": "noise_reduction", "path": f"{_IMG}/noiseReduce/template/0",
     "tag": "generalLevel", "kind": "int", "range": (0, 100)},
    {"key": "wdr", "path": f"{_IMG}/WDREX/template/0",
     "tag": "mode", "kind": "enum", "options": ("close", "open", "auto")},
    {"key": "wdr_level", "path": f"{_IMG}/WDREX/template/0",
     "tag": "WDRLevel", "kind": "int", "range": (0, 100)},
    {"key": "backlight", "path": f"{_IMG}/BLC/template/0",
     "tag": "enabled", "kind": "bool"},
    {"key": "defog", "path": f"{_IMG}/Defog/template/0",
     "tag": "enbaled", "kind": "bool"},
    {"key": "defog_strength", "path": f"{_IMG}/Defog/template/0",
     "tag": "defogStrength", "kind": "int", "range": (0, 100)},
    {"key": "white_balance", "path": f"{_IMG}/whiteBalance/template/0",
     "tag": "WhiteBalanceStyle", "kind": "str"},
    {"key": "wb_red", "path": f"{_IMG}/whiteBalance/template/0",
     "tag": "WhiteBalanceRed", "kind": "int", "range": (0, 100)},
    {"key": "wb_blue", "path": f"{_IMG}/whiteBalance/template/0",
     "tag": "WhiteBalanceBlue", "kind": "int", "range": (0, 100)},
    {"key": "light_suppression", "path": f"{_IMG}/lightSuppression/template/0",
     "tag": "enabled", "kind": "bool"},
    {"key": "light_suppression_strength",
     "path": f"{_IMG}/lightSuppression/template/0",
     "tag": "lightSuppressionStrength", "kind": "int", "range": (0, 100)},
    {"key": "image_style", "path": f"{_IMG}/ImageStyle/template/0",
     "tag": "style", "kind": "str"},
    {"key": "scene_mode", "path": f"{_IMG}/SceneMode/template/0",
     "tag": "mode", "kind": "str"},
    {"key": "ir_light_mode", "path": f"{_IMG}/irLight",
     "tag": "mode", "kind": "str"},
    {"key": "ir_brightness", "path": f"{_IMG}/irLight",
     "tag": "brightnessLevel", "kind": "int", "range": (0, 100)},
    {"key": "ir_night_brightness", "path": f"{_IMG}/irLight",
     "tag": "nightBrightnessLevel", "kind": "int", "range": (0, 100)},
)

# Analytics detectors. Availability is read from the device's own capabilities
# endpoint rather than assumed — on the SP-C2QN only LineDetection and
# FieldDetection report IsSupport=true (face, loitering, parking… are false).
_SMART_SPECS: dict[str, dict] = {
    "line_crossing": {
        "label": "Line crossing", "cap": "LineDetection",
        "path": "/CGI/Smart/LineDetection/1/Channels/1/Scene/0",
    },
    "intrusion": {
        "label": "Intrusion", "cap": "FieldDetection",
        "path": "/ISAPI/Smart/FieldDetection/1/Channels/1/Scene/0",
    },
    "tamper": {
        "label": "Tamper / camera blocked", "cap": None,
        "path": "/ISAPI/System/Video/inputs/channels/1/tamperDetection",
    },
}

# Network services. ``tag`` is the element carrying the on/off state.
_SERVICE_SPECS: dict[str, dict] = {
    "snmp": {"label": "SNMP", "path": "/ISAPI/System/Network/SNMP",
             "tag": "enabled", "writable": True},
    "upnp": {"label": "UPnP", "path": "/ISAPI/System/Network/UPnP",
             "tag": "enabled", "writable": True},
    "ddns": {"label": "DDNS", "path": "/ISAPI/System/Network/DDNS/type/0",
             "tag": "enabled", "writable": True},
    "telnet": {"label": "Telnet", "path": _TELNET_PATH,
               "tag": "enable", "writable": True},
    "ftp": {"label": "FTP upload", "path": "/ISAPI/System/Network/ftp",
            "tag": "enabled", "writable": False},
    "qos": {"label": "QoS / DSCP", "path": "/ISAPI/System/Network/channels/1/QoS",
            "tag": "enabled", "writable": False},
    "alarm_server": {"label": "Alarm server",
                     "path": "/ISAPI/System/Network/AlarmServer",
                     "tag": "enabled", "writable": False},
}


def _tag(name: str, text: str) -> str | None:
    """First value of <name>, tolerating attributes.

    This firmware attaches option hints to elements, e.g.
    ``<mode opt="inside,gray,color,schedule">color</mode>`` — a bare
    ``<name>`` regex silently misses those.
    """
    m = re.search(rf"<{name}(?:\s[^>]*)?>([^<]*)</{name}>", text)
    if not m:
        return None
    v = m.group(1).strip()
    return v or None


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
        rf"\g<1>{v}\g<2>", xml, count=1, flags=re.DOTALL,
    )


def _set_text_overlay(
    xml: str, tid: int, enabled: bool | None, text: str | None
) -> str:
    """Patch one <TextOverlay> block, leaving every sibling untouched.

    This firmware capitalises the identifier as <ID>, unlike Hikvision's <id>,
    and <displayText> carries a max= attribute.
    """
    m = re.search(
        rf"(<TextOverlay>\s*<ID>{tid}</ID>.*?</TextOverlay>)", xml, re.DOTALL
    )
    if not m:
        return xml
    block = nb = m.group(1)
    if enabled is not None:
        nb = re.sub(
            r"<enabled>(?:true|false)</enabled>",
            f"<enabled>{'true' if enabled else 'false'}</enabled>", nb, count=1,
        )
    if text is not None:
        nb = re.sub(
            r"(<displayText[^>]*>)[^<]*(</displayText>)",
            rf"\g<1>{_esc(text)}\g<2>", nb, count=1,
        )
    return xml.replace(block, nb, 1)


def _has_region(xml: str) -> bool:
    """True if a usable line/region is actually drawn — a detector with none is
    inert even when enabled, so the UI must be able to say so."""
    pts = re.findall(
        r"<positionX>(\d+)</positionX>\s*<positionY>(\d+)</positionY>", xml
    )
    return len(set(pts)) >= 2


def _ok(status: int, text: str, op: str) -> None:
    """Secureye answers writes 200; some builds include a statusCode like ISAPI."""
    if status != 200:
        raise HTTPException(status_code=502, detail=f"{op} failed: HTTP {status}")
    m = re.search(r"<statusCode>(\d+)</statusCode>", text)
    if m and m.group(1) != "1":
        ss = re.search(r"<statusString>([^<]+)</statusString>", text)
        raise HTTPException(
            status_code=502, detail=f"{op} failed: {ss.group(1) if ss else 'error'}"
        )


class SecureyeDriver(OnvifDriver):
    driver_name = "secureye"

    # --- capabilities: ONVIF baseline + the ISAPI subset that actually works ---

    async def get_capabilities(self) -> Capabilities:
        caps = await super().get_capabilities()
        caps.driver_name = self.driver_name
        caps.supported_areas["motion"] = True
        caps.supported_areas["security"] = True
        # Native /CGI/ paths (not the Hikvision-style ISAPI ones, which 400)
        # serve these — all confirmed live on an SP-C2QN.
        caps.supported_areas["imaging"] = True
        caps.supported_areas["osd"] = True
        caps.supported_areas["storage"] = True
        caps.supported_areas["services"] = True
        caps.supported_areas["maintenance"] = True
        # Smart is only claimed if the device's own capability list reports at
        # least one supported detector.
        try:
            caps.supported_areas["smart"] = any((await self._smart_caps()).values())
        except Exception:
            caps.supported_areas["smart"] = False
        # Users stay off: this firmware returns them obfuscated (see docstring).
        caps.supported_areas["users"] = False
        # Imaging/OSD/storage are served by this vendor's own CGI namespace, so
        # confirm with a real read rather than trusting either the advertised
        # ONVIF XAddr (non-functional here) or the ISAPI paths (which 400).
        # Runs once per camera — the capability row is persisted.
        try:
            caps.supported_areas["imaging"] = (await self.get_imaging()).supported
        except Exception:
            caps.supported_areas["imaging"] = False
        caps.detail["native_api"] = "cgi"
        return caps

    # --- imaging (native /CGI/ namespace, table-driven) ---

    async def _read_paths(self, paths: set[str]) -> dict[str, str]:
        """Fetch each distinct endpoint once; skip any that error or 400."""
        out: dict[str, str] = {}
        for path in sorted(paths):
            try:
                st, xml = await cgi_request(
                    self.ip, path, self.username, self.password, port=self.http_port
                )
            except Exception:
                continue
            if st == 200:
                out[path] = xml
        return out

    async def get_imaging(self) -> ImagingInfo:
        """Read every image setting this firmware exposes.

        Neither inherited route works here: the ONVIF imaging service is
        advertised but non-functional, and Hikvision-style ``/ISAPI/Image/...``
        paths 400. These vendor CGI paths do. Settings are spread across several
        endpoints, so each is fetched once and shared.
        """
        info = ImagingInfo(supported=False, source="secureye-cgi")
        bodies = await self._read_paths({sp["path"] for sp in _IMAGE_SPECS})
        if not bodies:
            return info

        for spec in _IMAGE_SPECS:
            xml = bodies.get(spec["path"])
            if xml is None:
                continue
            raw = _tag(spec["tag"], xml)
            kind = spec["kind"]
            if kind == "bool":
                # A bool tag may legitimately hold "false", which _tag returns.
                m = re.search(rf"<{spec['tag']}>(true|false)</{spec['tag']}>", xml)
                if not m:
                    continue
                info.settings[spec["key"]] = "ON" if m.group(1) == "true" else "OFF"
                info.ranges[spec["key"]] = {"options": ["ON", "OFF"]}
            elif kind == "int":
                if raw is None or not raw.isdigit():
                    continue
                info.settings[spec["key"]] = int(raw)
                lo, hi = spec.get("range", (0, 100))
                info.ranges[spec["key"]] = {"min": lo, "max": hi}
            else:  # str / enum
                if raw is None:
                    continue
                info.settings[spec["key"]] = raw
                opts = list(spec.get("options") or ())
                # Prefer the device's own opt="a,b,c" list when it advertises one.
                _t = spec["tag"]
                adv = re.search(rf'<{_t} opt="([^"]+)"', xml)
                if adv:
                    opts = [o.strip() for o in adv.group(1).split(",") if o.strip()]
                if opts:
                    if raw not in opts:
                        opts = [*opts, raw]
                    info.ranges[spec["key"]] = {"options": opts}

        info.supported = bool(info.settings)
        return info

    async def set_imaging(self, patch: dict) -> ImagingInfo:
        """Apply image settings, grouping edits by endpoint.

        Several settings share one endpoint (brightness/contrast/saturation/hue
        all live in ``color``), so edits are batched per path — one read, one
        write — and only the supplied keys are touched.
        """
        by_key = {sp["key"]: sp for sp in _IMAGE_SPECS}
        unknown = set(patch) - set(by_key)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported image settings: {sorted(unknown)}",
            )
        if not patch:
            return await self.get_imaging()

        grouped: dict[str, list[tuple[dict, object]]] = {}
        for key, value in patch.items():
            spec = by_key[key]
            grouped.setdefault(spec["path"], []).append((spec, value))

        for path, edits in grouped.items():
            st, xml = await cgi_request(
                self.ip, path, self.username, self.password, port=self.http_port
            )
            if st != 200:
                raise HTTPException(
                    status_code=502, detail=f"Could not read image settings ({path})"
                )
            for spec, value in edits:
                tag, kind = spec["tag"], spec["kind"]
                if kind == "bool":
                    on = str(value).strip().upper() in ("ON", "TRUE", "1")
                    repl = "true" if on else "false"
                    xml = re.sub(
                        rf"<{tag}>(?:true|false)</{tag}>",
                        f"<{tag}>{repl}</{tag}>", xml, count=1,
                    )
                elif kind == "int":
                    lo, hi = spec.get("range", (0, 100))
                    v = max(lo, min(hi, int(value)))
                    xml = re.sub(
                        rf"<{tag}>\d+</{tag}>", f"<{tag}>{v}</{tag}>", xml, count=1
                    )
                else:
                    opts = spec.get("options")
                    if opts and str(value) not in opts:
                        raise HTTPException(
                            status_code=400,
                            detail=f"{spec['key']} must be one of {list(opts)}",
                        )
                    # Preserve any opt="..." attribute the device sent back.
                    xml = re.sub(
                        rf'(<{tag}(?:\s+opt="[^"]*")?>)[^<]*(</{tag}>)',
                        rf"\g<1>{_esc(str(value))}\g<2>", xml, count=1,
                    )
            st2, resp = await cgi_request(
                self.ip, path, self.username, self.password,
                method="PUT", port=self.http_port, body=xml,
            )
            _ok(st2, resp, f"SetImaging({path.rsplit('/', 3)[-3]})")

        return await self.get_imaging()

    # --- OSD overlays (native /CGI/ namespace) ---

    async def get_osd(self) -> OsdInfo:
        try:
            st, xml = await cgi_request(
                self.ip, _OVERLAY_PATH, self.username, self.password,
                port=self.http_port,
            )
        except Exception:
            return OsdInfo(supported=False, source="secureye-cgi")
        if st != 200:
            return OsdInfo(supported=False, source="secureye-cgi")

        slots = []
        # Note: this firmware uses <ID> (capitalised), unlike Hikvision's <id>.
        for blk in re.findall(r"<TextOverlay>(.*?)</TextOverlay>", xml, re.DOTALL):
            sid = _tag("ID", blk)
            if sid is None or not sid.isdigit():
                continue
            en = re.search(r"<enabled>(true|false)</enabled>", blk)
            dt = re.search(r"<displayText[^>]*>([^<]*)</displayText>", blk)
            px, py = _tag("positionX", blk), _tag("positionY", blk)
            slots.append({
                "id": int(sid),
                "enabled": (en.group(1) == "true") if en else None,
                "text": dt.group(1) if dt else "",
                "x": int(px) if px and px.isdigit() else None,
                "y": int(py) if py and py.isdigit() else None,
            })
        first = slots[0] if slots else {}
        return OsdInfo(
            supported=True,
            datetime_enabled=_nested_enabled("DateTimeOverlay", xml),
            channel_name_enabled=_nested_enabled("channelNameOverlay", xml),
            text_enabled=first.get("enabled"),
            text=first.get("text"),
            slots=slots,
            source="secureye-cgi",
        )

    async def set_osd(self, patch: dict) -> OsdInfo:
        st, xml = await cgi_request(
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
        edits = list(patch.get("slots") or [])
        if patch.get("text_enabled") is not None or patch.get("text") is not None:
            edits.append({
                "id": 1,
                "enabled": patch.get("text_enabled"),
                "text": patch.get("text"),
            })
        for slot in edits:
            if slot.get("id") is None:
                continue
            xml = _set_text_overlay(
                xml, int(slot["id"]), slot.get("enabled"), slot.get("text")
            )
        st2, resp = await cgi_request(
            self.ip, _OVERLAY_PATH, self.username, self.password,
            method="PUT", port=self.http_port, body=xml,
        )
        _ok(st2, resp, "SetOSD")
        return await self.get_osd()

    # --- storage (ISAPI hdd list — this path works, unlike ContentMgmt/Storage) ---

    async def get_storage(self) -> StorageInfo:
        try:
            st, xml = await cgi_request(
                self.ip, _HDD_PATH, self.username, self.password, port=self.http_port
            )
        except Exception:
            return StorageInfo(supported=False, source="secureye-isapi")
        if st != 200:
            return StorageInfo(supported=False, source="secureye-isapi")
        slots: list[StorageSlot] = []
        for blk in re.findall(r"<hdd>(.*?)</hdd>", xml, re.DOTALL):
            cap = _tag("capacity", blk)
            free = _tag("freeSpace", blk)
            slots.append(
                StorageSlot(
                    name=_tag("hddName", blk) or _tag("id", blk),
                    status=_tag("status", blk),
                    capacity_mb=int(cap) if cap and cap.isdigit() else None,
                    free_mb=int(free) if free and free.isdigit() else None,
                )
            )
        return StorageInfo(
            supported=True, present=bool(slots), slots=slots,
            source="secureye-isapi",
        )

    # --- motion detection (ISAPI, namespace-free XML) ---

    async def get_motion(self) -> MotionInfo:
        try:
            st, xml = await cgi_request(
                self.ip, _MOTION_PATH, self.username, self.password,
                port=self.http_port,
            )
        except Exception:
            return MotionInfo(supported=False, source="secureye-isapi")
        if st != 200:
            return MotionInfo(supported=False, source="secureye-isapi")
        en = re.search(r"<enabled>(true|false)</enabled>", xml)
        sl = re.search(r"<sensitivityLevel>(\d+)</sensitivityLevel>", xml)
        return MotionInfo(
            supported=True,
            enabled=(en.group(1) == "true") if en else None,
            sensitivity=int(sl.group(1)) if sl else None,
            sensitivity_max=100,
            source="secureye-isapi",
        )

    async def set_motion(self, patch: dict) -> MotionInfo:
        st, xml = await cgi_request(
            self.ip, _MOTION_PATH, self.username, self.password, port=self.http_port
        )
        if st != 200:
            raise HTTPException(status_code=502, detail="Could not read motion config")
        if patch.get("enabled") is not None:
            v = "true" if patch["enabled"] else "false"
            xml = re.sub(
                r"<enabled>(?:true|false)</enabled>", f"<enabled>{v}</enabled>",
                xml, count=1,
            )
        if patch.get("sensitivity") is not None:
            s = max(0, min(100, int(patch["sensitivity"])))
            xml = re.sub(
                r"<sensitivityLevel>\d+</sensitivityLevel>",
                f"<sensitivityLevel>{s}</sensitivityLevel>", xml, count=1,
            )
        st2, resp = await cgi_request(
            self.ip, _MOTION_PATH, self.username, self.password,
            method="PUT", port=self.http_port, body=xml,
        )
        _ok(st2, resp, "SetMotion")
        return await self.get_motion()

    # --- smart analytics (gated by the device's own capability list) ---

    async def _smart_caps(self) -> dict[str, bool]:
        """Which detectors the device says it supports.

        Read from ``/ISAPI/Smart/channels/1/capabilities`` rather than assumed:
        on the SP-C2QN only LineDetection and FieldDetection are IsSupport=true
        (face, loitering, parking, heat-map … all report false).
        """
        try:
            st, xml = await cgi_request(
                self.ip, _SMART_CAP_PATH, self.username, self.password,
                port=self.http_port,
            )
        except Exception:
            return {}
        if st != 200:
            return {}
        out: dict[str, bool] = {}
        for name, sup in re.findall(
            r"<Type>([^<]+)</Type>\s*<IsSupport>(true|false)</IsSupport>", xml
        ):
            out[name] = sup == "true"
        return out

    async def get_smart(self) -> SmartInfo:
        caps = await self._smart_caps()
        detectors: list[SmartDetector] = []
        any_supported = False
        for key, spec in _SMART_SPECS.items():
            det = SmartDetector(key=key, label=spec["label"])
            cap_name = spec["cap"]
            # A detector the device explicitly disables is reported, not hidden.
            if cap_name and caps.get(cap_name) is False:
                detectors.append(det)
                continue
            try:
                st, xml = await cgi_request(
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
                sl = re.search(
                    r"<sensitivityLevel>(\d+)</sensitivityLevel>", xml
                )
                det.sensitivity = int(sl.group(1)) if sl else None
                det.configured = _has_region(xml)
            detectors.append(det)
        return SmartInfo(
            supported=any_supported, detectors=detectors, source="secureye"
        )

    async def set_smart(self, key: str, patch: dict) -> SmartInfo:
        spec = _SMART_SPECS.get(key)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"Unknown detector '{key}'")
        st, xml = await cgi_request(
            self.ip, spec["path"], self.username, self.password, port=self.http_port
        )
        if st != 200:
            raise HTTPException(
                status_code=502,
                detail=f"{spec['label']} is not supported by this camera",
            )
        if patch.get("enabled") is not None:
            v = "true" if patch["enabled"] else "false"
            xml = re.sub(r"<enabled>(?:true|false)</enabled>", f"<enabled>{v}</enabled>", xml)
        if patch.get("sensitivity") is not None:
            sv = max(1, min(100, int(patch["sensitivity"])))
            xml = re.sub(
                r"<sensitivityLevel>\d+</sensitivityLevel>",
                f"<sensitivityLevel>{sv}</sensitivityLevel>", xml,
            )
        st2, resp = await cgi_request(
            self.ip, spec["path"], self.username, self.password,
            method="PUT", port=self.http_port, body=xml,
        )
        _ok(st2, resp, f"Set{spec['label']}")
        return await self.get_smart()

    # --- network services ---

    async def get_services(self) -> ServicesInfo:
        entries: list[ServiceEntry] = []
        any_ok = False
        for key, spec in _SERVICE_SPECS.items():
            e = ServiceEntry(
                key=key, label=spec["label"], writable=spec["writable"]
            )
            try:
                st, xml = await cgi_request(
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
                    rf"<{spec['tag']}>(true|false)</{spec['tag']}>", xml
                )
                e.enabled = (m.group(1) == "true") if m else None
                host = _tag("ipAddress", xml)
                if host and host != "0.0.0.0":
                    e.detail = host
            entries.append(e)
        return ServicesInfo(
            supported=any_ok, services=entries, source="secureye"
        )

    async def set_service(self, key: str, enabled: bool) -> ServicesInfo:
        spec = _SERVICE_SPECS.get(key)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"Unknown service '{key}'")
        if not spec["writable"]:
            raise HTTPException(
                status_code=400,
                detail=f"{spec['label']} is read-only — configure it on the camera",
            )
        st, xml = await cgi_request(
            self.ip, spec["path"], self.username, self.password, port=self.http_port
        )
        if st != 200:
            raise HTTPException(
                status_code=502,
                detail=f"{spec['label']} not available on this camera",
            )
        tag = spec["tag"]
        xml = re.sub(
            rf"<{tag}>(?:true|false)</{tag}>",
            f"<{tag}>{'true' if enabled else 'false'}</{tag}>", xml, count=1,
        )
        st2, resp = await cgi_request(
            self.ip, spec["path"], self.username, self.password,
            method="PUT", port=self.http_port, body=xml,
        )
        _ok(st2, resp, f"Set{spec['label']}")
        return await self.get_services()

    # --- maintenance ---

    async def reboot(self) -> dict:
        st, resp = await cgi_request(
            self.ip, _REBOOT_PATH, self.username, self.password,
            method="PUT", port=self.http_port,
        )
        _ok(st, resp, "Reboot")
        return {"status": "rebooting"}

    # --- camera-side security (IP filter) ---

    def _local_source_ip(self) -> str | None:
        """The address the camera sees us as, from a real socket — never guessed.

        An allowlist built from the wrong address locks OpenNVR out permanently
        (recovery = physical reset button).
        """
        try:
            with socket.create_connection((self.ip, self.http_port), timeout=3) as s:
                return s.getsockname()[0]
        except Exception:
            return None

    async def get_security(self) -> SecurityInfo:
        info = SecurityInfo(supported=True, source="secureye-isapi")
        info.server_ip = self._local_source_ip()
        try:
            st, xml = await cgi_request(
                self.ip, _IPFILTER_PATH, self.username, self.password,
                port=self.http_port,
            )
        except Exception:
            return info
        if st != 200:
            return info
        info.ip_filter_supported = True
        en = re.search(r"<enabled>(true|false)</enabled>", xml)
        info.ip_filter_enabled = (en.group(1) == "true") if en else None
        mode = _tag("permissionType", xml)
        # This firmware reports "unset" until a mode is chosen — normalize to None
        # so the UI doesn't render it as a real allow/deny state.
        info.ip_filter_mode = mode if mode in ("allow", "deny") else None
        info.ip_filter_entries = re.findall(r"<ipAddress>([0-9.]+)</ipAddress>", xml)
        # Secureye advertises capacity as max="16"; Hikvision uses size="64".
        cap = re.search(r'<IPFilterAddressList[^>]*\bmax="(\d+)"', xml) or re.search(
            r'<IPFilterAddressList[^>]*\bsize="(\d+)"', xml
        )
        info.ip_filter_max = int(cap.group(1)) if cap else None
        return info

    async def set_ip_filter(
        self, *, enabled: bool, mode: str, entries: list[str]
    ) -> SecurityInfo:
        """Write the IP filter with the same lockout guardrails as Hikvision:
        no empty allowlist, this server's verified address force-included, list
        staged with the filter disabled and read back before being armed."""
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

        current = await self.get_security()
        if current.ip_filter_max and len(entries) > current.ip_filter_max:
            raise HTTPException(
                status_code=400,
                detail=f"This camera accepts at most {current.ip_filter_max} "
                       f"filter entries ({len(entries)} given)",
            )

        addr_xml = "".join(
            f"<IPFilterAddress><id>{i + 1}</id>"
            f"<ipAddress>{_esc(a)}</ipAddress></IPFilterAddress>"
            for i, a in enumerate(entries)
        )

        def _body(is_enabled: bool) -> str:
            # No namespace — this firmware's ISAPI XML is namespace-free.
            return (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<IPFilter>"
                f"<enabled>{'true' if is_enabled else 'false'}</enabled>"
                f"<permissionType>{mode}</permissionType>"
                f"<IPFilterAddressList>{addr_xml}</IPFilterAddressList>"
                "</IPFilter>"
            )

        st, resp = await cgi_request(
            self.ip, _IPFILTER_PATH, self.username, self.password,
            method="PUT", port=self.http_port, body=_body(False),
        )
        _ok(st, resp, "SetIPFilter")

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

        st, resp = await cgi_request(
            self.ip, _IPFILTER_PATH, self.username, self.password,
            method="PUT", port=self.http_port, body=_body(True),
        )
        _ok(st, resp, "EnableIPFilter")
        after = await self.get_security()
        if not after.ip_filter_supported:
            raise HTTPException(
                status_code=502,
                detail="Lost access to the camera after enabling the IP filter",
            )
        return after
