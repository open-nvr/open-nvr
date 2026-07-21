# Tier-0 compute-gated inference: `detect-pipeline` service

## Summary
Adds **Tier-0 detection** to OpenNVR — an always-on, per-camera pipeline
(`motion → region select → cheap detect → track → best-frame`) that lets AI run
efficiently on affordable hardware (Raspberry Pi 5 / Intel N100). It ships as a
new `detect-pipeline` container that is a **pure additive consumer**: it
subscribes to streams MediaMTX already republishes and emits to the inference
NATS subjects adapters already use. Nothing about how cameras are ingested,
recorded, or served changes.

This is **PR A** of the [compute-gated inference design](docs/design/compute-gated-inference.md)
— *efficient full-time detection, no gating*. The gate + shadow mode land in PR B.

## Why
Running heavy AI on every frame of every camera doesn't fit modest hardware.
Tier-0 is the always-on floor that catches presence cheaply and (in PR B) will
gate the expensive interpretation — the same proven architecture Frigate uses.
Design invariant held throughout: **the gate is never in the path of catching an
event; recording is never gated.**

## What's included
- **Ported CV pipeline** (from Frigate `6f80bcd19`, MIT — see `detect-pipeline/NOTICE`):
  hwaccel ffmpeg decode/scale presets (VAAPI/QSV/NVIDIA/Jetson/RKMPP/RPi),
  self-restarting I420 frame source, `improved_motion` detector
  (contrast/persistence/lightning suppression), region geometry, attribute-aware
  best-frame selection.
- **Lean size-aware tracker** — bottom-center distance (not centroid-IoU) to avoid
  ID churn; lifecycle + `motionless_count`.
- **Detector-adapter seam** — `DetectorAdapter` interface + tensor shaping; local
  reference detectors (OpenCV HOG people-detector by default, blob, stub).
- **Service** — `WorkerManager` runs one worker per camera; entrypoint `opennvr-tier0`.
- **Runnable CLI** — `python -m detect_pipeline --source <video|rtsp> --out annotated.mp4`
  for manual verification.
- **Adapter contract v1.1** — optional `Accelerator`/`InputSpec`/`DetectorSpec` in
  the SDK (`ai-adapter`), vendored into KAI-C in lockstep (backward-compatible,
  `extra="ignore"`).

## How it's wired (product integration)
- **Discovery**: reuses the existing `GET /api/v1/internal/camera-agent/cameras`
  (`X-Internal-Api-Key`) — resolves each camera to the MediaMTX tap URL, so **no
  new server endpoint**.
- **Input**: pulls the MediaMTX republish (OpenNVR keeps ownership of the single
  camera connection).
- **Output**: publishes to `opennvr.inference.tier0.<camera_id>.completed` —
  existing consumers work unchanged.
- **Deployment**: `detect-pipeline` service in `docker-compose.yml`, **pulled from
  GHCR** like `opennvr-core`/`yolov8-adapter`. **On by default**;
  `DETECT_PIPELINE_ENABLED=false` disables without a redeploy.

## Safety: no impact to existing behavior
The diff vs `main` is **additive only**. Modified files are limited to four
sanctioned, backward-compatible edits: `docker-compose.yml` (new service block),
`.env.example` (new vars), `.github/workflows/ci.yml` (new job), and
`kai-c/kai_c/contract_types.py` (optional vendored fields). **No changes to any
existing `server/`, `mediamtx`, or entrypoint runtime code.** Disable the flag and
the deployment is byte-for-byte today's behavior.

## Testing
- **82 tests** in `detect-pipeline` (unit + integration + a real-ffmpeg decode
  test + an end-to-end synthetic-clip smoke) + 4 KAI-C contract tests — all green.
- **CI**: added to the main `ci.yml` (runs on every push/PR, with ffmpeg) and a
  dedicated `publish-detect-pipeline.yml` that gates the GHCR image build on the
  tests plus a container-level smoke (import graph, entrypoint, ffmpeg).

## Deliberately deferred (labeled, not overclaimed)
- Kalman/Norfair motion prediction in the tracker (lean matcher for now).
- Learned per-camera region grid (needs OpenNVR-side storage).
- KAI-C-backed accelerator detector adapter (uses local HOG until then).
- The gate itself + shadow mode → **PR B**.

## Reviewer notes / caveats
- Paired with an additive commit on `ai-adapter` `main` (contract v1.1) —
  merge/lockstep together.
- CI/Docker were not run locally; the workflows/Dockerfile are validated as YAML
  and modeled on the existing `publish-images.yml`. Please watch their first run,
  and do an on-hardware camera smoke (`docker compose up`, watch
  `opennvr.inference.tier0.*`).

## How to try
```bash
# manual, on a clip or a camera substream:
cd detect-pipeline && pip install -e .
python -m detect_pipeline --source people.mp4 --out annotated.mp4 --detector hog

# in the stack:
docker compose pull && docker compose up -d      # on by default
```
