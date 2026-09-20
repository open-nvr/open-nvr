"""Actions: routing to the right site and camera, arguments, responses, errors."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

from pyopennvr import OpenNVRAuthError, OpenNVRNotFoundError, SignedMedia
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.opennvr.const import CONF_MEDIA_TTL, DOMAIN

from . import URL, create_mock_config_entry, setup_mock_config_entry, system_info
from .conftest import FakeStream

FRONT = "camera.front_door"


async def _setup(hass: HomeAssistant, **kwargs):
    entry = create_mock_config_entry(**kwargs)
    await setup_mock_config_entry(hass, entry)
    return entry


async def _call(hass: HomeAssistant, service: str, data: dict, response: bool = False):
    return await hass.services.async_call(DOMAIN, service, data, blocking=True,
                                          return_response=response)


async def test_ptz(hass: HomeAssistant, mock_client: MagicMock,
                   mock_stream: type[FakeStream]) -> None:
    await _setup(hass)
    await _call(hass, "ptz", {"camera": FRONT, "action": "move", "argument": "Left",
                              "speed": 0.4})
    assert mock_client.ptz_move.call_args.args == (1, -0.4, 0.0, 0.0)
    assert mock_client.ptz_move.call_args.kwargs["correlation_id"]
    await _call(hass, "ptz", {"camera": FRONT, "action": "zoom", "argument": "in"})
    assert mock_client.ptz_move.call_args.args == (1, 0.0, 0.0, 0.5)
    await _call(hass, "ptz", {"camera": FRONT, "action": "stop"})
    mock_client.ptz_stop.assert_called_once()
    mock_client.ptz_presets.return_value = [{"token": "p1", "name": "Gate"}]
    await _call(hass, "ptz", {"camera": FRONT, "action": "preset", "argument": "gate"})
    assert mock_client.ptz_goto_preset.call_args.args == (1, "p1")
    with pytest.raises(ServiceValidationError) as exc:
        await _call(hass, "ptz", {"camera": FRONT, "action": "preset", "argument": "moon"})
    assert exc.value.translation_key == "unknown_preset"
    with pytest.raises(ServiceValidationError) as exc:
        await _call(hass, "ptz", {"camera": FRONT, "action": "move", "argument": "in"})
    assert exc.value.translation_key == "bad_ptz_argument"


async def test_camera_must_be_ours(hass: HomeAssistant, mock_client: MagicMock,
                                   mock_stream: type[FakeStream]) -> None:
    await _setup(hass)
    with pytest.raises(ServiceValidationError) as exc:
        await _call(hass, "ptz", {"camera": "camera.someone_elses", "action": "stop"})
    assert exc.value.translation_key == "not_an_opennvr_camera"


async def test_create_and_end_event(hass: HomeAssistant, mock_client: MagicMock,
                                    mock_stream: type[FakeStream]) -> None:
    await _setup(hass)
    mock_client.create_event.return_value = {"id": 77, "camera_id": 1}
    result = await _call(hass, "create_event", {"camera": FRONT, "label": "doorbell",
                                                "sub_label": "front", "duration": 20},
                         response=True)
    assert result == {"event_id": 77}
    call = mock_client.create_event.call_args
    assert call.args == (1,) and call.kwargs["label"] == "doorbell"
    assert call.kwargs["note"] == "front" and call.kwargs["duration_s"] == 20
    await _call(hass, "end_event", {"event_id": 77})       # the only site
    assert mock_client.end_event.call_args.args == (77,)
    mock_client.end_event.side_effect = OpenNVRNotFoundError("no event 78")
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "end_event", {"event_id": 78})
    assert exc.value.translation_key == "not_found"


async def test_site_must_be_named_when_there_are_two(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    first = await _setup(hass)
    mock_client.get_system_info.return_value = system_info(site_id="second")
    await _setup(hass, unique_id="second")
    with pytest.raises(ServiceValidationError) as exc:
        await _call(hass, "end_event", {"event_id": 5})
    assert exc.value.translation_key == "entry_required"
    await _call(hass, "end_event", {"event_id": 5, ATTR_CONFIG_ENTRY_ID: first.entry_id})
    with pytest.raises(ServiceValidationError):
        await _call(hass, "end_event", {"event_id": 5, ATTR_CONFIG_ENTRY_ID: "nope"})


async def test_export_recording(hass: HomeAssistant, mock_client: MagicMock,
                                mock_stream: type[FakeStream],
                                aioclient_mock: AiohttpClientMocker) -> None:
    await _setup(hass, options={CONF_MEDIA_TTL: 2})
    mock_client.sign_media.return_value = SignedMedia(
        url=f"{URL}/api/v1/media/s/m1.clip", expires_at="2026-09-18T12:00:00+00:00")
    aioclient_mock.get(f"{URL}/api/v1/media/s/m1.clip", content=b"clip-bytes")
    result = await _call(hass, "export_recording", {
        "camera": FRONT, "start": "2026-09-18T10:00:00+00:00",
        "end": "2026-09-18T10:00:30+00:00", "with_hash": True}, response=True)
    assert result["url"].endswith("/m1.clip") and result["bytes"] == 10
    assert result["sha256"] == hashlib.sha256(b"clip-bytes").hexdigest()
    call = mock_client.sign_media.call_args
    assert call.args == ("clip",) and call.kwargs["camera_id"] == 1
    assert call.kwargs["duration_s"] == 30 and call.kwargs["ttl_s"] == 7200
    assert call.kwargs["start"] == "2026-09-18T10:00:00+00:00"
    with pytest.raises(ServiceValidationError) as exc:
        await _call(hass, "export_recording", {
            "camera": FRONT, "start": "2026-09-18T10:00:00+00:00",
            "end": "2026-09-18T09:00:00+00:00"}, response=True)
    assert exc.value.translation_key == "bad_clip_range"


async def test_protect_recording(hass: HomeAssistant, mock_client: MagicMock,
                                 mock_stream: type[FakeStream]) -> None:
    await _setup(hass)
    await _call(hass, "protect_recording", {"event_id": 9, "pre_s": 5})
    call = mock_client.protect_event.call_args
    assert call.args == (9,) and call.kwargs["pre_s"] == 5 and call.kwargs["post_s"] == 10


async def test_ack_alerts(hass: HomeAssistant, mock_client: MagicMock,
                          mock_stream: type[FakeStream]) -> None:
    await _setup(hass)
    mock_client.ack_alerts.return_value = {"acknowledged": 3}
    assert await _call(hass, "ack_alerts", {"source": "loitering", "severity": "high"},
                       response=True) == {"count": 3}
    kw = mock_client.ack_alerts.call_args.kwargs
    assert kw["ids"] is None and kw["source"] == "loitering" and kw["severity"] == "high"
    await _call(hass, "ack_alerts", {"alert_ids": [1, 2]}, response=True)
    assert mock_client.ack_alerts.call_args.kwargs["ids"] == [1, 2]
    with pytest.raises(ServiceValidationError) as exc:
        await _call(hass, "ack_alerts", {"alert_ids": [1], "source": "x"}, response=True)
    assert exc.value.translation_key == "ack_ids_or_filter"
    mock_client.ack_alerts.side_effect = OpenNVRAuthError("lacks alerts.manage", 403)
    with pytest.raises(HomeAssistantError) as exc:
        await _call(hass, "ack_alerts", {}, response=True)
    assert exc.value.translation_key == "not_permitted"


async def test_search_events(hass: HomeAssistant, mock_client: MagicMock,
                             mock_stream: type[FakeStream]) -> None:
    await _setup(hass)
    mock_client.search.return_value = {"total": 3, "interpretation": {"labels": ["car"]},
                                       "results": [
        {"id": 5, "camera_id": 1, "label": "car", "started_at": "2026-09-18T01:00:00+00:00",
         "evidence_url": "/api/v1/events/5/evidence"},
        {"id": 6, "camera_id": 1, "label": "car", "started_at": "2026-09-18T00:30:00+00:00",
         "evidence_url": None},
        {"id": 7, "camera_id": 9, "label": "car", "started_at": "2026-09-18T00:20:00+00:00",
         "evidence_url": "/api/v1/events/7/evidence"},             # a camera not shown
    ]}
    mock_client.sign_media.return_value = SignedMedia(url=f"{URL}/api/v1/media/s/m1.t",
                                                      expires_at="x")
    result = await _call(hass, "search_events", {
        "camera": FRONT, "label": "car", "start": "2026-09-18T00:00:00+00:00",
        "limit": 10}, response=True)
    kw = mock_client.search.call_args.kwargs
    assert kw["camera_id"] == 1 and kw["label"] == "car" and kw["limit"] == 10
    assert kw["from_"] == "2026-09-18T00:00:00+00:00" and kw["to"] is None
    rows = result["results"]
    assert [r["id"] for r in rows] == [5, 6]
    assert rows[0]["thumbnail_url"].endswith("/m1.t") and "evidence_url" not in rows[0]
    assert "thumbnail_url" not in rows[1]
    assert result["interpretation"] == {"labels": ["car"]} and result["total"] == 3
    assert mock_client.sign_media.call_args.kwargs["name"] == "evidence"


async def test_services_need_a_loaded_site(hass: HomeAssistant, mock_client: MagicMock,
                                           mock_stream: type[FakeStream]) -> None:
    entry = await _setup(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "ptz")      # registered once, in async_setup
    with pytest.raises(ServiceValidationError) as exc:
        await _call(hass, "ptz", {"camera": FRONT, "action": "stop"})
    assert exc.value.translation_key == "entry_not_loaded"
