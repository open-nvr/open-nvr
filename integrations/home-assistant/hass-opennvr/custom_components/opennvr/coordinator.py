"""Coordinator: REST refresh plus the events websocket, for one OpenNVR site.

* Every 30 s a REST refresh reads the site, its cameras, the entity catalogue
  (cheap when unchanged: ETag) and every entity state. That is the baseline,
  and the fallback while the socket is down.
* In between, the events socket (protocol v2, via pyopennvr) pushes changes:
  ``entity_state`` updates one key and wakes only that key's listeners, so a
  busy camera doesn't rewrite every entity; ``state_snapshot`` and
  ``site_mode`` update the shared data; ``descriptors_changed`` re-reads the
  catalogue. Every other frame type is forwarded on a dispatcher signal for
  the platforms that want it.
* The server decides every value (design §6.10); nothing here interprets them.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from pyopennvr import (
    Camera,
    EntityCatalog,
    EntityDescriptor,
    EventStream,
    OpenNVRAuthError,
    OpenNVRClient,
    OpenNVRConnectionError,
    OpenNVRContractError,
    OpenNVRError,
    SiteMode,
    SystemInfo,
    check_contract,
)

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_CAMERAS,
    DOMAIN,
    RECENT_FRAMES,
    REFRESH_INTERVAL,
    WS_EVENT_TYPES,
    signal_frame,
)

if TYPE_CHECKING:
    from . import OpenNVRConfigEntry

_LOGGER = logging.getLogger(__name__)

KeyListener = Callable[[dict[str, Any]], None]


@dataclass
class OpenNVRSiteData:
    """What the coordinator knows about the site."""

    info: SystemInfo
    #: Every camera the token can see (the options flow offers these).
    all_cameras: dict[int, Camera]
    #: The ones this entry shows.
    cameras: dict[int, Camera]
    catalog: EntityCatalog
    #: ``{key: {"state", "attributes"}}``; event entities have none.
    states: dict[str, dict[str, Any]] = field(default_factory=dict)
    site_mode: SiteMode | None = None


class OpenNVRCoordinator(DataUpdateCoordinator[OpenNVRSiteData]):
    """One per config entry."""

    config_entry: OpenNVRConfigEntry

    def __init__(self, hass: HomeAssistant, entry: OpenNVRConfigEntry,
                 client: OpenNVRClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {entry.title}",
                         update_interval=REFRESH_INTERVAL)
        self.client = client
        self._stream: EventStream | None = None
        self._key_listeners: dict[str, list[KeyListener]] = {}
        #: Recent frames, for diagnostics.
        self.recent_frames: deque[dict[str, Any]] = deque(maxlen=RECENT_FRAMES)

    # ── REST ─────────────────────────────────────────────────────────────

    async def _async_setup(self) -> None:
        """Once, before the first refresh: is this the server we were set up
        against, and do we speak its contract?"""
        try:
            info = await self.client.get_system_info()
        except OpenNVRAuthError as err:
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN,
                                        translation_key="auth_failed") from err
        except OpenNVRError as err:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="cannot_connect",
                               translation_placeholders={"error": str(err)}) from err
        try:
            check_contract(info)
        except OpenNVRContractError as err:
            raise ConfigEntryError(
                translation_domain=DOMAIN, translation_key="unsupported_contract",
                translation_placeholders={"version": info.contract_version}) from err
        if self.config_entry.unique_id and info.site_id != self.config_entry.unique_id:
            # Another OpenNVR answers at this URL now. Its entities must not
            # be mixed into this site's; the user reconfigures.
            raise ConfigEntryError(translation_domain=DOMAIN, translation_key="wrong_site")

    async def _async_update_data(self) -> OpenNVRSiteData:
        old = self.data
        try:
            info = await self.client.get_system_info()
            all_cameras = {c.id: c for c in await self.client.get_cameras()}
            catalog = await self.client.get_entities(etag=old.catalog.etag if old else None)
            states = await self.client.get_entity_states()
            site_mode = await self._site_mode(info)
        except OpenNVRAuthError as err:
            # Revoked, expired, or a scope it cannot work without was taken
            # away: only a new token helps.
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN,
                                        translation_key="auth_failed") from err
        except OpenNVRConnectionError as err:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="cannot_connect",
                               translation_placeholders={"error": str(err)}) from err
        except OpenNVRError as err:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="unexpected",
                               translation_placeholders={"error": str(err)}) from err
        if catalog is None:  # 304: unchanged
            catalog = old.catalog if old else EntityCatalog(etag="", descriptors=())
        chosen = self.config_entry.options.get(CONF_CAMERAS)
        cameras = (all_cameras if chosen is None
                   else {cid: c for cid, c in all_cameras.items() if cid in set(chosen)})
        return OpenNVRSiteData(info=info, all_cameras=all_cameras, cameras=cameras,
                               catalog=catalog, states=states, site_mode=site_mode)

    async def _site_mode(self, info: SystemInfo) -> SiteMode | None:
        if not info.has("site_mode"):
            return None
        try:
            return await self.client.get_site_mode()
        except OpenNVRAuthError:
            return None  # the token may read the site but not its mode

    # ── what the platforms read ──────────────────────────────────────────

    def descriptors(self) -> list[EntityDescriptor]:
        """The catalogue, less entities of cameras this entry doesn't show."""
        if self.data is None:
            return []
        cams = self.data.cameras
        return [d for d in self.data.catalog.descriptors
                if d.camera_id is None or d.camera_id in cams]

    def state_of(self, key: str) -> dict[str, Any] | None:
        return self.data.states.get(key) if self.data else None

    @callback
    def async_add_key_listener(self, key: str, listener: KeyListener) -> CALLBACK_TYPE:
        """Call ``listener`` with each pushed update of ``key``: ``{"state",
        "attributes"}`` for a state, ``{"event": {...}}`` for an event."""
        self._key_listeners.setdefault(key, []).append(listener)

        @callback
        def remove() -> None:
            listeners = self._key_listeners.get(key, [])
            if listener in listeners:
                listeners.remove(listener)
            if not listeners:
                self._key_listeners.pop(key, None)

        return remove

    # ── the events socket ────────────────────────────────────────────────

    @property
    def stream_state(self) -> str:
        return self._stream.state if self._stream else "stopped"

    @property
    def stream(self) -> EventStream | None:
        return self._stream

    @callback
    def async_start_stream(self) -> None:
        verify = self.client.ssl is not False
        self._stream = EventStream(self.client, async_get_clientsession(self.hass, verify),
                                   self._on_frame, on_state=self._on_stream_state,
                                   types=WS_EVENT_TYPES)
        self.config_entry.async_create_background_task(
            self.hass, self._run_stream(self._stream), name=f"{DOMAIN} events {self.name}")

    async def _run_stream(self, stream: EventStream) -> None:
        try:
            await stream.run()
        except Exception:  # a bug here must not end push updates silently
            _LOGGER.exception("OpenNVR events stream stopped unexpectedly")

    async def async_stop_stream(self) -> None:
        if self._stream is not None:
            await self._stream.stop()

    @callback
    def _on_stream_state(self, state: str) -> None:
        _LOGGER.debug("OpenNVR events stream: %s", state)
        if state == "auth_failed":
            # The token was revoked or its scopes changed while connected.
            self.config_entry.async_start_reauth(self.hass)

    @callback
    def _on_frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("event_type")
        if kind != "heartbeat":
            self.recent_frames.append(frame)
        data = self.data
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        if kind == "entity_state":
            self._on_entity_state(payload)
        elif data is None:
            return
        elif kind == "state_snapshot":
            snap = frame.get("entity_states")
            if isinstance(snap, dict):
                data.states.update(snap)
            if isinstance(frame.get("site_mode"), dict):
                data.site_mode = _site_mode_from(frame["site_mode"], data.site_mode)
            self.async_update_listeners()
        elif kind == "site_mode":
            data.site_mode = _site_mode_from(payload, data.site_mode)
            self.async_update_listeners()
        elif kind == "descriptors_changed":
            if payload.get("etag") != data.catalog.etag:
                self.hass.async_create_task(self.async_request_refresh())
        elif kind == "lagged":
            # The server dropped frames for us (queue full): some updates are
            # gone, so re-read everything rather than wait up to 30 s.
            _LOGGER.debug("OpenNVR events stream lagged (%s dropped)", frame.get("dropped"))
            self.hass.async_create_task(self.async_request_refresh())
        elif kind not in ("subscribed", "heartbeat"):
            async_dispatcher_send(self.hass, signal_frame(self.config_entry.entry_id), frame)

    @callback
    def _on_entity_state(self, payload: dict[str, Any]) -> None:
        key = payload.get("key")
        if not isinstance(key, str):
            return
        if isinstance(payload.get("event"), dict):
            update: dict[str, Any] = {"event": payload["event"]}
        else:
            update = {"state": payload.get("state"),
                      "attributes": payload.get("attributes") or {}}
            if self.data is not None:
                self.data.states[key] = update
        for listener in list(self._key_listeners.get(key, ())):
            listener(update)


def _site_mode_from(d: dict[str, Any], old: SiteMode | None) -> SiteMode | None:
    if "mode" not in d:
        return old
    mode = SiteMode.from_dict(d)
    if old is not None and "modes" not in d:
        mode = SiteMode(mode=mode.mode, changed_at=mode.changed_at,
                        changed_by=mode.changed_by, modes=old.modes)
    return mode
