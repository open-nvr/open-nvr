# Design proposal: compute-gated inference

**Status:** Draft / for discussion
**Scope:** KAI-C middleware, adapter manifest, operator UI
**Related:** `DESIGN_NOTES.md`, `ARCHITECTURE.md`, sovereignty gates (V-###)

## Summary

Continuous heavy AI inference (VLM captioning, LLM reasoning, face/plate
recognition) across every camera, every frame, does not fit on the affordable
hardware our users own — a Raspberry Pi 5, an Intel N100 mini-PC, a modest
server. This proposal adds a **compute gate**: a cheap, always-on layer decides
when the expensive layer runs, so heavy inference is spent only on frames that
actually warrant it.

The design is built around one hard constraint from the outset:

> **A missed critical event is unacceptable. The gate must never be able to
> cause one.**

We satisfy that not by promising a perfect gate, but by ensuring the gate is
**never in the path of catching an event** — only in the path of *how much
compute we spend interpreting an event we have already detected and recorded.*

This is the same architecture Frigate and Blue Iris have used for years
(detection runs on motion, not on every frame). It is proven and, frankly,
table stakes — OpenNVR is currently missing it.

## The non-negotiable invariants

Accuracy is preserved **by construction** if three invariants hold. Every part
of the design below exists to keep them true.

1. **Recording is never gated.** Continuous/motion recording runs independently
   of any AI decision. The worst a bad gate can do is delay an *alert*; it can
   never cause *lost footage*. Every event remains reviewable after the fact.

2. **The cheap tier defers to the accurate model — it never overrides it.** The
   cheap layer decides "worth a closer look"; the heavy model makes the actual
   determination. Final alert precision therefore equals the heavy model's
   precision, unchanged. The gate only affects *when* the good model runs.

3. **Every skip is recorded and measurable.** No frame is passed over without a
   logged score and threshold. "Why was there no alert at 03:14?" is always
   answerable, and the real miss rate can be *measured* rather than assumed.

## Architecture: two tiers

```
        every frame, every camera
                  │
                  ▼
   ┌───────────────────────────────┐
   │  TIER 0 — always on, cheap     │   never gated
   │  motion + small detector (YOLO-nano)   │   → records, fires basic alerts
   │  "did something relevant appear?"      │   → the safety floor
   └───────────────┬───────────────┘
                   │ relevant object / zone / rule hit
                   ▼
   ┌───────────────────────────────┐
   │  TIER 1 — gated, expensive     │   runs only on Tier-0 hits
   │  VLM caption, LLM rules, face, plate    │   → rich understanding
   │  "what exactly is it, does a rule fire?"│   → auditable dispatch
   └───────────────────────────────┘
```

**Tier 0 is the safety guarantee.** It is a *real detector*, not just pixel
diffing, and it runs on every frame. Presence of a person or vehicle is caught
here, recorded here, and can alert here. Nothing about catching an event depends
on the gate.

**Tier 1 is what the gate protects.** Deferring an expensive semantic pass does
not miss the event — Tier 0 already detected and recorded it. We are only
choosing not to run the costly *interpretation* on idle frames.

So the gate sits in front of the expensive **brain**, never in front of the
always-on **eyes**.

### Why pixel-diff alone is not Tier 0

Naive frame differencing false-triggers on rain, headlights, swaying trees, IR
day/night switching, and camera auto-exposure — and, worse, can *miss* a slow or
distant subject. Tier 0's floor is therefore a small object detector (motion may
front-run it as an even cheaper pre-filter, but detection, not motion, is what
decides "relevant"). Tier 0's model size is chosen to fit the target hardware
while being good enough to never miss the event classes that matter.

## Tier-0 triggers are pluggable — the domain-agnostic gate

Everything above describes the **default** trigger: motion plus a small object
detector, tuned for security cameras. That is the right default because most
cameras are CCTV and "did a person/vehicle appear?" is the question. But object
detection must not be *baked in* as the only way to wake Tier 1 — that would
quietly turn OpenNVR into Frigate (a fixed object pipeline) and throw away its
defining property: **any model, behind the governed adapter contract, discovered
in the registry, exported as a skill.**

So the real abstraction of compute-gated inference is not "object detection gates
things." It is:

> a cheap, always-on **trigger signal** -> a gate decision -> invoke a registered
> **expensive model** -> governed, audited result on the bus.

