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
OnvifDriver — the universal, any-brand baseline.

Built entirely on the hand-rolled SOAP-over-httpx-Digest primitive in
``services.onvif_digest_service`` (NOT the optional onvif-zeep library), so it
works on any device that speaks ONVIF regardless of whether zeep is installed.
Read-only for Phase 0 (info, capabilities, network, storage). Response shapes
were verified against a real Hikvision device_service.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import UTC, datetime

from fastapi import HTTPException

from services import onvif_digest_service as ods

from ..base import (
    CameraDriver,
    Capabilities,
    DeviceInfo,
    EncoderInfo,
    ImagingInfo,
    MotionInfo,
    NetworkInfo,
    OsdInfo,
    SmartDetector,
    SmartInfo,
    StorageInfo,
    StorageSlot,
    TimeInfo,
    UsersInfo,
)


def _esc(s: str) -> str:
    """Minimal XML-escape for values interpolated into SOAP bodies."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _check_ok(status: int, text: str, op: str) -> None:
    """Raise if a SOAP write didn't succeed (non-200 or a SOAP Fault)."""
    if status == 200 and "Fault>" not in text:
        return
    reason = _first(r"<[^>]*Text[^>]*>([^<]+)</[^>]*Text>", text)
    raise HTTPException(status_code=502, detail=f"{op} failed: {reason or text[:200]}")


def _nested_mode(block: str, text: str) -> str | None:
    """The <tt:Mode> value inside a named imaging block (WDR/Backlight/…)."""
    m = re.search(rf"<tt:{block}>(.*?)</tt:{block}>", text, re.DOTALL)
    if not m:
        return None
    mm = re.search(r"<tt:Mode>([^<]+)</tt:Mode>", m.group(1))
    return mm.group(1) if mm else None


def _block_modes(block: str, text: str) -> list[str]:
    """All <tt:Mode> options inside a named block (from GetOptions)."""
    m = re.search(rf"<tt:{block}>(.*?)</tt:{block}>", text, re.DOTALL)
    return re.findall(r"<tt:Mode>([^<]+)</tt:Mode>", m.group(1)) if m else []


def _range(tag: str, text: str) -> dict | None:
    m = re.search(
        rf"<tt:{tag}>\s*<tt:Min>(-?\d+)</tt:Min>\s*<tt:Max>(-?\d+)</tt:Max>",
        text,
        re.DOTALL,
    )
    return {"min": int(m.group(1)), "max": int(m.group(2))} if m else None


def _parse_dt_block(text: str, tag: str) -> str | None:
    """Extract an ISO datetime from an ONVIF <tt:UTCDateTime>/<tt:LocalDateTime>."""
    m = re.search(rf"<tt:{tag}>(.*?)</tt:{tag}>", text, re.DOTALL)
    if not m:
        return None
    b = m.group(1)

    def g(t: str) -> int | None:
        mm = re.search(rf"<tt:{t}>(\d+)</tt:{t}>", b)
        return int(mm.group(1)) if mm else None

    y, mo, d, h, mi, s = (g("Year"), g("Month"), g("Day"),
                          g("Hour"), g("Minute"), g("Second"))
    if None in (y, mo, d, h, mi, s):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}"


