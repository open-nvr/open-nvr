"""Media browser and the HA-authenticated media proxy."""

from __future__ import annotations

from unittest.mock import MagicMock

from pyopennvr import EntityCatalog, OpenNVRNotFoundError, SignedMedia
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from homeassistant.components.media_source import (
    Unresolvable,
    async_browse_media,
    async_resolve_media,
)
from homeassistant.core import HomeAssistant

from . import URL, create_mock_config_entry, load_fixture, setup_mock_config_entry
from .conftest import FakeStream

ROOT = "media-source://opennvr"


@pytest.fixture
async def entry(hass: HomeAssistant, mock_client: MagicMock, mock_stream: type[FakeStream]):
    catalog = load_fixture("entity_list")
    catalog["entities"].append({
        "key": "camera.1.detections", "platform": "event", "name": "Detection",
        "device": {"kind": "camera", "id": 1}, "camera_id": 1, "required_scope": "x",
        "origin": "core", "enabled_default": True, "descriptor_version": 1,
        "event_types": ["person", "car"]})
    mock_client.get_entities.return_value = EntityCatalog.from_dict(catalog)
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    return entry


async def test_browse_tree(hass: HomeAssistant, entry, mock_client: MagicMock) -> None:
    root = await async_browse_media(hass, ROOT)
    assert [c.title for c in root.children] == ["OpenNVR"]
    site = await async_browse_media(hass, f"{ROOT}/{entry.entry_id}")
    assert [c.title for c in site.children] == ["Alerts", "Events", "Recordings"]

    mock_client.get_alerts.return_value = {"alerts": [], "total": 7}
    alerts = await async_browse_media(hass, f"{ROOT}/{entry.entry_id}/alerts")
    assert [c.title for c in alerts.children] == [
        "Critical (7)", "High (7)", "Medium (7)", "Low (7)"]

    events = await async_browse_media(hass, f"{ROOT}/{entry.entry_id}/events")
    assert [c.title for c in events.children] == ["Front door", "Garage"]
    labels = await async_browse_media(hass, f"{ROOT}/{entry.entry_id}/events/1")
    assert [c.title for c in labels.children] == ["All", "Person", "Car"]

    days = await async_browse_media(hass, f"{ROOT}/{entry.entry_id}/recordings/3")
    assert len(days.children) == 7
    hours = await async_browse_media(hass, days.children[1].media_content_id)
    assert len(hours.children) == 24 and hours.children[0].title == "23:00–00:00"
    played = await async_resolve_media(hass, hours.children[0].media_content_id, None)
    assert played.mime_type == "video/mp4" and "/clip/3/" in played.url
    assert played.url.endswith("/3600") or "/3600?" in played.url


async def test_alert_pages(hass: HomeAssistant, entry, mock_client: MagicMock) -> None:
    mock_client.get_alerts.return_value = {"total": 51, "alerts": [
        {"id": 9, "fired_at": "2026-09-18T10:00:00+00:00", "title": "Loitering",
         "images": ["snapshot.jpg", "crop.jpg"]},
        {"id": 8, "fired_at": "2026-09-18T09:00:00+00:00", "title": "No image", "images": []},
    ]}
    page = await async_browse_media(hass, f"{ROOT}/{entry.entry_id}/alerts/high/0")
    assert mock_client.get_alerts.call_args.kwargs == {"severity": "high", "skip": 0,
                                                       "limit": 50}
    [alert, more] = page.children
    assert "Loitering" in alert.title and alert.can_play
    assert alert.thumbnail == f"/api/opennvr/{entry.entry_id}/alert/9/snapshot.jpg"
    assert more.title == "More…" and more.media_content_id.endswith("/alerts/high/1")
    played = await async_resolve_media(hass, alert.media_content_id, None)
    assert played.mime_type == "image/jpeg" and "/alert/9/snapshot.jpg" in played.url