Object motion is one *instance* of the cheap trigger. The trigger is itself a
pluggable capability, declared by the model's adapter (the same
`CapabilitiesResponse` that already carries `InputSpec` / `DetectorSpec` /
`Accelerator`). A model declares **what wakes it** via a `TriggerPolicy`:

| `TriggerPolicy` | Cheap Tier-0 signal | Example domain |
|---|---|---|
| `motion` (default) | object motion + small detector | security / CCTV |
| `scene_change` | frame-delta / contour change | microscopy, structural change |
| `interval` | a schedule (every N min/hours) | crop / vegetation survey, time-lapse |
| `field_statistic` | diffuse-motion / brightness / texture statistic | wind, rain, fog, smoke |
| `chained` | another cheap model's output | "maybe-interesting" -> confirm |
| `always` | never gate (run every frame) | domains that require it |

The gate is written against `TriggerPolicy`, **not** against "motion." It never
needs to know the *domain* — it maps whichever cheap trigger fired to whichever
registered model should run, then publishes that model's result to the audited
bus for the camera-agent and other consumers to act on. Microscope data,
wind/rain patterns, contour change, and crop surveys are all the *same shape*:

```
  cheap always-on TRIGGER  ->  GATE (policy)  ->  registered EXPENSIVE model  ->  audited bus -> agent acts
```

This is exactly the axis Frigate cannot follow: its triggers *and* its detectors
are a fixed object-detection set. OpenNVR's gate stays model-agnostic because the
trigger is a declared capability, not a hardcoded assumption.

**State today / design obligation.** Tier 0 ships one trigger — `motion` plus
object detection (the CCTV default). PR B's gate MUST be authored against the
`TriggerPolicy` interface rather than hardcoding motion, and the contract needs a
`TriggerPolicy` field alongside `DetectorSpec`. If PR B hardcodes
"motion -> object," it re-narrows OpenNVR into an object NVR — the one outcome to
avoid. The non-object trigger *signals* themselves (`scene_change`,
`field_statistic`, ...) are follow-ups; naming the abstraction now is what keeps
the door open.


## Controls that enforce "never miss a critical event"

| Control | Guarantee it provides |
|---|---|
| **Recording never gated** | Every event is on disk and reviewable, regardless of any AI decision |
| **Tier 0 always on** | Detection never waits on the gate; presence is caught every frame |
| **Per-camera "always analyze"** | Critical cameras (gate, cash room, perimeter) run the full pipeline every frame — gate disabled entirely |
| **Critical-class force-escalate** | Named classes/zones (e.g. person-in-zone after hours) always escalate to Tier 1 regardless of gate score |
| **Heartbeat pass** | The full pipeline runs every N seconds even on a "static" scene, bounding worst-case interpretation latency and catching slow changes |
| **Recall-biased defaults** | Thresholds default to letting too much through, not too little; false positives cost compute, false negatives are the error we refuse |
| **Shadow mode** | The miss rate is *measured* on the operator's own cameras before the gate is ever trusted (see below) |

### Shadow / calibration mode — the proof mechanism

Before an operator trusts the gate, they run it in **shadow mode**: the gate
computes and logs the decision it *would* make, but Tier 1 still runs on every
frame. We then compare, on the operator's real cameras:

> Did the gate ever score below threshold on a frame that Tier 1 flagged as an
> event?

That produces a **measured miss rate on this specific deployment** — not a
vendor claim. The same data auto-tunes per-camera thresholds. Shadow mode is
also how we honestly derive any published "runs N cameras on a Pi 5" figure.

Metric to record per camera during shadow: `frames_seen`,
`frames_gate_would_skip`, `tier1_events_on_skipped_frames` (the misses),
`tier1_events_total`. Target before enabling: `tier1_events_on_skipped_frames == 0`
over a representative window, with a configurable safety margin on the threshold.

## Honest statement of residual risk

No system misses zero events — continuous full inference still misses what the
*model itself* is bad at (tiny, occluded, or very fast subjects). What this
design guarantees is narrower and truthful:

> **The gate adds no miss risk beyond the Tier-0 detector's own accuracy**, and
> even a Tier-0 miss is recoverable because the footage is always recorded.

We should never market "zero missed events." We should market "the gate is not
in the path of catching events, and here is the measured miss rate on your
hardware."

## The payoff features (enabled once the gate exists)

### Natural-language standing rules

Continuous monitoring expressed in plain text — *"alert me if a dog enters the
porch."* Distinct from the on-demand camera agent (which answers when asked).

