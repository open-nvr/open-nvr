"""Notification-ready events (design §7.7).

For each OpenNVR alert, and each time an alert's or event's media is ready,
the integration fires a Home Assistant event carrying **relay URLs**: paths
under Home Assistant's own URL (``/api/opennvr/<site>/m/<token>``) that a
phone can fetch from anywhere it can reach HA, relative so the Companion app
resolves them against HA's URL.

* ``opennvr_alert``: when an alert lands (with its first image, if any).
* ``opennvr_media_ready``: when its clip can be played (source ``alert`` or
  ``event``), with the image and the clip; alerts carry their severity and
  title again, so one trigger has everything a notification needs.

Links live for the entry's "notification link lifetime" option.
"""

from __future__ import annotations

from collections import OrderedDict
import logging
from typing import Any

from pyopennvr import OpenNVRError

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import CONF_MEDIA_TTL, DEFAULT_MEDIA_TTL, DOMAIN, signal_frame
from .views import relay_path

_LOGGER = logging.getLogger(__name__)

EVENT_ALERT = f"{DOMAIN}_alert"
EVENT_MEDIA_READY = f"{DOMAIN}_media_ready"
#: Alerts remembered so their media_ready can repeat severity and title.
RECENT_ALERTS = 256


@callback
def async_setup_notifications(hass: HomeAssistant, entry) -> None:
    notifier = _Notifier(hass, entry)
    entry.async_on_unload(async_dispatcher_connect(
        hass, signal_frame(entry.entry_id), notifier.on_frame))


class _Notifier:
    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self._alerts: OrderedDict[int, dict[str, Any]] = OrderedDict()

    @property
    def _coordinator(self):
        return self.entry.runtime_data.coordinator

    @callback
    def on_frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("event_type")
        if kind not in ("app_alert", "media_ready"):
            return
        camera_id = frame.get("camera_id")
        shown = self._coordinator.data.cameras
        if camera_id is not None and camera_id not in shown:
            return  # a camera this entry doesn't show
        handler = self._alert if kind == "app_alert" else self._media_ready
        self.entry.async_create_task(self.hass, handler(frame))

    def _base(self, camera_id: int | None) -> dict[str, Any]:
        site = self._coordinator.data.info.site_id
        entity_id = None
        if camera_id is not None:
            entity_id = er.async_get(self.hass).async_get_entity_id(
                "camera", DOMAIN, f"{site}:camera.{camera_id}")
        return {"config_entry_id": self.entry.entry_id, "site_id": site,
                "camera_id": camera_id, "camera_entity_id": entity_id}

    async def _relay(self, kind: str, **params: Any) -> str | None:
        ttl = int(self.entry.options.get(CONF_MEDIA_TTL, DEFAULT_MEDIA_TTL)) * 3600
        try:
            media = await self._coordinator.client.sign_media(kind, ttl_s=ttl, **params)
        except OpenNVRError as err:
            _LOGGER.debug("Could not sign %s media: %s", kind, err)
            return None
        return relay_path(self._coordinator.data.info.site_id, media.url)

    async def _alert(self, frame: dict[str, Any]) -> None:
        p = frame.get("payload") or {}
        source = p.get("source") if isinstance(p.get("source"), dict) else {}
        info = {"id": p.get("id"), "alert_id": p.get("alert_id"),
                "severity": p.get("severity"), "title": p.get("title"),
                "app": source.get("name"), "alert_type": p.get("alert_type"),
                "correlation_id": p.get("correlation_id")}
        if isinstance(info["id"], int):
            self._alerts[info["id"]] = info
            while len(self._alerts) > RECENT_ALERTS:
                self._alerts.popitem(last=False)
        images = p.get("images") or []
        image_url = (await self._relay("alert_image", id=info["id"], name=images[0])
                     if images and isinstance(info["id"], int) else None)
        self.hass.bus.async_fire(EVENT_ALERT, {**self._base(frame.get("camera_id")), **info,
                                               "image_url": image_url})

    async def _media_ready(self, frame: dict[str, Any]) -> None:
        p = frame.get("payload") or {}
        source, obj_id = p.get("source"), p.get("id")
        if source not in ("alert", "event") or not isinstance(obj_id, int):
            return
        images = list(p.get("images") or [])
        clip = p.get("clip") if isinstance(p.get("clip"), dict) else {}
        camera_id = frame.get("camera_id")
        if source == "alert":
            image_url = (await self._relay("alert_image", id=obj_id, name=images[0])
                         if images else None)
            extra = self._alerts.get(obj_id, {"id": obj_id, "alert_id": p.get("alert_id")})
        else:
            name = "evidence" if "evidence" in images else (images[0] if images else None)
            image_url = (await self._relay("event", id=obj_id, name=name)
                         if name else None)
            extra = {"id": obj_id, "label": p.get("label"), "zone_ids": p.get("zone_ids")}
        clip_url = None
        if camera_id is not None and clip.get("start") and clip.get("duration_s"):
            clip_url = await self._relay("clip", camera_id=camera_id, start=clip["start"],
                                         duration_s=clip["duration_s"])
        self.hass.bus.async_fire(EVENT_MEDIA_READY, {
            **self._base(camera_id), **extra, "source": source,
            "image_url": image_url, "clip_url": clip_url})
