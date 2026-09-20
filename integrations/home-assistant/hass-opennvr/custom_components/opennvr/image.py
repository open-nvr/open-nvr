"""Image entities the server describes: the last object seen on a camera.

The state names the event whose evidence image to show (``event_id``,
``image``). The bytes are fetched through a short-lived signed media URL, so
this entity needs no more than the token's ``recordings.view``, and a
self-signed certificate is handled like every other call.
"""

from __future__ import annotations

from datetime import datetime
import logging

from pyopennvr import EntityDescriptor, OpenNVRError
import aiohttp

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import OpenNVRConfigEntry
from .coordinator import OpenNVRCoordinator
from .descriptor import OpenNVRDescriptorEntity, async_setup_platform_entities

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

#: A signed URL only has to outlive the one fetch that follows.
SIGNED_TTL_S = 120


async def async_setup_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    entry.async_on_unload(async_setup_platform_entities(
        entry.runtime_data.coordinator, "image", OpenNVRImage, async_add_entities))


class OpenNVRImage(OpenNVRDescriptorEntity, ImageEntity):
    _attr_content_type = "image/jpeg"

    def __init__(self, coordinator: OpenNVRCoordinator, desc: EntityDescriptor) -> None:
        OpenNVRDescriptorEntity.__init__(self, coordinator, desc)
        ImageEntity.__init__(self, coordinator.hass)
        self._cached: tuple[tuple[int, str], bytes] | None = None

    @property
    def image_last_updated(self) -> datetime | None:
        state = self.raw_state
        return dt_util.parse_datetime(state) if isinstance(state, str) else None

    def _source(self) -> tuple[int, str] | None:
        attrs = (self.resolved or {}).get("attributes") or {}
        event_id = attrs.get("event_id")
        if not isinstance(event_id, int) or isinstance(event_id, bool):
            return None
        return event_id, str(attrs.get("image") or "evidence")

    async def async_image(self) -> bytes | None:
        source = self._source()
        if source is None:
            return None
        if self._cached and self._cached[0] == source:
            return self._cached[1]
        client = self.coordinator.client
        try:
            media = await client.sign_media("event", id=source[0], name=source[1],
                                            ttl_s=SIGNED_TTL_S)
            session = async_get_clientsession(self.hass, client.ssl is not False)
            async with session.get(media.url, ssl=client.ssl,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    _LOGGER.debug("%s: image fetch returned %s", self.key, resp.status)
                    return None
                data = await resp.read()
                self._attr_content_type = resp.content_type or "image/jpeg"
        except (OpenNVRError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("%s: image fetch failed: %s", self.key, err)
            return None
        self._cached = (source, data)
        return data
