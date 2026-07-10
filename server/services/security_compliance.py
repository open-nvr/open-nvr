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
Lite security & §889 compliance check.

A local, read-only posture check computed **entirely from OpenNVR's own camera
inventory** (the ONVIF manufacturer/model it already stores, plus stream and
credential config). Nothing is scanned, nothing leaves the box — it just reads
what OpenNVR already knows and flags the obvious problems, with a focus on the
NDAA §889 / FCC Covered-List covered-vendor question.

Scope is deliberately *lite*: it uses the small, **public** FCC Covered List of
named vendors and a few widely-documented affiliate brands. It is NOT a formal
assessment — it does not resolve unlabelled OEM rebrands, cross-reference CVEs,
probe the network, or produce a signed, audit-ready attestation. Those are the
job of the full **OpenNVR Scout** assessment, which this check points to when it
finds covered equipment.

The core ``check_cameras`` is a pure function over camera-like objects so it is
unit-testable without a database.
"""
from __future__ import annotations

import ipaddress
from typing import Any, Iterable, Optional

# Public FCC Covered List / NDAA §889 named vendors, plus a few widely-documented
# consumer/OEM affiliate brands. Public information — not a proprietary map.
# key (lowercased substring) -> (covered parent display name, kind)
_COVERED: dict[str, tuple[str, str]] = {
    "hikvision": ("Hikvision", "branded"),
    "dahua": ("Dahua", "branded"),
    "huawei": ("Huawei", "branded"),
    "hytera": ("Hytera", "branded"),
    "zte": ("ZTE", "branded"),
    "ezviz": ("Hikvision", "affiliate"),
    "annke": ("Hikvision", "affiliate"),
    "lorex": ("Dahua", "affiliate"),
}

_WEAK_USERNAMES = {"", "admin", "root", "user", "guest", "administrator"}


def covered_status(*text: Optional[str]) -> Optional[dict]:
    """Match camera identity strings against the public covered list. Returns
    ``{covered, parent, kind, match}`` or None. ``kind`` is 'branded' for a
    named covered vendor, 'affiliate' for a documented consumer/OEM brand."""
    hay = " ".join((t or "").lower() for t in text)
    for key, (parent, kind) in _COVERED.items():
        if key in hay:
            return {"covered": True, "parent": parent, "kind": kind, "match": key}
    return None


def _is_public_ip(ip: Optional[str]) -> bool:
    try:
        return ipaddress.ip_address((ip or "").strip()).is_global
    except ValueError:
        return False


def _plaintext_stream(cam: Any) -> bool:
    url = (getattr(cam, "rtsp_url", "") or "").strip().lower()
    if url:
        return url.startswith("rtsp://")   # rtsps:// would be encrypted
    return True   # a plain RTSP camera with no encrypted profile configured


def _weak_credentials(cam: Any) -> bool:
    return (getattr(cam, "username", "") or "").strip().lower() in _WEAK_USERNAMES


def _camera_flags(cam: Any, cov: Optional[dict]) -> list[dict]:
    flags: list[dict] = []
    if cov:
        via = " — via an affiliate/OEM brand" if cov["kind"] == "affiliate" else ""
        flags.append({
            "code": "covered_vendor", "severity": "high",
            "label": f"NDAA §889 covered vendor ({cov['parent']}){via}",
        })
    if _is_public_ip(getattr(cam, "ip_address", "")):
        flags.append({
            "code": "internet_exposed", "severity": "high",
            "label": "Camera has a public, internet-routable IP address",
        })
    if _plaintext_stream(cam):
        flags.append({
            "code": "plaintext_stream", "severity": "medium",
            "label": "Camera stream is unencrypted RTSP (no RTSPS)",
        })
    if _weak_credentials(cam):
        flags.append({
            "code": "weak_credentials", "severity": "medium",
            "label": "Camera uses a default or blank username",
        })
    return flags


def check_cameras(cameras: Iterable[Any]) -> dict:
    """Run the lite check over camera-like objects (attributes: id, name,
    ip_address, manufacturer, model, rtsp_url, username). Pure — no I/O."""
    cameras = list(cameras)
    devices: list[dict] = []
    covered_n = exposed_n = plain_n = weak_n = 0

    for c in cameras:
        cov = covered_status(
            getattr(c, "manufacturer", ""), getattr(c, "model", ""), getattr(c, "name", "")
        )
        flags = _camera_flags(c, cov)
        codes = {f["code"] for f in flags}
        covered_n += 1 if cov else 0
        exposed_n += 1 if "internet_exposed" in codes else 0
        plain_n += 1 if "plaintext_stream" in codes else 0
        weak_n += 1 if "weak_credentials" in codes else 0
        devices.append({
            "id": getattr(c, "id", None),
            "name": getattr(c, "name", "") or "",
            "ip": getattr(c, "ip_address", "") or "",
            "manufacturer": getattr(c, "manufacturer", "") or "",
            "model": getattr(c, "model", "") or "",
            "covered": cov,
            "flags": flags,
        })

    covered_found = covered_n > 0
    if covered_found:
        posture = "covered_vendor"
    elif any(d["flags"] for d in devices):
        posture = "attention"
    else:
        posture = "ok"

    return {
        "posture": posture,
        "covered_vendor_found": covered_found,
        "summary": {
            "cameras": len(cameras),
            "covered_vendor": covered_n,
            "internet_exposed": exposed_n,
            "plaintext_stream": plain_n,
            "weak_credentials": weak_n,
        },
        "cameras": devices,
        # Honest scope note surfaced by the UI — this is the upsell hook.
        "note": (
            "Lite local check from your camera inventory using the public FCC "
            "Covered List. It does not resolve unlabelled OEM rebrands, cross-"
            "reference CVEs, or produce a signed §889 attestation — that is the "
            "full OpenNVR Scout assessment."
        ),
    }
