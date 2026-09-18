# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""REST client against the contract fixtures (the server's own examples)."""

from __future__ import annotations

from pathlib import Path

import pytest

import pyopennvr
from pyopennvr import (
    OpenNVRAuthError,
    OpenNVRClient,
    OpenNVRConnectionError,
    OpenNVRContractError,
    OpenNVRNotFoundError,
    OpenNVRRequestError,
    check_contract,
)

from .conftest import FIX, fake_site, fixture

TOKEN = "onvr_abcdef12_secret"


def test_fixtures_match_the_servers_copy():
    """In the monorepo the server's fixtures are the source; the copy here is
    what survives the repo split. They must not drift."""
    server = Path(__file__).resolve().parents[4] / "server" / "contract" / "fixtures"
    if not server.is_dir():
        pytest.skip("not in the OpenNVR monorepo")
    for f in server.glob("*.json"):
        assert (FIX / f.name).read_text(encoding="utf-8") == f.read_text(encoding="utf-8"), f.name


async def test_system_info_and_contract():
    async with fake_site({("GET", "/api/v1/system/info"): fixture("system_info")}) as (
            base, session, _):
        info = await OpenNVRClient(base, TOKEN, session).get_system_info()
    assert info.site_id == "0755a940-1ff5-4861-ac08-1f57bb29a180"
    assert info.has("entities") and not info.recording_pause_enabled
    check_contract(info)
    newer = pyopennvr.SystemInfo.from_dict({**fixture("system_info"), "contract_version": "2.0.0"})
    with pytest.raises(OpenNVRContractError) as exc:
        check_contract(newer)
    assert exc.value.server_version == "2.0.0"


async def test_requests_carry_the_token_and_correlation_id():
    async with fake_site({("PUT", "/api/v1/site-mode"): fixture("site_mode")}) as (
            base, session, rec):
        mode = await OpenNVRClient(base, TOKEN, session).set_site_mode(
            "armed_away", correlation_id="ha-automation-7")
    req = rec.requests[0]
    assert req["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert req["headers"]["X-Correlation-Id"] == "ha-automation-7"
    assert req["body"] == b'{"mode": "armed_away", "reason": null}'
    assert mode.mode == "armed_away"


@pytest.mark.parametrize("status, error", [
    (401, OpenNVRAuthError), (403, OpenNVRAuthError), (404, OpenNVRNotFoundError),
    (422, OpenNVRRequestError), (503, OpenNVRConnectionError),
])
async def test_http_errors_map_to_library_errors(status, error):
    async with fake_site({("GET", "/api/v1/site-mode"): (status, {"detail": "nope"})}) as (
            base, session, _):
        with pytest.raises(error):
            await OpenNVRClient(base, TOKEN, session).get_site_mode()


async def test_unreachable_is_a_connection_error():
    import aiohttp

    async with aiohttp.ClientSession() as session:
        client = OpenNVRClient("http://127.0.0.1:9", TOKEN, session, request_timeout=2)
        with pytest.raises(OpenNVRConnectionError):
            await client.get_site_mode()


async def test_entities_skip_unknown_platforms_and_honour_the_etag():
    catalog = fixture("entity_list")
    catalog["entities"].append({"key": "x.y", "platform": "lock", "name": "Future",
                                "device": {"kind": "site", "id": "site"}})

    async def entities(request):
        from aiohttp import web

        if request.headers.get("If-None-Match") == f'"{catalog["etag"]}"':
            return web.Response(status=304)
        return web.json_response(catalog)

    async with fake_site({("GET", "/api/v1/entities"): entities}) as (base, session, rec):
        client = OpenNVRClient(base, TOKEN, session)
        got = await client.get_entities()
        again = await client.get_entities(etag=got.etag)
    assert [d.key for d in got.descriptors] == [
        e["key"] for e in fixture("entity_list")["entities"]]
    assert [s["key"] for s in got.skipped] == ["x.y"]
    det = next(d for d in got.descriptors if d.key == "camera.1.detection")
    assert det.command == {"type": "core_control", "control": "detection"}
    assert again is None


async def test_stream_and_media_urls_are_re_rooted_on_this_site():
    info = {"camera_id": 1, "stream_name": "cam-1", "token": "jwt",
            "urls": {"webrtc": "https://localhost/webrtc/cam-1/whep",
                     "rtsps": "rtsps://localhost:8322/cam-1"}}
    async with fake_site({("GET", "/api/v1/streams/1/info"): info,
                          ("POST", "/api/v1/media/sign"): fixture("media_sign")}) as (
            base, session, rec):
        client = OpenNVRClient(base, TOKEN, session)
        stream = await client.get_stream_info(1)
        media = await client.sign_media("event", id=5)
    assert stream.webrtc_url == f"{base}/webrtc/cam-1/whep"
    assert media.url.startswith(f"{base}/api/v1/media/s/m1.")
    assert rec.requests[1]["body"] == b'{"kind": "event", "id": 5}'


async def test_query_params_drop_none_and_rename_from():
    async with fake_site({("GET", "/api/v1/search"): fixture("search")}) as (
            base, session, rec):
        await OpenNVRClient(base, TOKEN, session).search(
            q="loiter", camera_id=None, from_="2026-09-18T00:00:00Z")
    assert rec.requests[0]["query"] == {"q": "loiter", "from": "2026-09-18T00:00:00Z"}


async def test_json_bodies_keep_booleans():
    async with fake_site({("PUT", "/api/v1/cameras/3"): {"id": 3, "name": "c",
                                                         "is_active": True,
                                                         "detection_enabled": False}}) as (
            base, session, rec):
        cam = await OpenNVRClient(base, TOKEN, session).set_detection(3, False)
    assert rec.requests[0]["body"] == b'{"detection_enabled": false, "reason": null}'
    assert cam.detection_enabled is False


def test_ws_url():
    client = OpenNVRClient("https://nvr.local/", TOKEN, session=None)  # type: ignore[arg-type]
    assert client.ws_url("T", since=5, epoch="e1", types=["entity_state"]) == (
        "wss://nvr.local/api/v1/events/ws?ticket=T&v=2&since=5&epoch=e1&types=entity_state")
    assert "since" not in client.ws_url("T", since=5, epoch=None)