def _prefix_to_netmask(prefix: int) -> str | None:
    """Convert an IPv4 CIDR prefix length (e.g. 24) to a dotted netmask."""
    if prefix is None or not (0 <= prefix <= 32):
        return None
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF if prefix else 0
    return ".".join(str((mask >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def _first(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None


class OnvifDriver(CameraDriver):
    driver_name = "onvif"

    @property
    def _device_url(self) -> str:
        return f"{self.scheme}://{self.ip}:{self.http_port}/onvif/device_service"

    # --- identity ---

    async def get_info(self) -> DeviceInfo:
        info = await ods.get_device_info(
            self.ip, self.username, self.password, self.http_port, self.scheme
        )
        return DeviceInfo(
            manufacturer=info.get("manufacturer"),
            model=info.get("model"),
            firmware_version=info.get("firmwareversion"),
            serial_number=info.get("serialnumber"),
            hardware_id=info.get("hardwareid"),
        )

    # --- capabilities ---

    async def get_capabilities(self) -> Capabilities:
        """Discover what the device advertises via ONVIF GetServices and turn
        the corresponding capability areas on.

        Unlike the old static baseline, this is dynamic: the same reader methods
        that the UI calls (get_osd via Media2, get_motion/get_smart via
        Analytics, subscribe_events via the Events service, get_users/reboot via
        the Device service) are backed here, so advertising an area does NOT
        surface a tab that errors — every reader degrades to ``supported=False``
        data when the device turns out not to implement it. This is what makes
        "dig out everything" work on ANY ONVIF brand, not just ones with a
        native driver.
        """
        # Populates ``self._caps_raw`` (the GetServices map) without a 2nd call.
        with contextlib.suppress(Exception):
            await self._service_urls()
        xaddrs = getattr(self, "_caps_raw", {}) or {}
        has = lambda k: bool(xaddrs.get(k))  # noqa: E731
        # Advertisement-based gating: an area is on when the device advertises
        # the ONVIF service that backs it (and this driver implements a reader
        # for it). This is a single cheap GetServices call. Each reader degrades
        # to supported=False data — never raises — so on the rare device that
        # advertises a service but doesn't really populate it, the tab shows the
        # frontend's per-area "not available" empty state rather than erroring.
        supported = {
            "info": True,
            "network": True,  # device service — read-only display
            "time": True,  # device service GetSystemDateAndTime
            "imaging": has("imaging") or has("media"),
            "encoder": has("media") or has("media2"),
            "ptz": has("ptz"),
            "osd": has("media2"),  # OSD get/set via Media2
            "events": has("events"),  # pull-point subscribe
            "motion": has("analytics"),  # Analytics cell-motion
            "smart": has("analytics"),  # Analytics rules (read-only over ONVIF)
            "storage": has("recording") or has("search"),  # on-camera recordings
            # SystemReboot is a universal ONVIF device operation.
            "maintenance": True,
            # ONVIF GetUsers support is genuinely inconsistent across brands, so
            # the pure baseline leaves Users off; native drivers that verify it
            # (Hikvision/Xiongmai/…) turn it on.
            "users": False,
            "audio": False,  # detected in a later phase
        }
        return Capabilities(
            driver_name=self.driver_name,
            supported_areas=supported,
            onvif_endpoints=xaddrs,
            detail={"native_api": "onvif"},
        )

    # --- network (read only) ---

    async def get_network(self) -> NetworkInfo:
        net = NetworkInfo(supported=True, source="onvif")
        try:
            _, iface = await ods._onvif_request(
                self._device_url,
                "<tds:GetNetworkInterfaces/>",
                self.username,
                self.password,
            )
            net.mac_address = _first(r"<tt:HwAddress>([^<]+)</tt:HwAddress>", iface)
            mtu = _first(r"<tt:MTU>(\d+)</tt:MTU>", iface)
            net.mtu = int(mtu) if mtu else None
            net.ip_address = _first(r"<tt:Address>([0-9.]+)</tt:Address>", iface)
            prefix = _first(r"<tt:PrefixLength>(\d+)</tt:PrefixLength>", iface)
            if prefix:
                net.subnet_mask = _prefix_to_netmask(int(prefix))
            dhcp = _first(r"<tt:DHCP>(true|false)</tt:DHCP>", iface)
            net.dhcp = (dhcp == "true") if dhcp else None
        except Exception:
            return NetworkInfo(supported=False, source="onvif")

        try:
            _, gw = await ods._onvif_request(
                self._device_url,
                "<tds:GetNetworkDefaultGateway/>",
                self.username,
                self.password,
            )
            net.gateway = _first(r"<tt:IPv4Address>([0-9.]+)</tt:IPv4Address>", gw)
        except Exception:
            pass

        try:
            _, dns = await ods._onvif_request(
                self._device_url, "<tds:GetDNS/>", self.username, self.password
            )
            servers = re.findall(r"<tt:IPv4Address>([0-9.]+)</tt:IPv4Address>", dns)
            servers = [s for s in servers if s and s != "0.0.0.0"]
            net.dns_primary = servers[0] if servers else None
            net.dns_secondary = servers[1] if len(servers) > 1 else None
        except Exception:
            pass

        return net

    # (storage is implemented below via the ONVIF Recording service)

    # --- time / NTP (read + write) ---

    async def get_time(self) -> TimeInfo:
        _, sdt = await ods._onvif_request(
            self._device_url,
            "<tds:GetSystemDateAndTime/>",
            self.username,
            self.password,
        )
        info = TimeInfo(supported=True)
        dtype = _first(r"<tt:DateTimeType>([^<]+)</tt:DateTimeType>", sdt)
        info.mode = "ntp" if (dtype or "").upper() == "NTP" else "manual"
        info.timezone = _first(r"<tt:TZ>([^<]+)</tt:TZ>", sdt)
        dls = _first(r"<tt:DaylightSavings>(true|false)</tt:DaylightSavings>", sdt)
        info.daylight_savings = (dls == "true") if dls else None
        info.utc_datetime = _parse_dt_block(sdt, "UTCDateTime")
        info.local_datetime = _parse_dt_block(sdt, "LocalDateTime")
        try:
            _, ntp = await ods._onvif_request(
                self._device_url, "<tds:GetNTP/>", self.username, self.password
            )
            fd = _first(r"<tt:FromDHCP>(true|false)</tt:FromDHCP>", ntp)
            info.ntp_from_dhcp = (fd == "true") if fd else None
            info.ntp_server = _first(
                r"<tt:DNSname>([^<]+)</tt:DNSname>", ntp
            ) or _first(r"<tt:IPv4Address>([0-9.]+)</tt:IPv4Address>", ntp)
        except Exception:
            pass
        return info

    def _tz_block(self, timezone: str | None) -> str:
        return (
            f"<tds:TimeZone><tt:TZ>{_esc(timezone)}</tt:TZ></tds:TimeZone>"
            if timezone
            else ""
        )

    async def set_time_manual(self, timezone: str | None = None) -> TimeInfo:
        """Push the server's current UTC to the camera (DateTimeType=Manual).
        Optionally set the display timezone. Never changes IP/network."""
        now = datetime.now(UTC)
        body = (
            "<tds:SetSystemDateAndTime>"
            "<tds:DateTimeType>Manual</tds:DateTimeType>"
            "<tds:DaylightSavings>false</tds:DaylightSavings>"
            f"{self._tz_block(timezone)}"
            "<tds:UTCDateTime>"
            f"<tt:Time><tt:Hour>{now.hour}</tt:Hour>"
            f"<tt:Minute>{now.minute}</tt:Minute>"
            f"<tt:Second>{now.second}</tt:Second></tt:Time>"
            f"<tt:Date><tt:Year>{now.year}</tt:Year>"
            f"<tt:Month>{now.month}</tt:Month>"
            f"<tt:Day>{now.day}</tt:Day></tt:Date>"
            "</tds:UTCDateTime>"
            "</tds:SetSystemDateAndTime>"
        )
        status, text = await ods._onvif_request(
            self._device_url, body, self.username, self.password
        )
        _check_ok(status, text, "SetSystemDateAndTime")
        return await self.get_time()

    async def set_ntp(self, server: str, timezone: str | None = None) -> TimeInfo:
        """Point the camera at an NTP server and switch it to NTP time."""
        server = (server or "").strip()
        if not server:
            raise HTTPException(status_code=400, detail="NTP server is required")
        is_ip = bool(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", server))
        addr = (
            f"<tt:Type>IPv4</tt:Type><tt:IPv4Address>{_esc(server)}</tt:IPv4Address>"
            if is_ip
            else f"<tt:Type>DNS</tt:Type><tt:DNSname>{_esc(server)}</tt:DNSname>"
        )
        set_ntp = (
            "<tds:SetNTP><tds:FromDHCP>false</tds:FromDHCP>"
            f"<tds:NTPManual>{addr}</tds:NTPManual></tds:SetNTP>"
        )
        st, tx = await ods._onvif_request(
            self._device_url, set_ntp, self.username, self.password
        )
        _check_ok(st, tx, "SetNTP")

        set_dt = (
            "<tds:SetSystemDateAndTime>"
            "<tds:DateTimeType>NTP</tds:DateTimeType>"
            "<tds:DaylightSavings>false</tds:DaylightSavings>"
            f"{self._tz_block(timezone)}</tds:SetSystemDateAndTime>"
        )
        st2, tx2 = await ods._onvif_request(
            self._device_url, set_dt, self.username, self.password
        )
        _check_ok(st2, tx2, "SetSystemDateAndTime(NTP)")
        return await self.get_time()

    # --- imaging (read + write) ---

    _IMG_NUMERIC = ("brightness", "contrast", "saturation", "sharpness")

    async def _service_urls(self) -> dict[str, str]:
        """Resolve (and cache) the ONVIF service XAddrs the driver drives.

        Also caches the raw GetServices map in ``self._caps_raw`` so
        ``get_capabilities`` can gate areas without a second network call.
        """
        if getattr(self, "_svc", None) is None:
            try:
                caps = await ods.get_services(
                    self.ip, self.username, self.password, self.http_port, self.scheme
                )
            except Exception:
                caps = {}
            self._caps_raw = caps
            base = f"{self.scheme}://{self.ip}:{self.http_port}/onvif"
            self._svc = {
                "media": caps.get("media") or f"{base}/Media",
                "media2": caps.get("media2") or f"{base}/media2_service",
                "imaging": caps.get("imaging") or f"{base}/Imaging",
                "events": caps.get("events") or f"{base}/event_service",
                "analytics": caps.get("analytics") or f"{base}/analytics_service",
                # No portable default — only present if the camera advertises it.
                "recording": caps.get("recording"),
            }
        return self._svc

    async def _video_source_token(self) -> str:
        if getattr(self, "_vst", None):
            return self._vst
        urls = await self._service_urls()
        _, xml = await ods._onvif_request(
            urls["media"], "<trt:GetVideoSources/>", self.username, self.password
        )
        m = re.search(r'VideoSources token="([^"]+)"', xml)
        self._vst = m.group(1) if m else "VideoSource_1"
        return self._vst

    async def get_imaging(self) -> ImagingInfo:
        urls = await self._service_urls()
        token = await self._video_source_token()
        vst = f"<timg:VideoSourceToken>{token}</timg:VideoSourceToken>"
        _, cur = await ods._onvif_request(
            urls["imaging"],
            f"<timg:GetImagingSettings>{vst}</timg:GetImagingSettings>",
            self.username,
            self.password,
        )
        _, opt = await ods._onvif_request(
            urls["imaging"],
            f"<timg:GetOptions>{vst}</timg:GetOptions>",
            self.username,
            self.password,
        )

        def num(tag: str) -> int | None:
            v = _first(rf"<tt:{tag}>(\d+)</tt:{tag}>", cur)
            return int(v) if v is not None else None

        settings = {
            "brightness": num("Brightness"),
            "contrast": num("Contrast"),
            "saturation": num("ColorSaturation"),
            "sharpness": num("Sharpness"),
            "ir_cut_filter": _first(r"<tt:IrCutFilter>([^<]+)</tt:IrCutFilter>", cur),
            "wdr": _nested_mode("WideDynamicRange", cur),
            "backlight": _nested_mode("BacklightCompensation", cur),
        }
        ranges = {
            "brightness": _range("Brightness", opt),
            "contrast": _range("Contrast", opt),
            "saturation": _range("ColorSaturation", opt),
            "sharpness": _range("Sharpness", opt),
            "ir_cut_filter": {
                "options": re.findall(
                    r"<tt:IrCutFilterModes>([^<]+)</tt:IrCutFilterModes>", opt
                )
            },
            "wdr": {"options": _block_modes("WideDynamicRange", opt)},
            "backlight": {"options": _block_modes("BacklightCompensation", opt)},
        }
        settings = {k: v for k, v in settings.items() if v is not None}
        ranges = {k: v for k, v in ranges.items() if v and v != {"options": []}}
        return ImagingInfo(
            supported=bool(settings), settings=settings, ranges=ranges, source="onvif"
        )

    async def set_imaging(self, patch: dict) -> ImagingInfo:
        cur = await self.get_imaging()
        if not cur.supported:
            raise HTTPException(
                status_code=502, detail="Imaging not available on this device"
            )
        s = dict(cur.settings)
        for k, v in patch.items():
            if v is None or k not in s:
                continue
            if k in self._IMG_NUMERIC:
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
                r = cur.ranges.get(k) or {}
                v = max(r.get("min", 0), min(r.get("max", 100), v))
            s[k] = v

        # Canonical ONVIF ImagingSettings element order (Exposure/Focus/
        # WhiteBalance omitted — left as the camera has them).
        parts: list[str] = []
        if "backlight" in s:
            parts.append(
                f"<tt:BacklightCompensation><tt:Mode>{s['backlight']}</tt:Mode>"
                "</tt:BacklightCompensation>"
            )
        if "brightness" in s:
            parts.append(f"<tt:Brightness>{s['brightness']}</tt:Brightness>")
        if "saturation" in s:
            parts.append(f"<tt:ColorSaturation>{s['saturation']}</tt:ColorSaturation>")
        if "contrast" in s:
            parts.append(f"<tt:Contrast>{s['contrast']}</tt:Contrast>")
        if "ir_cut_filter" in s:
            parts.append(f"<tt:IrCutFilter>{s['ir_cut_filter']}</tt:IrCutFilter>")
        if "sharpness" in s:
            parts.append(f"<tt:Sharpness>{s['sharpness']}</tt:Sharpness>")
        if "wdr" in s:
            parts.append(
                f"<tt:WideDynamicRange><tt:Mode>{s['wdr']}</tt:Mode>"
                "</tt:WideDynamicRange>"
            )

        urls = await self._service_urls()
        token = await self._video_source_token()
        body = (
            "<timg:SetImagingSettings>"
            f"<timg:VideoSourceToken>{token}</timg:VideoSourceToken>"
            f"<timg:ImagingSettings>{''.join(parts)}</timg:ImagingSettings>"
            "<timg:ForcePersistence>true</timg:ForcePersistence>"
            "</timg:SetImagingSettings>"
        )
        st, tx = await ods._onvif_request(
            urls["imaging"], body, self.username, self.password
        )
        _check_ok(st, tx, "SetImagingSettings")
        return await self.get_imaging()

    # --- video encoder (read + write; MediaMTX reconcile is the router's job) ---

    async def _encoder_options(self, media_url: str, token: str) -> dict:
        _, opt = await ods._onvif_request(
            media_url,
            "<trt:GetVideoEncoderConfigurationOptions><trt:ConfigurationToken>"
            f"{token}</trt:ConfigurationToken></trt:GetVideoEncoderConfigurationOptions>",
            self.username,
            self.password,
        )
        resolutions: list[dict] = []
        seen: set[tuple[int, int]] = set()
        for rm in re.finditer(
            r"<tt:ResolutionsAvailable>\s*<tt:Width>(\d+)</tt:Width>\s*"
            r"<tt:Height>(\d+)</tt:Height>",
            opt,
            re.DOTALL,
        ):
            wh = (int(rm.group(1)), int(rm.group(2)))
            if wh not in seen:
                seen.add(wh)
                resolutions.append({"width": wh[0], "height": wh[1]})
        encodings = [e for e in ("H264", "H265", "MPEG4", "JPEG") if f"<tt:{e}>" in opt]
        return {
            "resolutions": resolutions,
            "fps_range": _range("FrameRateRange", opt),
            "gov_range": _range("GovLengthRange", opt),
            "bitrate_range": _range("BitrateRange", opt),
            "encodings": encodings or None,
        }

    async def get_encoder(self) -> EncoderInfo:
        urls = await self._service_urls()
        _, cfgs = await ods._onvif_request(
            urls["media"],
            "<trt:GetVideoEncoderConfigurations/>",
            self.username,
            self.password,
        )
        configs: list[dict] = []
        for m in re.finditer(
            r'<trt:Configurations token="([^"]+)"[^>]*>(.*?)</trt:Configurations>',
            cfgs,
            re.DOTALL,
        ):
            token, block = m.group(1), m.group(2)
            res = re.search(
                r"<tt:Resolution>\s*<tt:Width>(\d+)</tt:Width>\s*"
                r"<tt:Height>(\d+)</tt:Height>",
                block,
                re.DOTALL,
            )

            def n(tag: str) -> int | None:
                v = _first(rf"<tt:{tag}>(\d+)</tt:{tag}>", block)
                return int(v) if v is not None else None

            cfg = {
                "token": token,
                "name": _first(r"<tt:Name>([^<]+)</tt:Name>", block),
                "encoding": _first(r"<tt:Encoding>([^<]+)</tt:Encoding>", block),
                "width": int(res.group(1)) if res else None,
                "height": int(res.group(2)) if res else None,
                "fps": n("FrameRateLimit"),
                "bitrate": n("BitrateLimit"),
                "gov_length": n("GovLength"),
                "quality": _first(r"<tt:Quality>([\d.]+)</tt:Quality>", block),
                "options": await self._encoder_options(urls["media"], token),
            }
            configs.append(cfg)
        return EncoderInfo(supported=bool(configs), configs=configs, source="onvif")

    async def set_encoder(self, token: str, patch: dict) -> EncoderInfo:
        """Apply encoder changes by mutating the exact config XML the camera
        returned (safer than reconstructing it), then re-persist. The RTSP URL
        of a profile does not change for a resolution/bitrate/fps edit, so the
        router only needs a MediaMTX path bounce (not a re-provision)."""
        urls = await self._service_urls()
        _, raw = await ods._onvif_request(
            urls["media"],
            "<trt:GetVideoEncoderConfiguration><trt:ConfigurationToken>"
            f"{token}</trt:ConfigurationToken></trt:GetVideoEncoderConfiguration>",
            self.username,
            self.password,
        )
        m = re.search(
            r'(<trt:Configuration token="[^"]+"[^>]*>.*?</trt:Configuration>)',
            raw,
            re.DOTALL,
        )
        if not m:
            raise HTTPException(
                status_code=404, detail=f"Encoder configuration {token} not found"
            )
        block = m.group(1)
        if patch.get("width") and patch.get("height"):
            block = re.sub(
                r"<tt:Width>\d+</tt:Width>",
                f"<tt:Width>{int(patch['width'])}</tt:Width>",
                block,
                count=1,
            )
            block = re.sub(
                r"<tt:Height>\d+</tt:Height>",
                f"<tt:Height>{int(patch['height'])}</tt:Height>",
                block,
                count=1,
            )
        if patch.get("fps"):
            block = re.sub(
                r"<tt:FrameRateLimit>\d+</tt:FrameRateLimit>",
                f"<tt:FrameRateLimit>{int(patch['fps'])}</tt:FrameRateLimit>",
                block,
            )
        if patch.get("bitrate"):
            block = re.sub(
                r"<tt:BitrateLimit>\d+</tt:BitrateLimit>",
                f"<tt:BitrateLimit>{int(patch['bitrate'])}</tt:BitrateLimit>",
                block,
            )
        if patch.get("gov_length"):
            block = re.sub(
                r"<tt:GovLength>\d+</tt:GovLength>",
                f"<tt:GovLength>{int(patch['gov_length'])}</tt:GovLength>",
                block,
            )
        body = (
            f"<trt:SetVideoEncoderConfiguration>{block}"
            "<trt:ForcePersistence>true</trt:ForcePersistence>"
            "</trt:SetVideoEncoderConfiguration>"
        )
        st, tx = await ods._onvif_request(
            urls["media"], body, self.username, self.password
        )
        _check_ok(st, tx, "SetVideoEncoderConfiguration")
        return await self.get_encoder()

    # --- OSD via Media2 (read + write) -------------------------------------

    _MEDIA2_NS = "http://www.onvif.org/ver20/media/wsdl"

    async def _get_osds_raw(self) -> tuple[int, str]:
        urls = await self._service_urls()
        return await ods._onvif_request(
            urls["media2"],
            f'<tr2:GetOSDs xmlns:tr2="{self._MEDIA2_NS}"/>',
            self.username,
            self.password,
        )

    async def get_osd(self) -> OsdInfo:
        try:
            st, xml = await self._get_osds_raw()
        except Exception:
            return OsdInfo(supported=False, source="onvif")
        if st != 200 or "OSD" not in xml:
            return OsdInfo(supported=False, source="onvif")
        info = OsdInfo(supported=True, source="onvif")
        slots: list[dict] = []
        for m in re.finditer(
            r'<(?:\w+:)?OSDs\b([^>]*)>(.*?)</(?:\w+:)?OSDs>', xml, re.DOTALL
        ):
            attrs, block = m.group(1), m.group(2)
            tok = re.search(r'token="([^"]+)"', attrs)
            ts = re.search(
                r'<(?:\w+:)?TextString\b[^>]*>(.*?)</(?:\w+:)?TextString>',
                block,
                re.DOTALL,
            )
            text_type = plain = None
            if ts:
                text_type = _first(
                    r'<(?:\w+:)?Type>([^<]+)</(?:\w+:)?Type>', ts.group(1)
                )
                plain = _first(
                    r'<(?:\w+:)?PlainText>([^<]*)</(?:\w+:)?PlainText>', ts.group(1)
                )
            pos = re.search(r'x="(-?[0-9.]+)"\s+y="(-?[0-9.]+)"', block)
            is_dt = (text_type or "").lower() in ("date", "time", "dateandtime")
            if is_dt:
                info.datetime_enabled = True
            elif (text_type or "").lower() == "plain":
                info.text_enabled = True
                if info.text is None:
                    info.text = plain
            slots.append({
                "id": tok.group(1) if tok else None,
                "enabled": True,  # an OSD that exists is active (ONVIF has no flag)
                "text": None if is_dt else plain,
                "x": float(pos.group(1)) if pos else None,
                "y": float(pos.group(2)) if pos else None,
                "type": text_type,
            })
        info.slots = slots
        return info

    async def set_osd(self, patch: dict) -> OsdInfo:
        """Update a text OSD's PlainText. Echoes the camera's own OSD element
        back via Media2 SetOSD (safest — we don't reconstruct it)."""
        urls = await self._service_urls()
        st, xml = await self._get_osds_raw()
        if st != 200:
            raise HTTPException(status_code=502, detail="OSD not available on this device")
        target_id = patch.get("slot_id") or patch.get("id")
        new_text = patch.get("text")
        chosen = None
        for m in re.finditer(
            r'(<(?:\w+:)?OSDs\b[^>]*>.*?</(?:\w+:)?OSDs>)', xml, re.DOTALL
        ):
            b = m.group(1)
            tok = re.search(r'token="([^"]+)"', b)
            tid = tok.group(1) if tok else None
            if target_id and tid == target_id:
                chosen = b
                break
            if not target_id and "PlainText" in b:
                chosen = b
                break
        if not chosen:
            raise HTTPException(status_code=404, detail="OSD slot not found")
        if new_text is not None:
            chosen = re.sub(
                r'(<(?:\w+:)?PlainText>)[^<]*(</(?:\w+:)?PlainText>)',
                lambda mm: mm.group(1) + _esc(str(new_text)) + mm.group(2),
                chosen,
                count=1,
            )
        # Re-tag the outer <...:OSDs> element as <tr2:OSD> for SetOSD.
        inner = re.sub(r'^<(?:\w+:)?OSDs\b', '<tr2:OSD', chosen)
        inner = re.sub(r'</(?:\w+:)?OSDs>$', '</tr2:OSD>', inner)
        body = f'<tr2:SetOSD xmlns:tr2="{self._MEDIA2_NS}">{inner}</tr2:SetOSD>'
        st2, tx2 = await ods._onvif_request(
            urls["media2"], body, self.username, self.password
        )
        _check_ok(st2, tx2, "SetOSD")
        return await self.get_osd()

    # --- motion + smart via Analytics (read; motion write) -----------------

    _ANALYTICS_NS = "http://www.onvif.org/ver20/analytics/wsdl"

    async def _analytics_config_token(self) -> str | None:
        if getattr(self, "_ana_tok", "unset") != "unset":
            return self._ana_tok
        urls = await self._service_urls()
        try:
            _, xml = await ods._onvif_request(
                urls["media"],
                "<trt:GetVideoAnalyticsConfigurations/>",
                self.username,
                self.password,
            )
            m = re.search(r'<(?:\w+:)?Configurations\b[^>]*token="([^"]+)"', xml)
            self._ana_tok = m.group(1) if m else None
        except Exception:
            self._ana_tok = None
        return self._ana_tok

    async def get_motion(self) -> MotionInfo:
        token = await self._analytics_config_token()
        if not token:
            return MotionInfo(supported=False, source="onvif")
        urls = await self._service_urls()
        body = (
            f'<tan:GetAnalyticsModules xmlns:tan="{self._ANALYTICS_NS}">'
            f"<tan:ConfigurationToken>{token}</tan:ConfigurationToken>"
            "</tan:GetAnalyticsModules>"
        )
        try:
            st, xml = await ods._onvif_request(
                urls["analytics"], body, self.username, self.password
            )
        except Exception:
            return MotionInfo(supported=False, source="onvif")
        if st != 200:
            return MotionInfo(supported=False, source="onvif")
        cell = None
        for mm in re.finditer(
            r'<(?:\w+:)?AnalyticsModule\b([^>]*)>(.*?)</(?:\w+:)?AnalyticsModule>',
            xml,
            re.DOTALL,
        ):
            if "Motion" in mm.group(1) or "Cell" in mm.group(1):
                cell = mm.group(2)
                break
        if cell is None:
            return MotionInfo(supported=False, source="onvif")
        info = MotionInfo(supported=True, enabled=True, source="onvif")
        sens = re.search(r'Name="Sensitivity"[^>]*Value="(\d+)"', cell)
        if sens:
            info.sensitivity = int(sens.group(1))
        return info

    async def set_motion(self, patch: dict) -> MotionInfo:
        """Best-effort: update the cell-motion Sensitivity via
        ModifyAnalyticsModules (echoing back the camera's own module element)."""
        token = await self._analytics_config_token()
        urls = await self._service_urls()
        if not token:
            raise HTTPException(status_code=502, detail="Motion not available on this device")
        body = (
            f'<tan:GetAnalyticsModules xmlns:tan="{self._ANALYTICS_NS}">'
            f"<tan:ConfigurationToken>{token}</tan:ConfigurationToken>"
            "</tan:GetAnalyticsModules>"
        )
        _, xml = await ods._onvif_request(
            urls["analytics"], body, self.username, self.password
        )
        m = re.search(
            r'(<(?:\w+:)?AnalyticsModule\b[^>]*(?:Motion|Cell)[^>]*>.*?</(?:\w+:)?AnalyticsModule>)',
            xml,
            re.DOTALL,
        )
        if not m:
            raise HTTPException(status_code=502, detail="No cell-motion module to modify")
        module = m.group(1)
        sens = patch.get("sensitivity")
        if sens is not None:
            sens = max(0, min(100, int(sens)))
            if re.search(r'Name="Sensitivity"', module):
                module = re.sub(
                    r'(Name="Sensitivity"[^>]*Value=")\d+(")',
                    lambda mm: mm.group(1) + str(sens) + mm.group(2),
                    module,
                    count=1,
                )
        module = re.sub(r'^<(?:\w+:)?AnalyticsModule\b', '<tan:AnalyticsModule', module)
        module = re.sub(r'</(?:\w+:)?AnalyticsModule>$', '</tan:AnalyticsModule>', module)
        mod_body = (
            f'<tan:ModifyAnalyticsModules xmlns:tan="{self._ANALYTICS_NS}">'
            f"<tan:ConfigurationToken>{token}</tan:ConfigurationToken>"
            f"<tan:AnalyticsModule>{module}</tan:AnalyticsModule>"
            "</tan:ModifyAnalyticsModules>"
        )
        st2, tx2 = await ods._onvif_request(
            urls["analytics"], mod_body, self.username, self.password
        )
        _check_ok(st2, tx2, "ModifyAnalyticsModules")
        return await self.get_motion()

    async def get_smart(self) -> SmartInfo:
        """Read analytics rules (line crossing, field/intrusion, tamper, …) as a
        read-only detector list over generic ONVIF. Native drivers override this
        with per-vendor enable/sensitivity control."""
        token = await self._analytics_config_token()
        if not token:
            return SmartInfo(supported=False, source="onvif")
        urls = await self._service_urls()
        body = (
            f'<tan:GetRules xmlns:tan="{self._ANALYTICS_NS}">'
            f"<tan:ConfigurationToken>{token}</tan:ConfigurationToken>"
            "</tan:GetRules>"
        )
        try:
            st, xml = await ods._onvif_request(
                urls["analytics"], body, self.username, self.password
            )
        except Exception:
            return SmartInfo(supported=False, source="onvif")
        if st != 200:
            return SmartInfo(supported=False, source="onvif")
        detectors: list[SmartDetector] = []
        for m in re.finditer(r'<(?:\w+:)?Rule\b([^>]*)>(.*?)</(?:\w+:)?Rule>', xml, re.DOTALL):
            attrs, block = m.group(1), m.group(2)
            name = _first(r'Name="([^"]+)"', attrs)
            rtype = _first(r'Type="([^"]+)"', attrs) or ""
            rtype = rtype.split(":")[-1]
            configured = bool(re.search(r'<(?:\w+:)?PolygonConfiguration|<(?:\w+:)?Polyline', block))
            detectors.append(
                SmartDetector(
                    key=name or rtype,
                    label=rtype or (name or "Rule"),
                    supported=True,
                    enabled=True,
                    configured=configured or None,
                )
            )
        return SmartInfo(supported=bool(detectors), detectors=detectors, source="onvif")

    async def set_smart(self, key: str, patch: dict) -> SmartInfo:
        # Generic ONVIF analytics rules are not portably writable (enable/region
        # semantics vary per vendor); native drivers override this. Kept explicit
        # so the API returns a clear message rather than a 500.
        raise HTTPException(
            status_code=501,
            detail=(
                "Changing smart-detection rules isn't supported over generic "
                "ONVIF for this camera — use the camera's own web UI."
            ),
        )

    # --- storage via Recording service (read only) -------------------------

    async def get_storage(self) -> StorageInfo:
        urls = await self._service_urls()
        rec_url = urls.get("recording")
        if not rec_url:
            return StorageInfo(supported=False, source="onvif")
        try:
            st, xml = await ods._onvif_request(
                rec_url,
                '<trec:GetRecordings xmlns:trec="http://www.onvif.org/ver10/recording/wsdl"/>',
                self.username,
                self.password,
            )
        except Exception:
            return StorageInfo(supported=False, source="onvif")
        if st != 200:
            return StorageInfo(supported=False, source="onvif")
        present = "RecordingToken" in xml or "Recordings" in xml
        return StorageInfo(
            supported=True,
            present=present,
            slots=[StorageSlot(name="onvif-recording", status="ok" if present else "empty")],
            source="onvif",
        )

    # --- users + reboot via Device service ---------------------------------

    async def get_users(self) -> UsersInfo:
        try:
            _, xml = await ods._onvif_request(
                self._device_url, "<tds:GetUsers/>", self.username, self.password
            )
        except Exception:
            return UsersInfo(supported=False, source="onvif")
        users: list[dict] = []
        for m in re.finditer(r'<(?:\w+:)?User\b[^>]*>(.*?)</(?:\w+:)?User>', xml, re.DOTALL):
            block = m.group(1)
            name = _first(r'<(?:\w+:)?Username>([^<]+)</(?:\w+:)?Username>', block)
            level = _first(r'<(?:\w+:)?UserLevel>([^<]+)</(?:\w+:)?UserLevel>', block)
            if not name:
                continue
            users.append({
                "id": name,
                "name": name,
                "level": level,
                "is_current": name == self.username,
            })
        return UsersInfo(supported=bool(users), users=users, source="onvif")

    async def create_user(self, username: str, password: str, level: str) -> UsersInfo:
        username = (username or "").strip()
        if not username:
            raise HTTPException(status_code=400, detail="Username is required")
        lvl = (level or "User").capitalize()
        if lvl not in ("Administrator", "Operator", "User"):
            lvl = "User"
        body = (
            "<tds:CreateUsers><tds:User>"
            f"<tt:Username>{_esc(username)}</tt:Username>"
            f"<tt:Password>{_esc(password or '')}</tt:Password>"
            f"<tt:UserLevel>{lvl}</tt:UserLevel>"
            "</tds:User></tds:CreateUsers>"
        )
        st, tx = await ods._onvif_request(
            self._device_url, body, self.username, self.password
        )
        _check_ok(st, tx, "CreateUsers")
        return await self.get_users()

    async def delete_user(self, username: str) -> UsersInfo:
        username = (username or "").strip()
        # Never delete the account OpenNVR authenticates with — that locks us out.
        if username and username == self.username:
            raise HTTPException(
                status_code=400,
                detail="Refusing to delete the account OpenNVR uses to reach this camera",
            )
        body = (
            "<tds:DeleteUsers>"
            f"<tt:Username>{_esc(username)}</tt:Username>"
            "</tds:DeleteUsers>"
        )
        st, tx = await ods._onvif_request(
            self._device_url, body, self.username, self.password
        )
        _check_ok(st, tx, "DeleteUsers")
        return await self.get_users()

    async def reboot(self) -> dict:
        st, tx = await ods._onvif_request(
            self._device_url, "<tds:SystemReboot/>", self.username, self.password
        )
        _check_ok(st, tx, "SystemReboot")
        return {"status": "rebooting"}

    # --- events via ONVIF pull-point subscription --------------------------

    _EVENTS_NS = "http://www.onvif.org/ver10/events/wsdl"

    @staticmethod
    def _parse_onvif_events(xml: str):
        """Yield normalized event dicts from a PullMessages response."""
        for nm in re.finditer(
            r'<(?:\w+:)?NotificationMessage\b.*?</(?:\w+:)?NotificationMessage>',
            xml,
            re.DOTALL,
        ):
            block = nm.group(0)
            topic = _first(r'<(?:\w+:)?Topic\b[^>]*>([^<]+)</(?:\w+:)?Topic>', block)
            val = re.search(
                r'<(?:\w+:)?SimpleItem\b[^>]*'
                r'Name="(?:State|IsMotion|IsInside|Motion|active|LogicalState)"'
                r'[^>]*Value="(true|false)"',
                block,
                re.IGNORECASE,
            ) or re.search(r'Value="(true|false)"', block)
            state = "active" if (val and val.group(1).lower() == "true") else "inactive"
            etype = (topic or "event").split("/")[-1].split(":")[-1] or "event"
            yield {
                "event_type": etype,
                "event_state": state,
                "description": topic,
            }

    async def subscribe_events(self):
        """Async generator of normalized events via ONVIF pull-point.

        CreatePullPointSubscription → poll PullMessages, re-subscribing on error.
        Works on any ONVIF camera that advertises the Events service.
        """
        urls = await self._service_urls()
        ev_url = urls["events"]
        create = (
            f'<tev:CreatePullPointSubscription xmlns:tev="{self._EVENTS_NS}">'
            "<tev:InitialTerminationTime>PT120S</tev:InitialTerminationTime>"
            "</tev:CreatePullPointSubscription>"
        )
        pull = (
            f'<tev:PullMessages xmlns:tev="{self._EVENTS_NS}">'
            "<tev:Timeout>PT30S</tev:Timeout>"
            "<tev:MessageLimit>20</tev:MessageLimit>"
            "</tev:PullMessages>"
        )
        backoff = 5
        while True:
            try:
                st, xml = await ods._onvif_request(
                    ev_url, create, self.username, self.password
                )
                if st != 200:
                    raise RuntimeError(f"CreatePullPointSubscription HTTP {st}")
                addr = _first(
                    r'<(?:\w+:)?Address>([^<]+)</(?:\w+:)?Address>', xml
                ) or ev_url
            except Exception:
                await asyncio.sleep(backoff)
                continue
            # Poll this subscription until it errors, then re-subscribe.
            while True:
                try:
                    st, xml = await ods._onvif_request(
                        addr, pull, self.username, self.password, timeout=40.0
                    )
                except Exception:
                    break
                if st != 200:
                    break
                for ev in self._parse_onvif_events(xml):
                    yield ev
