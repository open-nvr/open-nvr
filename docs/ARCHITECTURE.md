# OpenNVR Architecture

The one-page mental model of how OpenNVR fits together — read this first, then
follow the links for depth. For *why* specific decisions were made see
[`DESIGN_NOTES.md`](DESIGN_NOTES.md); for the security control matrix see
[`SECURITY_ARCHITECTURE.md`](SECURITY_ARCHITECTURE.md).

## The big picture

OpenNVR is a self-hosted, offline-first NVR with a pluggable AI layer. It's a
**three-tier** system where each tier can only reach the next through a narrow,
audited interface:

```
   Cameras (RTSP/RTSPS)
        │
        ▼
┌─────────────────────┐   provisions paths     ┌──────────────┐
│  Camera Plane       │ ─────────────────────► │  MediaMTX    │  RTSP in →
│  server/ (FastAPI)  │ ◄───── webhooks ────── │  (streaming) │  HLS/WebRTC out,
│  + React frontend   │                        └──────────────┘  records segments
└─────────┬───────────┘
          │ infer by model name (+ X-Internal-Api-Key)
          ▼
┌─────────────────────┐   name → URL, gate,    ┌──────────────┐
│  Middleware (KAI-C) │ ─── audit, correlate ─►│  AI Adapters │  YOLO / BLIP /
│  kai-c/             │                        │ (ai-adapter) │  InsightFace / …
└─────────┬───────────┘                        └──────────────┘
          │ publishes results
          ▼
        NATS  ──►  detector/alert apps (examples/, built on the SDK)
```

1. **Camera Plane** — `server/` (FastAPI backend + the built React app) and
   **MediaMTX**. The backend owns auth, RBAC, camera/recording metadata, config,
   and cloud egress. **MediaMTX carries the video bytes** (RTSP in; HLS/WebRTC
   out; recording segmentation). The backend only provisions MediaMTX paths and
   stores metadata — it is not in the media path.
2. **Middleware Gateway** — `kai-c/` (KAI-C). The backend asks it to run
   inference *by model name*; KAI-C resolves the name to an adapter URL, enforces
   the sovereignty gate, threads a correlation ID, audits the call, and broadcasts
   the result on NATS. See [`AI_ADAPTER_CONTRACT.md`](AI_ADAPTER_CONTRACT.md).
3. **Analytics Layer** — AI adapters (the separate `ai-adapter` repo). Any model
   behind `/health` + `/infer` + `/info` is a first-class capability. **All model
   weights live here**, never in the apps.

## Two data planes

- **Inference plane** — backend → KAI-C → adapter. The backend's own camera
  inference runs over this path (`POST /api/v1/infer/{model}`), audited and
  NATS-published.
- **Event / alert plane** — apps subscribe to `opennvr.inference.>` on NATS, run
  a rule, and publish `opennvr.alerts.>`; alert-sink apps subscribe to those.
  This is how the `examples/` apps (intrusion, loitering, LPR, …) work without
  touching the core.

## Request lifecycle (web)

```
Browser ──TLS──► nginx (:443) ──► backend (127.0.0.1:8000)
                                    │  RequestLoggingMiddleware
                                    │  router (Depends: auth / RBAC)
                                    │  service ──► SQLAlchemy ──► Postgres
                                    └──► KAI-C / MediaMTX / cloud as needed
Live video / playback: browser ──► nginx ──► MediaMTX (/webrtc /hls /playback)
```
Auth is JWT (access + refresh); MFA is mandatory. See the auth section of
`SECURITY_ARCHITECTURE.md`.

## Ports (default single-host stack)

| Service | Port(s) | Exposure |
|---|---|---|
| nginx (the only LAN edge) | 443 (+ 80→443) | LAN |
| backend API + frontend | 8000 | `127.0.0.1` (fronted by nginx) |
| KAI-C | 8100 | internal (backend → loopback) |
| AI adapter (e.g. yolov8) | 9002 (docker) / 9100 (bare dev) | internal |
| MediaMTX | RTSPS 8322 · HLS 8888 · WebRTC 8889 (+ICE 8189) · RTSP 8554 · admin 9997 · playback 9996 | `127.0.0.1` except WebRTC ICE |
| PostgreSQL, NATS | 5432 · 4222/8222 | internal |

## Code map — where things live

| Path | Responsibility |
|---|---|
| `server/main.py` | FastAPI app: lifespan/startup, router wiring, SPA serving |
| `server/core/` | Cross-cutting: `config`, `auth`, `database`, `permissions`, `policy`, `logging_config`, `secret_policy` |
| `server/routers/` | HTTP endpoints — one file per domain (cameras, recordings, auth, apps, cloud_*, …) |
| `server/services/` | Business logic + external systems (MediaMTX, ONVIF, cloud, KAI-C client, storage, recordings, inference) |
| `server/models.py` · `schemas.py` | SQLAlchemy tables · Pydantic request/response schemas |
| `server/migrations/` | Alembic schema migrations |
| `kai-c/main.py` · `kai-c/kai_c/` | KAI-C app · its modules (`registry`, `sovereignty`, `audit`, `correlation`, `nats_publisher`, `stream_proxy`, `metrics`) |
| `sdk/opennvr-app-sdk/` | App SDK — the `Detector` / `FrameApp` / `AlertSubscriber` archetypes |
| `examples/` | 14 copy-as-template apps built on the SDK |
| `app/src/` | React frontend — `views/`, `components/`, `services/`, `lib/`, `hooks/` |
| `scripts/` | Install/setup/secret/cert tooling + the `app-installer` reconciler |
| `mediamtx*.yml` · `nginx/` · `docker-compose*.yml` | Infrastructure / orchestration |

## Key invariants (don't break these)

- **Offline-first by default.** `DEPLOYMENT_MODE=offline` and
  `AI_SOVEREIGNTY=local_only` make cloud routes and non-local AI return 403 until
  an operator opts in. Gates live in `server/core/policy.py`. (V-009 / V-022)
- **Secrets validated at boot.** Weak/placeholder secrets abort startup. (V-002)
- **MediaMTX stays in the trust zone.** Its ingress URLs must be
  loopback/RFC1918/ULA/link-local; public exposure goes through a TLS proxy via
  `MEDIAMTX_EXTERNAL_*`. (V-015)
- **No default admin password.** First boot mints a one-time setup token. (V-001)

The `V-###` codes above are the security controls documented in
[`SECURITY_ARCHITECTURE.md`](SECURITY_ARCHITECTURE.md); code comments reference
them as `See V-###`.

## Where to go next

- **Build an app / detector** → [`FIRST_DETECTOR.md`](FIRST_DETECTOR.md), [`CONTRIBUTING_APPS.md`](CONTRIBUTING_APPS.md), [`APP_SURFACES.md`](APP_SURFACES.md)
- **The adapter wire spec** → [`AI_ADAPTER_CONTRACT.md`](AI_ADAPTER_CONTRACT.md)
- **Security & compliance** → [`SECURITY_ARCHITECTURE.md`](SECURITY_ARCHITECTURE.md), [`COMPLIANCE.md`](COMPLIANCE.md)
- **Design rationale** → [`DESIGN_NOTES.md`](DESIGN_NOTES.md)
- **Run it** → [`../DOCKER_QUICKSTART.md`](../DOCKER_QUICKSTART.md), [`LOCAL_SETUP.md`](LOCAL_SETUP.md)
