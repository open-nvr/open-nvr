# Tracking: compute-gated inference (efficient AI on modest hardware)

**Design:** `docs/design/compute-gated-inference.md` (branch `feat/compute-gated-inference`)
**Goal:** make OpenNVR run AI on affordable hardware (Raspberry Pi 5, Intel N100)
without downgrading accuracy — table stakes vs. Frigate — while keeping every
inference call governed and audited (the differentiator).
**Hard rule:** *a missed critical event is unacceptable.* The gate is never in
the path of catching an event; it only defers expensive interpretation of an
event already detected and recorded.

Ported CV logic is taken from **Frigate `6f80bcd19` (v0.18-beta, MIT)** — pin
this SHA, keep the MIT notice in a `NOTICE` file and ported-file headers. Do not
pull from AGPL-era tags.

---

## PR A — efficient full-time detection (no gating, low risk)

Everything runs on every frame; nothing is skipped, so accuracy cannot regress.
Delivers most of the "runs on a Pi 5 / N100" benefit and validates the CV port
before any gate exists.

**Decode / stream**
- [ ] ffmpeg role split: `detect` role hwaccel-decodes + scales the **substream**
      to raw yuv420p over a pipe; `record` role is `-c copy` remux of the
      **mainstream** (never decoded). Recording independent of inference.
- [ ] Port `ffmpeg_presets` hwaccel decode/scale strings (VAAPI, QSV, CUDA,
      Jetson nvmpi, RKMPP, RPi v4l2m2m) incl. the `hwdownload,format=nv12` step;
      `/dev/dri/renderD*` auto-select via `vainfo`.

**Adapter contract (KAI-C)**
- [ ] Extend `contract_types.CapabilityDescriptor` with `Accelerator`
      (backend/device) + `InputSpec` (width/height/layout/dtype/pixel_format) +
      `trigger` (classes/zones/min_score). Reuse existing `Cost` / `Scheduling`.
- [ ] Middleware does tensor shaping (crop → colour-convert → resize); adapters
      stay layout-dumb and return sorted, capped `(K,6)` normalized boxes.
- [ ] Reference cheap-detector adapter on an accelerator (OpenVINO for N100 iGPU,
      or Coral) — the always-on Tier-0 model.

**detect-pipeline worker (ports)**
- [ ] Motion — port `improved_motion.py` (threshold 30, lightning 0.8,
      contour_area 10, frame_alpha 0.01, delta_alpha 0.2; 4/96 percentile over
      50 frames; ≥10-frame persistence before background absorb; recalibrate on
      lightning/IR/PTZ).
- [ ] Region selection — port `util/object.py`: 3-source union (tracked-object
      estimates ∪ standalone motion clusters ∪ startup scan), learned 8×8 grid,
      `min_region = max(model,320)` /4-aligned, 1.35× cluster, 5%-area rule.
- [ ] Tracking — port `norfair_tracker.py`: size-aware bottom-center distance
      (not centroid-IoU), per-class R/Q/threshold, `initialization_delay` =
      min_initialized, `hit_counter_max` = max_disappeared.
- [ ] Best-frame — port `is_better_thumbnail` priority (attribute → not-edge →
      +0.05 score → +10% area).
- [ ] Emits detections + records; dispatch to the cheap adapter **through KAI-C**
      so sovereignty (`check_adapter`) + audit (`AuditStore.emit`) apply.

**Validation / acceptance (PR A)**
- [ ] Runs full-time (nothing gated) on a Pi 5 and an N100 with N×1080p cameras;
      record the sustained camera count + CPU/RAM (this is the honest input to
      the hardware guides — do not publish invented numbers).
- [ ] Tracking IDs are stable across a walk-through (no churn); regions don't
      jitter; no detector flood at dawn/dusk (lighting-transient test).
- [ ] Unit tests for region math, motion thresholds, tracker distance, best-frame.
- [ ] `NOTICE` + ported-file headers cite Frigate SHA `6f80bcd19` (MIT).

---

## PR B — the gate + all safety rails (ship together; merge after PR A soaks)

Only now is anything skipped, and only behind the rails. Default `mode: shadow`.

- [ ] Stationary interval gate: stationary tracks (no overlapping motion) seeded
      from last box, real detection only every `interval` frames; NCC/phase-corr
      appearance classifier to prevent flip-flop.
- [ ] Per-camera sensitivity + `always_analyze` override (critical cameras =
      gate disabled, full pipeline every frame).
- [ ] Critical-class force-escalate (named class/zone/time always → Tier 1).
- [ ] Heartbeat pass: full pipeline every N s regardless of gate score.
- [ ] **Shadow mode** + miss metric per camera: `frames_seen`,
      `frames_gate_would_skip`, `tier1_events_on_skipped_frames`,
      `tier1_events_total`. Gate the shadow→enforce transition on
      `tier1_events_on_skipped_frames == 0` over a representative window + margin.
- [ ] New `AuditEventType` (e.g. `inference_gated`) emitted via `AuditStore`
      with score + threshold on every skip.
- [ ] Tier-1 dispatch: best-crop only, once per track-state, rate-limited, async
      (separate send/receive; back-pressure by degrading to Tier-0, never block
      the frame loop).

**Acceptance (PB B)**
- [ ] Shadow mode reports a measured miss rate; enforce is refused until it hits
      the target on the operator's own cameras.
- [ ] Invariants demonstrably hold: recording never gated; `always_analyze`
      cameras never skip; every skip has an audit record.
- [ ] Measured compute reduction vs. PR A on the same hardware/scenes.

---

## Later (additive follow-ups, no fixed sequencing, no gate-accuracy risk)
- [ ] NL standing rules compiled to primitives (class + zone; VLM only to confirm).
- [ ] Dynamic FPS / compute reinvestment on active scenes.
- [ ] CLIP embeddings → semantic search feeding the camera agent.
- [ ] Audio-event adapter.

---

## Pitfalls (from Frigate source — do not relearn the hard way)
- Region jitter breaks tracking + adapter inputs → port the learned grid, don't reinvent.
- Centroid-IoU tracking churns IDs → use size-aware bottom-center distance.
- Stationary flip-flop burns Tier-1 budget → port the appearance classifier.
- Plain frame-diff floods the detector at dawn/dusk → port contrast norm + persistence + lightning threshold.
- Async Tier-1 must never stall Tier-0 capture.
- Best-frame must be attribute-aware, not max-score.

## Positioning reminder
Judge PR A + PR B by "credibly efficient and accurate on a Pi 5 / N100," **not**
Frigate parity. The differentiated effort (audit of non-events, governed
any-model adapter contract, provably-local enrichment) is where disproportionate
time goes — that's what Frigate structurally can't offer.
