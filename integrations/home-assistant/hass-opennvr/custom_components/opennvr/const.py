"""Constants for the OpenNVR integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "opennvr"

#: Options: the cameras this entry shows. Absent means every camera the token
#: can see, including ones added later.
CONF_CAMERAS = "cameras"
#: Options: lifetime of signed media links in notifications, in hours.
CONF_MEDIA_TTL = "media_ttl_hours"
DEFAULT_MEDIA_TTL = 24

#: Without these the integration cannot run: /system/info needs
#: settings.view; entities, the events socket and cameras need cameras.view.
REQUIRED_SCOPES = ("settings.view", "cameras.view")
#: Features degrade without these (live view, media, alerts); the config flow
#: names the missing ones instead of refusing.
RECOMMENDED_SCOPES = ("live.view", "recordings.view", "alerts.view")

#: REST refresh while the events socket pushes the changes in between; it is
#: also the fallback when the socket is down (design §7.3).
REFRESH_INTERVAL = timedelta(seconds=30)

#: Events-socket frame types the integration uses. Asking for only these
#: keeps the per-frame ``tracks`` firehose (many a second per camera, for
#: overlays) off the socket, where it would crowd out what HA needs.
WS_EVENT_TYPES = ["entity_state", "site_mode", "descriptors_changed", "camera_status",
                  "app_alert", "camera_event", "media_ready"]

#: How many recent events-socket frames diagnostics keeps.
RECENT_FRAMES = 20

PLATFORMS: list[Platform] = []


def signal_frame(entry_id: str) -> str:
    """Dispatcher signal carrying events-socket frames the coordinator does
    not consume itself (alerts, media_ready, live_state, ...)."""
    return f"{DOMAIN}_frame_{entry_id}"
