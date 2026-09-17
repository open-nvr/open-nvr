# Examples gallery

Every example here is a **copy-as-template** starting point — minimal, readable,
and opinionated. Pick one that's close to what you want to build, copy the
folder, and edit the predicate.

> **Building a brand-new app?** Skip the copy-and-delete. A generator
> scaffolds a minimal, runnable Detector app and you fill in one method —
> the rule. Follow **[Your first OpenNVR detector in 15 minutes](../docs/FIRST_DETECTOR.md)**.

The thirteen shipped examples cover two orthogonal axes of the OpenNVR pipeline:
*driving* inference vs *subscribing* to it, and *inference events* vs *alerts*.

> **Why write your rule as an SDK app?** Because every catalog app is
> automatically a conversational skill: the same `Detector` class serves the
> App Catalog (operator form, 24/7 daemon) *and* the camera agent
> (conversation-built session monitor). See
> [**Two Doors**](../docs/TWO_DOORS.md) for the full story and the
> capability-matching mechanism behind it, and
> [**App Surfaces**](../docs/APP_SURFACES.md) for how the same app also
> gets live config, state views, and action forms — all declared.

```
                      Drives inference?
                      ──────────────────
                       Yes                            No
                  ┌─────────────────────────┬──────────────────────┐
  Subscribes to   │                         │ inference-listener   │
  inference       │                         │ loitering-detection  │
  events          │                         │ occupancy-counting   │
                  │                         │ line-crossing        │
                  │                         │ abandoned-object     │
                  │                         │ footage-search       │
                  ├─────────────────────────┼──────────────────────┤
  Subscribes to   │ intrusion-detection¹    │ alerts-subscriber    │
  alert envelopes │ license-plate-          │ camera-agent²        │
                  │   recognition¹          │ home-assistant-      │
                  │ smart-doorbell¹         │   relay              │
                  │ package-delivery¹       │                      │
                  └─────────────────────────┴──────────────────────┘

  ¹ These four drive KAI-C directly AND emit their own alerts —
    they're the full producer-side templates. Start here if you want
    to learn the producer flow first.
  ² camera-agent sits in the "subscribes to inference" row because
    it's reactive — the user talks to it. It runs tools (which DO
    drive KAI-C) on demand, but the example is shaped as a
    conversation, not a polling daemon.
```

---

## 🐳 Run the detector apps against the stack

The migrated SDK detector apps ship a compose overlay
([`docker-compose.apps.yml`](../docker-compose.apps.yml) at the repo
root). On boot each app subscribes to the stack's NATS inference
stream, serves the SDK contract endpoints (`/health` `/manifest`
`/state`), **self-registers with the app registry** using the
deployment's `INTERNAL_API_KEY`, and appears in the **App Catalog**
(Settings → App Catalog) with a live status dot and an auto-generated
config form:

```bash
# From the repo root, on top of the standard stack:
docker compose -f docker-compose.yml -f docker-compose.apps.yml --profile apps up -d
```

Every app listed in [`server/config/apps_index.yml`](../server/config/apps_index.yml)
is in the overlay — that file is the App Catalog, and an entry is only
accepted with its compose service (`make validate-apps-index`). Each
app's runtime config is generated at `up` time from its
`config.docker.yml` template (secrets come from `.env`) — edit the
template's cameras/zones for your scene and re-run `up`, or edit them in
the catalog's config form once the app is up.

Three ways an app gets started, in order of how often they happen:

1. **On every install** — `occupancy-counting` and `footage-search`
   ride the always-on Tier-0 stream and need no extra adapter, so
   `start.sh` / `start.ps1` enable them (profile `default-apps`; opt out
   with `OPENNVR_DEFAULT_APPS=off`).
2. **Picked at install time** — `scripts/install.sh` offers the Camera
   Agent and every catalog app, several at once. Each catalog app carries
   its own id as a compose profile on every service it needs (app,
   config-init, dedicated adapters, egress-proxy), so
   `--profile license-plate-recognition` starts that app and nothing
   else. The picks persist in `.env` (`OPENNVR_EXAMPLE_*`) and
   `start.sh` replays them on every `up`.
