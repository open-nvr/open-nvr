"""Camera platform: entities, controls, snapshots, RTSPS, WebRTC over WHEP."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
from pyopennvr import (
    Camera as NvrCamera,
    OpenNVRAuthError,
    OpenNVRConnectionError,
    OpenNVRNotFoundError,
    StreamInfo,
    WhepSession,
)
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from homeassistant.components.camera import (
    DOMAIN as CAMERA_DOMAIN,
    CameraEntityFeature,
    async_get_image,
    async_get_stream_source,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform

from custom_components.opennvr.camera import rtsps_source
from custom_components.opennvr.const import DOMAIN

from . import CAMERAS, create_mock_config_entry, setup_mock_config_entry, system_info
from .conftest import FakeStream

FRONT = "camera.front_door"
GARAGE = "camera.garage"


@pytest.fixture
def mock_whep():
    whep = MagicMock()
    whep.offer = AsyncMock(return_value=WhepSession(
        "v=0 answer", "https://nvr.local/webrtc/cam-1/whep/s1", "streamjwt"))
    whep.add_candidate = AsyncMock()
    whep.close = AsyncMock()
    with patch("custom_components.opennvr.camera.Whep", return_value=whep):
        yield whep


async def _setup(hass: HomeAssistant, **entry_kwargs):
    entry = create_mock_config_entry(**entry_kwargs)
    await setup_mock_config_entry(hass, entry)
    return entry


def _entity(hass: HomeAssistant, entity_id: str):
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        if entity_id in platform.entities:
            return platform.entities[entity_id]
    raise KeyError(entity_id)


async def test_cameras_and_features(hass: HomeAssistant, mock_client: MagicMock,
                                    mock_stream: type[FakeStream], mock_whep) -> None:
    await _setup(hass)
    front, garage = hass.states.get(FRONT), hass.states.get(GARAGE)
    assert front.attributes[ATTR_SUPPORTED_FEATURES] == CameraEntityFeature.STREAM
    assert front.attributes["motion_detection"] is True       # pushed state
    assert garage.attributes.get("motion_detection") is not True   # camera row: off
    # Native WebRTC (WHEP): the frontend offers WebRTC, not HLS.
    assert {str(s) for s in _entity(hass, FRONT).camera_capabilities.frontend_stream_types}         == {"web_rtc"}


async def test_on_off_only_where_recording_may_pause(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream],
        mock_whep) -> None:
    mock_client.get_system_info.return_value = system_info(recording_pause_enabled=True)
    await _setup(hass)
    assert hass.states.get(FRONT).attributes[ATTR_SUPPORTED_FEATURES] == (
        CameraEntityFeature.STREAM | CameraEntityFeature.ON_OFF)
    await hass.services.async_call(CAMERA_DOMAIN, "turn_off", {ATTR_ENTITY_ID: FRONT},
                                   blocking=True)
    call = mock_client.set_camera_active.call_args
    assert call.args == (1, False) and call.kwargs["correlation_id"]


async def test_no_live_scope_no_cameras(hass: HomeAssistant, mock_client: MagicMock,
                                        mock_stream: type[FakeStream]) -> None:
    info = system_info()
    mock_client.get_system_info.return_value = system_info(
        caller={**info.caller, "scopes": ["settings.view", "cameras.view"]})
    await _setup(hass)
    assert hass.states.get(FRONT) is None


async def test_motion_detection_toggle(hass: HomeAssistant, mock_client: MagicMock,
                                       mock_stream: type[FakeStream], mock_whep) -> None:
    await _setup(hass)
    await hass.services.async_call(CAMERA_DOMAIN, "enable_motion_detection",
                                   {ATTR_ENTITY_ID: GARAGE}, blocking=True)
    assert mock_client.set_detection.call_args.args == (3, True)
    mock_client.set_detection.side_effect = OpenNVRAuthError("lacks cameras.manage", 403)
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(CAMERA_DOMAIN, "disable_motion_detection",
                                       {ATTR_ENTITY_ID: FRONT}, blocking=True)
    assert exc.value.translation_key == "not_permitted"
    mock_client.set_detection.side_effect = OpenNVRConnectionError("down")
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(CAMERA_DOMAIN, "disable_motion_detection",
                                       {ATTR_ENTITY_ID: FRONT}, blocking=True)
    assert exc.value.translation_key == "command_failed"


async def test_pushed_detection_and_online(hass: HomeAssistant, mock_client: MagicMock,
                                           mock_stream: type[FakeStream], mock_whep) -> None:
    await _setup(hass)
    [stream] = mock_stream.instances
    stream.on_frame({"v": 2, "seq": 1, "event_type": "entity_state",
                     "payload": {"key": "camera.1.detection", "state": False}})
    await hass.async_block_till_done()
    assert hass.states.get(FRONT).attributes.get("motion_detection") is not True
    stream.on_frame({"v": 2, "seq": 2, "event_type": "entity_state",
                     "payload": {"key": "camera.1.online", "state": False}})
    await hass.async_block_till_done()
    assert hass.states.get(FRONT).state == STATE_UNAVAILABLE
    stream.on_frame({"v": 2, "seq": 3, "event_type": "entity_state",
                     "payload": {"key": "camera.1.online", "state": True}})
    await hass.async_block_till_done()
    assert hass.states.get(FRONT).state != STATE_UNAVAILABLE


async def test_snapshot(hass: HomeAssistant, mock_client: MagicMock,
                        mock_stream: type[FakeStream], mock_whep) -> None:
    await _setup(hass)
    mock_client.get_snapshot.return_value = b"\xff\xd8jpeg"
    image = await async_get_image(hass, FRONT)
    assert image.content == b"\xff\xd8jpeg"
    mock_client.get_snapshot.assert_called_with(1)
    mock_client.get_snapshot.side_effect = OpenNVRConnectionError("down")
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, FRONT)


async def test_stream_source(hass: HomeAssistant, mock_client: MagicMock,
                             mock_stream: type[FakeStream], mock_whep) -> None:
    await _setup(hass)
    mock_client.get_stream_info.return_value = StreamInfo.from_dict({
        "camera_id": 1, "stream_name": "cam-1", "token": "a.b/c",
        "urls": {"webrtc": "https://nvr.local/webrtc/cam-1/whep",
                 "rtsps": "rtsps://192.168.1.20:8322/cam-1"}})
    assert await async_get_stream_source(hass, FRONT) == (
        "rtsps://opennvr:a.b%2Fc@192.168.1.20:8322/cam-1")


@pytest.mark.parametrize(("url", "expected"), [
    ("rtsps://127.0.0.1:8322/cam-1", None),       # default: loopback only
    ("rtsps://localhost:8322/cam-1", None),
    ("rtsps://[::1]:8322/cam-1", None),
    (None, None),
    ("rtsps://nvr.lan:8322/cam-1", "rtsps://opennvr:t@nvr.lan:8322/cam-1"),
    ("rtsps://old:pw@10.0.0.2:8322/cam-1", "rtsps://opennvr:t@10.0.0.2:8322/cam-1"),
])
def test_rtsps_source(url, expected) -> None:
    assert rtsps_source(url, "t") == expected


async def test_new_camera_appears(hass: HomeAssistant, mock_client: MagicMock,
                                  mock_stream: type[FakeStream], mock_whep,
                                  freezer: FrozenDateTimeFactory) -> None:
    await _setup(hass)
    mock_client.get_cameras.return_value = [NvrCamera.from_dict(c) for c in CAMERAS] + [
        NvrCamera.from_dict({"id": 7, "name": "Back yard"})]
    freezer.tick(31)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("camera.back_yard") is not None


async def test_webrtc_offer_answer_candidate_close(
        hass: HomeAssistant, hass_ws_client: WebSocketGenerator, mock_client: MagicMock,
        mock_stream: type[FakeStream], mock_whep) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "camera/webrtc/offer", "entity_id": FRONT,
                                    "offer": "v=0 offer"})
    response = await client.receive_json()
    assert response["success"]
    sub_id = response["id"]
    msg = await client.receive_json()
    assert msg["id"] == sub_id and msg["event"]["type"] == "session"
    session_id = msg["event"]["session_id"]
    msg = await client.receive_json()
    assert msg["event"] == {"type": "answer", "answer": "v=0 answer"}
    mock_whep.offer.assert_called_once_with(1, "v=0 offer")

    await client.send_json_auto_id({
        "type": "camera/webrtc/candidate", "entity_id": FRONT, "session_id": session_id,
        "candidate": {"candidate": "candidate:1 1 UDP 1 10.0.0.2 5000 typ host",
                      "sdpMid": "0"}})
    assert (await client.receive_json())["success"]
    session, candidate, mid = mock_whep.add_candidate.call_args.args
    assert session.session_url.endswith("/whep/s1") and mid == "0"
    assert candidate.startswith("candidate:1")

    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": sub_id})
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    mock_whep.close.assert_called_once()


async def test_webrtc_offer_errors(hass: HomeAssistant, hass_ws_client: WebSocketGenerator,
                                   mock_client: MagicMock, mock_stream: type[FakeStream],
                                   mock_whep) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)
    for error, text in ((OpenNVRNotFoundError("404"), "not streaming"),
                        (OpenNVRAuthError("WHEP refused", 401), "WHEP refused")):
        mock_whep.offer.side_effect = error
        await client.send_json_auto_id({"type": "camera/webrtc/offer", "entity_id": FRONT,
                                        "offer": "v=0 offer"})
        assert (await client.receive_json())["success"]
        assert (await client.receive_json())["event"]["type"] == "session"
        event = (await client.receive_json())["event"]
        assert event["type"] == "error" and event["code"] == "webrtc_offer_failed"
        assert text in event["message"]


async def test_candidates_before_the_answer_are_held(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream],
        mock_whep) -> None:
    from homeassistant.components.camera import RTCIceCandidateInit

    await _setup(hass)
    cam = _entity(hass, FRONT)
    early = RTCIceCandidateInit("candidate:9 1 UDP 1 10.0.0.9 5000 typ host", sdp_mid="0")
    await cam.async_on_webrtc_candidate("s-1", early)
    await cam.async_on_webrtc_candidate("s-1", RTCIceCandidateInit(""))  # end marker
    assert not mock_whep.add_candidate.called
    sent = []
    await cam.async_handle_async_webrtc_offer("v=0 offer", "s-1", sent.append)
    assert sent[0].answer == "v=0 answer"
    assert [c.args[1] for c in mock_whep.add_candidate.call_args_list] == [early.candidate]
    cam.close_webrtc_session("s-1")
    await hass.async_block_till_done()
    mock_whep.close.assert_called_once()



async def test_a_turned_off_camera_stays_available_to_turn_on(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream],
        mock_whep) -> None:
    mock_client.get_system_info.return_value = system_info(recording_pause_enabled=True)
    mock_client.get_cameras.return_value = [
        NvrCamera.from_dict({**CAMERAS[0], "is_active": False}),
        NvrCamera.from_dict(CAMERAS[1])]
    await _setup(hass)
    [stream] = mock_stream.instances
    stream.on_frame({"v": 2, "seq": 1, "event_type": "entity_state",
                     "payload": {"key": "camera.1.online", "state": False}})
    await hass.async_block_till_done()
    state = hass.states.get(FRONT)
    assert state.state != STATE_UNAVAILABLE             # off, yet reachable
    await hass.services.async_call("camera", "turn_on", {"entity_id": FRONT}, blocking=True)
    assert mock_client.set_camera_active.call_args.args == (1, True)


async def test_a_session_closed_while_its_offer_waits_is_deleted(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream],
        mock_whep) -> None:
    import asyncio

    await _setup(hass)
    cam = _entity(hass, FRONT)
    gate = asyncio.Event()

    async def slow_offer(camera_id, sdp):
        await gate.wait()
        return WhepSession("v=0 answer", "https://nvr.local/webrtc/cam-1/whep/s9", "jwt")

    mock_whep.offer.side_effect = slow_offer
    sent = []
    task = hass.async_create_task(cam.async_handle_async_webrtc_offer("v=0", "s-9", sent.append))
    await asyncio.sleep(0)
    cam.close_webrtc_session("s-9")
    gate.set()
    await task
    assert sent == []                         # nobody is listening any more
    mock_whep.close.assert_called_once()
    assert "s-9" not in cam._sessions


async def test_an_offer_timeout_still_answers_the_browser(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream],
        mock_whep) -> None:
    await _setup(hass)
    cam = _entity(hass, FRONT)
    mock_whep.offer.side_effect = TimeoutError()
    sent = []
    await cam.async_handle_async_webrtc_offer("v=0", "s-1", sent.append)
    assert sent[0].code == "webrtc_offer_failed"


async def test_the_rtsps_source_is_re_signed(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream],
        mock_whep, freezer: FrozenDateTimeFactory) -> None:
    await _setup(hass)
    cam = _entity(hass, FRONT)
    info = lambda token: StreamInfo.from_dict({  # noqa: E731
        "camera_id": 1, "stream_name": "cam-1", "token": token,
        "urls": {"webrtc": "https://nvr.local/webrtc/cam-1/whep",
                 "rtsps": "rtsps://10.0.0.2:8322/cam-1"}})
    mock_client.get_stream_info.return_value = info("first")
    assert "first" in await cam.stream_source()
    cam.stream = MagicMock()
    mock_client.get_stream_info.return_value = info("second")
    freezer.tick(50 * 60 + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert "second" in cam.stream.update_source.call_args.args[0]
    cam.stream = None
