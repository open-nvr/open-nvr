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
- **[FIRST_DETECTOR.md](FIRST_DETECTOR.md)** — write your first detector app in ~15 minutes.
- **[CONTRIBUTING_APPS.md](CONTRIBUTING_APPS.md)** — publish an app to the catalog.
- **[APP_SURFACES.md](APP_SURFACES.md)** — the surfaces (config, state, actions) an app exposes.
- **[APPS_INSTALL.md](APPS_INSTALL.md)** — one-click install design (desired-state + reconciler).

## Build on the AI layer
- **[AI_ADAPTER_CONTRACT.md](AI_ADAPTER_CONTRACT.md)** — the REST/WebSocket wire spec adapters implement.
- **[apps-index-entry.template.yml](apps-index-entry.template.yml)** — template for an App Store catalog entry.

## Security, compliance & deployment
- **[SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md)** — threat model + the `V-###` control matrix (code refs `See V-###` point here).
- **[COMPLIANCE.md](COMPLIANCE.md)** — control-to-framework mapping (procurement evidence).
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
