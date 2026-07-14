# Docker Quickstart

Five minutes from `git clone` to YOLOv8 detection running on your camera feed, using pre-built images from GHCR — no source build, no toolchain, no manual model downloads. If you intend to modify the code rather than just run it, see [CONTRIBUTING.md](CONTRIBUTING.md) and [`docs/LOCAL_SETUP.md`](docs/LOCAL_SETUP.md) for the bare-metal dev path.

## Prerequisites

- **Docker Engine 24+** with Compose v2 (Linux) or **Docker Desktop**
  (macOS / Windows).
- **8 GB RAM** recommended (4 GB will work, but YOLOv8 cold-start is
  tight).
- **20 GB free disk** — most of it is the AI adapter images and YOLO
  weights; the database and the app itself are small.
- A camera with **ONVIF or RTSP** support. Most modern IP cameras
  qualify. Phone webcams via apps like IP Webcam also work for testing.

## standard stack — NVR + YOLOv8

**One command:**

```bash
git clone https://github.com/open-nvr/open-nvr.git
cd open-nvr
./start.sh            # Windows: start.ps1
```

On a fresh checkout `./start.sh` creates `.env`, generates all secrets, walks you
through a few settings (Enter accepts the local defaults), optionally sets up an
example (pick **Camera Agent** to bring up core + agent together), then builds and
starts the stack and prints the first-time setup token. Run it again later to
**start as-is** or **reconfigure**; `./start.sh up` starts without prompting.

<details>
<summary>Prefer to drive Compose by hand?</summary>

```bash
cp .env.example .env
./scripts/generate-secrets.sh --write          # Windows: .\scripts\generate-secrets.ps1 -Write
docker compose -f docker-compose.yml up -d
```

</details>

The generate-secrets script writes cryptographically random values into
`.env` for the four secrets the core validates at boot (`SECRET_KEY`,
`CREDENTIAL_ENCRYPTION_KEY`, `INTERNAL_API_KEY`, `MEDIAMTX_SECRET`) plus
the PostgreSQL password. **There are no shipped default credentials** —
the core refuses to boot if any of those four are placeholders or shorter
than the minimum length.

### First boot

On first start the core prints a **setup token** to its log. Grab it:

```bash
docker compose -f docker-compose.yml logs opennvr-core | grep -i 'setup token'
```

Open <http://localhost:8000>, paste the token on the setup screen, choose
an admin username and password, and you're in. The token is single-use —
subsequent restarts skip the setup flow because an admin already exists.

### What you should see

```bash
docker compose -f docker-compose.yml ps
```

```
NAME                                STATUS
opennvr_core                        Up (healthy)
opennvr_db                          Up (healthy)
opennvr_mediamtx                    Up (healthy)
opennvr_nats                        Up (healthy)
opennvr_yolov8_adapter              Up (healthy)
opennvr_yolov8_weights_init         Exited (0)         # one-shot, done
opennvr_mediamtx_certs_init         Exited (0)         # one-shot, done
```

The two `Exited (0)` rows are correct — they're init containers that
finish after their setup work is done.

### Endpoints

| Service | URL |
|---|---|
| Web UI | <http://localhost:8000> |
| API docs (OpenAPI / Swagger) | <http://localhost:8000/docs> |
| MediaMTX HLS playback | <http://localhost:8888> |
| MediaMTX WebRTC | <http://localhost:8889> |

The MediaMTX endpoints are gated by JWT — the frontend handles the
exchange transparently when you open a stream from the web UI.

## Adding the camera-agent voice overlay

Once standard stack is running, the camera-agent overlay layers Whisper STT,
Piper TTS, and an Ollama-hosted LLM on top so you can talk to your
cameras.

```bash
# Pull the LLM model the agent uses (~2 GB, one-time)
docker compose -f docker-compose.yml \
               -f docker-compose.camera-agent.yml \
               --profile camera-agent run --rm ollama-model-pull

# Bring up the overlay
docker compose -f docker-compose.yml \
               -f docker-compose.camera-agent.yml \
               --profile camera-agent up -d
```

