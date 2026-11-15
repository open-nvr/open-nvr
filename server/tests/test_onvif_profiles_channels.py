# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ONVIF profiles of a recorder: which video source each belongs to, the
stream's real size, and the channels they group into (services/
onvif_digest_service.py). A 16-channel NVR used to come out of discovery as
one camera: the profiles were a flat list and the first one won."""

from __future__ import annotations

import datetime as _dt  # noqa: I001

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from services import onvif_digest_service as ods  # noqa: E402


def _profile(token: str, name: str, source: str, w: int, h: int) -> str:
    # Shaped like a Hikvision recorder's GetProfiles: the source Bounds are
    # the sensor (the same for main and sub), the encoder Resolution is the
    # stream. The audio block carries a SourceToken of its own.
    return f"""
    <trt:Profiles fixed="true" token="{token}">
      <tt:Name>{name}</tt:Name>
      <tt:VideoSourceConfiguration token="{source}Config">
        <tt:Name>VideoSourceConfig</tt:Name>
        <tt:UseCount>2</tt:UseCount>
        <tt:SourceToken>{source}</tt:SourceToken>
        <tt:Bounds x="0" y="0" width="1920" height="1080"/>
      </tt:VideoSourceConfiguration>
      <tt:AudioSourceConfiguration token="AudioSourceConfig">
        <tt:Name>AudioSourceConfig</tt:Name>
        <tt:UseCount>2</tt:UseCount>
        <tt:SourceToken>AudioSource_1</tt:SourceToken>
      </tt:AudioSourceConfiguration>
      <tt:VideoEncoderConfiguration token="{token}Enc">
        <tt:Name>VideoEncoder</tt:Name>
        <tt:UseCount>1</tt:UseCount>
        <tt:Encoding>H264</tt:Encoding>
        <tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution>
        <tt:Quality>3</tt:Quality>
      </tt:VideoEncoderConfiguration>
    </trt:Profiles>"""


NVR_XML = (
    '<?xml version="1.0"?><env:Envelope><env:Body><trt:GetProfilesResponse>'
    + _profile("Profile_101", "mainStream", "VideoSource_1", 1920, 1080)
    + _profile("Profile_102", "subStream", "VideoSource_1", 640, 480)
    + _profile("Profile_201", "mainStream", "VideoSource_2", 2560, 1440)
    + _profile("Profile_202", "subStream", "VideoSource_2", 704, 576)
    + "</trt:GetProfilesResponse></env:Body></env:Envelope>"
)


@pytest.mark.asyncio
async def test_profiles_carry_their_video_source_and_the_encoder_size(monkeypatch):
    async def fake_caps(*a, **k):
        return {"media": "http://10.0.0.5/onvif/media_service"}

    async def fake_request(url, body, username, password):
        assert "GetProfiles" in body
        return 200, NVR_XML

    monkeypatch.setattr(ods, "get_capabilities", fake_caps)
    monkeypatch.setattr(ods, "_onvif_request", fake_request)

    profiles = await ods.fetch_profiles_digest("10.0.0.5", "admin", "pw")
    assert [p["token"] for p in profiles] == ["Profile_101", "Profile_102", "Profile_201", "Profile_202"]
    assert [p["video_source_token"] for p in profiles] == [
        "VideoSource_1", "VideoSource_1", "VideoSource_2", "VideoSource_2"
    ], "never the audio source's token"
    # The encoder resolution, not the 1920x1080 sensor Bounds every profile shares.
    assert [(p["width"], p["height"]) for p in profiles] == [
        (1920, 1080), (640, 480), (2560, 1440), (704, 576)
    ]


def test_bounds_remain_the_fallback_when_no_encoder_block():
    import re

    block = '<tt:Bounds x="0" y="0" width="1280" height="720"/>'
    # Same regex order as fetch_profiles_digest: encoder first, Bounds second.
    assert re.search(r'width="(\d+)"\s+height="(\d+)"', block).groups() == ("1280", "720")


def _with_uris(profiles):
    for p in profiles:
        p["stream_uri"] = f"rtsp://10.0.0.5:554/Streaming/Channels/{p['token'][-3:]}"
    return profiles


def test_channels_group_by_video_source_with_main_and_sub():
    profiles = _with_uris([
        {"token": "Profile_101", "video_source_token": "VideoSource_1", "width": 1920, "height": 1080},
        {"token": "Profile_102", "video_source_token": "VideoSource_1", "width": 640, "height": 480},
        {"token": "Profile_201", "video_source_token": "VideoSource_2", "width": 2560, "height": 1440},
        {"token": "Profile_202", "video_source_token": "VideoSource_2", "width": 704, "height": 576},
        {"token": "Profile_203", "video_source_token": "VideoSource_2", "width": 352, "height": 288},
    ])
    channels = ods.group_channels(profiles)
    assert [(c["index"], c["name"], c["source_token"]) for c in channels] == [
        (1, "Ch 1", "VideoSource_1"), (2, "Ch 2", "VideoSource_2")
    ]
    assert channels[0]["main"]["token"] == "Profile_101"
    assert channels[0]["sub"]["token"] == "Profile_102"
    # Largest is main, SMALLEST is sub: the middle stream is neither.
    assert channels[1]["main"]["token"] == "Profile_201"
    assert channels[1]["sub"]["token"] == "Profile_203"
    assert len(channels[1]["profiles"]) == 3


def test_a_single_source_camera_is_one_channel_and_no_token_still_degrades_to_one():
    cam = ods.group_channels(_with_uris([
        {"token": "Profile_1", "video_source_token": "VideoSource_1", "width": 1920, "height": 1080},
        {"token": "Profile_2", "video_source_token": "VideoSource_1", "width": 640, "height": 360},
    ]))
    assert len(cam) == 1 and cam[0]["main"]["token"] == "Profile_1" and cam[0]["sub"]["token"] == "Profile_2"

    old = ods.group_channels(_with_uris([{"token": "Profile_1"}, {"token": "Profile_2"}]))
    assert len(old) == 1 and old[0]["source_token"] is None
    # No sizes known: device order decides main, the next one is sub.
    assert old[0]["main"]["token"] == "Profile_1" and old[0]["sub"]["token"] == "Profile_2"


def test_a_profile_without_a_stream_is_never_main_and_an_empty_slot_has_none():
    channels = ods.group_channels([
        {"token": "Profile_101", "video_source_token": "VideoSource_1", "width": 1920, "height": 1080, "stream_uri": None},
        {"token": "Profile_102", "video_source_token": "VideoSource_1", "width": 640, "height": 480,
         "stream_uri": "rtsp://10.0.0.5:554/Streaming/Channels/102"},
        {"token": "Profile_501", "video_source_token": "VideoSource_5", "width": 1920, "height": 1080, "stream_uri": None},
    ])
    assert channels[0]["main"]["token"] == "Profile_102" and channels[0]["sub"] is None
    assert channels[1]["main"] is None and channels[1]["sub"] is None


def test_sub_is_a_different_stream_than_main():
    uri = "rtsp://10.0.0.5:554/live"
    channels = ods.group_channels([
        {"token": "a", "video_source_token": "s", "width": 1920, "height": 1080, "stream_uri": uri},
        {"token": "b", "video_source_token": "s", "width": 640, "height": 480, "stream_uri": uri},
    ])
    assert channels[0]["main"]["token"] == "a" and channels[0]["sub"] is None
