"""Camera platform: live view over WebRTC (WHEP), snapshots, detection toggle.

Live view is HA's native WebRTC: the browser's offer is relayed to OpenNVR's
MediaMTX over WHEP (``/webrtc/<stream>/whep`` behind OpenNVR's nginx), the
answer comes back, and the media flows browser <-> MediaMTX directly.
Trickle ICE candidates are PATCHed to the session; closing DELETEs it.
Candidates the browser sends before the WHEP answer arrives are held and sent
once it has.

* Motion detection maps to the camera's object detection flag.
* On/off (``is_active``) also stops recording, so it is offered only where
  the site allows pausing recording (``recording_pause_enabled``, design §6.3).
* ``stream_source`` (RTSPS) exists only when OpenNVR publishes RTSPS on an
  address other than loopback (``MEDIAMTX_EXTERNAL_RTSPS_URL``); WebRTC needs
  none of it.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from pyopennvr import OpenNVRAuthError, OpenNVRError, OpenNVRNotFoundError, Whep, WhepSession

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    RTCIceCandidateInit,
    WebRTCAnswer,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenNVRConfigEntry, issues
from .const import DOMAIN
from .coordinator import OpenNVRCoordinator
from .entity import OpenNVREntity

_LOGGER = logging.getLogger(__name__)

# Commands go to one server; HA's own per-platform limit is not needed.
PARALLEL_UPDATES = 0

#: The token scope live view and snapshots need.
LIVE_SCOPE = "live.view"


async def async_setup_entry(hass: HomeAssistant, entry: OpenNVRConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    scopes = coordinator.data.info.scopes
    if scopes is not None and LIVE_SCOPE not in scopes:
        # Without live.view neither WHEP nor snapshots work; a camera entity
        # that is always broken helps nobody. (Setup named the scope.)
        return
    added: set[int] = set()

    @callback
    def add_new() -> None:
        new = [cid for cid in coordinator.data.cameras if cid not in added]
        added.update(new)
        if new:
            async_add_entities(OpenNVRCamera(coordinator, cid) for cid in new)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))


class OpenNVRCamera(OpenNVREntity, Camera):
    """One OpenNVR camera."""

    _attr_name = None  # the camera device's main entity

    def __init__(self, coordinator: OpenNVRCoordinator, camera_id: int) -> None:
        OpenNVREntity.__init__(self, coordinator, f"camera.{camera_id}",
                               {"kind": "camera", "id": camera_id})
        Camera.__init__(self)
        self.camera_id = camera_id
        client = coordinator.client
        self._whep = Whep(client, async_get_clientsession(coordinator.hass,
                                                          client.ssl is not False))
        self._sessions: dict[str, WhepSession] = {}
        #: Candidates that arrived before their session's WHEP answer.
        self._early: dict[str, list[RTCIceCandidateInit]] = {}

    # ── state ────────────────────────────────────────────────────────────

    @property
    def _camera(self):
        return self.coordinator.data.cameras.get(self.camera_id)

    def _state(self, suffix: str) -> Any:
        value = self.coordinator.state_of(f"camera.{self.camera_id}.{suffix}")
        return None if value is None else value.get("state")

    @property
    def available(self) -> bool:
        # Unavailable when OpenNVR is unreachable, the camera was removed or
        # deselected, or OpenNVR reports it offline.
        return (super().available and self._camera is not None
                and self._state("online") is not False)

    @property
    def supported_features(self) -> CameraEntityFeature:
        features = CameraEntityFeature.STREAM
        if self.coordinator.data.info.recording_pause_enabled:
            features |= CameraEntityFeature.ON_OFF
        return features

    @property
    def is_on(self) -> bool:
        cam = self._camera
        return bool(cam and cam.is_active)

    @property
    def motion_detection_enabled(self) -> bool:
        pushed = self._state("detection")
        if pushed is not None:
            return bool(pushed)
        cam = self._camera
        return bool(cam and cam.detection_enabled)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Pushed changes of the entities this one mirrors.
        for suffix in ("online", "detection"):
            self.async_on_remove(self.coordinator.async_add_key_listener(
                f"camera.{self.camera_id}.{suffix}", lambda _update: self.async_write_ha_state()))

    async def async_will_remove_from_hass(self) -> None:
        for session in self._sessions.values():
            await self._whep.close(session)
        self._sessions.clear()
        self._early.clear()
        await super().async_will_remove_from_hass()

    # ── controls ─────────────────────────────────────────────────────────

    async def _control(self, call, *args) -> None:
        try:
            await call(self.camera_id, *args,
                       correlation_id=self._context.id if self._context else None)
        except OpenNVRAuthError as err:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="not_permitted",
                                     translation_placeholders={"detail": str(err)}) from err
        except OpenNVRError as err:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="command_failed",
                                     translation_placeholders={"detail": str(err)}) from err
        await self.coordinator.async_request_refresh()

    async def async_enable_motion_detection(self) -> None:
        await self._control(self.coordinator.client.set_detection, True)

    async def async_disable_motion_detection(self) -> None:
        await self._control(self.coordinator.client.set_detection, False)

    async def async_turn_on(self) -> None:
        await self._control(self.coordinator.client.set_camera_active, True)

    async def async_turn_off(self) -> None:
        await self._control(self.coordinator.client.set_camera_active, False)

    # ── images and streams ───────────────────────────────────────────────

    async def async_camera_image(self, width: int | None = None,
                                 height: int | None = None) -> bytes | None:
        try:
            return await self.coordinator.client.get_snapshot(self.camera_id)
        except OpenNVRError as err:
            _LOGGER.debug("Snapshot of camera %s failed: %s", self.camera_id, err)
            return None

    async def stream_source(self) -> str | None:
        try:
            info = await self.coordinator.client.get_stream_info(self.camera_id)
        except OpenNVRError as err:
            _LOGGER.debug("Stream info of camera %s failed: %s", self.camera_id, err)
            return None
        source = rtsps_source(info.rtsps_url, info.token)
        # Raised only when something in HA actually asked for an RTSP stream
        # (recording, HLS); live view (WebRTC) never needs it.
        if source is None:
            issues.async_raise(self.hass, self.coordinator.config_entry, "rtsp_not_exposed")
        else:
            issues.async_clear(self.hass, self.coordinator.config_entry, "rtsp_not_exposed")
        return source

    async def async_handle_async_webrtc_offer(self, offer_sdp: str, session_id: str,
                                              send_message: WebRTCSendMessage) -> None:
        try:
            session = await self._whep.offer(self.camera_id, offer_sdp)
        except OpenNVRNotFoundError:
            self._early.pop(session_id, None)
            send_message(WebRTCError("webrtc_offer_failed",
                                     "The camera is not streaming right now"))
            return
        except OpenNVRError as err:
            self._early.pop(session_id, None)
            send_message(WebRTCError("webrtc_offer_failed", str(err)))
            return
        self._sessions[session_id] = session
        send_message(WebRTCAnswer(session.answer_sdp))
        for candidate in self._early.pop(session_id, []):
            await self._send_candidate(session, candidate)

    async def async_on_webrtc_candidate(self, session_id: str,
                                        candidate: RTCIceCandidateInit) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            self._early.setdefault(session_id, []).append(candidate)
            return
        await self._send_candidate(session, candidate)

    async def _send_candidate(self, session: WhepSession,
                              candidate: RTCIceCandidateInit) -> None:
        if not candidate.candidate:
            return  # end of candidates
        try:
            await self._whep.add_candidate(session, candidate.candidate, candidate.sdp_mid)
        except OpenNVRError as err:
            # MediaMTX also gathers candidates itself; one lost trickle
            # candidate rarely matters, and the session may already be gone.
            _LOGGER.debug("WHEP candidate for camera %s failed: %s", self.camera_id, err)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        self._early.pop(session_id, None)
        session = self._sessions.pop(session_id, None)
        if session is not None:
            self.hass.async_create_task(self._whep.close(session))


def rtsps_source(url: str | None, token: str) -> str | None:
    """An RTSPS URL HA's stream component can open: the stream token as the
    password (MediaMTX's JWT auth for RTSP). None when OpenNVR publishes RTSPS
    only on loopback, i.e. not exposed to other machines."""
    if not url:
        return None
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host == "localhost" or not host:
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass  # a name
    netloc = f"opennvr:{quote(token, safe='')}@{parts.netloc.rsplit('@', 1)[-1]}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
