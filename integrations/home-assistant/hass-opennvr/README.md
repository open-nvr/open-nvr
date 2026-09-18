# OpenNVR for Home Assistant

A Home Assistant custom integration for [OpenNVR](https://github.com/open-nvr/open-nvr). It covers live cameras, detections, alerts, controls, recordings and notifications, and needs no MQTT.

**Status:** in development. It isn't installable by end users until `pyopennvr` is published on PyPI and this folder is split into its own repository. See `docs/design/home-assistant-integration.md` and the implementation plan next to it.

## Setup
1. In OpenNVR, open **Settings > API Tokens** and create a token with the Home Assistant preset. The secret is shown once.
2. In Home Assistant, add the **OpenNVR** integration. Enter OpenNVR's address (for example `https://192.168.1.20`) and the token. Turn off certificate verification if the server uses OpenNVR's default self-signed certificate. Servers running the optional mDNS announcer are offered automatically; you still enter a token.
3. Choose the cameras. If you leave all of them selected, cameras you add to OpenNVR later appear too.

The token needs `settings.view` and `cameras.view`. Without `live.view`, `recordings.view` or `alerts.view`, the features that need them stay unavailable, and the setup step names what is missing.

If the token is revoked or expires, Home Assistant asks for a new one (reauthentication). If the server moves, use **Reconfigure**. The **Options** change the cameras and how long notification links stay valid.

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