The trap: evaluating the rule with a VLM on every frame reproduces the exact
cost problem the gate solves. The design instead **compiles the rule into cheap
primitives** — parse "dog" + "porch" into a detector class filter plus a zone
polygon (Tier 0), and escalate to the VLM/LLM (Tier 1) only for confirmation or
genuinely ambiguous phrasing. The cheap tier decides *worth looking*; the
accurate tier decides *is it real*. Final-rule precision is the heavy model's.

### Dynamic FPS / compute reinvestment

Because Tier 1 now runs far less often, the freed compute can be **reinvested
into accuracy** rather than only saved: raise FPS on active scenes, or run a
larger model on the frames that matter. This is what makes the gate
*accuracy-positive* — real-event recall can go **up**, not down.

### Adapter cost-tier in the manifest

Extend the adapter manifest (which already declares GPU / filesystem / network
for sovereignty) to declare a **compute cost tier** and **trigger conditions**.
The KAI-C scheduler uses these to gate dispatch. This keeps gating policy in the
governed contract, not bolted on, and lets an adapter say "I am expensive; only
call me on a Tier-0 hit in these zones."

## Approximate benefit

The mechanism is simple: **benefit ≈ 1 ÷ (fraction of time the scene is
active).** A camera that is genuinely interesting 10% of the time does ~10% of
the expensive work.

These are **estimates to validate via shadow mode, not measured benchmarks** —
they depend on scene activity, model sizes, resolution, and hardware:

| Scene type | Approx. heavy-inference reduction |
|---|---|
| Quiet (night, low-traffic, storage) | ~10–50× |
| Moderate (office, driveway) | ~5–15× |
| Busy (entrance, street) | ~2–5× |
| Blended across a typical estate | often ~5–15× |

Converts into: ~5–10× more cameras per box when heavy AI is the bottleneck;
the same job on cheaper hardware; higher recall if compute is reinvested; and,
under `cloud_allowed`, a roughly proportional drop in API cost. We deliberately
do **not** commit to a hard "N cameras at M fps" figure here — shadow mode
produces that honestly per deployment.

## What to reject from prior art

- **Cloud-VLM-as-default** — wrong for `local_only`, where there is no API to
  optimise. Gate to protect *local* compute; cloud escalation is opt-in only
  under `cloud_allowed`, never the default path.
- **Pixel-diff as the reference detector** — take the concept, rebuild it
  properly; it is the oldest and ugliest tuning problem in NVR.
- **"Cost-optimised" as the headline** — for a sovereignty product the story is
  "more cameras per box" and "always-on standing rules," not a cloud bill.

## Phased plan

The delivery plan is finalized in Part II under **"Finalized phased plan"** —
two implementation PRs (PR A: efficient full-time detection, no gating, low
risk; PR B: the gate + all its safety rails, shipped together), with the payoff
features as additive follow-ups. See that section for the authoritative
sequencing; the safety-critical rule is unchanged: the gate never ships without
its rails, and it defaults to shadow mode until the measured miss rate justifies
enforcement.

## Open questions / to validate on hardware

- Tier-0 model size vs. target hardware (Pi 5 / N100) — what is "good enough to
  never miss the event class" while still fitting the always-on budget?
- Default recall-biased thresholds and the shadow-mode safety margin.
- Where standing-rule compilation lives (KAI-C vs. a dedicated rules service).
- Audit-log volume: gate decisions are high-frequency; sample or summarise skips
  rather than logging every frame.
- Interaction with existing motion-record so we do not double-run motion.

## Config sketch (illustrative, not final)

```yaml
inference_gate:
  mode: shadow            # shadow | enforce | off
  heartbeat_seconds: 30   # full pipeline at least this often, per camera
  default_sensitivity: high   # high = recall-biased (skip less)
  cameras:
    cam-front-gate:
      always_analyze: true      # critical: gate disabled, full pipeline
    cam-storage:
      sensitivity: low          # quiet scene, gate aggressively
      force_escalate:
        - class: person
          zone: doorway
          when: after_hours
```

---

# Part II — Frigate source review & the finalized OpenNVR design

Part I is the concept. Part II is grounded in a source-level read of Frigate and
of OpenNVR's actual KAI-C code, and supersedes Part I where they differ.

## Licensing: we can reuse Frigate code

Frigate's `LICENSE` is now **MIT** (relicensed from AGPL-3.0). MIT is compatible
with OpenNVR's AGPLv3, so this is a reuse-vs-reimplement decision on *engineering*
merit, not a legal wall. Rules:

