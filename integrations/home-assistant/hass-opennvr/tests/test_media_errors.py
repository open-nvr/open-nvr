"""How the media proxy, passthrough, browser and image entity fail."""

from __future__ import annotations

from unittest.mock import MagicMock

import aiohttp
from pyopennvr import (
    EntityCatalog,
    OpenNVRAuthError,
    OpenNVRConnectionError,
    OpenNVRNotFoundError,
    SignedMedia,
)
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from homeassistant.components.media_player import BrowseError
from homeassistant.components.media_source import async_browse_media, async_resolve_media
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.opennvr.const import DOMAIN

from . import SITE_ID, URL, create_mock_config_entry, load_fixture, setup_mock_config_entry
from .conftest import FakeStream

SIGNED = f"{URL}/api/v1/media/s/m1.x"


@pytest.fixture
async def entry(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
                mock_client: MagicMock, mock_stream: type[FakeStream]):
    mock_client.sign_media.return_value = SignedMedia(url=SIGNED, expires_at="x")
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    return entry


@pytest.mark.parametrize(("error", "status"), [
    (OpenNVRAuthError("lacks alerts.view", 403), 403),
    (OpenNVRConnectionError("down"), 502),
    (OpenNVRNotFoundError("no such alert"), 404),
])
async def test_signing_refused(hass: HomeAssistant, entry, mock_client: MagicMock,
                               hass_client: ClientSessionGenerator, error, status) -> None:
    mock_client.sign_media.side_effect = error
    client = await hass_client()
    resp = await client.get(f"/api/opennvr/{entry.entry_id}/alert/9/snapshot.jpg")
    assert resp.status == status


async def test_alert_image_names_are_checked(hass: HomeAssistant, entry, mock_client: MagicMock,
                                             hass_client: ClientSessionGenerator) -> None:
    client = await hass_client()
    # (HA's own security filter already answers 400 to encoded traversal.)
    resp = await client.get(f"/api/opennvr/{entry.entry_id}/alert/9/a*b")
    assert resp.status == 404 and not mock_client.sign_media.called


@pytest.mark.parametrize(("kwargs", "status"), [
    ({"status": 500}, 502),
    ({"exc": aiohttp.ClientConnectionError("reset")}, 502),
    ({"exc": TimeoutError()}, 502),
    ({"status": 410}, 404),
])
async def test_upstream_failures(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
                                 entry, hass_client: ClientSessionGenerator,
                                 kwargs, status) -> None:
    aioclient_mock.get(SIGNED, **kwargs)
    client = await hass_client()
    resp = await client.get(f"/api/opennvr/{entry.entry_id}/event/5/scene")
    assert resp.status == status


@pytest.mark.parametrize(("error", "status"), [
    (OpenNVRNotFoundError("gone"), 404),
    (OpenNVRAuthError("lacks it", 403), 403),
    (OpenNVRConnectionError("down"), 502),
])
async def test_passthrough_errors(hass: HomeAssistant, entry, mock_client: MagicMock,
                                  hass_client: ClientSessionGenerator, error, status) -> None:
    mock_client.request.side_effect = error
    client = await hass_client()
    resp = await client.get(f"/api/opennvr/{SITE_ID}/passthrough/api/v1/cameras/")
    assert resp.status == status
    resp = await client.get("/api/opennvr/another-site/passthrough/api/v1/cameras/")
    assert resp.status == 404


async def test_browser_failures(hass: HomeAssistant, entry, mock_client: MagicMock) -> None:
    eid = entry.entry_id
    mock_client.get_alerts.side_effect = OpenNVRAuthError("lacks alerts.view", 403)
    with pytest.raises(BrowseError, match="may not list"):
        await async_browse_media(hass, f"media-source://{DOMAIN}/{eid}/alerts")
    mock_client.get_events.side_effect = OpenNVRConnectionError("down")
    with pytest.raises(BrowseError, match="did not answer"):
        await async_browse_media(hass, f"media-source://{DOMAIN}/{eid}/events/1/all/0")
    for bad in ("alerts/extreme/0", "recordings/99", "recordings/1/not-a-date", "nope"):
        with pytest.raises(BrowseError):
            await async_browse_media(hass, f"media-source://{DOMAIN}/{eid}/{bad}")
    rec = await async_resolve_media(hass, f"media-source://{DOMAIN}/{eid}/rec/1/1789725600",
                                    None)
    assert "/clip/1/1789725600/3600" in rec.url


async def test_event_pages_link_the_next(hass: HomeAssistant, entry,
                                         mock_client: MagicMock) -> None:
    mock_client.get_events.return_value = {"total": 120, "events": [
        {"id": 1, "label": "car", "started_at": None},            # unusable: skipped
        {"id": 2, "label": "car", "started_at": "2026-09-18T10:00:00+00:00"},
    ]}
    page = await async_browse_media(
        hass, f"media-source://{DOMAIN}/{entry.entry_id}/events/1/all/0")
    assert [c.title for c in page.children][-1] == "More…"
    assert len(page.children) == 2
    root = await async_browse_media(hass, f"media-source://{DOMAIN}/{entry.entry_id}/recordings")
    assert [c.title for c in root.children] == ["Front door", "Garage"]


async def test_image_without_a_picture(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
                                       mock_client: MagicMock,
                                       mock_stream: type[FakeStream]) -> None:
    from homeassistant.components.image import async_get_image

    catalog = load_fixture("entity_list")
    catalog["entities"].append({
        "key": "camera.1.last_object", "platform": "image", "name": "Last object",
        "device": {"kind": "camera", "id": 1}, "camera_id": 1, "required_scope": "x",
        "origin": "core", "enabled_default": True, "descriptor_version": 1})
    mock_client.get_entities.return_value = EntityCatalog.from_dict(catalog)
    mock_client.get_entity_states.return_value = {
        "camera.1.last_object": {"state": None, "attributes": {}}}
    entry = create_mock_config_entry()
    await setup_mock_config_entry(hass, entry)
    entity_id = er.async_get(hass).async_get_entity_id(
        "image", DOMAIN, f"{SITE_ID}:camera.1.last_object")
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, entity_id)               # nothing seen yet
    coordinator = entry.runtime_data.coordinator
    coordinator.async_set_state("camera.1.last_object", {
        "state": "2026-09-18T10:00:00+00:00", "attributes": {"event_id": 3}})
    mock_client.sign_media.return_value = SignedMedia(url=SIGNED, expires_at="x")
    aioclient_mock.get(SIGNED, status=404)
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, entity_id)               # OpenNVR has no picture
    mock_client.sign_media.side_effect = OpenNVRConnectionError("down")
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, entity_id)
