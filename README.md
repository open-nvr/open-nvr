<div align="center">

<img src=".github/opennvr-logo.svg" alt="OpenNVR" width="300" />

### Cameras are everywhere. Almost none of them are yours.

OpenNVR is the self-hosted network video recorder for everyone who'd rather not give their camera footage — or the AI that watches it — to a vendor's cloud.

[![CI](https://github.com/open-nvr/open-nvr/actions/workflows/ci.yml/badge.svg)](https://github.com/open-nvr/open-nvr/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.17261761-blue.svg)](https://doi.org/10.5281/zenodo.17261761)

[Quickstart](#quickstart) · [Talk to your cameras](#talk-to-your-cameras) · [Build on it](#build-on-it) · [Read the paper](https://doi.org/10.5281/zenodo.17261761)

</div>

---

## Why this exists

In 2016, a botnet called Mirai conscripted hundreds of thousands of IP cameras into the largest DDoS attack the internet had ever seen. In 2021, an attacker compromised cloud credentials at Verkada and took live feeds from around 150,000 cameras across hospitals, schools, prisons, and factories. Federal advisories continue to land against major vendors — Hikvision, Dahua, Uniview, Edimax — whose firmware quietly powers critical infrastructure around the world.

The pattern keeps repeating because the architecture is wrong. Cameras are connected to vendor clouds. The vendor holds the keys. The vendor controls the AI. The vendor's breach is your breach. A decade after Mirai, the industry has not fixed itself.

OpenNVR is the bet that the alternative is open-source surveillance infrastructure built around four commitments: **cameras you connect, hardware you own, AI you author, audit logs you can show to a regulator.**

The architecture is published — a peer-citable paper this year, 34 references, three-tier offline-first model, six categories of systemic IP-camera weakness it structurally eliminates ([DOI 10.5281/zenodo.17261761](https://doi.org/10.5281/zenodo.17261761)). This repo is the reference implementation.

## What makes it different

**It's secure by design.** Network isolation between the camera plane, the middleware gateway, and the analytics layer is the architecture, not a configuration toggle. Credentials are encrypted at rest with Fernet, RTSP travels over RTSPS to anything outside the host, and two independent default-deny gates — `DEPLOYMENT_MODE=offline` and `AI_SOVEREIGNTY=local_only` — keep cloud routes and AI egress returning HTTP 403 until an operator explicitly opens them. The systemic IP-camera weaknesses the paper documents — default credentials, hard-coded keys, unsigned firmware updates, exposed management interfaces, vendor-controlled cloud aggregation, opaque telemetry — are structurally eliminated rather than mitigated case by case. Threat model and control mapping in [`docs/SECURITY_ARCHITECTURE.md`](docs/SECURITY_ARCHITECTURE.md).

**It's auditable.** Every inference threads a correlation ID from the alert that fired, through the middleware that proxied it, to the model that made the call. Model weights are fingerprinted with sha256 and polled for drift. Cloud routes return HTTP 403 by default. The audit log answers *"why did this alert fire?"* without guesswork. Procurement-grade evidence in [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md).

**Its AI layer is open.** Any model behind a REST or WebSocket endpoint becomes a first-class capability through the AI Adapter Contract — a published wire spec. Object detection, license-plate OCR, face recognition, scene captioning, multi-object tracking, ASR, TTS, LLM tool-calling all ship out of the box. The SDK to write your own is Apache-2.0 and runs around thirty lines of Python.

**You can talk to it.** The included camera-agent is a voice loop — you ask out loud *"is there a person at the front door?"* and a local LLM answers grounded in a live frame from your camera, spoken back through Piper TTS. No cloud, no API keys.

**It's built for sovereignty.** For homelab users that means the doorbell that doesn't phone home. For defence, critical infrastructure, healthcare, and government deployments it means tactical AI that runs on your hardware under your control — models you've fine-tuned, models you can't share with a vendor, analytics whose detection logic itself is operationally sensitive. The procurement brief is in [`docs/GOVERNMENT_DEPLOYMENT.md`](docs/GOVERNMENT_DEPLOYMENT.md).

## Quickstart

Five minutes from `git clone` to YOLOv8 object detection running on your camera feed. Pre-built images on GHCR — no source build.

```bash
git clone https://github.com/open-nvr/open-nvr.git
cd open-nvr
cp .env.example .env
./scripts/generate-secrets.sh --write
docker compose -f docker-compose.tier0.yml up -d
```

`./start.sh up` prints the URLs you should visit. Open <https://localhost/> from the host itself, or <https://&lt;server-ip&gt;/> from a phone/laptop on the same LAN — the script auto-detects your LAN IP and generates a self-signed certificate that includes it. Your browser will warn about the self-signed CA once; click **Advanced → Accept the risk and continue**. The cert and key live in `./nginx-certs/` on the host and never leave the machine.

Live streams (WebRTC, HLS) and recording playback work from any device on your LAN. If your IP changes (DHCP renewal, moved boxes), run `./start.sh refresh-certs` to regenerate the cert with the new IP.

Grab the one-time setup token from the start-script output (or `docker compose logs opennvr-core | grep -A 6 'first-time setup token'`), set an admin password, add a camera. Detection overlays appear within thirty seconds of the camera connecting.

Full install, retention, production hardening: [`DOCKER_QUICKSTART.md`](DOCKER_QUICKSTART.md).

## Talk to your cameras

Layer the camera-agent on top of Tier 0:

```bash
docker compose -f docker-compose.tier0.yml \
               -f docker-compose.camera-agent.yml \
               --profile camera-agent run --rm ollama-model-pull
docker compose -f docker-compose.tier0.yml \
               -f docker-compose.camera-agent.yml \
               --profile camera-agent up -d
```

Open <http://localhost:9100/demo>, click Start, speak.

Underneath: a Pipecat-based pipeline with Silero VAD for turn detection, Whisper for STT, an Ollama-hosted LLM with OpenAI-style tool calling, Piper TTS for the spoken reply. The LLM has four tools registered — BLIP scene caption, YOLOv8 detection, InsightFace recognition, and the recent-events NATS feed — each reaching into the live camera frame to ground the answer.

Ask *"what's at the back gate?"* and the LLM calls BLIP. Ask *"is anyone in the kitchen?"* and it calls YOLOv8. Ask *"did anyone walk past in the last ten minutes?"* and it queries the inference event ring. All on your hardware. No cloud calls. No API keys.

This is the first OpenNVR example where the cameras have agency, not just data.

## Build on it

The AI Adapter Contract is what makes OpenNVR a platform, not a product. Any model behind a REST or WebSocket endpoint can become a first-class capability:

```python
from opennvr_adapter_sdk import (
    AdapterApp, AdapterService, BodyShape, BODY_BYTES_KEY,
    HardwareEvaluationResponse, HardwareVerdict,
    InferResponse, ModelInfo,
)

class MyDetector(AdapterService):
    def load(self):
        # eagerly load your weights
        ...

    def is_ready(self) -> bool:
        return True

    def fingerprint(self) -> str | None:
        return "sha256:..."

    def model_info(self) -> ModelInfo:
        return ModelInfo(
            name="my-model", version="1.0.0",
            framework="onnx", fingerprint=self.fingerprint(),
        )

    def hardware_evaluation(self) -> HardwareEvaluationResponse:
        return HardwareEvaluationResponse(verdict=HardwareVerdict.OK, ...)

    def infer(self, payload) -> InferResponse:
        frame_bytes = payload[BODY_BYTES_KEY]
        # ... your model ...
        return InferResponse(result={"detections": [...]})

app = AdapterApp(
    service=MyDetector(),
    name="my-detector", version="1.0.0", vendor="me", license="MIT",
    tasks_advertised=["object_detection"],
    body_shape=BodyShape.IMAGE,
).fastapi_app
```

`uvicorn my_module:app --port 9100`, register the URL with KAI-C, and your adapter is online — hot-swappable, audit-chained, fingerprint-tracked. The SDK is Apache-2.0 so your adapter can ship under any compatible license, including proprietary or classified.

Things developers are building today:

- **Tracker-stable alert deduplication** for warehouses ("don't fire 'person detected' sixty times for the same forklift driver walking past").
- **Pose-based fall detection** for memory-care facilities, with rules a HIPAA-bound auditor signed off on.
- **Semantic search across recorded footage** — "find clips with a red truck at night" — using a CLIP embedding adapter.
- **Site-specific PPE compliance** for construction with the false-positive threshold tuned to what the insurer will accept.
- **Drone-detection** on perimeter cameras, with the classifier weights kept off the vendor cloud.
- **Domain-specific NVRs** — cannabis dispensary compliance, school weapons detection, port cargo logging — built by forking an example and replacing the predicate.

Seven reference adapters and a one-command scaffold to start your own live in the sibling [ai-adapter](https://github.com/open-nvr/ai-adapter) repo. Full authoring walkthrough in the [SDK README](https://github.com/open-nvr/ai-adapter/blob/main/opennvr_adapter_sdk/README.md).

## What ships out of the box

| Example | What you'll build | Difficulty |
|---|---|---|
| [`intrusion-detection`](examples/intrusion-detection) | People in restricted zones during restricted hours | beginner |
| [`loitering-detection`](examples/loitering-detection) | Dwell-time state machine on a NATS inference stream | intermediate |
| [`license-plate-recognition`](examples/license-plate-recognition) | YOLOv8 + fast-plate-ocr chain with allowlists | intermediate |
| [`smart-doorbell`](examples/smart-doorbell) | InsightFace recognition with REST enrollment | intermediate |
| [`package-delivery`](examples/package-delivery) | Per-track state machine for arrival, linger, pickup | intermediate |
| [`camera-agent`](examples/camera-agent) | The voice agent above | advanced |
| [`home-assistant-relay`](examples/home-assistant-relay) | Bridge alerts into Home Assistant via MQTT discovery | intermediate |

Each example is a copy-as-template starting point. Replace the predicate — the zone check, the dwell timer, the plate watchlist — with your domain logic and you have a domain-specific NVR. Gallery walkthrough and the "drives inference vs subscribes to events" axis-grid in [`examples/README.md`](examples/README.md).

## Community

Bugs go in [Issues](https://github.com/open-nvr/open-nvr/issues), design questions in [Discussions](https://github.com/open-nvr/open-nvr/discussions), security reports via [private GHSA advisory](https://github.com/open-nvr/open-nvr/security/advisories/new). PR flow is in [`CONTRIBUTING.md`](CONTRIBUTING.md); the [roadmap](docs/ROADMAP.md) names where help is wanted next.

Commercial deployments — deployment assistance, NDA adapter authoring, compliance evidence packs, SLA-backed support — [contact@cryptovoip.in](mailto:contact@cryptovoip.in).

## Documentation

**Getting started** — [Docker quickstart](DOCKER_QUICKSTART.md) · [User manual](USER_MANUAL.md) · [Local dev setup](docs/LOCAL_SETUP.md) · [Use cases by industry](docs/USE_CASES.md) · [Comparisons](docs/COMPARISONS.md)

**Architecture & security** — [Security architecture](docs/SECURITY_ARCHITECTURE.md) · [Compliance mapping](docs/COMPLIANCE.md) · [Government deployment brief](docs/GOVERNMENT_DEPLOYMENT.md) · [AI Adapter Contract](docs/AI_ADAPTER_CONTRACT.md)

**Project** — [Roadmap](docs/ROADMAP.md) · [Support](docs/SUPPORT.md) · [Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md)

## License

OpenNVR is **AGPLv3**. The [adapter SDK](https://github.com/open-nvr/ai-adapter/tree/main/opennvr_adapter_sdk) is **Apache-2.0**, so adapters you write can ship under any compatible license — including proprietary or classified for the organisations where that matters.

---

<div align="center">

**OpenNVR — cameras you connect, hardware you own, AI you author, audit you can show.**

[⭐ Star on GitHub](https://github.com/open-nvr/open-nvr) · [📄 Read the paper](https://doi.org/10.5281/zenodo.17261761) · [⚡ Quickstart](#quickstart)

</div>
