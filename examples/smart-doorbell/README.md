# smart-doorbell example app

The third producer-side first-party OpenNVR example. Polls a
doorbell camera, runs face recognition via the InsightFace adapter
through KAI-C, and fires alerts with severity based on whether the
visitor is registered family, a known friend, or a stranger.

The enrollment flow is **pure REST** — no shared filesystem,
no desktop tool. Snap a photo, run one CLI command, the face is
registered. Same flow Frigate / Shinobi force you to set up via a
web UI.

## What it does

```
┌─────────────┐   every poll_interval_seconds
│  Doorbell   │ ──────────────────────────────────┐
│   camera    │                                   │
└─────────────┘                                   ▼
                              ┌───────────────────────────────────┐
                              │ frame_sources.fetch (HTTP / file) │
                              └──────────────┬────────────────────┘
                                             │ frame JPEG bytes
                                             ▼
                              ┌───────────────────────────────────┐
                              │ KAI-C → InsightFace adapter       │
                              │   POST /api/v1/infer/insightface  │
                              │   params={task:"face_recognition"} │
                              └──────────────┬────────────────────┘
                                             │ FaceRead
                                             ▼
                              ┌───────────────────────────────────┐
                              │ classify: family / known / unknown │
                              └──────────────┬────────────────────┘
                                             │
                                             ▼
                              ┌───────────────────────────────────┐
                              │  AlertDispatcher (stdout/webhook  │
                              │  /NATS). Unknown-face alerts      │
                              │  carry a base64 JPEG snapshot in  │
                              │  the envelope so a small relay    │
                              │  can post it to Telegram/ntfy.    │
                              └───────────────────────────────────┘
```

A single `correlation_id` flows through every step so KAI-C's audit
log joins the chain end-to-end: alert → KAI-C inference event →
adapter audit line.

## Why the REST-only enrollment matters

Most NVRs make you upload faces through a desktop GUI or copy files
to a shared volume. This one needs neither — `python
smart_doorbell.py enroll --image alice.jpg --person-id alice` works
from any machine that can reach the adapter, including a phone or a
small Python script. Side effects:

* You can enroll over Tailscale / VPN without exposing the camera.
* You can script bulk enrollment from a folder of family photos.
* Re-enrolling (haircut, glasses, weight change) is idempotent —
  same `person_id`, new image, overwrites the embedding.
* The face DB persists at `OPENNVR_INSIGHTFACE_FACE_DB` on the
  adapter; it survives restarts but never holds raw images,
  only the 512-d embedding vectors.

## Honesty up front

Real-world failure modes the example does NOT yet handle:

* **Twins / siblings with similar embeddings.** Cosine similarity
  doesn't separate strong genetic resemblance reliably; the
  `recognition_threshold` is a global knob, not per-person.
* **Aggressive face-occlusion** (sunglasses, scarf, hat). The
  adapter may detect the face but recognition similarity drops
  below threshold → falls back to UNKNOWN. Set `dedup_window`
  appropriately so a family member walking past doesn't flood you.
* **Bad enrollment photos.** A frontal, well-lit JPEG is what
  InsightFace expects. A side profile gives a poor embedding and
  the person won't match consistently.
* **Spoofing** (printed photo, screen). v0.1 has no liveness
  detection. Planned follow-up.

## Quick start

> **On the compose stack you do none of this.** `docker-compose.apps.yml`
> provisions `insightface-adapter` (published as
> `ghcr.io/open-nvr/insightface-adapter`) and registers it with KAI-C
> alongside the app — `--profile smart-doorbell`, the installer's app
> picker, or one click from the App Catalog. The enrolled-faces DB and
> the ~280 MB `buffalo_l` model pack each live on a named volume. The
> steps below are the bare-metal developer path.

