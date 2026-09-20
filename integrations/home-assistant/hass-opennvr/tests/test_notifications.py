"""Notification events with relay URLs, the HA-signed relay, and the blueprint."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
import shutil
from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from pyopennvr import SignedMedia
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events, async_mock_service
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.opennvr.notifications import EVENT_ALERT, EVENT_MEDIA_READY

from . import SITE_ID, URL, create_mock_config_entry, setup_mock_config_entry
from .conftest import FakeStream

TOKEN = "m1.eyJrIjoiY2xpcCJ9.c2lnbmF0dXJlLW9mLWF0LWxlYXN0LTE2"
BLUEPRINT = (Path(__file__).parents[1] / "blueprints" / "automation" / "opennvr"
             / "alert_notification.yaml")


@pytest.fixture
async def entry(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
                mock_client: MagicMock, mock_stream: type[FakeStream]):
    mock_client.sign_media.return_value = SignedMedia(
        url=f"{URL}/api/v1/media/s/{TOKEN}", expires_at="2026-09-19T10:00:00+00:00")
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    return entry


async def test_alert_and_media_ready_events(hass: HomeAssistant, entry,
                                            mock_client: MagicMock,
                                            mock_stream: type[FakeStream]) -> None:
    alerts = async_capture_events(hass, EVENT_ALERT)
    ready = async_capture_events(hass, EVENT_MEDIA_READY)
    [stream] = mock_stream.instances
    stream.on_frame({"v": 2, "seq": 1, "event_type": "app_alert", "camera_id": 1,
                     "payload": {"severity": "high", "alert_type": "loitering",
                                 "title": "Loitering at the gate", "id": 42,
                                 "alert_id": "a-42", "source": {"name": "loitering"},
                                 "correlation_id": "c-1", "images": ["snapshot.jpg"]}})
    await hass.async_block_till_done()
    [a] = alerts
    assert a.data["id"] == 42 and a.data["severity"] == "high"
    assert a.data["camera_entity_id"] == "camera.front_door"
    # The relay requires HA auth: the URL is signed by HA, for as long as
    # OpenNVR's own token lives, so a phone's fetcher (no login) can use it.
    assert a.data["image_url"].startswith(f"/api/opennvr/{SITE_ID}/m/{TOKEN}?authSig=")
    kw = mock_client.sign_media.call_args.kwargs
    assert mock_client.sign_media.call_args.args == ("alert_image",)
    assert kw["id"] == 42 and kw["name"] == "snapshot.jpg" and kw["ttl_s"] == 24 * 3600

    stream.on_frame({"v": 2, "seq": 2, "event_type": "media_ready", "camera_id": 1,
                     "payload": {"source": "alert", "id": 42, "alert_id": "a-42",
                                 "images": ["snapshot.jpg"],
                                 "clip": {"start": "2026-09-18T10:00:00+00:00",
                                          "duration_s": 12.0}}})
    await hass.async_block_till_done()
    [r] = ready
    assert r.data["source"] == "alert" and r.data["title"] == "Loitering at the gate"
    assert r.data["severity"] == "high"
    assert r.data["clip_url"].startswith(f"/api/opennvr/{SITE_ID}/m/{TOKEN}?authSig=")
    assert r.data["app"] == "loitering"
    clip = mock_client.sign_media.call_args
    assert clip.args == ("clip",) and clip.kwargs["camera_id"] == 1
    assert clip.kwargs["duration_s"] == 12.0


async def test_event_media_ready_and_hidden_cameras(
        hass: HomeAssistant, entry, mock_client: MagicMock,
        mock_stream: type[FakeStream]) -> None:
    ready = async_capture_events(hass, EVENT_MEDIA_READY)
    [stream] = mock_stream.instances
    stream.on_frame({"v": 2, "seq": 1, "event_type": "media_ready", "camera_id": 3,
                     "payload": {"source": "event", "id": 7, "label": "car",
                                 "images": ["scene", "evidence"],
                                 "clip": {"start": "2026-09-18T10:00:00+00:00",
                                          "duration_s": 20}}})
    stream.on_frame({"v": 2, "seq": 2, "event_type": "media_ready", "camera_id": 99,
                     "payload": {"source": "event", "id": 8, "images": []}})
    await hass.async_block_till_done()
    [r] = ready                                      # camera 99 isn't shown here
    assert r.data["label"] == "car" and r.data["camera_entity_id"] == "camera.garage"
    assert mock_client.sign_media.call_args_list[0].kwargs["name"] == "evidence"


async def test_relay_forwards_only_signed_tokens_to_authenticated_callers(
        hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry,
        hass_client: ClientSessionGenerator,
        hass_client_no_auth: ClientSessionGenerator) -> None:
    aioclient_mock.get(f"{URL}/api/v1/media/s/{TOKEN}", content=b"\xff\xd8img",
                       headers={"Content-Type": "image/jpeg"})
    path = f"/api/opennvr/{SITE_ID}/m/{TOKEN}"
    # Reachable by anyone on the internet through HA Cloud: without a login
    # or an HA signature, the token in the path alone unlocks nothing.
    anon = await hass_client_no_auth()
    assert (await anon.get(path)).status == 401
    assert aioclient_mock.call_count == 0
    # A notification fetcher has no login; its URL is signed by HA instead.
    signed = async_sign_path(hass, path, timedelta(hours=1))
    resp = await anon.get(signed)
    assert resp.status == 200 and await resp.read() == b"\xff\xd8img"
    stale = async_sign_path(hass, path, timedelta(seconds=-1))
    assert (await anon.get(stale)).status == 401
    # Signed for one path only: the signature does not carry over.
    other = f"/api/opennvr/{SITE_ID}/m/{TOKEN[:-4]}BBBB" + signed[len(path):]
    assert (await anon.get(other)).status == 401
    # A logged-in browser (the media browser, a card) needs no signature.
    user = await hass_client()
    assert (await user.get(path)).status == 200
    for bad in ("m1.x.y", "not-a-token", f"{TOKEN}/../../x", "m1.a.b..c"):
        assert (await user.get(f"/api/opennvr/{SITE_ID}/m/{bad}")).status == 404
    assert (await user.get(f"/api/opennvr/another-site/m/{TOKEN}")).status == 404
    assert aioclient_mock.call_count == 2
    # A well-formed token OpenNVR refuses (tampered, expired): gone, not an error.
    expired = TOKEN[:-4] + "AAAA"
    aioclient_mock.get(f"{URL}/api/v1/media/s/{expired}", status=403)
    assert (await user.get(f"/api/opennvr/{SITE_ID}/m/{expired}")).status == 404


async def test_notification_urls_are_signed_for_the_link_lifetime(
        hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry,
        mock_stream: type[FakeStream], hass_client_no_auth: ClientSessionGenerator,
        freezer: FrozenDateTimeFactory) -> None:
    """The event's URL works for a fetcher with no login exactly as long as
    the entry's "notification link lifetime" (24 h by default)."""
    aioclient_mock.get(f"{URL}/api/v1/media/s/{TOKEN}", content=b"\xff\xd8img")
    ready = async_capture_events(hass, EVENT_MEDIA_READY)
    [stream] = mock_stream.instances
    stream.on_frame({"v": 2, "seq": 1, "event_type": "media_ready", "camera_id": 1,
                     "payload": {"source": "event", "id": 7, "images": ["evidence"]}})
    await hass.async_block_till_done()
    [r] = ready
    anon = await hass_client_no_auth()
    assert (await anon.get(r.data["image_url"])).status == 200
    freezer.tick(timedelta(hours=23))
    assert (await anon.get(r.data["image_url"])).status == 200
    freezer.tick(timedelta(hours=2))
    assert (await anon.get(r.data["image_url"])).status == 401


