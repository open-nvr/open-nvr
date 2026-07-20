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
    NetworkInfo,
    StorageInfo,
    TimeInfo,
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
        return f"http://{self.ip}:{self.http_port}/onvif/device_service"

    # --- identity ---

    async def get_info(self) -> DeviceInfo:
        info = await ods.get_device_info(
            self.ip, self.username, self.password, self.http_port
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
        try:
            xaddrs = await ods.get_capabilities(
                self.ip, self.username, self.password, self.http_port
            )
        except Exception:
            xaddrs = {}
        has = lambda k: bool(xaddrs.get(k))  # noqa: E731
        supported = {
            "info": True,
            "network": True,  # device service — read-only display
            "time": True,  # device service GetSystemDateAndTime
            "imaging": has("imaging"),
            "encoder": has("media"),
            "osd": False,  # baseline OSD not implemented; vendor drivers add it
            "ptz": has("ptz"),
            # The device advertises an Events endpoint, but this baseline does
            # not implement subscribe_events() — advertising it would surface a
            # tab that fails on use. Vendor drivers that DO implement a native
            # event stream turn this on.
            "events": False,
            "motion": False,  # baseline motion not implemented; vendor drivers add it
            "storage": False,  # not exposed via the ONVIF baseline
            # Not implemented in the baseline (no get_users override) — a
            # vendor driver must turn this on. Advertising it here produced an
            # always-empty Users tab on non-Hikvision/Dahua cameras.
            "users": False,
            "audio": False,  # detected in a later phase
            # No reboot/config-export in the baseline — a vendor driver that
            # implements them turns this on. Otherwise the tab would offer
            # buttons that only error.
            "maintenance": False,
        }
        return Capabilities(
            driver_name=self.driver_name,
            supported_areas=supported,
            onvif_endpoints=xaddrs,
            detail={},
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

    # --- storage (read only) ---

    async def get_storage(self) -> StorageInfo:
        # The ONVIF baseline does not expose SD health portably; vendor drivers
        # override this (e.g. Hikvision ISAPI /ContentMgmt/Storage).
        return StorageInfo(supported=False, source="onvif")

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
        """Resolve (and cache) the media + imaging service XAddrs."""
        if getattr(self, "_svc", None) is None:
            try:
                caps = await ods.get_capabilities(
                    self.ip, self.username, self.password, self.http_port
                )
            except Exception:
                caps = {}
            base = f"http://{self.ip}:{self.http_port}/onvif"
            self._svc = {
                "media": caps.get("media") or f"{base}/Media",
                "imaging": caps.get("imaging") or f"{base}/Imaging",
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