- Pin any ported code to a specific **current, MIT-era commit SHA** and record it.
  This review is against **Frigate `6f80bcd19` (v0.18-beta, 2026-07-18), MIT** —
  use that SHA (or newer MIT) as the port baseline.
- Keep the MIT copyright/permission notice in a `NOTICE` file and file headers.
- Do **not** pull from old AGPL-era tags — that snapshot is still AGPL.

## What Frigate actually does (the parts worth taking)

From source (`frigate/video/detect.py`, `motion/improved_motion.py`, `track/*`,
`detectors/*`, `util/object.py`):

- **It is not "motion → crop → detect".** Each frame the detector runs on a
  *union* of regions from three sources: (a) Kalman-predicted boxes of existing
  tracked objects, (b) motion boxes not already covered, snapped to a **learned
  per-camera 8×8 region-size grid** built from historical true-positive boxes,
  (c) an 8-cell startup scan on the first frame. Regions are squared, `//4`-aligned,
  min 320 px, and cluster members must be ≥5 % of region area.
- **Motion** = running-average background subtraction on a downscaled Y-plane,
  with moving-average contrast normalization, a **10-frame persistence gate**
  before motion is absorbed into the background, and a `lightning_threshold` that
  stops *detection* but keeps *recording* during whole-frame flashes (dawn/dusk,
  IR cut). Plain frame-diff would flood the detector daily — the most
  underestimated piece in the codebase.
- **Tracking** = Norfair (Kalman) with a **size-aware bottom-center distance**
  (not centroid-IoU), per-class R/Q tuning, an initialization delay, and a
  "0.0 score on a missed frame" history. Centroid-IoU churns IDs.
- **Stationary interval gate** (the compute win): a stationary track
  (`motionless_count ≥ threshold`, no overlapping motion) is **seeded from its
  last box without calling the detector**, except one real detection every
  `interval` frames or when motion overlaps it. Zero regions ⇒ the detector runs
  zero times that frame. An NCC / phase-correlation appearance classifier stops
  stationary↔active flip-flop.
- **Best-frame** = attribute-aware: bigger face/plate first, then not-edge-clipped,
  then +0.05 score, then +10 % area. Feeding the expensive model *this* crop is
  the biggest accuracy-preserving efficiency.
- **Detector plugin contract** (`DetectionApi`): declares `type_key`
  (accelerator) and `supported_models`, reads `model.{width,height,input_tensor,
  input_dtype,pixel_format}`; the framework shapes the tensor; the plugin returns
  a sorted, capped `(K,6)` normalized-box array. Almost exactly our adapter
  contract.
- **Decode/record split**: separate ffmpeg per role. Detect ffmpeg hwaccel-decodes
  and scales the substream to raw yuv420p over a pipe; record ffmpeg is `-c copy`
  remux of the mainstream (never decoded). Recording quality is fully independent
  of inference.

## Grounding in KAI-C (what exists vs. what's new)

KAI-C today is a **governance middleware, not a detection pipeline**:

- Exists: `registry.py` (`AdapterRegistry`/`RegisteredAdapter`), `sovereignty.py`
  (`check_adapter`, egress), `audit.py` (`AuditStore.emit`, `AuditEventType`),
  `connector.py` (`KaiConnector.process_stream`), `stream_proxy.py` (`StreamProxy`
  WS relay + `_inspect_result_text` + inference broadcast), `events.py`
  (`InferenceCompletedEvent`, NATS), and `contract_types.py` — which **already
  defines `Cost`, `Scheduling`, `CapabilityDescriptor`, `Permissions`**.
- Does **not** exist: any motion / region / tracking / detection loop. Inference
  is on-demand (camera agent) or streamed straight through `StreamProxy`. There is
  no Tier-0.

So the gate is a **new pipeline component in front of KAI-C's dispatch**, reusing
KAI-C's governance for every adapter call it chooses to make.

## Finalized component design

New component **`detect-pipeline`** (one worker per camera; ports Frigate's proven
CV logic). It sits on the detect substream and calls adapters *through* KAI-C.

