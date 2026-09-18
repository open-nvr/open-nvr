"""HA-authenticated media proxy: OpenNVR images and clips under HA's URL.

The media browser, cards and anything else in Home Assistant load OpenNVR
media through these views, never from OpenNVR directly: a browser outside
the LAN (HA Cloud, a phone on mobile data) reaches Home Assistant, not
OpenNVR, and OpenNVR's certificate is usually self-signed.

* The paths carry no query string, so Home Assistant can sign them itself
  (``?authSig=``) for the media player and ``<img>`` tags (design R3).
* Each request asks OpenNVR for a short-lived signed URL for exactly that
  object (the token's scopes and cameras are checked there) and streams the
  bytes back; ``Range`` is passed through so video can seek where OpenNVR
  supports it.
* ``requires_auth``: only Home Assistant users; the entry must be loaded.

Paths (``<entry>`` is the config entry id):

* ``/api/opennvr/<entry>/event/<event_id>/<image>``: an event's image
  (``evidence``, ``scene``, ``plate``, ``plate_frame``);
* ``/api/opennvr/<entry>/alert/<alert_id>/<name>``: an alert's image;
* ``/api/opennvr/<entry>/clip/<camera_id>/<start_epoch>/<seconds>``: an MP4
  of that camera's recording.
"""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
import logging
import re

from aiohttp import ClientError, ClientTimeout, web
from pyopennvr import OpenNVRAuthError, OpenNVRError, OpenNVRNotFoundError

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

URL_BASE = f"/api/{DOMAIN}"
#: Signed URLs for a proxied fetch only have to outlive that fetch.
SIGNED_TTL_S = 120
#: The server's own limit for one clip.
MAX_CLIP_S = 3600
EVENT_IMAGES = ("evidence", "scene", "plate", "plate_frame")
_ALERT_IMAGE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
#: Response headers worth passing on from OpenNVR.
_PASS_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
                 "Content-Disposition")


def event_image_path(entry_id: str, event_id: int, image: str = "evidence") -> str:
    return f"{URL_BASE}/{entry_id}/event/{int(event_id)}/{image}"


def alert_image_path(entry_id: str, alert_id: int, name: str) -> str:
    return f"{URL_BASE}/{entry_id}/alert/{int(alert_id)}/{name}"


def clip_path(entry_id: str, camera_id: int, start: datetime, seconds: float) -> str:
    return (f"{URL_BASE}/{entry_id}/clip/{int(camera_id)}/{int(start.timestamp())}/"
            f"{max(1, min(int(seconds), MAX_CLIP_S))}")


class _MediaView(HomeAssistantView):
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _client(self, entry_id: str):
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if (entry is None or entry.domain != DOMAIN
                or entry.state is not ConfigEntryState.LOADED):
            raise web.HTTPNotFound
        return entry.runtime_data.client

    async def _proxy(self, request: web.Request, client, sign) -> web.StreamResponse:
        try:
            media = await sign()
        except OpenNVRNotFoundError as err:
            raise web.HTTPNotFound from err
        except OpenNVRAuthError as err:
            raise web.HTTPForbidden from err
        except OpenNVRError as err:
            _LOGGER.debug("Signing OpenNVR media failed: %s", err)
            raise web.HTTPBadGateway from err
        session = async_get_clientsession(self.hass, client.ssl is not False)
        headers = {"Range": request.headers["Range"]} if "Range" in request.headers else {}
        response: web.StreamResponse | None = None
        try:
            async with session.get(media.url, ssl=client.ssl, headers=headers,
                                   timeout=ClientTimeout(total=None, sock_connect=10,
                                                         sock_read=60)) as upstream:
                if upstream.status == HTTPStatus.NOT_FOUND:
                    raise web.HTTPNotFound
                if upstream.status not in (HTTPStatus.OK, HTTPStatus.PARTIAL_CONTENT):
                    _LOGGER.debug("OpenNVR media answered %s", upstream.status)
                    raise web.HTTPBadGateway
                response = web.StreamResponse(status=upstream.status)
                for name in _PASS_HEADERS:
                    if name in upstream.headers:
                        response.headers[name] = upstream.headers[name]
                response.headers["Cache-Control"] = "private, max-age=300"
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(1 << 16):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (ClientError, TimeoutError) as err:
            _LOGGER.debug("Fetching OpenNVR media failed: %s", err)
            if response is not None and response.prepared:
                return response  # headers are out; the client sees a short body
            raise web.HTTPBadGateway from err


class EventImageView(_MediaView):
    url = URL_BASE + "/{entry_id}/event/{event_id:[0-9]+}/{image}"
    name = f"api:{DOMAIN}:event_image"

    async def get(self, request: web.Request, entry_id: str, event_id: str,
                  image: str) -> web.StreamResponse:
        if image not in EVENT_IMAGES:
            raise web.HTTPNotFound
        client = self._client(entry_id)
        return await self._proxy(request, client, lambda: client.sign_media(
            "event", id=int(event_id), name=image, ttl_s=SIGNED_TTL_S))


class AlertImageView(_MediaView):
    url = URL_BASE + "/{entry_id}/alert/{alert_id:[0-9]+}/{name}"
    name = f"api:{DOMAIN}:alert_image"

    async def get(self, request: web.Request, entry_id: str, alert_id: str,
                  name: str) -> web.StreamResponse:
        if not _ALERT_IMAGE.fullmatch(name):
            raise web.HTTPNotFound
        client = self._client(entry_id)
        return await self._proxy(request, client, lambda: client.sign_media(
            "alert_image", id=int(alert_id), name=name, ttl_s=SIGNED_TTL_S))


class ClipView(_MediaView):
    url = URL_BASE + "/{entry_id}/clip/{camera_id:[0-9]+}/{start:[0-9]+}/{seconds:[0-9]+}"
    name = f"api:{DOMAIN}:clip"

    async def get(self, request: web.Request, entry_id: str, camera_id: str, start: str,
                  seconds: str) -> web.StreamResponse:
        duration = int(seconds)
        if not 0 < duration <= MAX_CLIP_S:
            raise web.HTTPNotFound
        client = self._client(entry_id)
        begin = datetime.fromtimestamp(int(start), UTC).isoformat()
        return await self._proxy(request, client, lambda: client.sign_media(
            "clip", camera_id=int(camera_id), start=begin, duration_s=duration,
            ttl_s=SIGNED_TTL_S))


def async_register_views(hass: HomeAssistant) -> None:
    for view in (EventImageView, AlertImageView, ClipView):
        hass.http.register_view(view(hass))
