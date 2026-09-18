"""Media browser (design §7.6).

    OpenNVR <site>
    ├── Alerts      → severity → alerts (image), newest first, 50 a page
    ├── Events      → camera → label → events (clip, thumbnail), 50 a page
    └── Recordings  → camera → day → hour (clip)

Everything plays through the HA-authenticated proxy (views.py), so it works
wherever Home Assistant is reachable, not only on the LAN. Identifiers start
with the config entry id:

* ``<entry>/alerts[/<severity>[/<page>]]``
* ``<entry>/events[/<camera>[/<label|all>[/<page>]]]``
* ``<entry>/recordings[/<camera>[/<YYYY-MM-DD>]]``
* playable: ``<entry>/alert/<id>/<image>``,
  ``<entry>/event/<id>/<camera>/<start_epoch>/<seconds>``,
  ``<entry>/rec/<camera>/<start_epoch>``

Recorded hours play as an MP4 clip of that hour; an hour without footage
fails to play rather than being hidden (OpenNVR lists no segments here).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pyopennvr import OpenNVRAuthError, OpenNVRError

from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .views import MAX_CLIP_S, alert_image_path, clip_path, event_image_path

PAGE = 50
SEVERITIES = ("critical", "high", "medium", "low")
RECORDING_DAYS = 7
#: Around an event's own span, so the clip shows the approach and the leaving.
CLIP_PAD_S = 5
#: An event still open (or without an end) plays this long.
OPEN_EVENT_S = 20


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    return OpenNVRMediaSource(hass)


def _folder(identifier: str, title: str, *, children_class: MediaClass = MediaClass.DIRECTORY,
            thumbnail: str | None = None) -> BrowseMediaSource:
    return BrowseMediaSource(domain=DOMAIN, identifier=identifier,
                             media_class=MediaClass.DIRECTORY,
                             media_content_type=MediaType.VIDEO, title=title, can_play=False,
                             can_expand=True, children_media_class=children_class,
                             thumbnail=thumbnail)


def _leaf(identifier: str, title: str, media_class: MediaClass, content_type: str,
          thumbnail: str | None = None) -> BrowseMediaSource:
    return BrowseMediaSource(domain=DOMAIN, identifier=identifier, media_class=media_class,
                             media_content_type=content_type, title=title, can_play=True,
                             can_expand=False, thumbnail=thumbnail)


def _local(value: str | None) -> str:
    when = dt_util.parse_datetime(value) if value else None
    return dt_util.as_local(when).strftime("%Y-%m-%d %H:%M:%S") if when else "?"


def _event_window(event: dict[str, Any]) -> tuple[datetime, int] | None:
    start = dt_util.parse_datetime(event.get("started_at") or "")
    if start is None:
        return None
    end = dt_util.parse_datetime(event.get("ended_at") or "") or (
        start + timedelta(seconds=OPEN_EVENT_S))
    seconds = int((end - start).total_seconds()) + 2 * CLIP_PAD_S
    return start - timedelta(seconds=CLIP_PAD_S), max(1, min(seconds, MAX_CLIP_S))


class OpenNVRMediaSource(MediaSource):
    name = "OpenNVR"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    # ── helpers ──────────────────────────────────────────────────────────

    def _entries(self) -> list:
        return [e for e in self.hass.config_entries.async_entries(DOMAIN)
                if e.state is ConfigEntryState.LOADED]

    def _entry(self, entry_id: str):
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise BrowseError(f"OpenNVR site {entry_id} is not connected")
        return entry

    @staticmethod
    async def _call(coro):
        try:
            return await coro
        except OpenNVRAuthError as err:
            raise BrowseError(f"The OpenNVR token may not list this: {err}") from err
        except OpenNVRError as err:
            raise BrowseError(f"OpenNVR did not answer: {err}") from err

    # ── resolve ──────────────────────────────────────────────────────────

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parts = (item.identifier or "").split("/")
        try:
            entry_id, kind = parts[0], parts[1]
            if kind == "alert" and len(parts) == 4:
                return PlayMedia(alert_image_path(entry_id, int(parts[2]), parts[3]),
                                 "image/jpeg")
            if kind == "event" and len(parts) == 6:
                start = datetime.fromtimestamp(int(parts[4]), dt_util.UTC)
                return PlayMedia(clip_path(entry_id, int(parts[3]), start, int(parts[5])),
                                 "video/mp4")
            if kind == "rec" and len(parts) == 4:
                start = datetime.fromtimestamp(int(parts[3]), dt_util.UTC)
                return PlayMedia(clip_path(entry_id, int(parts[2]), start, 3600), "video/mp4")
        except (IndexError, ValueError) as err:
            raise Unresolvable(f"Not an OpenNVR media id: {item.identifier}") from err
        raise Unresolvable(f"Not an OpenNVR media id: {item.identifier}")

    # ── browse ───────────────────────────────────────────────────────────

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        if not item.identifier:
            base = _folder("", "OpenNVR")
            base.children = [_folder(e.entry_id, e.title) for e in self._entries()]
            return base
        parts = item.identifier.split("/")
        entry = self._entry(parts[0])
        section = parts[1] if len(parts) > 1 else None
        try:
            if section is None:
                base = _folder(entry.entry_id, entry.title)
                base.children = [_folder(f"{entry.entry_id}/alerts", "Alerts"),
                                 _folder(f"{entry.entry_id}/events", "Events"),
                                 _folder(f"{entry.entry_id}/recordings", "Recordings")]
                return base
            if section == "alerts":
                return await self._alerts(entry, parts[2:])
            if section == "events":
                return await self._events(entry, parts[2:])
            if section == "recordings":
                return self._recordings(entry, parts[2:])
        except ValueError as err:
            raise BrowseError(f"Not an OpenNVR folder: {item.identifier}") from err
        raise BrowseError(f"Not an OpenNVR folder: {item.identifier}")

    async def _alerts(self, entry, rest: list[str]) -> BrowseMediaSource:
        client, eid = entry.runtime_data.client, entry.entry_id
        if not rest:
            base = _folder(f"{eid}/alerts", "Alerts")
            base.children = []
            for severity in SEVERITIES:
                total = (await self._call(client.get_alerts(severity=severity,
                                                            limit=1))).get("total", 0)
                base.children.append(_folder(f"{eid}/alerts/{severity}/0",
                                             f"{severity.title()} ({total})",
                                             children_class=MediaClass.IMAGE))
            return base
        severity, page = rest[0], int(rest[1]) if len(rest) > 1 else 0
        if severity not in SEVERITIES:
            raise ValueError(severity)
        found = await self._call(client.get_alerts(severity=severity, skip=page * PAGE,
                                                   limit=PAGE))
        base = _folder(f"{eid}/alerts/{severity}/{page}", f"{severity.title()} alerts",
                       children_class=MediaClass.IMAGE)
        base.children = []
        for alert in found.get("alerts", []):
            images = alert.get("images") or []
            title = f"{_local(alert.get('fired_at'))} · {alert.get('title') or 'Alert'}"
            if images:
                path = alert_image_path(eid, alert["id"], images[0])
                base.children.append(_leaf(f"{eid}/alert/{alert['id']}/{images[0]}", title,
                                           MediaClass.IMAGE, "image/jpeg", thumbnail=path))
        if (page + 1) * PAGE < int(found.get("total") or 0):
            base.children.append(_folder(f"{eid}/alerts/{severity}/{page + 1}", "More…",
                                         children_class=MediaClass.IMAGE))
        return base

    async def _events(self, entry, rest: list[str]) -> BrowseMediaSource:
        coordinator, eid = entry.runtime_data.coordinator, entry.entry_id
        cameras = coordinator.data.cameras
        if not rest:
            base = _folder(f"{eid}/events", "Events")
            base.children = [_folder(f"{eid}/events/{cid}", cam.name)
                             for cid, cam in sorted(cameras.items())]
            return base
        cid = int(rest[0])
        if cid not in cameras:
            raise ValueError(cid)
        if len(rest) == 1:
            desc = coordinator.descriptor(f"camera.{cid}.detections")
            labels = list(desc.event_types or []) if desc else []
            base = _folder(f"{eid}/events/{cid}", cameras[cid].name)
            base.children = [_folder(f"{eid}/events/{cid}/{label}/0", title,
                                     children_class=MediaClass.VIDEO)
                             for label, title in [("all", "All"),
                                                  *((lb, lb.title()) for lb in labels)]]
            return base
        label, page = rest[1], int(rest[2]) if len(rest) > 2 else 0
        found = await self._call(entry.runtime_data.client.get_events(
            camera_id=cid, label=None if label == "all" else label, skip=page * PAGE,
            limit=PAGE))
        base = _folder(f"{eid}/events/{cid}/{label}/{page}",
                       f"{cameras[cid].name}: {'all' if label == 'all' else label}",
                       children_class=MediaClass.VIDEO)
        base.children = []
        for event in found.get("events", []):
            window = _event_window(event)
            if window is None:
                continue
            start, seconds = window
            title = f"{_local(event.get('started_at'))} · {event.get('label') or 'event'}"
            if event.get("plate_text"):
                title += f" · {event['plate_text']}"
            thumb = (event_image_path(eid, event["id"]) if event.get("evidence_url")
                     else None)
            base.children.append(_leaf(
                f"{eid}/event/{event['id']}/{cid}/{int(start.timestamp())}/{seconds}", title,
                MediaClass.VIDEO, "video/mp4", thumbnail=thumb))
        if (page + 1) * PAGE < int(found.get("total") or 0):
            base.children.append(_folder(f"{eid}/events/{cid}/{label}/{page + 1}", "More…",
                                         children_class=MediaClass.VIDEO))
        return base

    def _recordings(self, entry, rest: list[str]) -> BrowseMediaSource:
        eid, cameras = entry.entry_id, entry.runtime_data.coordinator.data.cameras
        if not rest:
            base = _folder(f"{eid}/recordings", "Recordings")
            base.children = [_folder(f"{eid}/recordings/{cid}", cam.name)
                             for cid, cam in sorted(cameras.items())]
            return base
        cid = int(rest[0])
        if cid not in cameras:
            raise ValueError(cid)
        today = dt_util.now().date()
        if len(rest) == 1:
            base = _folder(f"{eid}/recordings/{cid}", cameras[cid].name)
            base.children = [_folder(f"{eid}/recordings/{cid}/{day.isoformat()}",
                                     day.isoformat(), children_class=MediaClass.VIDEO)
                             for day in (today - timedelta(days=n)
                                         for n in range(RECORDING_DAYS))]
            return base
        day = datetime.fromisoformat(rest[1]).date()
        midnight = dt_util.start_of_local_day(day)
        now = dt_util.now()
        base = _folder(f"{eid}/recordings/{cid}/{day.isoformat()}",
                       f"{cameras[cid].name}: {day.isoformat()}",
                       children_class=MediaClass.VIDEO)
        base.children = []
        for hour in range(23, -1, -1):
            start = midnight + timedelta(hours=hour)
            if start > now:
                continue
            base.children.append(_leaf(f"{eid}/rec/{cid}/{int(start.timestamp())}",
                                       f"{start:%H}:00–{(start + timedelta(hours=1)):%H}:00",
                                       MediaClass.VIDEO, "video/mp4"))
        return base