async def _blueprint_automation(hass: HomeAssistant, **inputs) -> None:
    target = Path(hass.config.path("blueprints/automation/opennvr/alert_notification.yaml"))
    await hass.async_add_executor_job(
        lambda: (target.parent.mkdir(parents=True, exist_ok=True),
                 shutil.copy(BLUEPRINT, target)))
    assert await async_setup_component(hass, "automation", {"automation": {
        "use_blueprint": {"path": "opennvr/alert_notification.yaml",
                          "input": {"notify_service": "notify.test_phone", **inputs}}}})
    await hass.async_block_till_done()


async def _settle(calls: list, want: int) -> None:
    """The automation waits up to an hour for the "Acknowledge" tap, so
    ``async_block_till_done`` would wait with it: poll for the call instead."""
    for _ in range(200):
        if len(calls) >= want:
            return
        await asyncio.sleep(0.01)


def _ready(severity: str = "high", **extra) -> dict:
    return {"source": "alert", "config_entry_id": "E1", "site_id": SITE_ID, "id": 5,
            "camera_id": 1, "camera_entity_id": "camera.front_door", "severity": severity,
            "title": "Loitering", "app": "loitering",
            "image_url": f"/api/opennvr/{SITE_ID}/m/{TOKEN}",
            "clip_url": f"/api/opennvr/{SITE_ID}/m/{TOKEN}", **extra}


async def test_blueprint_notifies_and_acknowledges(hass: HomeAssistant) -> None:
    notify = async_mock_service(hass, "notify", "test_phone")
    ack = async_mock_service(hass, "opennvr", "ack_alerts")
    await _blueprint_automation(hass, cooldown=0)
    hass.bus.async_fire(EVENT_MEDIA_READY, _ready("low"))
    await _settle(notify, 1)
    assert notify == []                               # below the minimum severity
    hass.bus.async_fire(EVENT_MEDIA_READY, _ready("critical"))
    await _settle(notify, 1)
    [call] = notify
    assert call.data["title"] == "Critical: Loitering"
    assert call.data["message"] == "OpenNVR · loitering"
    data = call.data["data"]
    assert data["image"].startswith(f"/api/opennvr/{SITE_ID}/m/")
    assert data["push"]["interruption-level"] == "critical"
    assert data["actions"][0]["action"] == "OPENNVR_ACK_E1_5"
    assert data["actions"][1]["uri"] == "entityId:camera.front_door"
    hass.bus.async_fire("mobile_app_notification_action", {"action": "OPENNVR_ACK_E1_5"})
    await _settle(ack, 1)
    [acked] = ack
    assert acked.data["config_entry_id"] == "E1" and acked.data["alert_ids"] in ([5], ["5"])


async def test_blueprint_filters(hass: HomeAssistant) -> None:
    notify = async_mock_service(hass, "notify", "test_phone")
    hass.states.async_set("alarm_control_panel.opennvr_site_mode", "disarmed")
    await _blueprint_automation(
        hass, cameras=["camera.garage"], cooldown=0,
        alarm_panel="alarm_control_panel.opennvr_site_mode")
    hass.bus.async_fire(EVENT_MEDIA_READY, _ready("high", camera_entity_id="camera.garage"))
    await _settle(notify, 1)
    assert notify == []                               # disarmed
    hass.states.async_set("alarm_control_panel.opennvr_site_mode", "armed_away")
    hass.bus.async_fire(EVENT_MEDIA_READY, _ready("high"))       # another camera
    await _settle(notify, 1)
    assert notify == []
    hass.bus.async_fire(EVENT_MEDIA_READY, _ready("high", camera_entity_id="camera.garage"))
    await _settle(notify, 1)
    assert len(notify) == 1
