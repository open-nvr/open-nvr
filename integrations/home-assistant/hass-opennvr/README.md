# OpenNVR for Home Assistant

A Home Assistant custom integration for [OpenNVR](https://github.com/open-nvr/open-nvr). It covers live cameras, detections, alerts, controls, recordings and notifications, and needs no MQTT.

**Status:** in development. It isn't installable by end users until `pyopennvr` is published on PyPI and this folder is split into its own repository. See `docs/design/home-assistant-integration.md` and the implementation plan next to it.

## Setup
1. In OpenNVR, open **Settings > API Tokens** and create a token with the Home Assistant preset. The secret is shown once.
2. In Home Assistant, add the **OpenNVR** integration. Enter OpenNVR's address (for example `https://192.168.1.20`) and the token. Turn off certificate verification if the server uses OpenNVR's default self-signed certificate. Servers running the optional mDNS announcer are offered automatically; you still enter a token.
3. Choose the cameras. If you leave all of them selected, cameras you add to OpenNVR later appear too.

The token needs `settings.view` and `cameras.view`. Without `live.view`, `recordings.view` or `alerts.view`, the features that need them stay unavailable, and the setup step names what is missing.

If the token is revoked or expires, Home Assistant asks for a new one (reauthentication). If the server moves, use **Reconfigure**. The **Options** change the cameras and how long notification links stay valid.

## Development
- **Tests:** run `scripts/ha-dev/test-integration.ps1` from the open-nvr repo root. Tests run in a Linux Python 3.14 container, because Home Assistant 2026.9 needs Python ≥ 3.14.2 and doesn't support Windows.
- **Live instance:** run `scripts/ha-dev/run-ha.ps1` to start a dev Home Assistant with this integration mounted.
