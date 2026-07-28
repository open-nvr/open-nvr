# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CameraContext: static roster, OpenNVR roster (internal key), frame cache."""
import pytest
import respx
from httpx import Response

from context import CameraContext, CameraContextError

FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"

ROSTER_URL = "http://nvr.test/api/v1/internal/camera-agent/cameras"


@pytest.fixture
def frame_file(tmp_path):
    p = tmp_path / "frame.jpg"
    p.write_bytes(FAKE_JPEG)
    return p


# ---- static cameras -------------------------------------------------------- #

async def test_static_cameras(frame_file):
    ctx = CameraContext(cameras=[
        {"camera_id": "camera_1", "name": "Door", "frame_url": frame_file.as_uri()},
    ])
    cams = await ctx.list_cameras()
    assert [c.camera_id for c in cams] == ["camera_1"]
    assert ctx.default_camera() == "camera_1"
    frame = await ctx.get_frame("camera_1")
    assert frame.jpeg == FAKE_JPEG
    await ctx.aclose()


async def test_no_cameras_configured_raises():
    ctx = CameraContext()
    with pytest.raises(CameraContextError, match="no cameras configured"):
        await ctx.list_cameras()
    await ctx.aclose()


async def test_unknown_camera_frame(frame_file):
    ctx = CameraContext(cameras=[
        {"camera_id": "camera_1", "name": "Door", "frame_url": frame_file.as_uri()},
    ])
    with pytest.raises(CameraContextError, match="unknown camera"):
        await ctx.get_frame("camera_9")
    await ctx.aclose()


async def test_frame_cache_ttl(frame_file):
    ctx = CameraContext(
        cameras=[{"camera_id": "camera_1", "name": "Door",
                  "frame_url": frame_file.as_uri()}],
        frame_cache_ttl_seconds=60.0,
    )
    first = await ctx.get_frame("camera_1")
    frame_file.write_bytes(b"CHANGED")
    second = await ctx.get_frame("camera_1")
    assert second.jpeg == first.jpeg  # cached, not re-read
    await ctx.aclose()


# ---- OpenNVR roster (internal API key, no login) ---------------------------- #

def roster_payload():
    return {"cameras": [
        {"camera_id": "cam5", "open_nvr_camera_id": "5", "name": "Gate",
         "frame_url": "rtsp://mediamtx:8554/cam-5?jwt=abc",
         "role": "Gate; location: yard", "source": "mediamtx"},
    ]}


@respx.mock
async def test_roster_fetch_uses_internal_key():
    route = respx.get(ROSTER_URL).mock(
        return_value=Response(200, json=roster_payload()))
    ctx = CameraContext(opennvr_cameras_url=ROSTER_URL, opennvr_api_key="sekrit")
    cams = await ctx.list_cameras()
    assert [c.camera_id for c in cams] == ["camera_5"]
    assert cams[0].frame_url.startswith("rtsp://mediamtx:8554/")
    assert route.calls.last.request.headers["X-Internal-Api-Key"] == "sekrit"
    await ctx.aclose()


@respx.mock
async def test_roster_bad_key_is_a_clear_error():
    respx.get(ROSTER_URL).mock(return_value=Response(401))
    ctx = CameraContext(opennvr_cameras_url=ROSTER_URL, opennvr_api_key="wrong")
    with pytest.raises(CameraContextError, match="internal API key"):
        await ctx.list_cameras()
    await ctx.aclose()


@respx.mock
async def test_roster_is_cached_within_ttl():
    route = respx.get(ROSTER_URL).mock(
        return_value=Response(200, json=roster_payload()))
    ctx = CameraContext(opennvr_cameras_url=ROSTER_URL, opennvr_api_key="k",
                        roster_ttl_seconds=3600)
    await ctx.list_cameras()
    await ctx.list_cameras()
    assert route.call_count == 1
    await ctx.aclose()


# ---- name-aware resolution (small LLMs pass names, not ids) ----------------

async def name_ctx(frame_file):
    ctx = CameraContext(cameras=[
        {"camera_id": "camera_1", "name": "sparsh", "frame_url": frame_file.as_uri()},
        {"camera_id": "camera_2", "name": "cpplus", "frame_url": frame_file.as_uri()},
    ])
    await ctx.refresh()
    return ctx


async def test_resolve_by_name(frame_file):
    ctx = await name_ctx(frame_file)
    assert ctx.resolve_id("cpplus") == "camera_2"
    assert ctx.resolve_id("CPPlus") == "camera_2"
    assert ctx.resolve_id("sparsh") == "camera_1"
    await ctx.aclose()


async def test_resolve_by_id_variants(frame_file):
    ctx = await name_ctx(frame_file)
    assert ctx.resolve_id("camera_2") == "camera_2"
    assert ctx.resolve_id("cam2") == "camera_2"
    assert ctx.resolve_id("cam 2") == "camera_2"
    assert ctx.resolve_id("2") == "camera_2"
    assert ctx.resolve_id("nonexistent") is None
    await ctx.aclose()


async def test_get_frame_accepts_name(frame_file):
    ctx = await name_ctx(frame_file)
    frame = await ctx.get_frame("cpplus")
    assert frame.camera_id == "camera_2"
    await ctx.aclose()


async def test_unknown_camera_error_lists_available(frame_file):
    ctx = await name_ctx(frame_file)
    with pytest.raises(CameraContextError) as exc:
        await ctx.get_frame("garagecam")
    msg = str(exc.value)
    assert "camera_1 (sparsh)" in msg and "camera_2 (cpplus)" in msg
    await ctx.aclose()


async def test_known_names_map(frame_file):
    ctx = await name_ctx(frame_file)
    assert ctx.known_names() == {"sparsh": "camera_1", "cpplus": "camera_2"}
    await ctx.aclose()


async def test_resolve_name_ignores_spacing_and_case(frame_file):
    # Whisper transcribes 'cpplus' as 'CP Plus'; LLMs pass it through.
    ctx = await name_ctx(frame_file)
    assert ctx.resolve_id("CP Plus") == "camera_2"
    assert ctx.resolve_id("cp-plus") == "camera_2"
    assert ctx.resolve_id("Camera 2") == "camera_2"
    await ctx.aclose()