Open <http://localhost:9100/demo>, click "Start", and ask
*"is there a person at the front door?"* — the agent fetches a live
frame, runs YOLOv8 + BLIP via tool calls, and speaks the answer back.

## Compose file reference

The repo ships one base stack plus optional overlays. Combine the base with an
overlay using repeated `-f` flags and the overlay's `--profile`.

| File | What it is | How to use |
|---|---|---|
| `docker-compose.yml` | **Core stack** — Postgres, MediaMTX, NATS, the YOLOv8 adapter, `opennvr-core` (backend + frontend + KAI-C), and nginx. | `docker compose -f docker-compose.yml up -d` |
| `docker-compose.apps.yml` | **Detector apps overlay** — the example SDK apps (intrusion, loitering, LPR, …). | add `-f docker-compose.apps.yml --profile apps` |
| `docker-compose.camera-agent.yml` | **Camera-agent overlay** — the voice/chat agent plus its Whisper/Piper/caption/Ollama adapters. | add `-f docker-compose.camera-agent.yml --profile camera-agent` (or `camera-agent-chat`) |
| `docker-compose.installer.yml` | **App-installer** — the single privileged reconciler for one-click installs (holds the Docker socket). Opt-in only. | add `-f docker-compose.installer.yml --profile app-installer` |

MediaMTX config lives in `mediamtx.docker.yml` (mounted into the container);
`mediamtx.yml` / `mediamtx.local.yml` are for running MediaMTX outside Docker.

## Common operations

```bash
# Stop everything
docker compose -f docker-compose.yml down

# Tail logs (all services, or a specific one)
docker compose -f docker-compose.yml logs -f
docker compose -f docker-compose.yml logs -f opennvr-core

# Refresh to the latest published images
docker compose -f docker-compose.yml pull
docker compose -f docker-compose.yml up -d

# Restart a single service after editing .env
docker compose -f docker-compose.yml restart opennvr-core
```

## Customisation

### Change recording storage location

Recording volume is mapped via `RECORDINGS_PATH` in `.env`. Defaults to
`./recordings` (relative to the compose file). Set an absolute path for
production:

```bash
# Linux
RECORDINGS_PATH=/var/lib/opennvr/recordings

# macOS
RECORDINGS_PATH=/Users/Shared/opennvr-recordings

# Windows
RECORDINGS_PATH=D:/opennvr-recordings
```

Then `docker compose -f docker-compose.yml up -d` to remount.

### Use a different YOLOv8 model

The default is `yolov8n` (nano, ~6 MB). Because Ultralytics retired the pre-built ONNX from its public URLs, the standard stack init container downloads the official `yolov8n.pt` checkpoint and exports it to ONNX with `ultralytics` on first boot — one-time, takes 1–3 min on x86 and 10–15 min on a Raspberry Pi 5, cached on the `opennvr_yolov8_weights` volume thereafter.

If you have a fine-tuned model or a private mirror that already serves a pre-built ONNX, point `YOLOV8_WEIGHTS_URL` at it in `.env`; the init container skips the `.pt` → ONNX export entirely:

```bash
YOLOV8_WEIGHTS_URL=https://example.com/internal/yolov8s-people.onnx
```

To pin a different upstream checkpoint instead, override `YOLOV8_PT_URL` (any `yolov8{n,s,m,l,x}.pt` URL from the ultralytics assets releases).

Wipe the cached weights volume to force a re-download/re-export after changing either:

```bash
docker compose -f docker-compose.yml down
docker volume rm open-nvr_opennvr_yolov8_weights
docker compose -f docker-compose.yml up -d
```

### Change the admin password

Use the web UI: profile menu → change password. Don't try to do it via
`.env` — admin credentials live in the database, not in environment
variables.

