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

And one for notifications: ``/api/opennvr/<site_id>/m/<token>`` relays one
OpenNVR signed-media token to OpenNVR's ``/api/v1/media/s/``. It accepts
nothing but the token's shape; OpenNVR checks its signature, expiry and the
one object it names. It requires Home Assistant auth like every other view;
a fetcher that cannot log in (a phone's notification fetcher) is given the
path SIGNED by Home Assistant (``signed_relay_path``, ``?authSig=``), which
the Companion apps resolve against HA's URL like any other relative
attachment path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
import logging
import re

from aiohttp import ClientError, ClientTimeout, web
from pyopennvr import OpenNVRAuthError, OpenNVRError, OpenNVRNotFoundError

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

URL_BASE = f"/api/{DOMAIN}"
#: Signed URLs for a proxied fetch only have to outlive that fetch.
SIGNED_TTL_S = 120
#: The server's own limit for one clip.
MAX_CLIP_S = 3600
EVENT_IMAGES = ("evidence", "scene", "plate", "plate_frame")
ALERT_IMAGE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
#: OpenNVR's signed-media token: ``m1.<payload>.<signature>``, base64url.
_SIGNED_TOKEN = re.compile(r"^m1\.[A-Za-z0-9_-]{1,2048}\.[A-Za-z0-9_-]{16,128}$")
#: Response headers worth passing on from OpenNVR.
_PASS_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
                 "Content-Disposition")


def relay_path(site_id: str, signed_url: str) -> str | None:
    """The relay path for an OpenNVR signed-media URL (``.../media/s/<token>``)."""
    token = signed_url.rsplit("/media/s/", 1)[-1]
    return f"{URL_BASE}/{site_id}/m/{token}" if _SIGNED_TOKEN.fullmatch(token) else None


@callback
def signed_relay_path(hass: HomeAssistant, site_id: str, signed_url: str,
                      ttl_s: int) -> str | None:
    """The relay path, signed by Home Assistant for ``ttl_s`` seconds (as
    long as OpenNVR's own token lives), for a fetcher with no HA login."""
    path = relay_path(site_id, signed_url)
    if path is None:
        return None
    return async_sign_path(hass, path, timedelta(seconds=ttl_s))


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

    def _entry(self, entry_id: str):
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if (entry is None or entry.domain != DOMAIN
                or entry.state is not ConfigEntryState.LOADED):
            raise web.HTTPNotFound
        return entry

    async def _client(self, entry_id: str):
        """The viewer client: this entry's cameras only, reading only."""
        entry = self._entry(entry_id)
        try:
            return await entry.runtime_data.coordinator.async_viewer_client()
        except OpenNVRAuthError as err:
            raise web.HTTPForbidden from err
        except OpenNVRError as err:
            raise web.HTTPBadGateway from err

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
        return await self._stream(request, client, media.url)

    async def _stream(self, request: web.Request, client, url: str) -> web.StreamResponse:
        session = async_get_clientsession(self.hass, client.ssl is not False)
        # Identity: the body is passed on as it comes, and must be the bytes
        # the Content-Length we copy describes (a transparently decompressed
        # gzip body would be cut short at the compressed length).
        headers = {"Accept-Encoding": "identity"}
        if "Range" in request.headers:
            headers["Range"] = request.headers["Range"]
        response: web.StreamResponse | None = None
        try:
            async with session.get(url, ssl=client.ssl, headers=headers,
                                   timeout=ClientTimeout(total=None, sock_connect=10,
                                                         sock_read=60)) as upstream:
                if upstream.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN,
                                       HTTPStatus.NOT_FOUND, HTTPStatus.GONE):
                    # Invalid, expired or not permitted: to the caller, gone.
                    raise web.HTTPNotFound
                if upstream.status not in (HTTPStatus.OK, HTTPStatus.PARTIAL_CONTENT):
                    _LOGGER.debug("OpenNVR media answered %s", upstream.status)
                    raise web.HTTPBadGateway
                response = web.StreamResponse(status=upstream.status)
                encoded = "Content-Encoding" in upstream.headers
                for name in _PASS_HEADERS:
                    if name in upstream.headers and not (encoded and name == "Content-Length"):
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
        client = await self._client(entry_id)
        return await self._proxy(request, client, lambda: client.sign_media(
            "event", id=int(event_id), name=image, ttl_s=SIGNED_TTL_S))


