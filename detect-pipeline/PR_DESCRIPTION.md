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
- **Detector-adapter seam** — `DetectorAdapter` interface + tensor shaping.
  Default detector is **YOLOv8 ONNX** (the same `yolov8n.onnx` the stack builds),
  with `blob`/`stub` reference detectors and `hog` (OpenCV 4.x only).
- **Pluggable inference backend** — the ONNX model runs through `cvdnn` *(default;
  `cv2.dnn`, zero extra dependency, CPU, any platform)* or `ort` *(ONNX Runtime with
  selectable execution providers)*. `ort` is the accelerator on-ramp on the **same
  ONNX model**: OpenVINO (Intel N100 iGPU/NPU), TensorRT/CUDA (Nvidia/Jetson),
  CoreML (Mac) — install the matching wheel + set `DETECT_ONNX_PROVIDERS`; CPU is
  always kept as a fallback. Coral/Hailo/RKNN are separate backends on the same
  seam (own model format + SDK) → follow-ups. Runs on **OpenCV 4.x and 5.x**.
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
- **102 passed, 1 skipped** in `detect-pipeline` (unit + integration + a real-ffmpeg
  decode test + an end-to-end synthetic-clip smoke) + KAI-C contract tests — green
  on OpenCV 5.0.0 (the skip is HOG, removed in OpenCV 5). The ONNX decode and both
  backends are unit-tested with injected net/session, so **CI needs no model file
  or onnxruntime**.
- **Real-backend verification**: the `ort` path was exercised end-to-end against a
  real ONNX Runtime session (not a fake), and `cvdnn` + `ort` produce identical
  results on the same model. Independent review verdict: merge-ready, no bugs.
- **CI**: added to the main `ci.yml` (runs on every push/PR, with ffmpeg) and a
  dedicated `publish-detect-pipeline.yml` that gates the GHCR image build on the
  tests plus a container-level smoke (import graph, entrypoint, ffmpeg).

## Deliberately deferred (labeled, not overclaimed — see `FOLLOWUPS.md`)
- Kalman/Norfair motion prediction in the tracker (lean matcher for now).
- Learned per-camera region grid (needs OpenNVR-side storage).
- Coral/Hailo/RKNN backends + KAI-C accelerator adapter (ORT already covers
  OpenVINO/TensorRT/CUDA/CoreML on the same model).
- Stronger detector families — YOLO26 / RF-DETR (NMS-free; need a different decode).
- The gate itself + shadow mode, and the pluggable `TriggerPolicy` → **PR B**.

## Reviewer notes / caveats
- Paired with an additive commit on `ai-adapter` (contract v1.1) — merge/lockstep
  together.
- CI/Docker were not run locally; the workflows/Dockerfile are validated as YAML
  and modeled on the existing `publish-images.yml`. Please watch their first run.
- **On-hardware validation still owed** (can't be done in review) — Levels 3–5 of
  `OpenNVR-Tier0-Test-Guide` / `detect-pipeline/README`: a live RTSP camera, a real
  accelerator (OpenVINO/TensorRT), and the sustained-camera capacity number. Do an
  on-hardware smoke (`docker compose up`, watch `opennvr.inference.tier0.*`).

## How to try
```bash
# manual, on a clip (real detection needs a model):
cd detect-pipeline && pip install -e .
python -m detect_pipeline --source people.mp4 --out annotated.mp4 \
    --detector onnx --model yolov8n.onnx           # add --backend ort for ONNX Runtime
python -m detect_pipeline --source clip.mp4  --out annotated.mp4 --detector blob   # no model

# in the stack:
docker compose pull && docker compose up -d        # on by default
```
