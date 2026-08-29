# license-plate-recognition example app

The third producer-side first-party OpenNVR example. Drives a
two-stage inference chain — **YOLOv8 vehicle detection → crop → OCR
on the crop** — and fires alerts per recognised plate. Pairs cleanly
with `intrusion-detection` and `loitering-detection` to show how a
monitoring app composes multiple adapters through KAI-C.

The single-purpose OCR adapter lives in `ai-adapter` as
[`adapters/fast_plate_ocr/`](https://github.com/open-nvr/ai-adapter/tree/main/adapters/fast_plate_ocr) —
it wraps [`fast-plate-ocr`](https://github.com/ankandrew/fast-plate-ocr)
(Apache-2.0, ONNX, CPU-only, plate-specific).

## What it does

```
┌──────────┐   every poll_interval_seconds
│  Camera  │ ──────────────────────────────────┐
└──────────┘                                   │
                                               ▼
                              ┌───────────────────────────────────┐
                              │ frame_sources.fetch (HTTP / file) │
                              └──────────────┬────────────────────┘
                                             │ frame JPEG bytes
                                             ▼
                              ┌───────────────────────────────────┐
                              │ KAI-C → YOLOv8 (vehicle detect)   │
                              │   POST /api/v1/infer/yolov8       │
                              └──────────────┬────────────────────┘
                                             │ list[VehicleDetection]
                                             ▼
                              ┌───────────────────────────────────┐
                              │ Pillow crop (lower_third|vehicle) │
                              └──────────────┬────────────────────┘
                                             │ plate crop JPEG
                                             ▼
                              ┌───────────────────────────────────┐
                              │ KAI-C → fast-plate-ocr (OCR)      │
                              │   POST /api/v1/infer/             │
                              │        fast_plate_ocr             │
                              └──────────────┬────────────────────┘
                                             │ PlateRead
                                             ▼
                              ┌───────────────────────────────────┐
                              │  AlertDispatcher (stdout / webhook │
                              │  / NATS), severity by watchlist    │
                              └───────────────────────────────────┘
```

A single `correlation_id` flows through every step so KAI-C's audit
log joins the chain end-to-end: alert → vehicle inference event →
OCR inference event → adapter audit lines.

## Honesty up front

This is a useful starting point, **not a production LPR system**.
Known limitations to be aware of when you read alerts in real life:

* **Cropping is heuristic.** The default `crop_strategy: lower_third`
  takes the bottom third of each vehicle bbox — works well for
  front/rear-facing fixed cameras, less well for oblique angles. For
  best accuracy, swap in a YOLOv8 model fine-tuned on plate ROI (e.g.
  the Ultralytics license-plate-detection notebook) and set
  `crop_strategy: vehicle` so the trained model's tight bbox feeds
  the OCR directly.
* **fast-plate-ocr is plate-specific.** Garbage in, garbage out on
  scenes without a clear plate — that's why we filter to vehicle
  detections upstream.
* **Dedup is per-plate-per-camera, time-windowed.** A plate that
  drifts out of frame and back within `dedup_window_seconds` will
  only fire once. A plate read with one character flipped will be
  treated as a different plate (no fuzzy matching). Both are
  intentional — false positives in fuzzy matching are worse than
  occasional missed re-fires.
* **No region-specific tuning.** `fast-plate-ocr` ships multiple
  regional weight bundles; swap via the adapter's `OPENNVR_LPR_MODEL`
  env var.

## Quick start

**On a compose install, skip this section.** The apps overlay ships and
registers the OCR adapter with the app (RFC-0002 decision 7 — an app's
required adapters install with it; yolov8 is already part of the standard
stack and is reused, not duplicated):

```bash
docker compose -f docker-compose.yml -f docker-compose.apps.yml \
  --profile apps up -d license-plate-recognition
```

Then edit `config.docker.yml`'s cameras (the template ships a
placeholder), re-run the `license-plate-recognition-config-init` service,
and restart the app. Results: `docker compose logs -f
license-plate-recognition` plus the alerts inbox; the adapter appears on
the AI Adapters page.

The manual path below is for **dev mode** — running the chain outside
compose:

```bash
# 1. Start the two upstream adapters (in the ai-adapter repo)
cd ai-adapter
docker build -f adapters/yolov8/Dockerfile -t opennvr/yolov8-adapter:local .
docker build -f adapters/fast_plate_ocr/Dockerfile \
             -t opennvr/fast-plate-ocr-adapter:local .

OPENNVR_ADAPTER_TOKEN=$(openssl rand -hex 16)
docker run --rm -d --name yolov8 -p 9002:9002 \
  -e OPENNVR_ADAPTER_TOKEN=$OPENNVR_ADAPTER_TOKEN \
  -v $(pwd)/model_weights:/weights:ro \
  opennvr/yolov8-adapter:local

docker run --rm -d --name lpr -p 9004:9004 \
  -e OPENNVR_ADAPTER_TOKEN=$OPENNVR_ADAPTER_TOKEN \
  opennvr/fast-plate-ocr-adapter:local

# 2. Start KAI-C and register both adapters
cd ../open-nvr/kai-c
INTERNAL_API_KEY=$(openssl rand -hex 32)
AI_SOVEREIGNTY=local_only INTERNAL_API_KEY=$INTERNAL_API_KEY \
  python -m uvicorn main:app --host 0.0.0.0 --port 8100 &

curl -X POST http://localhost:8100/api/v1/adapters/register \
  -H "X-Internal-Api-Key: $INTERNAL_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"yolov8","url":"http://127.0.0.1:9002"}'
curl -X POST http://localhost:8100/api/v1/adapters/register \
  -H "X-Internal-Api-Key: $INTERNAL_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"fast_plate_ocr","url":"http://127.0.0.1:9004"}'

# 3. Configure + run the LPR app
cd ../examples/license-plate-recognition
cp config.example.yml config.yml
# edit config.yml: kaic_api_key, camera frame_url, optional watchlists
python license_plate_recognition.py --config config.yml
```

You'll see lines like:

```
2026-05-22T18:10:43+00:00 INFO  license-plate-recognition: started: 2 cameras, poll=2.0s, ...
ALERT [info] 2026-05-22T18:11:02+00:00 camera=driveway title="Plate ABC1234 read" correlation_id=a4f1b... alert_id=alrt_8c2d31
ALERT [high] 2026-05-22T18:11:54+00:00 camera=parking-lot title="Watchlist plate BAD-001 seen" correlation_id=8d3f5... alert_id=alrt_91e2bb
```

## Operate

| Mode | Command |
|---|---|
| Daemon (production) | `python license_plate_recognition.py --config config.yml` |
| One cycle per camera then exit (testing) | `python license_plate_recognition.py --config config.yml --once` |
| Verbose | `python license_plate_recognition.py --config config.yml --log-level DEBUG` |

SIGINT / SIGTERM stops cleanly — the in-flight cycle finishes and
the alert dispatcher drains.

## Layout

```
examples/license-plate-recognition/
├── license_plate_recognition.py  CLI + LicensePlateRecognizer driver
├── plate_pipeline.py              The detect→crop→OCR pipeline (testable)
├── alerts.py                      Alert envelope + stdout / webhook / NATS dispatchers
├── frame_sources.py               file:// + http(s):// fetchers
├── config.example.yml             Operator config with every option
├── pyproject.toml                 Minimal deps: httpx, PyYAML, nats-py, Pillow
├── README.md                      you are here
└── tests/
    ├── test_plate_pipeline.py            (21 tests)
    └── test_license_plate_recognition.py (8 tests)
```

## Tests

```bash
uv pip install -e ".[dev]"
PYTHONPATH=. pytest tests/
```

The tests stub the KAI-C calls (no upstream needed) and exercise the
parser logic, cropping, dedup window, watchlist severity, and the
alert dispatch.

## Why this is a template

Copy this folder, rename it for your task, and replace the
**predicate**: in this example the predicate is "the OCR adapter
returned an accepted read." Other example apps replace it with
"watch labels appear in a zone" (intrusion-detection) or "watch
labels dwell longer than threshold" (loitering-detection). The
shape — frame fetch → KAI-C call chain → alert dispatch — is the
same across every example.