```
mainstream ─(-c copy)──────────────► recording (MediaMTX segments)      [never gated]

substream ─ffmpeg hwaccel decode──► detect-pipeline  (Tier 0, always on)
                                     ├─ motion            (port improved_motion)
                                     ├─ region select     (port learned grid)
                                     ├─ cheap detector    → via KAI-C, accel backend
                                     ├─ Norfair + stationary interval gate
                                     └─ gate decision + best-frame per track
                                            │ new track / zone / ambiguous / interesting class
                                            ▼
                                     Tier 1 dispatch ─► KAI-C ─► expensive adapter (VLM/LLM/face/LPR)
                                        (best crop, once per track-state, rate-limited, async)
                                            │
                                            ▼
                                     KAI-C audit (gate score+threshold) + NATS InferenceCompleted
```

Wiring to real modules:

- **Tier-1 dispatch** goes through `KaiConnector` / `StreamProxy`, so sovereignty
  (`check_adapter`) and audit (`AuditStore.emit`) already apply — the gate never
  bypasses governance.
- **Adapter cost-tier** reuses the existing `Cost` + `Scheduling` in
  `contract_types.py`; extend with `trigger` conditions and an `accelerator` +
  input-tensor descriptor (mirror `DetectionApi`).
- **Gate audit** = a new `AuditEventType` (e.g. `inference_gated`) emitted via
  `AuditStore` with score + threshold — satisfies invariant 3.
- **Broadcast** reuses `events.InferenceCompletedEvent` / NATS.
- **Recording independence** = the MediaMTX `-c copy` path, untouched (invariant 1).

## Reuse vs. reimplement (Frigate is MIT — decide on merit)

| Frigate piece | Decision | Why |
|---|---|---|
| `motion/improved_motion.py` | **Port** (attribute, pin SHA) | Lighting/IR suppression is hard-won; a rewrite invites daily false-trigger floods |
| `util/object.py` region math + learned grid | **Port** | Region jitter breaks tracking *and* adapter inputs |
| `track/norfair_tracker.py` + stationary classifier | **Port** | ID stability and stationary flip-flop are the subtle parts |
| best-frame `is_better_thumbnail` | **Port** | Accuracy-preserving; feeds Tier-1 the right crop |
| detector plugin contract | **Adapt, don't port** | Express it as the KAI-C adapter capability descriptor over HTTP/WS, not an in-proc class |
| SHM multi-process transport | **Reimplement** | Our adapters are network services; ship cropped tensors/JPEG via KAI-C, not shared memory |
| `ffmpeg_presets.py` hwaccel/role presets | **Port the presets** | VAAPI/QSV/NVDEC/RKMPP strings are directly reusable |

Net: port the CV-hard bits, express detection as a KAI-C adapter, keep governance
and transport ours.

## Adjacent optimizations (same effort, ordered by impact)

1. **Accelerator backends** declared in the adapter capability descriptor
   (OpenVINO / Coral / Hailo / TensorRT / RKNN) — the Tier-0 detector must run on
   one; ~10× vs. CPU. Likely a bigger win than the gate itself.
2. **Hardware-accelerated decode** on the detect ffmpeg (port `ffmpeg_presets`) —
   often a bigger CPU win than the gate.
3. **Detect-on-substream, record-on-mainstream** — we already have MAIN/SUB; make
   it the default detect path.
4. **Best-frame → one Tier-1 call per track** — makes the gate accuracy-*positive*.
5. **CLIP embeddings** over tracked objects → semantic search feeding the camera
   agent.
6. **Audio events** as an adapter.

Keep every enrichment local — differentiator: Frigate's newer GenAI can call
OpenAI; ours stays sovereign and audited.

## Finalized phased plan (this PR → two implementation PRs)

This PR (`feat/compute-gated-inference`) is **design only** (this doc); merge it
to anchor the work. Implementation follows as **two** PRs — the split is not
process for its own sake, it is the risk control that keeps invariant "a missed
critical event is unacceptable" enforceable. The two phases separate the
*low-risk, no-gating* work from the *gate* so the CV port is proven correct
before any gating exists.

**PR A — efficient full-time detection (no gating; low risk).**
Everything runs on every frame; nothing is skipped, so accuracy cannot regress —
this PR only makes detection *cheaper* and *possible on modest hardware*.
- ffmpeg role split + hwaccel decode presets (port `ffmpeg_presets`); detect on
  the substream, record `-c copy` on the mainstream.
- Capability descriptor gains `accelerator` + input-tensor spec; a reference
  OpenVINO/Coral cheap-detector adapter.