async def test_event_pages(hass: HomeAssistant, entry, mock_client: MagicMock) -> None:
    mock_client.get_events.return_value = {"total": 2, "events": [
        {"id": 5, "label": "car", "started_at": "2026-09-18T10:00:00+00:00",
         "ended_at": "2026-09-18T10:00:30+00:00", "evidence_url": "/api/v1/events/5/evidence",
         "plate_text": "KA01AB1234"},
        {"id": 6, "label": "car", "started_at": "2026-09-18T11:00:00+00:00",
         "ended_at": None, "evidence_url": None},
    ]}
    page = await async_browse_media(hass, f"{ROOT}/{entry.entry_id}/events/1/car/0")
    kw = mock_client.get_events.call_args.kwargs
    assert kw["camera_id"] == 1 and kw["label"] == "car" and kw["limit"] == 50
    [first, second] = page.children
    assert "KA01AB1234" in first.title
    assert first.thumbnail == f"/api/opennvr/{entry.entry_id}/event/5/evidence"
    assert second.thumbnail is None
    played = await async_resolve_media(hass, first.media_content_id, None)
    # 30 s event, padded 5 s either side, starting 5 s early.
    assert "/clip/1/1789725595/40" in played.url
    played = await async_resolve_media(hass, second.media_content_id, None)
    assert played.url.split("?")[0].endswith("/30")       # open: 20 s + padding


async def test_unknown_ids(hass: HomeAssistant, entry) -> None:
    from homeassistant.components.media_player import BrowseError

    with pytest.raises(Unresolvable):
        await async_resolve_media(hass, f"{ROOT}/{entry.entry_id}/nope/1", None)
    with pytest.raises(BrowseError):
        await async_browse_media(hass, f"{ROOT}/{entry.entry_id}/events/99")
    with pytest.raises(BrowseError):
        await async_browse_media(hass, f"{ROOT}/not-an-entry")


async def test_proxy_streams_signed_media(
        hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry,
        mock_client: MagicMock, hass_client: ClientSessionGenerator) -> None:
    # aioclient_mock before `entry`: HA's shared session is made during setup.
    mock_client.sign_media.return_value = SignedMedia(url=f"{URL}/api/v1/media/s/m1.x",
                                                      expires_at="x")
    aioclient_mock.get(f"{URL}/api/v1/media/s/m1.x", content=b"\xff\xd8jpeg",
                       headers={"Content-Type": "image/jpeg"})
    client = await hass_client()
    resp = await client.get(f"/api/opennvr/{entry.entry_id}/event/5/evidence")
    assert resp.status == 200 and await resp.read() == b"\xff\xd8jpeg"
    assert resp.headers["Content-Type"] == "image/jpeg"
    assert mock_client.sign_media.call_args.kwargs == {"id": 5, "name": "evidence",
                                                       "ttl_s": 120}
    resp = await client.get(f"/api/opennvr/{entry.entry_id}/clip/1/1789725600/30",
                            headers={"Range": "bytes=0-99"})
    assert resp.status == 200
    call = mock_client.sign_media.call_args
    assert call.args == ("clip",) and call.kwargs["duration_s"] == 30
    assert call.kwargs["start"] == "2026-09-18T10:00:00+00:00"
    assert aioclient_mock.mock_calls[-1][3]["Range"] == "bytes=0-99"


@pytest.mark.parametrize("path", [
    "/api/opennvr/{entry}/event/5/../../secrets",
    "/api/opennvr/{entry}/event/5/not_an_image",
    "/api/opennvr/{entry}/clip/1/1789725600/99999",
    "/api/opennvr/nope/event/5/evidence",
])
async def test_proxy_refuses(hass: HomeAssistant, entry, mock_client: MagicMock,
                             hass_client: ClientSessionGenerator, path: str) -> None:
    client = await hass_client()
    resp = await client.get(path.format(entry=entry.entry_id))
    assert resp.status == 404
    assert not mock_client.sign_media.called


async def test_proxy_needs_ha_auth_and_maps_errors(
        hass: HomeAssistant, entry, mock_client: MagicMock,
        hass_client: ClientSessionGenerator,
        hass_client_no_auth: ClientSessionGenerator) -> None:
    anon = await hass_client_no_auth()
    assert (await anon.get(f"/api/opennvr/{entry.entry_id}/event/5/evidence")).status == 401
    mock_client.sign_media.side_effect = OpenNVRNotFoundError("no such event")
    client = await hass_client()
    assert (await client.get(f"/api/opennvr/{entry.entry_id}/event/5/evidence")).status == 404
