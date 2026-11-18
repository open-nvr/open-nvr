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

Scope is deliberately *lite*: it uses the small, **public** §889 / FCC Covered
List of named vendors (``config/covered_vendors.json``, dated and sourced) and
a few widely-documented affiliate brands. It is NOT a formal
assessment — it does not resolve unlabelled OEM rebrands, cross-reference CVEs,
probe the network, or produce a signed, audit-ready attestation. Those are the
job of the full **OpenNVR Scout** assessment, which this check points to when it
finds covered equipment.

The core ``check_cameras`` is a pure function over camera-like objects so it is
unit-testable without a database.
"""
from __future__ import annotations

import ipaddress
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

# The §889 named vendors with their FCC Covered List entries (listing date and
# source for each), plus a few widely-documented consumer/OEM affiliate brands.
# Generated from a dated, hash-checked release of the public lists by
# scripts/refresh_covered_vendors.py; the check reads it locally.
_COVERED_FILE = Path(__file__).resolve().parent.parent / "config" / "covered_vendors.json"


@lru_cache(maxsize=1)
def _covered() -> tuple[dict, list[tuple[re.Pattern[str], dict]]]:
    """Load the covered-vendor snapshot once: its metadata and one matcher per
    brand string. A brand of four letters or fewer must stand on its own
    (``zte`` matches "ZTE F660" but not "Aztec Gate"); longer brand names do
    not occur inside ordinary words and still match anywhere."""
    with open(_COVERED_FILE, encoding="utf-8") as fh:
        snap = json.load(fh)
    matchers: list[tuple[re.Pattern[str], dict]] = []
    for vendor in snap["vendors"]:
        for key in vendor["match"]:
            pat = re.escape(key.lower())
            if len(key) <= 4:
                pat = rf"(?<![a-z]){pat}(?![a-z])"
            matchers.append((re.compile(pat), {**vendor, "key": key}))
    meta = {k: snap[k] for k in ("source", "release", "built_at")}
    return meta, matchers


_WEAK_USERNAMES = {"", "admin", "root", "user", "guest", "administrator"}


def covered_status(*text: Optional[str]) -> Optional[dict]:
    """Match camera identity strings against the covered list. Returns
    ``{covered, parent, kind, match, lists}`` or None. ``kind`` is 'branded'
    for a named covered vendor, 'affiliate' for a documented consumer/OEM
    brand. ``lists`` gives each list naming the vendor, its listing date and
    source (empty for an affiliate brand, which no list names)."""
    hay = " ".join((t or "").lower() for t in text)
    for pat, vendor in _covered()[1]:
        if pat.search(hay):
            return {
                "covered": True, "parent": vendor["parent"], "kind": vendor["kind"],
                "match": vendor["key"], "lists": vendor.get("lists", []),
            }
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
        # Which release of the lists this run matched against.
        "covered_list": _covered()[0],
        # Honest scope note surfaced by the UI — this is the upsell hook.
        "note": (
            "Lite local check from your camera inventory using the public FCC "
            "Covered List. It does not resolve unlabelled OEM rebrands, cross-"
            "reference CVEs, or produce a signed §889 attestation — that is the "
            "full OpenNVR Scout assessment."
        ),
    }
