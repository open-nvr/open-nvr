# license-plate-recognition example app

**The RFC-0002 reference implementation**: a first-party app as apps
are meant to look — a pure consumer of a contracted platform event,
with **zero inference code**. It subscribes to
`opennvr.events.plate.recognized.v1.>` (docs/EVENT_CONTRACTS.md),
routes severity through allow/deny watchlists, dedups per
(camera, plate), and fires alerts. That's the whole app.

The single-purpose OCR adapter lives in `ai-adapter` as
[`adapters/fast_plate_ocr/`](https://github.com/open-nvr/ai-adapter/tree/main/adapters/fast_plate_ocr) —
it wraps [`fast-plate-ocr`](https://github.com/ankandrew/fast-plate-ocr)
(Apache-2.0, ONNX, CPU-only, plate-specific).

## What it does

```
┌──────────┐  Tier-0 detects, tracks, keeps the best frame
│  Camera  │ ───────────────────────────────────────────────┐
└──────────┘                                                ▼
                       ┌─────────────────────────────────────────────┐
                       │ PLATFORM (RFC-0002 decision 9)              │
                       │  gate escalation → Tier-1 dispatch          │
                       │  best-frame crop → KAI-C → fast_plate_ocr   │
                       │  (once per vehicle VISIT, on cameras        │
                       │   assigned the LPR skill; core's per-visit  │
                       │   enrichment is the fallback producer)      │
                       │  KAI-C publishes:                           │
                       │  opennvr.events.plate.recognized.v1.<cam>   │
                       └──────────────────┬──────────────────────────┘
                                          │ the contracted envelope
                                          ▼
                       ┌─────────────────────────────────────────────┐
                       │ THIS APP (PlateAlerter, ~100 lines of rule) │
                       │  camera scope (assignment table) →          │
                       │  min_confidence → dedup window →            │
                       │  watchlist severity → AlertDispatcher       │
                       │  (stdout / webhook / NATS)                  │
                       └─────────────────────────────────────────────┘
```

A single `correlation_id` flows from the alert back through the
domain event to KAI-C's audit rows, so the chain joins end-to-end.

**Why this shape** (v1 of this app polled frames and drove its own
YOLOv8 → crop → OCR chain): the platform already detects every vehicle
(Tier-0) and already runs the OCR once per visit. The old app re-ran
detection on the same pixels — double CPU for identical work (RFC-0002
gap 2). As a consumer, adding this app to an install adds **no**
inference cost, and two plate-consuming apps cost the same as one.

## Honesty up front

* **No events, no alerts.** Plates flow only where the platform chain
  can run: the `fast_plate_ocr` adapter registered (ships with this
  app's compose overlay), and either core's plate enrichment on
  vehicle visits (default-on) or Tier-1 dispatch on cameras assigned
  the LPR skill (gate `enforce` + dispatch URL, off by default).
* **Dedup is per-plate-per-camera, time-windowed**, exactly as before:
  a plate re-read within `dedup_window_seconds` fires once; a
  one-character misread is a different plate (no fuzzy matching).
* **Regional tuning** lives in the adapter (`OPENNVR_LPR_MODEL`), not
  here — this app never sees a pixel.

## Quick start

**On a compose install:** the apps overlay ships and registers the OCR
adapter with the app (RFC-0002 decision 7):

```bash
docker compose -f docker-compose.yml -f docker-compose.apps.yml \
  --profile apps up -d license-plate-recognition
```

Then assign cameras the **License Plate Recognition** skill (Cameras →
edit → Assignments). No camera URLs to configure — the app scopes
itself to assigned cameras via the assignment table. Results:
`docker compose logs -f license-plate-recognition` plus the alerts
inbox; watchlists are editable live from the App Catalog config form.

**Dev mode** (outside compose): a NATS broker with plate events on it
is the only requirement.

```bash
cp config.example.yml config.yml   # edit nats_url; optionally watchlists
python license_plate_recognition.py --config config.yml
```

You'll see lines like:

```
2026-08-29T18:11:02+00:00 ALERT [info] camera=cam-1 title="Plate ABC1234 read" correlation_id=a4f1b...
2026-08-29T18:11:54+00:00 ALERT [high] camera=cam-2 title="Watchlist plate BAD001 seen" correlation_id=8d3f5...
```

## Operate

| Mode | Command |
|---|---|
| Daemon (production) | `python license_plate_recognition.py --config config.yml` |
| One event then exit (smoke test) | `python license_plate_recognition.py --config config.yml --once` |
| Verbose | `python license_plate_recognition.py --config config.yml --log-level DEBUG` |

SIGINT / SIGTERM stops cleanly — the NATS subscription drains and the
alert dispatcher flushes.

## Layout

```
examples/license-plate-recognition/
├── license_plate_recognition.py  PlateAlerter (Detector) + config + CLI
├── alerts.py                     Shim → the SDK §11.5 alert stack
├── config.example.yml            Operator config with every option
├── pyproject.toml                One dependency: the app SDK
├── README.md                     you are here
└── tests/
    └── test_license_plate_recognition.py (16 tests)
```

## Tests

```bash
uv pip install -e ".[dev]"
PYTHONPATH=. pytest tests/
```

The tests feed `plate.recognized.v1` envelopes straight through the
handler (no NATS, no core needed) and exercise severity routing, the
dedup window, confidence filtering, camera scoping, malformed-input
isolation, live watchlist updates, and the config loader.

## Why this is a template

Copy this folder, rename it for your event, and replace the
**predicate**: here it is "an accepted plate read arrived — is it
watchlisted, and did we already fire for it?" The shape — subscribe to
a contracted domain event → apply your rule → dispatch alerts — is
what every consumer app looks like. If your rule needs raw detections
instead of a domain event, extend the same `Detector` base without
overriding `handle_event` (see `occupancy-counting`).
