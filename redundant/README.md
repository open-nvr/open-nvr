# `redundant/` — parked configuration

Files here were **moved, not deleted**, as part of the config single-source-of-truth
cleanup (see the repo-level change that introduced this folder). They are kept for
reference / easy restore. Nothing in the supported build paths reads from this folder.

## Supported config surface (top level)

| Kept file | Purpose |
| --- | --- |
| `docker-compose.tier0.yml` | Demo / quickstart (pre-built images). Cross-platform default. |
| `docker-compose.linux.yml` | Linux production (host network, ONVIF discovery). |
| `docker-compose.camera-agent.yml` | Overlay that builds the `examples/camera-agent` voice agent. |
| `mediamtx.docker.yml` | MediaMTX config used by every Docker compose (mounted as `/mediamtx.yml`). |
| `mediamtx.local.yml` | MediaMTX config for dev-local (plaintext RTSP, no certs). |
| `.env` (from `.env.example`) | **Single source of truth for Docker.** |
| `server/.env.example`, `kai-c/.env.example`, `app/env.example` | Standalone (non-Docker) dev templates. |

## What's parked here and why

| File | Was | Why parked | Restore |
| --- | --- | --- | --- |
| `docker-compose.yml` | Root bridge / build-from-source compose for Windows/macOS | Superseded: `docker-compose.tier0.yml` is now the cross-platform path. Windows/macOS hybrid dev lives in `dev-local/`. | `git mv redundant/docker-compose.yml ../docker-compose.yml` |
| `mediamtx.yml` | Root bare-metal MediaMTX config | Not referenced by any kept compose; the Docker path uses `mediamtx.docker.yml`, dev-local uses `mediamtx.local.yml`. | `git mv redundant/mediamtx.yml ../mediamtx.yml` |
| `server-env.example` | `server/env.example` (the 159-line variant) | One of **two** conflicting server templates. Its richer content was folded into `server/.env.example`; this duplicate is parked to avoid drift. | content already lives in `server/.env.example` |

If you restore a file, re-point the launchers (`start.sh`, `start.ps1`, `scripts/install.*`)
and `Makefile` accordingly.