```bash
# 1. Start the InsightFace adapter (in the ai-adapter repo).
#    On first boot the adapter downloads the buffalo_l model pack into
#    ~/.insightface inside the container (InsightFace's default root).
#    Mount a host directory there so the download happens once.
cd ai-adapter
docker build -f adapters/insightface/Dockerfile -t opennvr/insightface-adapter:local .
OPENNVR_ADAPTER_TOKEN=$(openssl rand -hex 16)
mkdir -p face-db model-cache
docker run --rm -d --name insightface -p 9005:9005 \
  -e OPENNVR_ADAPTER_TOKEN=$OPENNVR_ADAPTER_TOKEN \
  -v $(pwd)/face-db:/data \
  -v $(pwd)/model-cache:/root/.insightface \
  opennvr/insightface-adapter:local

# 2. Start KAI-C and register the adapter
cd ../open-nvr/kai-c
INTERNAL_API_KEY=$(openssl rand -hex 32)
AI_SOVEREIGNTY=local_only INTERNAL_API_KEY=$INTERNAL_API_KEY \
  python -m uvicorn main:app --host 0.0.0.0 --port 8100 &
curl -X POST http://localhost:8100/api/v1/adapters/register \
  -H "X-Internal-Api-Key: $INTERNAL_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"insightface","url":"http://127.0.0.1:9005"}'

# 3. Configure
cd ../examples/smart-doorbell
cp config.example.yml config.yml
# edit config.yml: kaic_api_key, adapter_token, camera frame_url

# 4. Enroll family — one REST call per person
python smart_doorbell.py enroll \
  --config config.yml \
  --person-id alice --name "Alice Smith" --image ~/photos/alice.jpg \
  --category family

python smart_doorbell.py enroll \
  --config config.yml \
  --person-id bob --name "Bob Jones" --image ~/photos/bob.jpg \
  --category family

# 5. Sanity check
python smart_doorbell.py list-faces --config config.yml --category family

# 6. Start the daemon
python smart_doorbell.py daemon --config config.yml
```

You'll see lines like:

```
2026-05-22T18:10:43+00:00 INFO  smart-doorbell: started: 1 cameras, poll=1.0s, threshold=0.50
ALERT [LOW] 2026-05-22T18:11:02+00:00 camera=front-door title='Known visitor at front-door: Alice Smith' correlation_id=a4f1b... alert_id=alrt_8c2d31
ALERT [HIGH] 2026-05-22T18:12:54+00:00 camera=front-door title='Unknown visitor at front-door' correlation_id=8d3f5... alert_id=alrt_91e2bb
```

## Telegram / ntfy / Discord delivery

The example fires alerts to **stdout** (always), **webhook** (any
URL), and **NATS** (any subscriber on `opennvr.alerts.>`). Unknown
faces carry a base64 JPEG snapshot in `evidence.snapshot_b64`, so:

* **Telegram bot** — point `webhook_url` at a small relay that
  reads `evidence.snapshot_b64` and POSTs to
  `https://api.telegram.org/bot<TOKEN>/sendPhoto`. ~15 lines of
  Python or [n8n](https://n8n.io/) / [Node-RED](https://nodered.org/).
* **ntfy** — POST the snapshot as a [ntfy attachment](https://docs.ntfy.sh/publish/#attachments).
* **Discord** — Discord webhooks accept `multipart/form-data`
  with a `file` part; same shape as the Telegram relay.
* **Home Assistant** — subscribe to `opennvr.alerts.app.smart-doorbell.>`
  via the `home-assistant-relay` example (coming next) and the
  doorbell becomes an HA event automatically.

## Operate

| Mode | Command |
|---|---|
| Daemon (production) | `python smart_doorbell.py daemon --config config.yml` |
| One cycle then exit (testing) | `python smart_doorbell.py daemon --once --config config.yml` |
| Enroll | `python smart_doorbell.py enroll --config config.yml --person-id ID --name "Display" --image FILE --category family` |
| List | `python smart_doorbell.py list-faces --config config.yml [--category family]` |
| Delete | `python smart_doorbell.py delete-face --config config.yml --person-id ID` |
| Verbose | `python smart_doorbell.py daemon --config config.yml --log-level DEBUG` |

SIGINT / SIGTERM stops cleanly — the in-flight cycle finishes,
dispatcher drains.

## Layout

```
examples/smart-doorbell/
├── smart_doorbell.py              CLI + SmartDoorbell driver
├── face_recognition_pipeline.py   Testable pipeline (no daemon loop)
├── alerts.py                      Alert envelope + stdout/webhook/NATS dispatchers
├── frame_sources.py               file:// + http(s):// frame fetchers
├── config.example.yml             Operator config with every option
├── pyproject.toml                 Minimal deps (httpx, PyYAML, nats-py)
├── Dockerfile                     Slim container image
├── README.md                      you are here
└── tests/
    ├── test_face_recognition_pipeline.py   (15 tests)
    └── test_smart_doorbell.py              (16 tests)
```

## Tests

```bash
uv pip install -e ".[dev]"
PYTHONPATH=. pytest tests/
```

31 tests total. The tests stub the recognition client (no KAI-C
needed) and exercise the parser, dedup window, severity routing,
snapshot attachment.

## Why this is a template

Copy this folder, rename for your task, and replace the predicate.
For a `smart-doorbell` the predicate is "the recognised-face DB
returned a match." For other tasks:

* `intruder-after-hours` — recognised-face DB + restricted hours
* `package-delivery` — vehicle/package detection + porch state machine
* `lost-pet-finder` — pet face/breed adapter + watchlist matching

Everything else — KAI-C call, correlation_id, audit trail, alert
dispatch, frame fetching, SIGINT handling, dedup — is the template.
