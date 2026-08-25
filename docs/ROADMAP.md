# Public Roadmap

This page lives because two audiences need it: procurement evaluators
who can't sign anything without seeing where the project is going, and
contributors who want to know where help is wanted. We try to be honest
about what's shipped, what's in progress, and what's speculative — if
something below changes, the changelog will record the rationale.

The cadence we target is one minor release per quarter (v0.1 → v0.2 →
v0.3 across roughly nine months), with patch releases as needed in
between. Real timing depends on contributor capacity; this isn't a
commercial commitment.

## v0.1 — shipped

The current release. Everything below is in main and runnable today.

### Architecture & security
- Three-tier offline-first deployment model (isolated camera network →
  middleware gateway → analytics) — the [published paper's](https://doi.org/10.5281/zenodo.17261761)
  reference implementation.
- Strong-secret validator refuses to boot on placeholder credentials.
  One-time setup token for first-boot admin provisioning.
- RTSPS / HLS-TLS / WebRTC-TLS on every operator-facing transport.
  Plaintext loopback for the in-host KAI-C inference tap (`docs/SECURITY_ARCHITECTURE.md`
  §"RTSP encryption posture").
- End-to-end `X-Correlation-Id` propagation from alert → middleware →
  adapter.
- Model-fingerprint sha256 polled every 60s; drift surfaces as
  `adapter.fingerprint_mismatch` audit events.
- Append-only audit log with public NATS subject scheme for SIEM /
  custom dashboards.
- Two independent default-deny gates for anything outbound:
  `DEPLOYMENT_MODE=offline` (default) makes cloud routes return HTTP
  403; `AI_SOVEREIGNTY=local_only` (default) refuses AI adapters
  declaring `network_egress`. Both flips are audit-logged.

### AI capabilities (shipped adapters)
- YOLOv8 object detection (ONNX, CPU + GPU).
- InsightFace face detection + recognition with REST-based face DB.
- Whisper ASR (faster-whisper, CPU + GPU).
- Piper TTS with inline audio response option.
- fast-plate-ocr license-plate recognition.
- BLIP scene-caption.
- ByteTrack multi-object tracking (post-processor; first non-detection
  adapter shipping).
- Ollama LLM integration with OpenAI-style tool-calling.

### Example apps
- `intrusion-detection` — zone + schedule predicate over detection
  events.
- `loitering-detection` — dwell-time state machine on NATS subscriber.
- `inference-listener` — minimal NATS subscriber template.
- `alerts-subscriber` — fan-out alerts to webhooks / SIEM / chat.
- `license-plate-recognition` — two-stage YOLOv8 + fast-plate-ocr
  chain with allow / deny watchlists.
- `smart-doorbell` — InsightFace recognition with severity routing,
  REST-based enrollment.
- `package-delivery` — per-track state machine for arrival / linger /
  pickup with porch-pirate severity.
- `camera-agent` — voice loop over Pipecat + Whisper + Ollama + Piper,
  with BLIP / YOLOv8 / InsightFace as grounded tools.
- `home-assistant-relay` — MQTT-discovery bridge for HA dashboards.

### Developer surface
- `opennvr-adapter-sdk` published from source (PyPI release wires off
  the first `sdk-v*` tag).
- Adapter template scaffold (`./templates/adapter-template/scaffold.sh`
  in the ai-adapter repo) for one-command new-adapter authoring.
- Conformance test suite (`python -m conformance ...`) that proves an
  adapter will register cleanly with KAI-C.
- Pre-built container images on GHCR for the core + all seven shipped
  adapters. standard stack install is `docker compose up`, no source build.

### Documentation
- [README](../README.md) — three-audience hero (homelab / procurement /
  defence) + 5-minute install.
- [SECURITY.md](../SECURITY.md) — disclosure timeline + operator
  checklist.
- [SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md) — full V-* control
  inventory mapped to paper sections.
- [COMPLIANCE.md](COMPLIANCE.md) — paper § → control → code mapping
  plus framework alignment table.
- [GOVERNMENT_DEPLOYMENT.md](GOVERNMENT_DEPLOYMENT.md) — procurement
  one-pager with the operational-sovereignty section.
- [USE_CASES.md](USE_CASES.md) — per-industry fit guide.
- [COMPARISONS.md](COMPARISONS.md) — head-to-head with Frigate /
  ZoneMinder / Shinobi / Viseron / Verkada.

## v0.1.x — patch releases

Already-identified follow-ups that are scoped, but not blockers for
v0.1.0 to ship:

- **Camera-agent overlay polish** — currently `build:` from source.
  Publish `ghcr.io/open-nvr/camera-agent` and a per-adapter Ollama
  wrapper so the camera-agent install is a pure `docker compose pull`.
- **URL-shape validator** for `MEDIAMTX_RTSP_URL` (reject userinfo or
  trailing paths that would produce malformed inference-tap URLs).
- **Test-environment coverage gap** for the ByteTrack adapter — add
  `supervision` to a `[tracking]` extra in `pyproject.toml` so unit
  tests run in CI, not just the Dockerfile-based smoke matrix.
- **README scaffold-artifact note** — `demo_stub` / `review_stub`
  cleanup instruction added to the README for contributors who run
  the template scaffold locally.

## v0.2 — targeting (≈ quarter from v0.1.0)

The headline themes are *new AI capabilities* and *operator polish*.

### AI adapters
- **YOLOv11** — newer detector with native ByteTrack-style tracking;
  ships as a per-adapter image alongside YOLOv8 rather than replacing
  it.
- **CLIP / SigLIP embeddings** — semantic search across recorded
  footage ("find clips with a red truck"). The most-requested capability
  from the design discussions.
- **Pose estimation** — fall detection, ergonomics, sports analytics.
  YOLOv8-pose or MediaPipe Pose; operator choice via a thin contract
  wrapper.
- **Audio events** — PANNs or YAMNet for gunshot / glass-break / dog-
  bark detection. The audio-input shape isn't represented in v0.1's
  shipped adapters.
- **Lightweight CPU-first Tier-0 detector** — an alternative to YOLOv8
  for low-power boxes with no hardware accelerator, selectable via the
  existing `DETECT_DETECTOR` switch. Every candidate exports to ONNX and
  rides the pipeline's existing cv2.dnn/ort path — the per-model work is
  an output-decoding head next to the YOLOv8 one. Candidates, in rough
  preference order:
  - *RF-DETR nano* (Apache-2.0) — the friendliest license; the RF-DETR
    family is the first real-time line past 60 mAP on COCO, with strong
    domain transfer.
  - *YOLO26n* (AGPL-3.0) — the strongest pure-CPU story: up to ~43%
    faster CPU inference than YOLO11-N, NMS-free end-to-end output
    (simpler decode head, stable fp16/fp32).
  - *RTMDet-tiny* (MIT) — throughput specialist.
  - *NanoDet-Plus / PP-PicoDet* — the ultra-light legacy tier for very
    weak boxes.
  Licensing note: "we only use the model" is not an exemption — AGPL
  weights/code carry copyleft on distribution and network use. That is
  compatible with OpenNVR's AGPL core, but weights must stay
  runtime-downloaded (as yolov8n is today), never vendored into
  Apache-licensed components. Trades some accuracy for a pipeline that
  stays in single-digit CPU per camera; complements — doesn't replace —
  the substream tap and `DETECT_HWACCEL` paths, and pairs with the
  accelerator detector adapters (Coral / Hailo / RKNN) tracked in
  `detect-pipeline/FOLLOWUPS.md`.
- **Detector / VLM eval harness** — a scripted benchmark that replays
  recorded site footage (or a labeled clip set) through candidate
  detectors and caption VLMs and reports recall / precision per label,
  CPU cost per frame, and describe-quality spot checks side by side —
  so a `DETECT_DETECTOR` or `OLLAMA_VLM_MODEL` swap is a measured
  decision, not vibes. Motivating case: moondream answering "appears to
  be empty" on a frame with a person directly in front of the lens,
  where qwen3-vl:4b answers correctly.

### Operator polish
- **Active Directory / SAML SSO** for staff authentication. v0.1 uses
  local accounts; v0.2 makes enterprise / municipal deployment less
  awkward.
- **Multi-host deployment story.** v0.1 scales fine to ~50 cameras on
  one host; v0.2 documents the multi-host pattern with KAI-C federation
  for installs that span sites. See **Horizontal sharding for Tier-0**
  below for the piece that has to land first.

### Horizontal sharding for Tier-0

**The gap.** `detect-pipeline` starts a worker for *every* camera the
discovery endpoint returns. There is no shard key, camera allowlist, or
replica index anywhere in the service, so a second instance is not a way
to split the fleet — both instances would analyse **every** camera. That
is duplicated decode and inference, duplicated NATS events on the same
subjects, and duplicated rows in the events store (track ids are assigned
per process, so the `uq_events_visit` constraint does not dedupe them).
Scaling past one host today means a bigger host, not another one.

**The shape of the fix.** Give each instance a stable slice of the fleet
and let discovery filter to it:

```
DETECT_SHARD=<index>/<total>     # e.g. 0/3, 1/3, 2/3
```

with the assignment made in `providers._to_spec`/`list_cameras`, keyed by
a *stable* camera identifier (`open_nvr_camera_id`, not list position, so
adding a camera does not reshuffle every other one). `<total>` of 1 —
the default — must be byte-identical to today's behaviour.

**What makes it more than a modulo.** The naive version is a few lines;
the parts that need design are:

- **Coverage proof.** A camera silently owned by *no* shard is worse than
  one owned by two — it is simply never analysed, and nothing currently
  reports "this camera has no worker anywhere". Core sees every request,
  so it is the natural place to notice a camera no shard claimed.
- **Rebalancing.** Changing `<total>` moves cameras between hosts. Their
  in-flight tracks end, best frames are lost, and both hosts briefly
  analyse the moved cameras. Acceptable on an explicit operator action;
  it should not happen because one host restarted.
- **Failure of a shard.** Nothing takes over that host's cameras. Either
  accept it and alert (simplest, and honest), or move to leases — at
  which point this stops being sharding and becomes cluster membership,
  which is a much larger commitment.
- **Interaction with the tap handshake.** Core picks main-vs-substream
  from the reader's reported decode capability. Shards may have different
  hardware, so that decision is already per-caller — worth confirming it
  stays coherent when several readers share a camera list.

**Why it is not in v0.1.** Single-host is a deliberate scope choice, and
the honest ceiling is set by memory (one model per detector instance,
pooled since v0.1) rather than by the absence of sharding. An operator at
~50 cameras is served by a larger host; sharding matters at the point
where multi-site federation does, which is exactly v0.2's remit.
- **Adapter route enrichment.** KAI-C's `ADAPTER_REGISTRY` is currently
  a single-URL default; v0.2 supports per-task-class routing so
  operators with mixed adapter fleets get cleaner deployments.

### TelemetrySource abstraction
The data-shape work that unblocks any moving-camera use case (drone
patrol, body-worn cameras, dashcam fleets). Designed but not built —
camera streams gain a parallel telemetry stream carrying
`{lat, lon, alt, heading, pitch, roll}` per-frame. v0.2 lands the
abstraction; v0.3 lands the first drone example built on it.

## v0.3 — targeting (≈ two quarters from v0.1.0)

Bigger architectural moves and the community-investment work that
opens with v0.2 as the platform substrate.

### Architecture
- **go2rtc evaluation as MediaMTX alternative.** go2rtc's HEVC handling
  and WebRTC behind NAT is materially better; the question is whether
  the migration cost justifies the swap. v0.3 work is the evaluation
  + migration path doc; the swap itself may slip to v0.4 depending on
  what the eval finds.
- **Hash-chained audit log integrity** — moving from append-only to
  tamper-evident. The paper's §9.3 assurance work; relevant for
  regulated deployments where the audit log itself is in the threat
  model.
- **Hardware trust anchor integration** — TPM attestation, secure-boot
  binding for the middleware. Same paper §9.3 origin.

### Adapter ecosystem
- **Re-identification adapter** (OSNet / TransReID) — "did this person
  come back later?" across cameras. The retail / loss-prevention
  segment's most-asked capability.
- **Depth estimation** (Depth Anything / MiDaS) — fall detection
  precision, perimeter rule expressiveness.
- **Larger VLM** (LLaVA-NeXT, Qwen2-VL, Florence-2) as a heavier
  alternative to BLIP for installs with GPU budget.

### Distribution
- **First Drone Patrol example** built on the TelemetrySource v0.2
  primitive. The v0.2 hero example, slipped to v0.3 because the
  primitive lands first.
- **Federated AI prototype** — the paper's §9.2 future work direction:
  anomaly-detection models trained locally, sharing only anonymised
  parameters with trusted consortiums.

## Speculative / community-driven (v0.4 and beyond)

These are directions we'd like to go but where we're not yet committing
to a release window. Pull requests and design discussions are welcome.

- **Common Criteria / FIPS 140-3 conformance.** The paper's §9.3
  assurance frame; relevant for defence and high-regulation deployments.
- **Body-worn camera ingest profile.** Different connectivity model
  (buffered upload over intermittent links) than fixed-camera RTSP.
- **Multi-tenancy primitives.** Current model is single-tenant per
  install; multi-tenancy (per-customer separation on shared hardware)
  is requested for MSP deployments.
- **Edge co-processor integration.** Coral / TensorRT / Hailo adapter
  variants beyond the ONNX-only adapters that ship in v0.1.
- **Native Kubernetes operator** for Helm-chart deployments at scale.

## How to influence the roadmap

There are three honest ways to move items up the list. Open a [Discussion](https://github.com/open-nvr/open-nvr/discussions) describing the use case and your environment — we weight the roadmap by user-reported pull, not engineering convenience, and a thread that draws other voices is the strongest signal. If the item is an adapter, contribute it directly: the template scaffold turns most adapter authoring into ~30 lines of Python plus your model, and the [authoring guide](https://github.com/open-nvr/ai-adapter#-write-your-own-adapter) is the on-ramp. For commercial deployments where you'd like specific capabilities accelerated under contract, see [SUPPORT.md](SUPPORT.md) for the commercial path.

## What you can rely on

A few stable commitments worth being explicit about. These are the
intent — if any of them have to change, the rationale will land in a
Discussion and a migration path will ship alongside.

1. **The AI Adapter Contract is stable.** We intend to support contract
   v1 through at least v0.4. If we have to break it earlier — material
   security finding, upstream-spec drift we can't avoid — the change
   will be documented and a migration path will ship alongside. A
   future contract v2 will ship SDK v2.x in parallel; adapters built
   against v1 will continue to work via a compatibility shim.
2. **AGPL won't change.** The server licence is AGPLv3 and will stay
   AGPLv3. The SDK licence is Apache-2.0 and will stay Apache-2.0.
3. **No default-on cloud connectivity.** No operator-facing
   functionality will be cloud-default. Any outbound connection an
   adapter makes will require explicit operator configuration AND be
   audit-logged via `inference.refused_sovereignty` / `policy.opt_in`
   events. The federated-AI roadmap item (v0.3) is opt-in by design and
   will land with the same posture.
4. **Honest CHANGELOG.** Breaking changes get a major-version bump.
   We're 0.x — bumps are cheaper here than at 1.x — but the policy
   stays the same when we cross 1.0.

If something shipped under one of these commitments would need to change,
it'll be a Discussion before a PR. That's the trust we want with
operators who put OpenNVR on critical infrastructure.
