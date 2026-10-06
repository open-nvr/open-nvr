<div align="center">

<img src=".github/opennvr-logo.svg" alt="OpenNVR" width="300" />

### Your cameras. Your hardware. Your AI. Your audit.

**OpenNVR is open-source video infrastructure for organisations and people who cannot hand their cameras to a vendor.** It records and streams like any NVR, puts any AI model you choose behind the cameras, ships ready-made applications, and lets you write your own — on hardware you own, air-gapped if you need, with every inference audited.

[![CI](https://github.com/open-nvr/open-nvr/actions/workflows/ci.yml/badge.svg)](https://github.com/open-nvr/open-nvr/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.22804254-blue.svg)](https://doi.org/10.5281/zenodo.22804254)
[![Discord](https://img.shields.io/badge/Discord-join_the_community-5865F2?logo=discord&logoColor=white)](https://opennvr.org/discord)

[▶ Install walkthrough (video)](https://www.youtube.com/watch?v=PMC1WWjo654) · [⚡ Get it running](#get-it-running) · [🧩 Write an adapter](https://github.com/open-nvr/ai-adapter#write-your-own-adapter) · [📱 Write an app](docs/FIRST_DETECTOR.md) · [🏛 Government brief](docs/GOVERNMENT_DEPLOYMENT.md) · [🔒 Security](docs/SECURITY_ARCHITECTURE.md) · [💬 Discord](https://opennvr.org/discord)

</div>

---

## Get it running

Two commands. You need [Docker](https://docs.docker.com/get-docker/), nothing else. No account, no API key, no cloud.

```bash
git clone https://github.com/open-nvr/open-nvr.git && cd open-nvr
./start.sh          # Windows: .\start.ps1
```

A short wizard follows; every question has a working default in `[brackets]`, so Enter through all of them gives a running stack. It generates the secrets, pulls the images, and prints your URL and a one-time setup token.

> First run takes 8–15 minutes, almost all of it downloading images. Every start after that is seconds.

Open the URL, accept the self-signed certificate, paste the token, add a camera. Detection overlays appear within about 30 seconds. **No camera to hand?** [OpenNVR Cam](https://play.google.com/store/apps/details?id=org.opennvr.cam) turns an Android phone into an ONVIF camera, or let the agent use the machine's own webcam.

<details>
<summary><b>Day-two commands</b></summary>

| Situation | Command |
|---|---|
| Start now, no prompt | `./start.sh up` |
| Re-print the setup token | `./start.sh token` |
| Change settings or swap the example | `./start.sh reconfigure` |
| Your LAN IP changed | `./start.sh refresh-certs` *(Linux/macOS)* |
| Stop everything | `./start.sh down` |
| Tail live logs | `./start.sh logs` |
| Pick up new images after an upgrade | `docker compose pull && ./start.sh up` |

Unattended installs: `cp .env.example .env && ./scripts/generate-secrets.sh --write && ./start.sh up`. Retention, hardening and every compose file: [`DOCKER_QUICKSTART.md`](DOCKER_QUICKSTART.md).
</details>


## What it looks like

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="data/images/svg/opennvr-screens-dark.svg">
  <img src="data/images/svg/opennvr-screens-light.svg" alt="OpenNVR screens: dashboard, live view with detection boxes, natural-language search, the App Catalog, and the Vehicles (ANPR) page" width="100%">
</picture>

Dashboard · Live view · Search · App Catalog · Vehicles (ANPR). The dark and light variants follow your GitHub theme.

## What OpenNVR is

Three things in one install, each usable on its own.

1. **A complete NVR.** ONVIF and RTSP cameras in, recording and live view out, a detector always on. Works on day one with no AI configured at all.
2. **An AI platform for cameras.** Any model behind a REST or WebSocket endpoint becomes a capability through a published contract. Object detection, plates, faces, pose, scene description, open-vocabulary search, tracking, speech in and out all ship. Thirteen installable applications sit on top: intrusion, loitering, occupancy, line crossing, plates, a doorbell that knows your family, a guard-scan check, a gate controller, an agent you can talk to.
3. **A product you build on.** Two Apache-2.0 SDKs on PyPI. Write a server-side application or an adapter for your own model in an afternoon, run it on your own site, keep it private or publish it. The platform supplies cameras, streams, recording, detections, an event store with evidence, alerts, auth and audit. You supply the rule.

```bash
pip install opennvr-app-sdk        # write an application: your rule on the platform's events
pip install opennvr-adapter-sdk    # write an adapter: your model as a first-class capability
```

## Why organisations that cannot outsource trust run it

Governments, defence, critical infrastructure, healthcare and schools share one constraint: the footage, the models and the decisions must stay inside the perimeter and be explainable afterwards. Most camera software is built the other way round, with the vendor's cloud holding the keys. OpenNVR is built for the constraint.

- **Nothing leaves unless you open the door.** Two independent default-deny gates, `DEPLOYMENT_MODE=offline` and `AI_SOVEREIGNTY=local_only`, return HTTP 403 to every cloud route and every non-local model until an operator opts in. Apps reach the outside world only through an egress proxy on an allowlist an administrator approved.
- **The camera network is isolated by architecture.** Cameras, the middleware gateway and the analytics layer are separate planes. Camera credentials are encrypted at rest. Video leaves the host only over RTSPS.
- **Every inference is auditable.** One correlation id runs from the alert that fired, through the gateway that proxied it, to the model that answered. Model weights are fingerprinted and polled for drift. The audit log answers "why did this alert fire" without guesswork.
- **Procurement can check it.** A §889 covered-vendor self-check is built in. The architecture is a peer-citable paper ([DOI 10.5281/zenodo.22804254](https://doi.org/10.5281/zenodo.22804254)); this repository is its reference implementation.
- **The sensitive part can stay sensitive.** Fine-tuned weights you cannot share, detection logic that is itself operationally sensitive, adapters under a classified licence: the Apache-2.0 SDK lets them run on the platform without ever being published.

The procurement brief is [`docs/GOVERNMENT_DEPLOYMENT.md`](docs/GOVERNMENT_DEPLOYMENT.md); the control-by-control threat model is [`docs/SECURITY_ARCHITECTURE.md`](docs/SECURITY_ARCHITECTURE.md); the evidence pack mapping is [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md).

## Buy the hardware once

AI capability is arriving faster than any hardware refresh cycle. A camera system that needs a new appliance for each new capability will always be a generation behind. In OpenNVR a new capability is software.

- **A new model is an adapter, not a device.** Swap the detector, add plate reading, add a vision-language model, upgrade to this month's weights: each is a container registered with the gateway, hot-swappable and fingerprint-tracked, on the box you already have.
- **The always-on detector is deliberately cheap.** Tier-0 scans the camera's substream at a low frame rate and wakes the expensive models only when something is worth a closer look. That gate is why a mini-PC carries a whole site: the heavy models run on seconds of footage per hour, not all of it.
- **It adapts to the box it finds.** A smaller machine runs the same software with the heavier capabilities switched off; turning one on is a decision, never a surprise.

| Your hardware | What's comfortable |
|---|---|
| Raspberry Pi 4/5, 4 GB | Recording, streaming, Tier-0 detection, zone / line / dwell apps |
| Mini-PC, 8–16 GB | All of the above, plus plates, faces, the text camera agent |
| Desktop or server with an NVIDIA GPU | Everything, plus scene captioning, VQA, the hands-free voice agent |
| Apple Silicon | As above, with the LLM on the host via Metal |

Detection using too much CPU is almost always video decode, not the model. Store the camera's substream URL, check `DETECT_FPS`, attach a hardware decoder with `DETECT_HWACCEL`. The full strategy is [`docs/DETECT_CPU.md`](docs/DETECT_CPU.md).

## Built to scale, built to be built on

**Every part is its own service.** Core, the media server, the message bus, the AI gateway, the detect pipeline, each adapter and each app run as separate containers that talk over NATS and HTTP. Compose is what ships; the service boundaries are what let you run the same images under any orchestrator and add replicas where the load is. Need more plate OCR throughput? Run more OCR adapters; the gateway registers each one and queues fairly per camera.

**Writing your own is the normal path, not the advanced one.**

- `opennvr-app new my-app`, edit one method, run the tests, point it at the stack. [Your first detector in 15 minutes](docs/FIRST_DETECTOR.md).
- An adapter is about thirty lines around your model. [Write your own adapter](https://github.com/open-nvr/ai-adapter#write-your-own-adapter).
- **Publishing is optional.** An app or adapter you write runs on your site with no obligation to list it anywhere. If you do list it in the App Catalog, you keep the copyright, your name is on the card, OpenNVR takes no fee, and you can sell what the code needs through the built-in licence hook. [The deal for developers](docs/DEVELOPER_PROGRAM.md).
- **Fine-tune what ships.** Every reference model is a swap away from your own weights: point the adapter at your fine-tune, or train one and keep it private.

## How it fits together

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="data/images/svg/opennvr-concept-dark.svg">
  <img src="data/images/svg/opennvr-concept-light.svg" alt="Your cameras feed Records (always on, never gated), then Watches (a cheap always-on detector), then Events (one bus everything listens to), then Apps and the assistant. Watches asks KAI-C, the socket, for a task. A model adapter plugs into KAI-C and is checked (weights fingerprint, declared permissions, local-only policy) before it is trusted. The animation unplugs YOLOv8 and plugs in your own model: the AI wire pauses while recording continues, the new adapter is verified, then serving resumes." width="100%">
</picture>

**Records never depends on AI.** Footage is written in one-minute chunks whatever the models are doing. In the animation the AI wire goes quiet while a model is swapped and recording carries on. If every adapter on the box crashes, you still have the video.

**Watches asks for a task, never for a model.** The always-on detector asks KAI-C, the socket, for `object_detection`. Whatever adapter is plugged in answers, and it is checked before it is trusted: weights fingerprint, declared permissions, the local-only policy. Swap YOLOv8 for your own fine-tuned model and nothing else on the wire changes.

**Adapters answer "what is it", apps decide "does it matter".** An adapter reads a plate or recognises a face and says nothing about whether you should care. Apps hold the policy: this zone, these hours, that watchlist. Keeping them apart is what lets you swap a model without rewriting a rule, and write a rule without knowing which model is behind it.

**Everything worth remembering lands on one bus and in one store.** Events is the bus everything listens to. One row per visit, with its best frame and every claim the models made, carrying which model made it and how confident it was. The search page, the agent and any app you install next month all ask the same store, so they give the same answer.

The three-tier model, the wire contracts and the offline-first design are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Add a capability in one click

**App Catalog** in the sidebar lists the installable applications. Pick one, click Install, choose which cameras it watches. Each app declares what it needs before you install it, which cameras, which hosts it may reach, whether it wants a licence key, and an administrator approves that, so an app cannot quietly acquire access it never asked for. Cameras get jobs too: assign camera 1 to plates and cameras 2–3 to counting on each camera's settings page ([how assignments work](docs/CAMERA_ASSIGNMENTS.md)).

| Application | What it does |
|---|---|
| [`intrusion-detection`](examples/intrusion-detection) | People in restricted zones during restricted hours |
| [`loitering-detection`](examples/loitering-detection) | Dwell-time alerts on a live inference stream |
| [`occupancy-counting`](examples/occupancy-counting) | Zone occupancy with over/under alerts |
| [`line-crossing`](examples/line-crossing) | Directional tripwire and entry/exit counting |
| [`abandoned-object`](examples/abandoned-object) | Unattended items, with owner-proximity suppression |
| [`license-plate-recognition`](examples/license-plate-recognition) | Plate reads, registers, watchlists, per-camera scan modes |
| [`gate-controller`](examples/gate-controller) | Open the barrier for allowed vehicles only |
| [`smart-doorbell`](examples/smart-doorbell) | Face recognition with enrolment, strangers alerted with a photo |
| [`package-delivery`](examples/package-delivery) | Arrival, linger and pickup per parcel |
| [`guard-scan-compliance`](examples/guard-scan-compliance) | Was every person wanded, and what did the scanner find |
| [`footage-search`](examples/footage-search) | "Red truck at the dock yesterday" over recorded footage |
| [`alert-notifier`](examples/alert-notifier) | What actually deserves to reach the guard's phone |
| [`camera-agent`](examples/camera-agent) | Ask your cameras questions, by voice or text, all local |
| [`home-assistant-relay`](examples/home-assistant-relay) | *Deprecated*: Home Assistant support is [built in](docs/HOME_ASSISTANT_USER_GUIDE.md); kept as a small NATS-to-MQTT bridge example |

Fourteen of the sixteen shipped examples are listed above; [`inference-listener`](examples/inference-listener) and [`alerts-subscriber`](examples/alerts-subscriber) are minimal subscriber templates. Every app is a copy-as-template starting point. Replace the predicate and you have a purpose-built NVR for your domain. Gallery and roadmap: [`examples/README.md`](examples/README.md), [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Talk to your cameras

<a href="https://opennvr.org/camera-agent">
  <img src="data/images/svg/opennvr-agent.svg" alt="The OpenNVR agent watches two cameras: a user asks about a plate, the agent searches history and answers; a monitored plate passes the road camera, the alarm automation fires and the agent speaks the alert" width="100%">
</a>

```bash
examples/camera-agent/quickstart.sh          # voice: click Start and speak
examples/camera-agent/quickstart.sh --chat   # chat: type and read
```

Open <http://localhost:9100/demo>. A local LLM does tool-calling over your live frames: a detector for "is anyone in the kitchen", a captioner for "what's at the back gate", the event store for "did a red truck come by the dock earlier", your face database for "who was at the door this morning". The brain is a dial, not a dependency: the bundled runtime, Ollama on the host where the GPU is, or any OpenAI-compatible endpoint you bring. Model picks and hardware notes: [`examples/camera-agent/README.md`](examples/camera-agent/README.md).

## Build on it

**An application** subscribes to the platform's events and holds one rule:

```bash
pip install opennvr-app-sdk && opennvr-app new my-app   # scaffold, edit on_detections, uv run pytest
```

**An adapter** puts your model behind the contract:

```python
from opennvr_adapter_sdk import AdapterApp, AdapterService, BodyShape, BODY_BYTES_KEY, InferResponse

class MyDetector(AdapterService):
    def load(self): ...                       # load your weights
    def is_ready(self) -> bool: return True
    def infer(self, payload) -> InferResponse:
        frame = payload[BODY_BYTES_KEY]       # ... your model ...
        return InferResponse(result={"detections": [...]})

app = AdapterApp(service=MyDetector(), name="my-detector", version="1.0.0",
                 vendor="me", license="MIT", tasks_advertised=["object_detection"],
                 body_shape=BodyShape.IMAGE).fastapi_app
```

Register the URL with the gateway and it is online: hot-swappable, audit-chained, fingerprint-tracked. Thirteen reference adapters and a one-command scaffold live in [ai-adapter](https://github.com/open-nvr/ai-adapter); the wire spec is [`docs/AI_ADAPTER_CONTRACT.md`](docs/AI_ADAPTER_CONTRACT.md).

## Tell us the problem your cameras should solve

A camera system should make a site safer, more reliable and more productive, and it should be able to show its work. Most of what OpenNVR ships started as one organisation's specific problem: a jewellery showroom that had to prove every visitor was wanded, a society that wanted strangers' vehicles flagged and residents' waved through, a dock that wanted to search footage in plain words.

If you have a camera-based use case, describe it to us. We will tell you whether an existing app and adapter already solve it, whether a fine-tune of a shipped model gets you there, or whether it needs new work, and what that costs. Deployment assistance, adapter authoring under NDA, fine-tuning on your footage, compliance evidence packs and supported deployments are how the project is funded: [contact@opennvr.org](mailto:contact@opennvr.org). The enterprise offer is in [`docs/ENTERPRISE.md`](docs/ENTERPRISE.md).

## How it compares

Frigate is the right call for many homelabs, and [we say so](docs/COMPARISONS.md). OpenNVR solves a different problem: auditable AI surveillance with operator-controlled, sovereign AI.

| | **OpenNVR** | **Frigate** | **ZoneMinder** | **Verkada** |
|---|:---:|:---:|:---:|:---:|
| Self-hosted, runs air-gapped | ✅ | ✅ | ✅ | ❌ |
| Open adapter contract: any model, any licence, out of tree | ✅ | in-tree | ❌ | ❌ |
| Talk to your cameras, locally | ✅ | ❌ | ❌ | ❌ |
| End-to-end audit chain and model-drift detection | ✅ | ❌ | ❌ | ❌ |
| Default-deny sovereignty gates | ✅ | partial | ❌ | ❌ |
| §889 covered-vendor self-check | ✅ | ❌ | ❌ | ❌ |

## Community

[Join the Discord](https://opennvr.org/discord) for questions and to show what you built. Bugs go in [Issues](https://github.com/open-nvr/open-nvr/issues), design questions in [Discussions](https://github.com/open-nvr/open-nvr/discussions), security reports via a [private advisory](https://github.com/open-nvr/open-nvr/security/advisories/new). The most valuable help, in order: build an app or adapter and tell us · follow the [open-nvr organisation](https://github.com/open-nvr) · star the repository. Issues labelled [good first issue](https://github.com/open-nvr/open-nvr/labels/good%20first%20issue) are finishable in an evening.

## Documentation

**Getting started** — [Docker quickstart](DOCKER_QUICKSTART.md) · [User manual](USER_MANUAL.md) · [Camera assignments](docs/CAMERA_ASSIGNMENTS.md) · [Enrichment and search](docs/ENRICHMENT.md) · [Local dev setup](docs/LOCAL_SETUP.md) · [Use cases by industry](docs/USE_CASES.md) · [Home Assistant](docs/HOME_ASSISTANT_USER_GUIDE.md)

**Architecture and security** — [Security architecture](docs/SECURITY_ARCHITECTURE.md) · [Compliance mapping](docs/COMPLIANCE.md) · [Government deployment brief](docs/GOVERNMENT_DEPLOYMENT.md) · [Enterprise](docs/ENTERPRISE.md) · [Reference appliance](docs/REFERENCE_APPLIANCE.md) · [AI Adapter Contract](docs/AI_ADAPTER_CONTRACT.md) · [Edge autonomy and robotics](docs/EDGE_AUTONOMY.md) · [The paper](https://doi.org/10.5281/zenodo.22804254)

**Project** — [Roadmap](docs/ROADMAP.md) · [Support](docs/SUPPORT.md) · [Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md)

## License and trademark

OpenNVR is dual-licensed. The platform core is **AGPL-3.0-or-later**: free forever, on any hardware, for anyone who honours the AGPL including its network clause. The developer edges, the [app SDK](sdk/opennvr-app-sdk) and the [adapter SDK](https://github.com/open-nvr/ai-adapter/tree/main/opennvr_adapter_sdk), are **Apache-2.0**, so what you write can ship under any licence, including proprietary or classified. The **OpenNVR Commercial License** covers what the AGPL does not: pre-installed hardware under your brand, embedding in proprietary software, hosted offerings without source disclosure, white-labelling. Policy, decision matrix and FAQ: [`docs/LICENSING.md`](docs/LICENSING.md). Core contributions require a [CLA](docs/CLA.md); SDK contributions need a DCO sign-off.

"OpenNVR" and the logo are trademarks of the project; see [`TRADEMARK.md`](TRADEMARK.md).

---

<div align="center">

**Cameras you connect. Hardware you own. AI you choose and author. Audit you can show.**

[⭐ Star on GitHub](https://github.com/open-nvr/open-nvr) · [📄 Read the paper](https://doi.org/10.5281/zenodo.22804254) · [⚡ Get it running](#get-it-running)

</div>