- `detect-pipeline` worker: motion (port) → region grid (port) → cheap detector
  adapter → Norfair tracker (port) → tracks + best-frame; emits detections and
  records. **Runs full-time, everything analyzed** — so this PR validates the
  ported CV logic on real cameras at **zero accuracy risk**.

This PR alone delivers most of the "runs on a Pi 5 / N100" benefit (hwaccel
decode + accelerator backend + substream), independent of whether the gate ever
lands.

**PR B — the gate + all its safety rails (ship together, never apart).**
Only now is anything skipped, and only behind the rails that make it safe.
- Stationary interval gate; per-camera sensitivity + `always_analyze`;
  critical-class force-escalate; heartbeat pass.
- **Shadow mode** + the miss metric — measures the real miss rate on the
  operator's cameras *before* the gate is trusted; gates the transition from
  "measure" to "enforce".
- Gate-decision audit events; Tier-1 dispatch (best-crop, rate-limited, async)
  through KAI-C.

Merge PR A first and let it soak. PR B must never merge without its rails, and
`mode: shadow` is the default until the measured miss rate justifies `enforce`.

**Later (separate, not blocking):** the payoff features — NL standing rules
compiled to primitives, dynamic FPS / compute reinvestment, CLIP semantic
search, audio adapter — land as independent follow-ups once A and B are stable.
They are additive and carry no accuracy risk to the gate, so they need no fixed
sequencing.

## Pitfalls to plan for (from Frigate source)

- Region sizing/jitter → **port the learned grid**, don't reinvent.
- Tracking is size-aware bottom-center distance, **not** centroid-IoU → ID churn otherwise.
- Stationary flip-flop wastes Tier-1 budget → port the NCC/phase-correlation appearance classifier.
- Lighting transients flood the detector → port contrast normalization + persistence gate + `lightning_threshold`.
- Async Tier-1 must never stall Tier-0 capture → separate send/receive paths, back-pressure by degrading to Tier-0, never block the frame loop.
- Best-frame must be attribute-aware, not max-score.

## Source references

Frigate (MIT): `frigate/video/detect.py`, `frigate/motion/improved_motion.py`,
`frigate/util/object.py`, `frigate/track/{norfair_tracker,tracked_object,stationary_classifier}.py`,
`frigate/detectors/detection_api.py` + `plugins/{openvino,edgetpu_tfl}.py`,
`frigate/object_detection/base.py`, `frigate/video/ffmpeg.py`, `frigate/ffmpeg_presets.py`.
OpenNVR: `kai-c/kai_c/{contract_types,connector,stream_proxy,registry,sovereignty,audit,events}.py`.

---

# Appendix — portable specifics (exact values from Frigate 6f80bcd19)

Read from the local source so the implementation PRs port real numbers, not
approximations. File paths are relative to `frigate/`.

## Detector contract (`detectors/detection_api.py`) — mirror as the KAI-C adapter descriptor

```
class DetectionApi(ABC):
    type_key: str                       # accelerator discriminator: openvino|edgetpu|onnx|...
    supported_models: list[ModelTypeEnum]
    def __init__(cfg):  thresh = 0.4;  height = cfg.model.height;  width = cfg.model.width
    def detect_raw(tensor_input) -> np.ndarray   # (K,6): [class_id, score, ymin,xmin,ymax,xmax], normalized 0–1, sorted desc
```
- The **framework** shapes input via `create_tensor_input` (`util/object.py`):
  crop the region → `yuv_region_2_{rgb,bgr,yuv}` per `model.input_pixel_format`
  → resize to `(model.height, model.width)`. The plugin stays layout-dumb.
- `model` config carries `width, height, input_tensor` (layout), `input_dtype`
  (`int` | `float`→/255 | `float_denorm`), `input_pixel_format` (rgb|bgr|yuv).
- Default confidence `thresh = 0.4`. `calculate_grids_strides()` (strides 8/16/32)
  is the shared YOLO-family anchor helper.

→ **KAI-C mapping:** the adapter capability descriptor declares `type_key`
(accelerator), `supported_models`, and the input spec; KAI-C ships the
already-cropped/resized tensor (or JPEG) and expects the sorted, capped `(K,6)`
normalized array back. Extend `contract_types.CapabilityDescriptor` accordingly.

## Motion defaults (`config/camera/motion.py` + `motion/improved_motion.py`)

