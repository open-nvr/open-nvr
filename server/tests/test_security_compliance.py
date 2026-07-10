# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
Tests for the lite security & §889 compliance check.

    cd server && pytest tests/test_security_compliance.py -v

``check_cameras`` is pure over camera-like objects, so no DB is needed.
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.security_compliance import check_cameras, covered_status  # noqa: E402


def _cam(**kw):
    base = dict(id=1, name="Cam", ip_address="10.0.0.5", manufacturer="",
               model="", rtsp_url="rtsps://10.0.0.5/s", username="operator1")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_covered_status_branded_and_affiliate():
    assert covered_status("Hikvision", "DS-2CD")["kind"] == "branded"
    assert covered_status("Hikvision")["parent"] == "Hikvision"
    aff = covered_status("Annke", "C500")
    assert aff["covered"] and aff["kind"] == "affiliate" and aff["parent"] == "Hikvision"
    assert covered_status("Lorex")["parent"] == "Dahua"
    assert covered_status("Axis", "M3046") is None


def test_flags_and_posture_covered_vendor():
    cams = [
        _cam(id=1, name="Lobby", manufacturer="Hikvision", model="DS-2CD",
             ip_address="8.8.8.8", rtsp_url="rtsp://8.8.8.8/s", username="admin"),
        _cam(id=2, name="Dock", manufacturer="Axis", model="M3046",
             ip_address="10.0.0.6", rtsp_url="rtsps://10.0.0.6/s", username="operator1"),
    ]
    r = check_cameras(cams)
    assert r["posture"] == "covered_vendor" and r["covered_vendor_found"] is True
    assert r["summary"] == {"cameras": 2, "covered_vendor": 1,
                            "internet_exposed": 1, "plaintext_stream": 1, "weak_credentials": 1}
    hik = r["cameras"][0]
    codes = {f["code"] for f in hik["flags"]}
    assert codes == {"covered_vendor", "internet_exposed", "plaintext_stream", "weak_credentials"}
    assert hik["covered"]["parent"] == "Hikvision"
    # the clean Axis has no flags
    assert r["cameras"][1]["flags"] == [] and r["cameras"][1]["covered"] is None


def test_posture_attention_and_ok():
    # a non-covered camera with only a weak-cred issue → attention
    att = check_cameras([_cam(manufacturer="Axis", username="admin")])
    assert att["posture"] == "attention" and att["covered_vendor_found"] is False
    # a fully clean camera → ok
    ok = check_cameras([_cam(manufacturer="Axis", username="operator1",
                             rtsp_url="rtsps://10.0.0.5/s", ip_address="10.0.0.5")])
    assert ok["posture"] == "ok" and ok["summary"]["covered_vendor"] == 0


def test_plaintext_default_when_no_url():
    r = check_cameras([_cam(manufacturer="Axis", rtsp_url="", ip_address="10.0.0.5",
                            username="operator1")])
    assert r["summary"]["plaintext_stream"] == 1     # no encrypted profile → flagged