3. **Later, from the App Catalog** — the copy-paste command shown on the
   card, or one click with the opt-in reconciler below.

Not everything under `examples/` is an app. `yolo-pose-weights/` and
`yolov8-weights/` are weight-baking helper images the overlays use;
`alerts-subscriber/` and `inference-listener/` are SDK tutorials with
no compose service. None of the four is in the catalog or the installer.

### One-click install (opt-in)

The copy-paste command above is always available. There is also an
**opt-in** one-click install path where an operator with the
`apps.install` permission installs a curated app straight from the App
Catalog — but the web app **never runs Docker**. It only writes a
desired-state row; a separate, minimally-privileged reconciler
(`scripts/app-installer`) is the single component that holds the Docker
socket and applies it. It is **off by default** (`APPS_INSTALL_ENABLED`)
— sovereign / air-gapped deployments leave it off and use the copy-paste
command. See **[docs/APPS_INSTALL.md](../docs/APPS_INSTALL.md)** for the
full design (desired-state + reconciler, RBAC, digest pinning, audit).

---

## ✅ Shipped — runnable today

### [`intrusion-detection/`](intrusion-detection)

**Detect people or vehicles entering restricted zones during restricted hours,
fire an alert.** The canonical producer-side template — drives KAI-C, walks
the audit chain end to end, demonstrates both HTTP-polling and WebSocket-
streaming transports.

| | |
|---|---|
| Pattern | Drives inference (HTTP poll or WS stream) → fires alerts |
| Adapter | YOLOv8 (object detection) |
| Difficulty | ⭐ beginner |
| Best for learning | The full producer pipeline from camera → KAI-C → alert |
| Tests | 87 |

```bash
cd examples/intrusion-detection && uv sync --extra dev
cp config.example.yml config.yml      # edit camera URLs, zones, hours
python intrusion_detection.py --config config.yml
```

---

### [`loitering-detection/`](loitering-detection)

**Detect people who linger in a zone longer than a threshold.** The canonical
subscriber-side template — rides someone else's NATS inference stream and
adds a dwell-time state machine. Zero adapter GPU cost on top of the producer
already running.