| Field | Default | Meaning |
|---|---|---|
| `threshold` | **30** | pixel-diff threshold (1–255); higher = less sensitive |
| `lightning_threshold` | **0.8** | if motion covers >80 % of frame, **stop detecting but keep recording** (dawn/dusk/IR) |
| `contour_area` | **10** | min contour area to count as a motion box |
| `frame_alpha` | **0.01** | background running-average alpha, steady state |
| `delta_alpha` | **0.2** | background alpha while calibrating |
| `improve_contrast` | **True** | contrast-normalize before diff |

Algorithm invariants to port: contrast clip to the **4th/96th percentile over a
50-frame moving window**; motion must **persist ≥10 frames** before it is absorbed
into the background; "calibrated" once `pct_motion < 0.05` **and** `≤4` contours;
recalibrate whenever `pct_motion > lightning_threshold`, area >80 %, or the PTZ
motor is moving (rebaseline `avg_frame` on motor stop). This block is the
single biggest false-positive defense — do not shortcut it.

## Detect / stationary (`config/camera/detect.py`)

| Field | Default | Meaning |
|---|---|---|
| `fps` | **5** | detection frame rate (on the substream) |
| `min_initialized` | `fps // 2` | consecutive hits before a track is created (`initialization_delay`) |
| `max_disappeared` | ~`fps × 5` | frames a track survives unmatched (`hit_counter_max`) |
| `stationary.threshold` | (frames of no motion to mark stationary) | flips a track to "stationary" |
| `stationary.interval` | (N) | re-run the detector on a stationary track **once every N frames** |
| `stationary.classifier` | bool | enables the NCC/phase-correlation appearance check to prevent flip-flop |

Stationary tracks (no overlapping motion) are **seeded from their last box with
the detector skipped**, except every `interval`-th frame — zero regions ⇒ zero
detector calls that frame. This is the core steady-state compute win.

## Region math (`util/object.py`)

- `min_region = max(model.h, model.w)` if `<320` (rounded **up to a multiple of 4**),
  else **320** (`get_min_region_size`).
- Cluster region size multiplier **1.35** (`get_cluster_region`); startup-scan
  regions multiplier **1**, top **8** historically-popular grid cells on frame 1.
- A box joins a cluster only if `area(box) / area(cluster_region) ≥ 0.05` (5 %).
- Per-camera **8×8 learned region-size grid** built from historical true-positive
  boxes — snap motion regions to it (this is what keeps detector inputs stable).

## Best-frame priority (`util/image.py::is_better_thumbnail`) — port verbatim

1. person → better (or first) **face** attr; car → better (or first) **license_plate**
   attr. Once the thumb has the attribute, only a *better* one replaces it.
2. else new is **not edge-clipped** while current is.
3. else `new.score > current.score + 0.05`.
4. else `new.area > current.area × 1.1`.

Feed **this** crop to Tier-1 — a plain "max score" picker hands the VLM
edge-clipped, face-less frames.

## Tracking (`track/norfair_tracker.py`) — per-class Kalman (R, Q, distance_threshold)

| Class | R | Q | dist_thresh | Notes |
|---|---|---|---|---|
| default | 3.4 | — | 2.5 | |
| car | 3.4 | 0.03 | 2.5 | |
| license_plate | 2.5 | 0.05 | 3.75 | |
| person (PTZ) | 4.5 | 0.25 | 2.0 | reid histogram dist 0.5, `reid_hit_counter_max` 10 |
| (dynamic) | 4 | 0.2 | 3.0 | |

`initialization_delay = detect.min_initialized`, `hit_counter_max =
detect.max_disappeared`. Distance = **size-aware bottom-center + width/height
ratios**, not centroid-IoU (prevents ID swaps between nearby same-class objects).

## Hardware-accel decode presets (`ffmpeg_presets.py`) — directly reusable strings

The `hwdownload,format=nv12` step (bringing GPU frames back to system memory as
yuv420p for the pipe) is the load-bearing detail.

| Backend | Decode | Scale |
|---|---|---|
| Intel/AMD **VAAPI** | `-hwaccel vaapi -hwaccel_device {dev} -hwaccel_output_format vaapi` | `scale_vaapi=w={w}:h={h},hwdownload,format=nv12` |
| Intel **QSV** | `-hwaccel qsv -qsv_device {dev} -hwaccel_output_format qsv -c:v h264_qsv` | `vpp_qsv=w={w}:h={h}:format=nv12,hwdownload,format=nv12,format=yuv420p` |
| **NVIDIA** CUDA | `-hwaccel cuda -hwaccel_output_format cuda` | `scale_cuda=w={w}:h={h},hwdownload,format=nv12` |
| **Jetson** | `-c:v h264_nvmpi -resize {w}x{h}` | (scaled in decoder) |
| **Rockchip** RKMPP | `-hwaccel rkmpp -hwaccel_output_format drm_prime` | (drm_prime) |
| **RPi** v4l2m2m | `-c:v h264_v4l2m2m` | `-vf fps={fps},scale={w}:{h}` |

