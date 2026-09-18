# OpenNVR for Home Assistant

A Home Assistant custom integration for [OpenNVR](https://github.com/open-nvr/open-nvr). It covers live cameras, detections, alerts, controls, recordings and notifications, and needs no MQTT.

**Status:** in development. It isn't installable by end users until `pyopennvr` is published on PyPI and this folder is split into its own repository. See `docs/design/home-assistant-integration.md` and the implementation plan next to it.

## Setup
1. In OpenNVR, open **Settings > API Tokens** and create a token with the Home Assistant preset. The secret is shown once.
2. In Home Assistant, add the **OpenNVR** integration. Enter OpenNVR's address (for example `https://192.168.1.20`) and the token. Turn off certificate verification if the server uses OpenNVR's default self-signed certificate. Servers running the optional mDNS announcer are offered automatically; you still enter a token.
3. Choose the cameras. If you leave all of them selected, cameras you add to OpenNVR later appear too.

The token needs `settings.view` and `cameras.view`. Without `live.view`, `recordings.view` or `alerts.view`, the features that need them stay unavailable, and the setup step names what is missing.

If the token is revoked or expires, Home Assistant asks for a new one (reauthentication). If the server moves, use **Reconfigure**. The **Options** change the cameras and how long notification links stay valid.

## Notifications
The integration fires two Home Assistant events for automations:
- `opennvr_alert` when an OpenNVR alert lands: severity, title, app, camera entity, and `image_url`.
- `opennvr_media_ready` when an alert's or event's clip can be played: the same, plus `clip_url`.

The URLs are relative paths under Home Assistant's own address (`/api/opennvr/<site>/m/<token>`), so a phone can fetch them from anywhere it can reach Home Assistant. Each link is an OpenNVR-signed token for exactly one picture or clip, valid for the "notification link lifetime" option. The relay needs no login, and OpenNVR checks the signature on every fetch.

The blueprint `blueprints/automation/opennvr/alert_notification.yaml` sends a phone notification with the picture and the clip. It filters by severity, camera and site mode, and supports quiet hours and a cooldown. The notification offers "Acknowledge" (which acknowledges the alert in OpenNVR) and "Live view".

## Dashboard cards
A card calls the websocket command `opennvr/card_session`. It gets back a credential that OpenNVR mints from the integration's token: read-only, limited to the cameras this entry shows, valid for at most ten minutes, and revoked together with the integration's token. The card then uses OpenNVR's API, events socket and WebRTC directly, which needs Home Assistant's origin in OpenNVR's `CORS_ORIGINS`. A browser that can't reach OpenNVR, for example through HA Cloud, can instead read through `/api/opennvr/<site>/passthrough/<path>`. That path is GET only, limited to the paths OpenNVR allows, and requires a Home Assistant login.

## Repairs
Home Assistant raises a repair, and clears it once fixed, when:
- the token no longer works, or expires within 7 days (the fix asks for a new token);
- the token may not be used from Home Assistant's address;
- the server is too old for the integration, or the integration too old for the server;
- OpenNVR advertises no WebRTC address other devices can reach (`MEDIAMTX_WEBRTC_HOSTS`);
- Home Assistant asked for an RTSP stream that OpenNVR doesn't publish;
- the two clocks differ by more than a minute;
- certificate verification is off.

## Development
- **Tests:** run `scripts/ha-dev/test-integration.ps1` from the open-nvr repo root. Tests run in a Linux Python 3.14 container, because Home Assistant 2026.9 needs Python ≥ 3.14.2 and doesn't support Windows.
- **Live instance:** run `scripts/ha-dev/run-ha.ps1` to start a dev Home Assistant with this integration mounted.
- **End to end:** `scripts/ha-dev/e2e-ha.ps1` starts a fresh Home Assistant against the running OpenNVR. It onboards HA, adds the integration through the config flow, and checks entities, occupancy, restart, audit correlation and the revoked-token repair. It turns one camera's detection off and on again, and removes its token and entry at the end.