| | |
|---|---|
| Pattern | Subscribes to NATS inference events → fires alerts |
| Adapter | (rides upstream's YOLOv8 — no direct adapter call) |
| Difficulty | ⭐⭐ intermediate |
| Best for learning | NATS subscription, state machines, multi-app deployments |
| Tests | 50 |

```bash
cd examples/loitering-detection && uv sync --extra dev
cp config.example.yml config.yml      # edit threshold seconds, zones
python loitering_detection.py --config config.yml
```

---

### [`occupancy-counting/`](occupancy-counting)

**Count people (or vehicles) in a zone; alert when it's too crowded — or
too empty.** Rides the same NATS inference stream as loitering-detection
and counts in-zone detections per frame, firing an *edge-triggered* alert
on band transitions (`over` / `under` / back-to-`normal`) so a crowded
room emits one alert, not one per frame. Per-camera thresholds, debounce,
optional under-occupancy for posts that must stay staffed.

| | |
|---|---|
| Pattern | Subscribes to NATS inference events → fires alerts |
| Adapter | (rides upstream's YOLOv8 — no direct adapter call) |
| Difficulty | ⭐⭐ intermediate |
| Best for learning | Edge-triggered state machines, zone counting, threshold debounce |
| Tests | 7 |

```bash
cd examples/occupancy-counting && uv sync --extra dev
cp config.example.yml config.yml      # edit zones + max/min occupancy
python occupancy_counting.py --config config.yml
```

---

### [`line-crossing/`](line-crossing)

**Alert when a tracked person or vehicle crosses a line in a chosen
direction.** Perimeter tripwire, directional entry/exit counter, one-way
corridor. Needs a *tracked* stream (`track_id` on detections — chain the
`bytetrack` adapter) so it knows the same object moved across the wire.
Directional segment-crossing geometry with `a_to_b` / `b_to_a` / `both`.

| | |
|---|---|
| Pattern | Subscribes to NATS inference events (tracked) → fires alerts |
| Adapter | (rides upstream's detector + `bytetrack` — no direct call) |
| Difficulty | ⭐⭐ intermediate |
| Best for learning | Per-track state, directional segment-crossing geometry |
| Tests | 8 |

```bash
cd examples/line-crossing && uv sync --extra dev
cp config.example.yml config.yml      # edit line endpoints + direction
python line_crossing.py --config config.yml
```

---

### [`abandoned-object/`](abandoned-object)

**Alert when a bag, suitcase, or box is left stationary and unattended.**
The "unattended baggage" primitive for transport hubs and lobbies. Tracks
stationary watched-objects in a zone and fires when one is unattended past
a dwell threshold — with **person-proximity suppression** so a bag next to
its owner doesn't alert. Needs a tracked stream (`bytetrack`).

| | |
|---|---|
| Pattern | Subscribes to NATS inference events (tracked) → fires alerts |
| Adapter | (rides upstream's detector + `bytetrack` — no direct call) |
| Difficulty | ⭐⭐⭐ advanced |
| Best for learning | Multi-track state, spatial proximity suppression, anchor/dwell logic |
| Tests | 6 |

```bash
cd examples/abandoned-object && uv sync --extra dev
cp config.example.yml config.yml      # edit zone, object labels, thresholds
python abandoned_object.py --config config.yml
```

---

### [`footage-search/`](footage-search)

**Search recorded footage in plain language — "every red truck at the dock
yesterday."** An indexer subscribes to detector + captioner events into a
local SQLite index; a `search` CLI parses a natural-language query into
labels + descriptor keywords + time window + camera and returns matching
moments, each with the `correlation_id` for the recorded segment. Works on
existing adapters (object class from the detector, "red" from BLIP
captions); point it at the `vlm` adapter for precise attributes. Optional
local-Ollama query parsing. No cloud, no API keys.

| | |
|---|---|
| Pattern | Indexer subscribes to NATS → SQLite; CLI natural-language search |
| Adapters | (rides upstream's detector + a captioner/VLM — no direct call) |
| Difficulty | ⭐⭐⭐ advanced |
| Best for learning | Building a searchable index off the event bus, NL→filter parsing |
| Tests | 6 |

```bash
cd examples/footage-search && uv sync --extra dev
cp config.example.yml config.yml
python footage_search.py index  --config config.yml      # build the index
python footage_search.py search --config config.yml "red truck yesterday"
```

---

### [`inference-listener/`](inference-listener)

**Minimal NATS subscriber template.** Subscribes to
`opennvr.inference.{adapter}.{camera_id}.completed` and prints every event to
stdout. Read this first if you're building a custom dashboard, metrics
aggregator, or SIEM bridge.

| | |
|---|---|
| Pattern | Subscribes to NATS inference events → consumes them |
| Adapter | (none) |
| Difficulty | ⭐ beginner |
| Best for learning | The smallest-possible OpenNVR subscriber app |
| Tests | 7 |

```bash
cd examples/inference-listener && uv sync --extra dev
cp config.example.yml config.yml
python inference_listener.py --config config.yml
```

---

### [`alerts-subscriber/`](alerts-subscriber)

**Fan out alerts to webhooks, logs, or your own tooling.** Subscribes to
`opennvr.alerts.*` and forwards each fired alert. Pair with
`intrusion-detection` or `loitering-detection` (both can publish to NATS)
to build an alert router, SIEM forwarder, or Slack/Discord/PagerDuty bridge.

| | |
|---|---|
| Pattern | Subscribes to NATS alert envelopes → routes them |
| Adapter | (none) |
| Difficulty | ⭐ beginner |
| Best for learning | The alerts-subscriber side of the event bus |
| Tests | 13 |

```bash
cd examples/alerts-subscriber && uv sync --extra dev
cp config.example.yml config.yml
python alerts_subscriber.py --config config.yml
```

---

### [`license-plate-recognition/`](license-plate-recognition)

**Detect every vehicle on your driveway, log every plate.** The first
example to drive a two-stage inference chain through KAI-C — YOLOv8 for
vehicle detection, then the `fast-plate-ocr` adapter for OCR on each
vehicle crop. Ships with watchlist (allowlist / denylist) severity
routing and a Pillow-based cropping helper.

| | |
|---|---|
| Pattern | Drives YOLOv8 + fast-plate-ocr chain → fires alerts |
| Adapters | YOLOv8 + fast-plate-ocr (Apache-2.0, ONNX, CPU-only, plate-specific) |
| Difficulty | ⭐⭐ intermediate |
| Best for learning | Chaining multiple adapters under one correlation ID |
| Tests | 34 |

```bash
cd examples/license-plate-recognition && uv sync --extra dev
cp config.example.yml config.yml      # edit camera URLs + watchlists
python license_plate_recognition.py --config config.yml
```

---

### [`smart-doorbell/`](smart-doorbell)

**Know who's at the door — family, friend, or stranger.** Drives the
InsightFace adapter via KAI-C; classifies each detected face into
family / known / unknown and routes severity accordingly. Unknown-face
alerts include a base64 JPEG snapshot in the envelope, so a short
downstream relay (see `alerts-subscriber/`) can post the photo to
Telegram / ntfy / Discord with the notification. **Pure REST
enrollment** — `smart_doorbell.py enroll --image alice.jpg ...`, no
shared volume, no desktop tool.

| | |
|---|---|
| Pattern | Drives InsightFace → severity-routed alerts |
| Adapters | InsightFace (face detection + recognition + embedding) |
| Difficulty | ⭐⭐ intermediate |
| Best for learning | Per-person dedup, severity routing, REST enrollment flow |
| Tests | 31 |

```bash
cd examples/smart-doorbell && uv sync --extra dev
cp config.example.yml config.yml      # edit doorbell URL + tokens
# Enroll family (REST, no GUI)
python smart_doorbell.py enroll --config config.yml \
  --person-id alice --name "Alice Smith" --image alice.jpg --category family
# Run the daemon
python smart_doorbell.py daemon --config config.yml
```

---

### [`package-delivery/`](package-delivery)

**Alert me when a package arrives — and when it leaves.** YOLOv8 on a porch
ROI with a per-track state machine that distinguishes arrive → (linger) →
disappear. The "package taken by a stranger" path fires with high severity
when no person was seen near the porch at pickup time, so homelab users
aren't woken every time they bring in their own boxes.

| | |
|---|---|
| Pattern | Drives YOLOv8 → IoU tracker → state machine → fires alerts |
| Adapters | YOLOv8 |
| Difficulty | ⭐⭐ intermediate |
| Best for learning | Duration-based predicates, per-track state machines, ROI filtering |
| Tests | 54 |

```bash
cd examples/package-delivery && uv sync --extra dev
cp config.example.yml config.yml      # edit camera URLs + porch ROI
python package_delivery.py --config config.yml
```

---

### [`camera-agent/`](camera-agent)

**Ask your cameras.** A voice agent that listens for spoken questions
in a browser tab, grounds its answers in live camera feeds via tool
calling (BLIP scene caption + YOLOv8 detection + InsightFace
recognition + NATS event history), and replies through Piper TTS.
Pipecat-based pipeline with Silero VAD for natural turn-taking. All
CPU-runnable. The first OpenNVR example where cameras have agency,
not just data.

| | |
|---|---|
| Pattern | WebSocket voice conversation → tool-calling LLM → live camera adapters |
| Adapters | Whisper + Ollama + Piper (voice path) + BLIP + YOLOv8 + InsightFace (tools) |
| Difficulty | ⭐⭐⭐ advanced |
| Best for learning | Pipecat pipelines, OpenAI-style tool calling against local Ollama, custom Pipecat services bridging an adapter contract |
| Tests | 52 |

```bash
cd examples/camera-agent && uv sync --extra dev
cp config.example.yml config.yml      # edit camera URLs, system prompt
python camera_agent.py --config config.yml
# then open http://localhost:9100/demo, click Start, and speak
```

---

### [`home-assistant-relay/`](home-assistant-relay)

**Every OpenNVR alert in your Home Assistant dashboard.** NATS
subscriber that bridges `opennvr.alerts.>` into HA entities via MQTT
discovery (recommended — HA auto-creates the entities on first fire)
or HA's REST API. Built-in mapping rules for every shipped OpenNVR
producer-side example; operators override per source / per camera in
config. Closes the loop: OpenNVR fires alerts → HA dashboards and
automations consume them with zero extra wiring.

| | |
|---|---|
| Pattern | NATS subscriber → MQTT discovery / HA REST → HA entities |
| Adapters | (none — this is a subscriber-only bridge) |
| Difficulty | ⭐⭐ intermediate |
| Best for learning | NATS alert subscription, HA's MQTT discovery contract, two-backend publisher abstraction |
| Tests | 55 |

```bash
cd examples/home-assistant-relay && uv sync --extra dev
cp config.example.yml config.yml      # edit nats_url + mqtt.host / username / password
python home_assistant_relay.py --config config.yml
```

---

## 💡 More on the roadmap

These are explicitly welcome contributions
(see also the [adapter wishlist](https://github.com/open-nvr/ai-adapter#-adapters-wed-love-to-see)):

(Abandoned-object detection and natural-language footage search already
ship — see the gallery above.)

| Category | Idea |
|---|---|
| Safety | Fall detection (pose), fire/smoke detection, PPE compliance (hard hat / vest / mask) |
| Security | Weapon detection, tailgating at access points |
| Analytics | Crowd density, queue length, dwell-time heatmaps, vehicle classification |
| Audio | Glass-break detection, gunshot detection, aggression detection |
| Forensic | Tamper-evident incident export |
| Wildlife | Pet / livestock detection, bird-species ID |

Open a [discussion](https://github.com/open-nvr/open-nvr/discussions) before
you start coding — we'll help scope it and (if it fits the first-party tier)
provide review.

---

## 🛠️ How an example is structured

Every shipped example follows the same layout so you can read one and know
where everything lives in the others:

```
examples/<example-name>/
├── README.md             # problem + screenshots + how to run
├── <example>.py          # main loop + the predicate class + CLI
├── alerts.py             # Alert dataclass + dispatch channels (stdout / webhook / NATS)
├── config.example.yml    # what an operator configures
├── pyproject.toml        # minimal deps (httpx, PyYAML, nats-py, websockets)
└── tests/                # focused, readable-in-5-minutes test suite
```

`alerts.py` and the config-loading shape are deliberately consistent across
all thirteen shipped examples so you can copy one folder, rename `<example>.py`,
and replace the predicate with your domain logic — everything else (alert
routing, correlation IDs, NATS publishing, SIGINT handling) is the template.

---

## 🤝 Contributing your own example

The fastest path to a first-party example slot:

1. Open a [discussion](https://github.com/open-nvr/open-nvr/discussions) with
   your idea, the camera setup you'll demo on, and the adapter(s) you'll
   chain.
2. Scaffold a working app — `python3 scripts/create_opennvr_app.py my-app` —
   or fork and copy one of the thirteen shipped examples as your starting
   template. The generator gives you a runnable, test-green Detector to fill
   in; see **[docs/FIRST_DETECTOR.md](../docs/FIRST_DETECTOR.md)**.
3. Replace the predicate (`zone.contains?`, the dwell-time state machine,
   etc.) with your domain logic.
4. Keep the file layout, the config shape, the alert dispatcher pattern, and
   the test surface roughly the same so future readers see one shape across
   the gallery.
5. Open a PR. We'll review for: clarity, test coverage of the predicate,
   honest documentation of what the example does NOT yet do, and consistency
   with the rest of the gallery.

Examples are first-class community contribution surface. If yours gets in,
your name goes on it.

### Getting it into the App Store

An example is a template; the **App Store index** makes an app browsable and
one-click installable in every deployment's App Catalog. That's a separate,
documented, validated PR flow — build on the App SDK, publish + digest-pin
your image, add one reviewed entry to
[`server/config/apps_index.yml`](../server/config/apps_index.yml), and run
`make validate-apps-index`. Full walkthrough (and the trust model behind the
curated index) in
**[docs/CONTRIBUTING_APPS.md](../docs/CONTRIBUTING_APPS.md)**.
