# Docker Quickstart

Five minutes from `git clone` to YOLOv8 detection running on your camera feed, using pre-built images from GHCR — no source build, no toolchain, no manual model downloads. If you intend to modify the code rather than just run it, see [CONTRIBUTING.md](CONTRIBUTING.md) and [`docs/LOCAL_SETUP.md`](docs/LOCAL_SETUP.md) for the bare-metal dev path.

## Prerequisites

- **Docker Engine 24+** with Compose v2 (Linux) or **Docker Desktop**
  (macOS / Windows).
- **x86-64 (amd64) or ARM64.** `opennvr-core`, `detect-pipeline`,
  `camera-agent` and `yolov8-weights` publish both. The AI adapter images
  (`yolov8-adapter`, `moondream-adapter`, `whisper-adapter`,
  `piper-adapter`, ...) are built in the separate
  [`open-nvr/ai-adapter`](https://github.com/open-nvr/ai-adapter) repo and
  **older pinned tags are amd64-only** — see
  [ARM64 / Apple Silicon](#no-matching-manifest-for-linuxarm64v8-apple-silicon--arm) below.
- **8 GB RAM** recommended (4 GB will work, but YOLOv8 cold-start is
  tight).
- **macOS / Windows: check Docker Desktop's VM allowance.** Every
  container shares one VM whose CPUs/RAM are a Docker Desktop setting
  (Settings → Resources; WSL2 backend: `%UserProfile%\.wslconfig`) and
  the default is often half the machine. Give it ≥4 CPUs and ≥6 GB
  (≥8 GB if you run the camera-agent with the bundled LLM container).
  The installer measures the allowance and warns when it's undersized —
  it cannot raise it for you.
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

The launcher also picks a bindable WebRTC ICE port and pre-flights every
published port before starting. Driving Compose by hand skips both, so on
Windows — where WinNAT reserves port ranges that are re-rolled at every
boot — a collision surfaces as an opaque `ports are not available` error
rather than an explanation. Set `WEBRTC_ICE_PORT` in `.env` if you hit
one. See #298.

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
| MediaMTX HLS playback | <http://localhost:8888> — needs `OPENNVR_DEBUG_PORTS=1` |
| MediaMTX WebRTC | <http://localhost:8889> — needs `OPENNVR_DEBUG_PORTS=1` |

MediaMTX’s HLS, WebRTC, playback and admin ports are **not published on
the host by default** — browsers reach them through nginx (`/hls/`,
`/webrtc/`, `/playback/`), so publishing them only widens the surface for
one unbindable port to take the whole service down. Set
`OPENNVR_DEBUG_PORTS=1` in `.env` to publish them on loopback for debugging.

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
| `docker-compose.apps.yml` | **Detector apps overlay** — the example SDK apps (intrusion, loitering, LPR, …). Two of them — occupancy-counting and footage-search — are ON BY DEFAULT via `start.sh`/`start.ps1` (profile `default-apps`; they ride the always-on Tier-0 stream, no extra adapter/model/GPU; opt out with `OPENNVR_DEFAULT_APPS=off` in `.env`). | everything else: add `-f docker-compose.apps.yml --profile apps` |
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

### Turn off object detection entirely

Running OpenNVR for models that aren't about detection (captioning/VQA,
LPR, face recognition, your own adapters) — or just as an NVR? Two lines:

```bash
# .env
DETECT_PIPELINE_ENABLED=false
```

then `./start.sh up`. The Tier-0 detection loop idles (zero inference,
zero decode), while recording, playback, live view, the camera-agent, and
every other adapter keep working. Flip it back to `true` any time.

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

### "no matching manifest for linux/arm64/v8" (Apple Silicon / ARM)

The pull aborts partway through with something like:

```
 ✘ Image ghcr.io/open-nvr/yolov8-adapter:0.1.1  Error no matching manifest
   for linux/arm64/v8 in the manifest list entries
Error response from daemon: no matching manifest for linux/arm64/v8 in the
manifest list entries: no match for platform in manifest: not found
```

That image has no ARM64 build. The daemon names the platform but not the
cause, and every other image in the pull reports `Interrupted` — which
looks like a network failure and is not.

`./scripts/install.sh` (and `install.ps1`) now check every pinned image's
manifest list *before* pulling and print the full list of images that lack
a build for your architecture, so you find out in one shot rather than one
image at a time.

**Fixing it depends on which image is named:**

- **`core`, `detect-pipeline`, `camera-agent`** — built
  in this repo and published for `linux/amd64` **and** `linux/arm64`. If one
  of these is named, you are on a tag published before ARM64 support landed;
  move to `:main`/`:latest` or a release tag newer than that.
- **`*-adapter`** — built in
  [`open-nvr/ai-adapter`](https://github.com/open-nvr/ai-adapter).
  ARM64 manifest lists start at adapter tag **0.1.3** (the current
  `.env.example` pin); tags 0.1.1 and earlier are amd64-only. If you are
  named one of these, your `.env` carries a stale `ADAPTER_TAG` — raise it
  to `0.1.3` or later, or run the adapters emulated.

**Running the amd64 images under emulation** works and is the fastest way to
get a demo up on an M-series Mac, at a real cost in inference latency:

1. Docker Desktop → **Settings → General → "Use Rosetta for x86_64/amd64
   emulation on Apple Silicon"** → Apply & restart. (Without this, emulated
   containers frequently fail to start at all rather than merely running
   slowly.)
2. Re-run the installer with the opt-in flag:

   ```bash
   OPENNVR_ALLOW_EMULATION=1 ./scripts/install.sh
   ```

   ```powershell
   $env:OPENNVR_ALLOW_EMULATION = "1"; .\scripts\install.ps1
   ```

   This exports `DOCKER_DEFAULT_PLATFORM=linux/amd64` for the pull and every
   subsequent `docker compose` call, so the stack does not come up half
   emulated and half native.

Emulation is a workaround, not a supported configuration — treat detection
throughput numbers measured this way as meaningless.

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
