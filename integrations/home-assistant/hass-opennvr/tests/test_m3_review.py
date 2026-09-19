"""Fixes from the M3 review (HA-307)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import async_mock_service
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from homeassistant.components.media_source import async_browse_media
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.opennvr.const import CONF_CAMERAS, DOMAIN

from . import create_mock_config_entry, setup_mock_config_entry
from .conftest import FakeStream
from .test_notifications import _blueprint_automation, _ready, _settle


async def _setup(hass: HomeAssistant, **kwargs):
    entry = create_mock_config_entry(**kwargs)
    await setup_mock_config_entry(hass, entry)
    return entry


async def test_viewer_client_is_a_session_for_the_shown_cameras(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream],
        freezer: FrozenDateTimeFactory) -> None:
    entry = await _setup(hass, options={CONF_CAMERAS: [3]})
    coordinator = entry.runtime_data.coordinator
    mock_client.open_session.return_value = {
        "token": "onvr_viewer01_x",
        "expires_at": (dt_util.utcnow() + timedelta(seconds=600)).isoformat()}
    await coordinator.async_viewer_client()
    await coordinator.async_viewer_client()
    assert mock_client.open_session.call_count == 1                 # reused
    assert mock_client.open_session.call_args.kwargs == {"camera_ids": [3], "ttl_s": 600}
    freezer.tick(500)                                               # < 2 min left: renew
    await coordinator.async_viewer_client()
    assert mock_client.open_session.call_count == 2


async def test_clip_of_a_hidden_camera_is_not_served(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream],
        hass_client: ClientSessionGenerator) -> None:
    entry = await _setup(hass, options={CONF_CAMERAS: [3]})
    client = await hass_client()
    resp = await client.get(f"/api/opennvr/{entry.entry_id}/clip/1/1789725600/30")
    assert resp.status == 404 and not mock_client.sign_media.called


async def test_search_keeps_to_the_shown_cameras(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    await _setup(hass, options={CONF_CAMERAS: [3]})
    mock_client.search.return_value = {"results": [
        {"kind": "alert", "id": 1, "camera_id": 1}, {"kind": "alert", "id": 2, "camera_id": 3},
        {"kind": "alert", "id": 3, "camera_id": None}]}
    result = await hass.services.async_call(DOMAIN, "search_events", {}, blocking=True,
                                            return_response=True)
    assert [r["id"] for r in result["results"]] == [2, 3]


async def test_an_empty_alert_list_acknowledges_nothing(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    await _setup(hass)
    mock_client.ack_alerts.return_value = {"acknowledged": 0}
    await hass.services.async_call(DOMAIN, "ack_alerts", {"alert_ids": []}, blocking=True,
                                   return_response=True)
    assert mock_client.ack_alerts.call_args.kwargs["ids"] == []


async def test_card_session_with_no_cameras_is_refused(
        hass: HomeAssistant, hass_ws_client: WebSocketGenerator, mock_client: MagicMock,
        mock_stream: type[FakeStream]) -> None:
    await _setup(hass)
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": "opennvr/card_session", "camera_ids": []})
    assert (await ws.receive_json())["error"]["code"] == "not_found"


@pytest.mark.parametrize(("day", "hours"), [("2026-10-25", 25), ("2026-03-29", 23),
                                            ("2026-06-10", 24)])
async def test_recording_hours_on_dst_days(hass: HomeAssistant, mock_client: MagicMock,
                                           mock_stream: type[FakeStream], day, hours,
                                           freezer: FrozenDateTimeFactory) -> None:
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to("2026-12-01T12:00:00+00:00")
    entry = await _setup(hass)
    page = await async_browse_media(
        hass, f"media-source://{DOMAIN}/{entry.entry_id}/recordings/1/{day}")
    starts = [c.media_content_id.rsplit("/", 1)[1] for c in page.children]
    assert len(starts) == hours and len(set(starts)) == hours


async def test_alerts_with_unservable_image_names_are_skipped(
        hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]) -> None:
    entry = await _setup(hass)
    mock_client.get_alerts.return_value = {"total": 2, "alerts": [
        {"id": 1, "fired_at": None, "title": "a", "images": ["../x", "ok.jpg"]},
        {"id": 2, "fired_at": None, "title": "b", "images": ["a/b"]}]}
    page = await async_browse_media(
        hass, f"media-source://{DOMAIN}/{entry.entry_id}/alerts/high/0")
    assert [c.media_content_id.rsplit("/", 1)[1] for c in page.children] == ["ok.jpg"]


async def test_blueprint_runs_in_parallel_and_covers_camera_less_alerts(
        hass: HomeAssistant) -> None:
    notify = async_mock_service(hass, "notify", "test_phone")
    async_mock_service(hass, "opennvr", "ack_alerts")
    await _blueprint_automation(hass, cooldown=0)
    hass.bus.async_fire("opennvr_media_ready", _ready("high"))
    hass.bus.async_fire("opennvr_media_ready", {**_ready("high"), "id": 6})
    await _settle(notify, 2)
    assert len(notify) == 2                       # the second didn't wait for an ack
    # An alert about no camera: notified from opennvr_alert itself.
    hass.bus.async_fire("opennvr_alert", {**_ready("critical"), "id": 7, "camera_id": None,
                                          "camera_entity_id": None})
    # A camera alert's opennvr_alert is left to its media_ready.
    hass.bus.async_fire("opennvr_alert", {**_ready("critical"), "id": 8})
    await _settle(notify, 4)
    await asyncio.sleep(0.2)
    assert len(notify) == 3
    site_wide = notify[-1].data["data"]["actions"]
    assert [a["action"] for a in site_wide] == ["OPENNVR_ACK_E1_7"]   # no live view
