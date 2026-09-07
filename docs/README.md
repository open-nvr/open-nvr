# OpenNVR Documentation

Start here. Pick the row that matches what you're doing.

## New to the project?
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — how the whole system fits together (read this first).
- **[DESIGN_NOTES.md](DESIGN_NOTES.md)** — the *why* behind non-obvious decisions.

## Run it
- **[../DOCKER_QUICKSTART.md](../DOCKER_QUICKSTART.md)** — Docker install, retention, production hardening, compose-file reference.
- **[LOCAL_SETUP.md](LOCAL_SETUP.md)** — run the backend / frontend / KAI-C from source for development.
- **[../USER_MANUAL.md](../USER_MANUAL.md)** — day-to-day operator guide (add cameras, playback, users).

## Contribute
- **[../CONTRIBUTING.md](../CONTRIBUTING.md)** — PR flow, conventions, running tests.
- **[DEVELOPER_PROGRAM.md](DEVELOPER_PROGRAM.md)** — **start here if you are building an app**: the deal (open source under the org, we build and ship it, no fee, sell what the code needs), the compatibility promise.
- **[APP_LISTING_TERMS.md](APP_LISTING_TERMS.md)** — the terms a catalog listing is under: what you keep, what you promise, removal, liability.
- **[FIRST_DETECTOR.md](FIRST_DETECTOR.md)** — write your first detector app in ~15 minutes.
- **[CONTRIBUTING_APPS.md](CONTRIBUTING_APPS.md)** — publish an app to the catalog.
- **[EXTERNAL_APP_WALKTHROUGH.md](EXTERNAL_APP_WALKTHROUGH.md)** — a paid, out-of-tree app built on the PyPI SDK, and what the walk found.
- **[APP_SURFACES.md](APP_SURFACES.md)** — the surfaces (config, state, actions) an app exposes.
- **[APPS_INSTALL.md](APPS_INSTALL.md)** — one-click install design (desired-state + reconciler).
- **[APP_CREDENTIALS.md](APP_CREDENTIALS.md)** — per-app keys: the register handshake, roster scoping, rotate/revoke.
- **[APP_NETWORK.md](APP_NETWORK.md)** — enforced egress: the internal apps network, the proxy, declared and allowed hosts, what the operator sees, plain-TCP clients.
- **[APP_PLATFORM.md](APP_PLATFORM.md)** — the `OpenNVR` client: cameras, snapshots, recordings, timeline, AI, alerts, durable state, domain-event consumption.
- **[SDK_REFERENCE.md](SDK_REFERENCE.md)** — index of every public `opennvr_app_sdk` name, by task.
- **[PLATFORM_API.md](PLATFORM_API.md)** — the operator API: users (incl. superusers + MFA), roles and the permission catalogue, cameras, assignments, per-camera access, apps and licences.

## Build on the AI layer
- **[AI_ADAPTER_CONTRACT.md](AI_ADAPTER_CONTRACT.md)** — the REST/WebSocket wire spec adapters implement.
- **[apps-index-entry.template.yml](apps-index-entry.template.yml)** — template for an App Store catalog entry.

## Security, compliance & deployment
- **[SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md)** — threat model + the `V-###` control matrix (code refs `See V-###` point here).
- **[COMPLIANCE.md](COMPLIANCE.md)** — control-to-framework mapping (procurement evidence).
- **[ENTERPRISE.md](ENTERPRISE.md)** — the enterprise offer: reference appliance deployment, the compliance evidence pack, §889, support with response times, custom AI.
- **[REFERENCE_APPLIANCE.md](REFERENCE_APPLIANCE.md)** — the known-good site: three sizes, storage arithmetic, three network segments, host hardening, the checklist the evidence pack scores.
- **[GOVERNMENT_DEPLOYMENT.md](GOVERNMENT_DEPLOYMENT.md)** — air-gapped / regulated deployment brief.
- **[EDGE_AUTONOMY.md](EDGE_AUTONOMY.md)** — edge / robotics on-board agent notes.
- **[../SECURITY.md](../SECURITY.md)** — how to report a vulnerability.

## Product & positioning
- **[../README.md](../README.md)** · **[../POSITIONING.md](../POSITIONING.md)** · **[COMPARISONS.md](COMPARISONS.md)** · **[USE_CASES.md](USE_CASES.md)** · **[TWO_DOORS.md](TWO_DOORS.md)**

## Project & legal
- **[ROADMAP.md](ROADMAP.md)** · **[SUPPORT.md](SUPPORT.md)** · **[../CHANGELOG.md](../CHANGELOG.md)**
- **[LICENSING.md](LICENSING.md)** · **[CLA.md](CLA.md)** · **[../TRADEMARK.md](../TRADEMARK.md)**

## Working with AI assistants
Point your assistant at [ARCHITECTURE.md](ARCHITECTURE.md) and this index first. Keeping docs
consistent (one canonical doc per topic) is what lets an assistant reason about the codebase
without tripping on contradictions. If your setup uses a repo-level agent file
(e.g. `AGENTS.md` / `CLAUDE.md`), have it link here.

---
*Detailed design blueprints (HTML) live in [`design/`](design/) and are linked from [TWO_DOORS.md](TWO_DOORS.md).*
