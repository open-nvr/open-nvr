# Intrusion-detection example app

The first first-party OpenNVR example app per §12 of the [AI Adapter Contract](../../docs/AI_ADAPTER_CONTRACT.md). Watches one or more cameras for persons/vehicles entering operator-defined restricted zones during operator-defined restricted hours, and fires alerts to stdout and (optionally) a webhook.

This is also the canonical **consumer-side** validation of the contract: every previous milestone built producer-side surface (adapters, KAI-C registry, audit, sovereignty). This example proves the whole chain works end-to-end as an operator-facing artifact.

## What it does

```
┌──────────┐  every poll_interval_seconds
│  Camera  │ ─────────────────────────────┐
└──────────┘                              │
                                          ▼
                              ┌──────────────────────┐
                              │ frame_sources.fetch  │  file:// or http(s)://
                              └──────────┬───────────┘
                                         │ raw JPEG bytes
                                         ▼
                              ┌──────────────────────┐
                              │   KaicClient         │  base64 frame
                              │   POST /api/v1/      │  X-Correlation-Id
                              │   infer/{adapter}    │  X-Internal-Api-Key
                              └──────────┬───────────┘
                                         │ §5.1 DetectionResult
                                         ▼
                              ┌──────────────────────┐
                              │ filter watch_labels  │
                              │ → bbox_center        │
                              │ → zone.contains?     │
                              │ → restricted_hours?  │
                              └──────────┬───────────┘
                                         │ yes
                                         ▼
                              ┌──────────────────────┐
                              │  AlertDispatcher     │  stdout (always)
                              │                      │  + webhook (optional)
                              └──────────────────────┘
```

Every alert carries a `correlation_id` that joins back to KAI-C's audit log — an operator investigating an incident can pull the full causal chain: the alert → the KAI-C inference event → the adapter's audit line.

## Operational notes

**Polling is serial across cameras.** With N cameras and per-camera inference latency L, the cycle takes ~N×L. If `request_timeout_seconds` (default 30s) is much greater than `poll_interval_seconds` (default 1s), one slow inference blocks the whole loop for the timeout. For N > ~10 cameras or when sub-second responsiveness matters, parallel polling lands in A2.5b.

**Fail-fast on bad camera URLs.** `IntrusionDetector.__init__` raises on the first unsupported `frame_url`, so a single typo aborts startup before any detection runs. Operator notices the typo immediately rather than silently losing one camera in a fleet of ten — but the trade-off is real, so review the full config before deploying.

**Restricted hours use the host timezone.** `datetime.now()` picks up the host TZ. DST transitions can cause one duplicated or skipped hour per year — operators in TZ-sensitive deployments should pin the container to UTC and translate their restricted-hours window accordingly.

**Webhook payloads include topology metadata.** The §11.5 alert shape carries `correlation_id`, adapter name, model version, and camera_id. If the webhook URL is compromised (or pointed at an untrusted destination via config tampering), the attacker learns internal deployment topology. Treat the config file as a sensitive secret; restrict its filesystem permissions accordingly.

## What's NOT in v1

- **RTSP stream input** — only HTTP snapshot polling. RTSP needs an ffmpeg subprocess; lands in A2.5b.
- **WebSocket streaming through KAI-C** — KAI-C's WS proxy isn't built yet (A2.4b). HTTP polling at 1 fps is sufficient for most security-camera use cases.
- **Tracking / persistence across frames** — every cycle is independent. Same person standing in a zone for 60s fires 60 alerts (unless you ack/snooze in the receiving system).
- **Adapter discovery / multi-camera-per-adapter routing** — `kaic_adapter_name` is single-valued in config. Multi-adapter fanout lands as a follow-up.
- **OpenNVR alerts API integration** — webhook + stdout for v1. Native OpenNVR alerts-inbox integration lands alongside the operator-UI alerts work in A2.5b.

## Quick start