`LibvaGpuSelector` auto-picks `/dev/dri/renderD*` via `vainfo`. Recording uses a
separate `-c copy` remux (`-f segment -segment_time 10`) — never decoded.

## Concrete `contract_types` extension (illustrative)

```python
class Accelerator(BaseModel):
    backend: str            # "openvino" | "edgetpu" | "onnx" | "tensorrt" | "rknn" | "cpu"
    device: str | None = None
class InputSpec(BaseModel):
    width: int; height: int
    layout: str = "nhwc"    # nhwc | nchw
    dtype: str = "uint8"    # uint8 | float | float_denorm
    pixel_format: str = "rgb"
# add to CapabilityDescriptor: accelerator: Accelerator | None, input: InputSpec | None,
#                              cost: Cost (exists), scheduling: Scheduling (exists),
#                              trigger: {classes: [...], zones: [...], min_score: float}
```

---

# Competitive positioning — build this as table stakes, win elsewhere

Read this before picking up the implementation PRs. It sets the *goal*, and the
goal is not "beat Frigate at detection."

## Where this sits vs. Frigate

On the detection pipeline itself, **do not try to out-Frigate Frigate.** Frigate
has a 5+ year head start on exactly this loop, a large maintainer community,
mature accelerator support (Coral, Hailo, OpenVINO, TensorRT, RKNN), a polished
UI, and years of real-world tuning behind the motion / stationary / tracking
edge cases we are porting. Framed as "OpenNVR detection vs. Frigate detection,"
OpenNVR loses for years, and reimplementing their CV loop is a permanent
maintenance tax against a faster-moving upstream.

So this work is **catch-up to table stakes, not a competitive win.** Its job is
to remove the disqualifier — "OpenNVR can't run efficiently on affordable
hardware" — so OpenNVR is allowed to compete at all in the self-hosted space.
Necessary, not sufficient.

**Scope the port as "credibly efficient and accurate," not "better than
Frigate."** The moment this becomes a feature-for-feature race on detection
quality and UI polish, a smaller team loses to Frigate's community
indefinitely. Port enough to be credible; stop there.

## Where OpenNVR actually wins (and why the gate matters)

The gate matters because it *frees* effort to spend on the axes where OpenNVR is
not competing with Frigate at all:

- **Governance / audit / sovereignty.** Frigate has none of this and it is hard
  to retrofit onto its design. The gate turns it from a claim into a demonstrated
  feature: an audit record for *non-events* ("nothing alerted at 03:14 —
  score 0.02 < 0.15 threshold") is something no competitor produces.
- **Any-model adapter contract.** Frigate's detectors are a fixed plugin set.
  OpenNVR's "any model behind a governed HTTP/WS contract" is a different
  proposition, and the accelerator/trigger descriptor in this design extends it
  rather than copying Frigate's closed detector list.
- **Provably-local enrichment.** Frigate's newer GenAI features can call OpenAI.
  OpenNVR's local VLM agent is the sovereign version — a differentiator that
  *strengthens* as Frigate leans further on cloud.
- **The buyer.** Frigate owns homelab/hobbyist. OpenNVR's wedge is regulated /
  air-gapped / §889 / government / compliance buyers who cannot use a
  cloud-touching or unauditable system. Frigate is not built for them.

## The one-line framing

Build the gate because **without it OpenNVR is disqualified** — a self-hosted NVR
that melts a Pi is a non-starter and reviewers will say so. Position it as
**"Frigate-class efficiency, plus governance and provable locality Frigate
structurally can't offer."** Efficiency is the price of entry; governance is the
reason to choose OpenNVR.

## Implication for the phased plan

- PR A + PR B (efficient full-time detection, then gate + rails) = **reach table
  stakes.** Judge them by "is it credibly efficient and accurate on a Pi 5 /
  N100," not by Frigate parity.
- The differentiated effort — audit of gate decisions, the governed adapter
  contract, provably-local standing rules — is where disproportionate time
  should go, because that is the part Frigate cannot easily answer.
