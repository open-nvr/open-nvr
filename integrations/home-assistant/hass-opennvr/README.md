# OpenNVR for Home Assistant

A Home Assistant custom integration for [OpenNVR](https://github.com/open-nvr/open-nvr). It covers live cameras, detections, alerts, controls, recordings and notifications, and needs no MQTT.

**Status:** in development. It isn't installable by end users until `pyopennvr` is published on PyPI and this folder is split into its own repository. See `docs/design/home-assistant-integration.md` and the implementation plan next to it.

## Development
- **Tests:** run `scripts/ha-dev/test-integration.ps1` from the open-nvr repo root. Tests run in a Linux Python 3.14 container, because Home Assistant 2026.9 needs Python ≥ 3.14.2 and doesn't support Windows.
- **Live instance:** run `scripts/ha-dev/run-ha.ps1` to start a dev Home Assistant with this integration mounted.