```bash
# 1. Build & run the YOLOv8 adapter (the example talks to it via KAI-C).
#    A2.2 ships the Dockerfile in ai-adapter/adapters/yolov8/.
cd ai-adapter
docker build -f adapters/yolov8/Dockerfile -t opennvr/yolov8-adapter:local .
docker run --rm -d --name yolov8 -p 9002:9002 \
  -e OPENNVR_ADAPTER_TOKEN=$(openssl rand -hex 16) \
  -v $(pwd)/model_weights:/weights:ro \
  opennvr/yolov8-adapter:local

# 2. Run KAI-C from source (A2.4 doesn't ship a versioned image yet —
#    that's a follow-up). Either `python -m uvicorn main:app --port 8100`
#    from the kai-c/ directory, or build a local image from kai-c/Dockerfile.
cd ../open-nvr/kai-c
export INTERNAL_API_KEY=$(openssl rand -hex 32)
AI_SOVEREIGNTY=local_only INTERNAL_API_KEY=$INTERNAL_API_KEY \
  python -m uvicorn main:app --host 0.0.0.0 --port 8100 &

# 3. Register the adapter with KAI-C
curl -X POST http://localhost:8100/api/v1/adapters/register \
  -H "X-Internal-Api-Key: $INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"yolov8","url":"http://127.0.0.1:9002"}'

# 4. Configure + run the intrusion detector
cd ../examples/intrusion-detection
cp config.example.yml config.yml
# edit config.yml: kaic_api_key, camera frame_url, zone, restricted_hours
python intrusion_detection.py --config config.yml
```

You'll see lines like:

```
2026-05-19T14:32:18+00:00 INFO intrusion-detection: intrusion-detection started: 2 cameras, poll=1.0s, watch=['person', 'car'], hours=22:00:00-06:00:00
ALERT [HIGH] 2026-05-19T22:14:07+00:00 camera=cam-front-gate title='Person in restricted zone \'front-yard-restricted\'' correlation_id=a4f1b... alert_id=alrt_8c2d31
```

## Config

Copy `config.example.yml` and edit. Required: `kaic_url`, `cameras` (with `camera_id`, `frame_url`, `zone`). See the config file's inline comments for every field.

## Operate

| Mode | Command |
|---|---|
| Daemon (production) | `python intrusion_detection.py --config config.yml` |
| One cycle per camera then exit (testing) | `python intrusion_detection.py --config config.yml --once` |
| Verbose | `python intrusion_detection.py --config config.yml --log-level DEBUG` |

Send `SIGINT` (Ctrl-C) or `SIGTERM` to stop cleanly — the detector finishes its current cycle and exits.

## Layout

```
examples/intrusion-detection/
├── intrusion_detection.py  Main loop + IntrusionDetector class + CLI
├── zone.py                 Point-in-polygon (ray-cast) + bbox_center
├── alerts.py               Alert dataclass + stdout/webhook channels
├── frame_sources.py        file://, http(s):// — pluggable
├── config.example.yml      Sample config with every option
├── Dockerfile              Drop-in run-this
├── pyproject.toml          Minimal deps (httpx, PyYAML)
├── README.md               you are here
└── tests/
    ├── test_zone.py            (11 tests)
    ├── test_alerts.py          (12 tests)
    ├── test_frame_sources.py   (14 tests)
    └── test_intrusion_detection.py  (16 tests)
```

## Tests

```bash
pip install -e ".[dev]"
PYTHONPATH=. pytest tests/
```

53 tests total. Coverage: zone math (convex + concave + edge cases), alert routing (stdout + webhook + failure isolation), frame sources (file + HTTP + transport errors + unsupported schemes), and the full `IntrusionDetector.step()` loop with a stubbed KAI-C (alert paths, no-alert paths, restricted-hours edges, KAI-C errors, correlation_id threading).

## Why this is a template

If you want to build a new monitoring app for OpenNVR — package detection, loitering detection, PPE compliance, fall detection, fire/smoke — copy this directory. Replace:

- `zone.py` with whatever spatial logic your task needs (line crossings? heatmaps? bounding-region intersection?)
- The detection-filter logic in `IntrusionDetector.step()` with your task-specific predicate
- Watch-labels + alert title/description for your domain

Everything else — KAI-C call, correlation_id, audit trail, alert dispatch, frame fetching, config loading, SIGINT handling — is the same template.

That template is exactly what §12.4 of the design doc calls "first-party example as a first-class community contribution lane." Submit a PR adding your example under `examples/{your-slug}/` and you join the catalogue.

## Roadmap (A2.5b)

- RTSP input via ffmpeg subprocess
- OpenNVR backend snapshot URL (`opennvr://cameras/{id}/snapshot`)
- Native OpenNVR alerts-API integration (replace webhook for OpenNVR deployments)
- WebSocket streaming through KAI-C once that proxy lands
- Per-detection deduplication (so a stationary person doesn't fire continuously)