### Increase log verbosity

```bash
LOG_LEVEL=DEBUG
```

in `.env`, then restart `opennvr-core`. Note this is verbose — only flip
it for troubleshooting.

## Troubleshooting

### Core refuses to start: "Refusing to boot on placeholder secret"

You skipped the `generate-secrets.sh` step or `.env` still has the
`dev_` defaults from `.env.example`. Re-run:

```bash
./scripts/generate-secrets.sh --write
docker compose -f docker-compose.yml up -d
```

### Port already in use

Another service is listening on 8000, 8888, 8889, or 5432. Find and
stop it, or override the host-side port in
`docker-compose.yml` (change `"127.0.0.1:8000:8000"` to
`"127.0.0.1:8080:8000"` for example).

### YOLOv8 adapter never goes healthy

Check the init container's logs:

```bash
docker compose -f docker-compose.yml logs yolov8-weights-init
```

The init container downloads `yolov8n.pt` (with retries) and exports it to ONNX via `ultralytics`. Failure modes worth checking: a network blocked from reaching `github.com/ultralytics/assets/releases/...` (set `YOLOV8_PT_URL` to your own mirror); pip can't install `ultralytics` (offline or proxied environment — same fix, host the wheels on a private index, or set `YOLOV8_WEIGHTS_URL` to a pre-built ONNX you already have). The container retries transient errors five times with backoff before failing.

### Camera shows up but no detections

Check the inference event bus:

```bash
docker compose -f docker-compose.yml logs nats
docker compose -f docker-compose.yml logs opennvr-core | grep -i kai_c
```

KAI-C polls the YOLOv8 adapter on a per-camera schedule; if the schedule
is paused (e.g., camera is offline), no events fire. The web UI's
"Cameras" page shows the current schedule state.

### Disk filling up

```bash
docker system df              # see where the space went
docker system prune -a        # ⚠ removes ALL unused Docker data, not just OpenNVR
```

For OpenNVR specifically, recordings under `RECORDINGS_PATH` grow most
aggressively. Configure retention in the web UI's per-camera settings.

## Production deployment

standard stack is intended for the demo + homelab use case. For an internet-
facing deployment you'll want:

1. **Front the service with a real reverse proxy.** Caddy, Traefik, or
   nginx with a real TLS certificate. Don't expose 127.0.0.1:8000
   directly.
2. **Keep Docker bridge networking enabled.** Discover cameras through
   explicit IPs or operator-approved unicast subnet scanning; do not expose
   the application stack through host networking.
3. **Restrict the MediaMTX listeners.** The standard stack compose binds them
   to 127.0.0.1; expose only via your reverse proxy with auth.
4. **Back up the database.** The `opennvr_db_data` volume holds your
   camera list, user accounts, and audit log.
5. **Configure retention.** Default retention is 7 days per camera —
   adjust per-camera in the web UI based on your disk budget.

Full production hardening checklist in
[`docs/SECURITY_ARCHITECTURE.md`](docs/SECURITY_ARCHITECTURE.md).

## Next steps

1. **Add a camera.** Web UI → Cameras → Add. ONVIF discovery is the
   easiest route; RTSP URL works if ONVIF isn't supported.
2. **Configure AI detection.** Web UI → AI Models. YOLOv8 is enabled by
   default in standard stack; toggle per-camera as needed.
3. **Configure retention.** Web UI → Cameras → per-camera recording
   settings. Default is 7 days.
4. **Browse the API.** <http://localhost:8000/docs> — every endpoint is
   documented with example payloads.

## Support

User questions go in [Discussions](https://github.com/open-nvr/open-nvr/discussions); bug reports in [Issues](https://github.com/open-nvr/open-nvr/issues); security via [SECURITY.md](SECURITY.md). If you want to send patches back, [CONTRIBUTING.md](CONTRIBUTING.md) covers the flow.
