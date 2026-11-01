# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A camera's substream is found and verified when the camera is added.

The rule (services.substream_discovery): the device's own ONVIF sub first,
then the vendor-convention guess; each is probed and only one that answers
is stored — except a device-reported one that could not be checked at all,
which the pipeline can survive (it falls back to the main stream). A guess
the camera refuses is never stored.
"""
import os
import secrets
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_sub_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from services.substream_discovery import (  # noqa: E402
    DiscoveredSubstream,
    discover_substream,
    parse_ffprobe_outcome,
)

HIK_MAIN = "rtsp://admin:pw@192.168.1.64:554/Streaming/Channels/101"
HIK_SUB = "rtsp://admin:pw@192.168.1.64:554/Streaming/Channels/102"
DAHUA_MAIN = "rtsp://admin:pw@192.168.1.2:554/cam/realmonitor?channel=1&subtype=0"
DAHUA_SUB = "rtsp://admin:pw@192.168.1.2:554/cam/realmonitor?channel=1&subtype=1"


def _prober(answers: dict):
    """A probe that answers per URL and records what it was asked."""
    asked = []

    async def probe(url, timeout):
        asked.append(url)
        return answers.get(url, ("refused", None))
    probe.asked = asked
    return probe


async def _no_onvif(ip, u, p):
    return None


@pytest.mark.asyncio
async def test_the_vendor_guess_is_stored_only_when_the_camera_answers():
    probe = _prober({HIK_SUB: ("ok", (640, 360))})
    found = await discover_substream(HIK_MAIN, "192.168.1.64", "admin", "pw",
                                     prober=probe, onvif=_no_onvif)
    assert found == DiscoveredSubstream(HIK_SUB, "vendor", True, 640, 360)
    assert found.resolution == "640x360"
    assert probe.asked == [HIK_SUB]
    # the same camera with its sub stream disabled: 404 → nothing stored
    probe = _prober({HIK_SUB: ("refused", None)})
    assert await discover_substream(HIK_MAIN, "192.168.1.64", "admin", "pw",
                                    prober=probe, onvif=_no_onvif) is None
    # unreachable right now: a GUESS is never stored unverified
    probe = _prober({HIK_SUB: ("inconclusive", None)})
    assert await discover_substream(HIK_MAIN, "192.168.1.64", "admin", "pw",
                                    prober=probe, onvif=_no_onvif) is None


@pytest.mark.asyncio
async def test_the_device_reported_sub_comes_first_and_survives_an_unverifiable_probe():
    onvif_sub = "rtsp://admin:pw@192.168.1.64:554/Streaming/Channels/103"

    async def onvif(ip, u, p):
        assert (ip, u, p) == ("192.168.1.64", "admin", "pw")
        return onvif_sub
    probe = _prober({onvif_sub: ("ok", (704, 576)), HIK_SUB: ("ok", (640, 360))})
    found = await discover_substream(HIK_MAIN, "192.168.1.64", "admin", "pw",
                                     prober=probe, onvif=onvif)
    assert found.url == onvif_sub and found.source == "onvif" and found.verified
    assert probe.asked == [onvif_sub], "the vendor guess is not even tried"
    # could not be checked: still stored, marked unverified
    probe = _prober({onvif_sub: ("inconclusive", None)})
    found = await discover_substream(HIK_MAIN, "192.168.1.64", "admin", "pw",
                                     prober=probe, onvif=onvif)
    assert found == DiscoveredSubstream(onvif_sub, "onvif", False)
    assert found.resolution == "unverified"
    # refused by the camera: fall through to the vendor guess
    probe = _prober({onvif_sub: ("refused", None), HIK_SUB: ("ok", (640, 360))})
    found = await discover_substream(HIK_MAIN, "192.168.1.64", "admin", "pw",
                                     prober=probe, onvif=onvif)
    assert found.url == HIK_SUB and found.source == "vendor"


@pytest.mark.asyncio
async def test_no_credentials_means_no_onvif_but_the_vendor_rule_still_runs():
    calls = []

    async def onvif(ip, u, p):
        calls.append(ip)
        return "rtsp://x/should-not-be-used"
    probe = _prober({DAHUA_SUB: ("ok", (704, 480))})
    found = await discover_substream(DAHUA_MAIN, "192.168.1.2", None, None,
                                     prober=probe, onvif=onvif)
    assert calls == [] and found.url == DAHUA_SUB and found.source == "vendor"


@pytest.mark.asyncio
async def test_unknown_vendor_and_a_sub_equal_to_main_find_nothing():
    probe = _prober({})
    assert await discover_substream("rtsp://u:p@10.0.0.9:554/live", "10.0.0.9", "u", "p",
                                    prober=probe, onvif=_no_onvif) is None
    assert probe.asked == []

    async def same(ip, u, p):
        return HIK_MAIN
    assert await discover_substream(HIK_MAIN, "192.168.1.64", "u", "p",
                                    prober=_prober({HIK_MAIN: ("ok", (1920, 1080))}),
                                    onvif=same) is None
    assert await discover_substream(None, "10.0.0.9", "u", "p", prober=probe) is None


@pytest.mark.asyncio
async def test_a_probe_or_onvif_that_raises_is_a_miss_not_a_crash():
    async def boom_onvif(ip, u, p):
        raise RuntimeError("onvif down")

    async def boom_probe(url, timeout):
        raise OSError("no ffprobe")
    assert await discover_substream(HIK_MAIN, "192.168.1.64", "u", "p",
                                    prober=boom_probe, onvif=boom_onvif) is None


def test_ffprobe_outcomes_are_classified():
    assert parse_ffprobe_outcome(0, "640,360\n", "") == ("ok", (640, 360))
    assert parse_ffprobe_outcome(0, "", "") == ("refused", None)          # no video
    assert parse_ffprobe_outcome(1, "", "method DESCRIBE failed: 404 Not Found") == ("refused", None)
    assert parse_ffprobe_outcome(1, "", "401 Unauthorized") == ("refused", None)
    assert parse_ffprobe_outcome(1, "", "Connection refused") == ("refused", None)
    assert parse_ffprobe_outcome(1, "", "Connection timed out") == ("inconclusive", None)
    assert parse_ffprobe_outcome(None, "", "") == ("inconclusive", None)


@pytest.mark.asyncio
async def test_camera_create_stores_the_discovered_sub_and_leaves_the_rest_alone(monkeypatch):
    """The route helper: a found sub lands in substream_url; nothing found,
    or discovery blowing up, leaves the model untouched."""
    import routers.cameras as cams
    import services.substream_discovery as sd
    from schemas import CameraCreate

    create = CameraCreate(name="Gate", ip_address="192.168.1.64", rtsp_url=HIK_MAIN,
                          username="admin", password="pw")
    assert create.substream_url is None

    async def found(main, ip, u, p, **kw):
        assert (main, ip, u, p) == (HIK_MAIN, "192.168.1.64", "admin", "pw")
        return DiscoveredSubstream(HIK_SUB, "vendor", True, 640, 360)
    monkeypatch.setattr(sd, "discover_substream", found)
    out = await cams._discover_substream_for_create(create)
    assert out.substream_url == HIK_SUB and out.rtsp_url == HIK_MAIN

    async def nothing(*a, **kw):
        return None
    monkeypatch.setattr(sd, "discover_substream", nothing)
    assert (await cams._discover_substream_for_create(create)).substream_url is None

    async def boom(*a, **kw):
        raise RuntimeError("network")
    monkeypatch.setattr(sd, "discover_substream", boom)
    assert (await cams._discover_substream_for_create(create)).substream_url is None
