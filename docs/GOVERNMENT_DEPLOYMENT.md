# OpenNVR for Government & Public-Sector Deployments

*A printable one-pager for IT decision-makers, security officers, and
compliance leads.*

## The problem

Many IP cameras currently deployed in government, defense, healthcare,
education, and critical-infrastructure environments fall into one of three
categories:

1. **FCC Covered List vendors.** Hikvision, Dahua, and certain Hytera /
   Hangzhou Hikvision Digital Technology equipment are restricted for
   public-safety, government-facility, and critical-infrastructure
   surveillance use under the Secure Networks Act. ([FCC Covered List](https://www.fcc.gov/supplychain/coveredlist))
2. **End-of-life or unmaintained.** Devices running firmware that stopped
   receiving updates ~3 years after release, with unpatched high-severity
   CVEs still active in production networks.
3. **Vendor-cloud-bound.** Cameras whose live feeds, recordings, or AI
   analytics flow through a vendor-managed cloud aggregation layer — the
   architecture that produced the 2021 Verkada breach, exposing ~150,000
   cameras across hospitals, schools, and enterprises in a single
   credential compromise.

Procurement officers need a path that mitigates exposure **without ripping
out the camera fleet itself.**

## The substitution

OpenNVR is a self-hosted middleware layer that sits between your cameras
and everything downstream. It:

- **Works with any ONVIF or RTSP camera** you already own. No vendor lock-in.
- **Re-streams over TLS** through a hardened software gateway you control
  (MediaMTX with RTSPS, HLS-TLS, WebRTC-TLS by default).
- **Records, plays back, and serves the web UI** entirely on your hardware.
  No cloud egress unless you explicitly opt in — and that opt-in lands in
  an audit log.
- **Runs AI workloads locally.** Object detection, license-plate OCR, face
  recognition, scene captioning, multi-object tracking, voice query — all
  via the open AI Adapter Contract v1, all on your hardware, all
  customer-controlled.

The camera vendor's flaws stop at the camera. Everything past that point is
software you own, audit, and patch on your own schedule.

## Why it's defensible

OpenNVR's architecture is described in a **peer-citable published paper**:

> *Eliminating Systemic IP Camera Vulnerabilities via Offline-First Open
> Security Architecture* — Singh, Bhandari, Singh, Kushwaha, Kaura (2025).
> [DOI 10.5281/zenodo.17261761](https://doi.org/10.5281/zenodo.17261761)

The paper synthesizes 34 authoritative sources — CISA advisories
(AVTECH, Edimax), NVD CVE records (Hikvision CVE-2021-36260, Dahua
CVE-2022-30563, Uniview CVE-2023-0773, Edimax CVE-2025-1316, ThroughTek
Kalay SDK CVE-2021-28372, iLnkP2P CVE-2019-11219/11220), the 2021 Verkada
breach, the Mirai / Persirai botnet campaigns, and academic measurement
studies — into a six-category framing of systemic IP-camera weaknesses
and a three-tier offline-first architecture that structurally eliminates
each. OpenNVR is that architecture's open-source reference
implementation. Every paper § maps to OpenNVR code; the mapping is at
[`docs/COMPLIANCE.md`](COMPLIANCE.md).

## What it aligns with

OpenNVR's architecture maps cleanly onto the major frameworks compliance officers ask about: CISA Secure-by-Design (secure defaults, customer-managed cryptography, minimised attack surface); NIST Cybersecurity Framework 2.0 across Identify / Protect / Detect / Respond / Recover; NIST AI Risk Management Framework 1.0 (AI sovereignty enforcement at the adapter-contract layer); ISO/IEC 27001:2022 (certificate-based auth, RBAC, append-only audit log as ISMS evidence); ETSI EN 303 645 for the consumer-IoT baseline (no default passwords, secure update path, encrypted communications); and the data-protection regimes (EU GDPR and India DPDP Act 2023 — customer-owned keys, operator-controlled retention, no vendor-cloud egress).

The framework-by-framework evidence trail with paper-section citations and the OpenNVR control mapping is in [`docs/COMPLIANCE.md`](COMPLIANCE.md). For ISO 27001 or SOC 2 evidence packs, that page's audit-chain quick reference maps each auditor question to its answer in the OpenNVR audit log.

## Operational sovereignty: your AI, your tactics, your hardware

Camera security is half the story. The other half is **what runs on the
video.** For defence, critical-infrastructure, and government operators,
the AI analytics layer is where tactical doctrine lives — what you watch
for, how you weight signals, what triggers escalation, what an anomaly
looks like in *your* environment. That doctrine is operationally
sensitive. It is not something you want sitting in a vendor's cloud, on
a vendor's roadmap, or visible through a vendor's support team.

OpenNVR's adapter contract is the mechanism that puts the AI layer
under operator control the same way the recording and transport layers
already are. Every analytic capability — detection, recognition,
tracking, OCR, scene understanding, audio events, anomaly scoring — is
a contract-compliant container you run on your hardware. Bring a model
you've fine-tuned on your own deployment data. Bring a model you cannot
share with a vendor for classification reasons. Bring a model whose
inference behaviour you cannot disclose to anyone who didn't sign your
NDA. The contract is the interface; what's behind it is yours.

This matters concretely. On defence and military bases it shows up as perimeter-intrusion classifiers trained on your specific terrain and threat signatures, asset-tracking models tied to your inventory, and behaviour-anomaly detectors weighted for your operational tempo — none of which can leave the base. In critical infrastructure (power, water, telecom) it looks like equipment-tamper detectors trained on your specific cabinet and substation imagery, drone-detection models tuned to your local airspace baseline, and fence-line and yard-monitoring analytics that escalate to your SCADA stack rather than a vendor's. In government facilities it's visitor-flow analytics with retention policies set by your records office (not a vendor's TOS), tailgating and access-deviation detection bound to your badge system, and package screening with item lists that change weekly without waiting on a vendor product cycle.

In healthcare it's fall and patient-deterioration models that respect HIPAA boundaries because they never leave the host, plus restricted-area logic with site-specific rules (medication rooms, NICU corridors, behavioural-health units) that no vendor product covers. In education it's weapon-detection models you can re-train as adversaries adapt, after-hours intrusion with school-specific schedules, and behavioural alerting with rules your safety committee — not a vendor — defines. In industrial and OT environments it's PPE compliance against site-specific rules, hazardous-zone entry detection tied to your lockout-tagout system, and equipment-behaviour anomaly models trained on your normal operational baseline.

What this transforms, in plain terms: tactical doctrine that today lives
in human-staffed control rooms, in standard operating procedures, and in
officer training becomes deployable AI that runs on every camera, 24×7,
under your roof. It evolves as your threat model evolves — not as a
vendor's roadmap permits. Iteration is days, not vendor-product
quarters, because the platform is yours.

**Why the contract matters, not just the models we ship today.** OpenNVR
ships YOLOv8, InsightFace, Whisper, Piper, fast-plate-ocr, BLIP, and
ByteTrack out of the box — useful demonstrations of the contract, not
the limit of it. The contract is what lets a uniformed-services
engineering team, a national lab, or a contracted SI add capabilities
under whatever licensing arrangement your programme requires.
Apache-2.0 SDK so your adapter can ship under any licence you choose,
including proprietary or classified. ~30 lines of Python per adapter
plus your model. Conformance test suite that proves the adapter will
register cleanly with KAI-C. Template scaffold (`templates/adapter-template/`
in the [ai-adapter repo](https://github.com/open-nvr/ai-adapter)) that
generates a working adapter directory from a single command.

The combination — camera-layer offline-first isolation, middleware that
you patch on your own cadence, AI capabilities you author and run
locally, audit chain that proves none of it touched a vendor cloud — is
what the paper means by "data and AI sovereignty." It is the difference
between buying a surveillance product and operating a surveillance
*capability*.

## What it does

- **Records:** every camera, configurable retention per camera, encrypted
  at rest under your `CREDENTIAL_ENCRYPTION_KEY`.
- **Plays back:** web UI at `https://your-host:8000`, timeline-indexed
  segments, MP4 export.
- **Detects:** YOLOv8 object detection out of the box; plug in any
  detector that speaks the AI Adapter Contract.
- **Recognizes:** InsightFace face recognition with operator-managed face DB
  (REST enrollment, no shared volume).
- **Reads plates:** fast-plate-ocr adapter for license-plate recognition.
- **Captions scenes:** BLIP scene-caption adapter (semantic context for
  audit log entries).
- **Tracks:** ByteTrack multi-object tracking with persistent IDs across
  frames.
- **Listens and speaks:** Whisper STT + Piper TTS + Ollama LLM = voice
  agent (`/demo` page). Ask out loud, "is there a person at the front
  door?" — get an answer grounded in a live frame.
- **Audits:** end-to-end correlation ID joining every alert to the model
  inference, the model weights' sha256, the operator who provisioned the
  camera, and the transport policy in effect.

## What it costs

The software is AGPLv3 — no per-camera licence fees, no per-seat fees, no cloud subscription. The hardware is commodity x86, anywhere from Raspberry Pi-class up to enterprise servers, so existing equipment is typically reusable. Storage scales linearly with the retention you configure: 1080p H.264 at 24×7 is roughly 25 GB per camera per week, much less with motion-triggered recording. Commercial support and indemnification are available through **[contact@opennvr.org](mailto:contact@opennvr.org)** and cover deployment assistance, custom-adapter authoring, compliance evidence packs, and SLA-backed incident response for regulated environments.

## What it doesn't do

A few things are honestly outside the scope of what OpenNVR delivers (paper §8). It doesn't replace the cameras themselves — you keep your existing fleet, and OpenNVR reduces what the camera vendor can compromise by isolating it behind middleware you control, but device-level firmware CVEs remain the vendor's to patch. It doesn't provide hardware tamper detection; locked racks, port security, and tamper alarms are operator controls outside the architecture. It doesn't guarantee against insider threats — a malicious actor with physical access to the camera VLAN could still exploit unpatched device flaws, and architectural isolation reduces but doesn't eliminate the attack surface. And it doesn't defend against hardware supply-chain implants in the cameras themselves; undocumented SoC backdoors would bypass network isolation entirely, and while no widespread evidence exists in commercial cameras today, the paper acknowledges this as a theoretical residual risk (§8.3).

These limitations are part of the architecture's defensibility, not against it — they're documented, scoped, and verifiable.

## Getting started

Pilot deployment, single host:

```bash
git clone https://github.com/open-nvr/open-nvr.git && cd open-nvr
cp .env.example .env
./scripts/generate-secrets.sh --write
docker compose -f docker-compose.yml up -d
```

Five minutes later, browse to `https://localhost:8000` and add your
first camera. Pre-built container images are pulled from `ghcr.io/open-nvr/`
— no source build required.

For a production deployment: [Docker Quickstart](../DOCKER_QUICKSTART.md)
covers the production-hardening checklist (reverse proxy with real TLS
certs, retention sizing, backup strategy, audit-log forwarding). For
custom AI adapters: [`ai-adapter` repo](https://github.com/open-nvr/ai-adapter)
with the SDK + template scaffold.

## Contact

Technical questions go in [GitHub Discussions](https://github.com/open-nvr/open-nvr/discussions). Security disclosures go through [private GHSA reporting](https://github.com/open-nvr/open-nvr/security/advisories) or `contact@opennvr.org`. For procurement, commercial licensing, and SLA-backed support, write to **[contact@opennvr.org](mailto:contact@opennvr.org)**.