class AlertImageView(_MediaView):
    url = URL_BASE + "/{entry_id}/alert/{alert_id:[0-9]+}/{name}"
    name = f"api:{DOMAIN}:alert_image"

    async def get(self, request: web.Request, entry_id: str, alert_id: str,
                  name: str) -> web.StreamResponse:
        if not ALERT_IMAGE_NAME.fullmatch(name):
            raise web.HTTPNotFound
        client = await self._client(entry_id)
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
        if int(camera_id) not in self._entry(entry_id).runtime_data.coordinator.data.cameras:
            raise web.HTTPNotFound  # a camera this entry doesn't show
        client = await self._client(entry_id)
        begin = datetime.fromtimestamp(int(start), UTC).isoformat()
        return await self._proxy(request, client, lambda: client.sign_media(
            "clip", camera_id=int(camera_id), start=begin, duration_s=duration,
            ttl_s=SIGNED_TTL_S))


class RelayView(_MediaView):
    """Signed media for notifications; see the module docstring.

    Home Assistant auth is required (a signed path counts). The view once
    ran without it, reasoning that the OpenNVR token in the path is itself a
    credential; but anyone who can reach HA at all (over Nabu Casa, the
    internet) could then use HA as an open relay into OpenNVR's media
    endpoint with any token they got hold of, and HA's own protections
    (login, IP bans, the signed-path TTL) would not apply to camera footage.
    """

    url = URL_BASE + "/{site_id}/m/{token}"
    name = f"api:{DOMAIN}:relay"

    async def get(self, request: web.Request, site_id: str,
                  token: str) -> web.StreamResponse:
        if not _SIGNED_TOKEN.fullmatch(token):
            raise web.HTTPNotFound
        entry = next((e for e in self.hass.config_entries.async_entries(DOMAIN)
                      if e.unique_id == site_id
                      and e.state is ConfigEntryState.LOADED), None)
        if entry is None:
            raise web.HTTPNotFound
        client = entry.runtime_data.client
        # Built from the configured site and the checked token only.
        return await self._stream(request, client,
                                  f"{client.base_url}/api/v1/media/s/{token}")


def passthrough_allowed(path: str, allowlist: tuple[str, ...]) -> bool:
    """``/api/v1/cameras/`` allows everything below it (and itself without
    the slash); ``/api/v1/search`` allows itself and what is below it."""
    for prefix in allowlist:
        base = prefix.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return True
    return False


class PassthroughView(_MediaView):
    """Read-only API relay for dashboard cards whose browser can't reach
    OpenNVR (design §7.8): GET only, and only below the prefixes the server
    publishes in ``/system/info`` (``passthrough_allowlist``), with the
    integration's token. HA users only."""

    url = URL_BASE + "/{site_id}/passthrough/{path:.+}"
    name = f"api:{DOMAIN}:passthrough"

    async def get(self, request: web.Request, site_id: str, path: str) -> web.Response:
        entry = next((e for e in self.hass.config_entries.async_entries(DOMAIN)
                      if e.unique_id == site_id
                      and e.state is ConfigEntryState.LOADED), None)
        if entry is None:
            raise web.HTTPNotFound
        full = "/" + path
        segments = full.split("/")
        if (".." in segments or "." in segments or "//" in full or "\\" in full
                or "%" in full or not full.startswith("/api/v1/")):
            raise web.HTTPNotFound
        if not passthrough_allowed(full,
                                   entry.runtime_data.coordinator.data.info.passthrough_allowlist):
            raise web.HTTPNotFound
        try:
            client = await entry.runtime_data.coordinator.async_viewer_client()
            data = await client.request("GET", full[len("/api/v1"):], params=dict(request.query))
        except OpenNVRNotFoundError as err:
            raise web.HTTPNotFound from err
        except OpenNVRAuthError as err:
            raise web.HTTPForbidden from err
        except OpenNVRError as err:
            raise web.HTTPBadGateway from err
        return web.json_response(data)


def async_register_views(hass: HomeAssistant) -> None:
    for view in (EventImageView, AlertImageView, ClipView, RelayView, PassthroughView):
        hass.http.register_view(view(hass))
