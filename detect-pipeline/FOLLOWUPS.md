# detect-pipeline — tracked follow-ups

Deliberately out of PR A, in rough priority order. Each is issue-ready.

---

## 1. Evaluate & adopt a stronger detector (RF-DETR / YOLO26) — the OpenCV-5 payoff

**Why.** The default detector is YOLOv8n ONNX via `cv2.dnn`. It's solid and
stack-consistent, but the 2026 CPU accuracy/speed leaders are **RF-DETR** and
**YOLO26** (and YOLO11 as a lighter step up). OpenCV 5's rewritten DNN engine
(ONNX op coverage ~22%→80%, dynamic shapes, INT8) is what makes running these on
CPU practical — this is how we actually cash in the move to OpenCV 5. This is
also the difference between a placeholder-quality detector and one worth putting
in front of the governance/audit story.

**Scope.**
- Benchmark on the target hardware (**Raspberry Pi 5, Intel N100**): accuracy
  (mAP on a representative clip set) and latency/FPS for candidates —
  YOLOv8n (baseline), YOLO11n, YOLO26n, RF-DETR (nano/small), each FP32 and
  **INT8-quantized**.
- Confirm each candidate's ONNX **loads and runs on OpenCV 5's new engine**
  (not just the classic-engine fallback) and produces correct outputs.
- **Decode path caveat:** our `OnnxYoloDetector` assumes the YOLOv8/YOLO11
  NMS-style output `(1, 4+nc, N)`. **RF-DETR (transformer, NMS-free) and newer
  end-to-end YOLOs have different outputs** — this needs a **per-model-family
  decode** (make `postprocess_*` pluggable, keyed off the DetectorSpec/model
  family), not a tweak to the existing YOLO decoder.
- Pick a new default from the measured numbers; keep YOLOv8n as a fallback.
- Wire the choice through the adapter `DetectorSpec` (model family + input +
  labels) so it's declared, not hard-coded.

**Acceptance.** Measured accuracy+latency table on Pi 5 / N100; new default
justified by data (not vibes); decode validated on OpenCV 5's engine; existing
tests + a decode test per supported model family, all green.

**Notes / links.** RF-DETR is NMS-free and strong on small objects; YOLO26 adds
small-target-aware label assignment + INT8/FP16 ONNX export. Do NOT invent
benchmark numbers — measure on the real boxes before publishing any figure.

---

## 2. Accelerator backends — Coral / Hailo / RKNN (ORT EPs already shipped)

**Shipped in PR A:** the ONNX detector now has a **pluggable backend** —
`cvdnn` (default, zero-dep CPU) and `ort` (ONNX Runtime). ORT execution providers
already cover **OpenVINO** (Intel N100 iGPU/NPU), **TensorRT/CUDA** (Nvidia/Jetson),
and **CoreML** on the *same* ONNX model — install the matching wheel and set
`DETECT_ONNX_PROVIDERS`. See README "Pluggable inference backend."

**Remaining:** Coral (EdgeTPU) and Hailo are **not** ORT providers — they need
their own model format + SDK (TFLite+libedgetpu; HailoRT `.hef`), and Rockchip
needs RKNN. Add each as a backend on the same `build_backend` seam, ideally
dispatched through a KAI-C-backed adapter that declares its accelerator via the
v1.1 `DetectorSpec` (governed + audited region-crop dispatch). Also declare the
chosen backend/provider through `DetectorSpec` so it's config, not hard-coded.
Frigate-parity axis.

## 3. Real-inference smoke on OpenCV 5

Load the real `yolov8n.onnx` through `cv2.dnn` on OpenCV 5.0's new engine on a
host, run one inference on a known image, confirm boxes are correct (the suite
uses a fake net; CI has no model). Low risk (ENGINE_AUTO falls back to the
classic engine) but the one runtime path not yet verified end-to-end.

## 4. Tracker: Kalman motion prediction (Norfair-style)

The tracker is a lean greedy size-aware matcher. A Kalman/Norfair upgrade
improves ID stability under occlusion/fast motion. Optional quality follow-up.

## 5. Learned per-camera region grid

Frigate snaps regions to an 8×8 grid learned from historical true-positive
boxes. We defer it because it needs OpenNVR-side storage (Frigate uses its own
DB models). Improves small-object recall and region stability.

## 6. The gate itself → PR B — ✅ LANDED

Stationary-object gate, shadow mode (default), `always_analyze`, critical-class
force-escalate, heartbeat, per-track cooldown, and gate-decision audit (incl.
non-events on `opennvr.tier0.gate.v1`). Implemented in `gate.py`, authored against
`TriggerPolicy` (motion default). **Still owed:** on-hardware validation of the
measured miss rate before flipping `enforce`.

---

## 7. Pluggable Tier-0 triggers (`TriggerPolicy`) — keep the gate domain-agnostic

**Why.** OpenNVR's identity is "any model behind a governed adapter contract,
discovered in the registry, exported as a skill" — not object detection. Tier-0
today ships one trigger: `motion` + a small object detector (the correct CCTV
default). But if PR B's gate hardcodes "motion → object," it quietly re-narrows
OpenNVR into an object NVR (a Frigate) and throws away the bring-your-own-model
property. The real abstraction is: **cheap always-on trigger signal → gate →
registered expensive model → audited bus.** Object motion is just one instance.

**Scope.**
- Add a `TriggerPolicy` to the adapter contract (`CapabilitiesResponse`, next to
  `DetectorSpec` / `InputSpec` / `Accelerator`) so a model declares *what wakes
  it*: `motion` (default), `scene_change` (frame-delta / contour change —
  microscopy, structural change), `interval` (schedule — crop/vegetation survey,
  time-lapse), `field_statistic` (diffuse-motion / brightness / texture — wind,
  rain, fog, smoke), `chained` (another cheap model's output), `always`.
- **Author PR B's gate against the `TriggerPolicy` interface, not against
  motion.** This is the load-bearing obligation — see the "Tier-0 triggers are
  pluggable" section in `docs/design/compute-gated-inference.md`.
- Ship `motion` first (already have it). The non-object trigger *signals*
  (`scene_change`, `field_statistic`, `interval`, ...) are their own follow-ups;
  this item is about the *abstraction* so the door stays open.

**Acceptance.** Contract carries `TriggerPolicy`; PR B's gate dispatches by
declared trigger, not a hardcoded motion assumption; a non-object trigger (e.g.
`interval` or `field_statistic`) can be added later without touching the gate
core. Note: `field_statistic` for weather is a weak *primary* signal — pixels are
a poor wind sensor; drive weather from a sensor/API and use the camera frame +
Tier-1 VLM as visual corroboration.

---

## 8. Wire consumers (camera-agent + apps) to Tier-0 events — PR B era

**Context (decided).** There is **no new "shareable perception" contract.** The
existing NATS bus + adapter contract + the `opennvr.tier0.v1` event schema (see
`README.md` → *Event schema*) *is* the sharing mechanism. Apps get the PR A/PR B
benefit by consuming these events — not by a new framework. Only **frame/stream
models** are in scope (object detectors, VLM captioners, face, plate, pose). STT
(Whisper), TTS (Piper/Ultravox), and the reasoning LLM (Qwen) do **not** touch
camera frames and are explicitly **out of scope** for this optimization (the LLM
still benefits *indirectly* — fewer/cheaper VLM tool-calls, cleaner inputs).

**Do this once PR B (the gate) exists — app-side wiring, not a platform contract.**
- **Camera-agent:** let relevant skills consume Tier-0 events / read the latest
  result, and **gate their VLM calls** off them instead of always firing a fresh
  on-demand inference. Answer metadata-only questions ("is anyone at the door?",
  "how many cars?") straight from Tier-0 tracks with **no VLM call**. Feed the VLM
  the Tier-0 **best frame/crop** when a call is actually needed.
- **Apps / dashboards / alert relays:** subscribe to
  `opennvr.inference.tier0.<camera>.completed` for cheap, always-on events.
- **Verification-driven:** if a given app does **not** get the benefit, treat it as
  a **wiring gap in that app** (not subscribing, or firing fresh inference) and fix
  the wiring — do not add a contract.

**Acceptance.** At least one camera-agent skill demonstrably answers from Tier-0
metadata with no VLM call, and gates its VLM on Tier-0 events; existing behavior
unchanged when the flag is off. Each change additive, opt-in, reversible.

---

## 9. Tier-0 metrics + measurement harness — ✅ LANDED (numbers owed on hardware)

**Done:** `metrics.py` exposes Prometheus `/metrics` (`DETECT_METRICS_PORT`, dep-free)
with the `tier0_*` + `gate_*` counters below; `bench.py` is the baseline-vs-gated
harness (`python -m detect_pipeline.bench`). **Also landed since:**
- **Process CPU/RAM** (`tier0_process_cpu_percent`, `tier0_process_resident_memory_bytes`)
  sampled from `/proc` on each scrape — the *same shape* the AI adapters already
  export, so the app can show detect-pipeline resource use beside them.
- **Expensive-model (Tier-1) call attributes** (land with #10): `tier1_dispatch_total`,
  `tier1_dispatch_errors_total`, `tier1_dispatch_dropped_total` (all `{camera,adapter}`),
  `tier1_dispatch_inflight` (gauge), `tier1_dispatch_latency_seconds{adapter}`
  (histogram). These are *how often* the costly path runs and *what each call costs*.

**Still owed:** run the harness on the real clip set / Pi 5 / N100 and record the
actual reduction factor, miss rate, and capacity — never invented. Surfacing these
in the OpenNVR app UI is #11 below.

**Why (original).** The detect-pipeline used to emit **logs only**; there was no way
to quantify PR A's efficiency or PR B's savings. KAI-C already exposes `/metrics`
(`adapter_infer_latency_seconds` p50/p95/p99 + counts) and an `audit.jsonl`, so
the *expensive* tier is measurable — but Tier-0 is a blind spot, and the gate's
miss-rate has no instrument until shadow mode exists. "Improvement" is only
provable as a **baseline (always-on / no-gate) vs. gated** comparison on the same
input.

### 9a. detect-pipeline `/metrics` (Prometheus; mirror KAI-C's pattern)

Expose a small `prometheus_client` endpoint from the service (optional/guarded,
like everything else). Names (labels in `{}`):

```
# throughput / motion-gate effectiveness (the core PR A story)
tier0_frames_total{camera}                      counter  # frames read from source
tier0_detector_runs_total{camera}               counter  # detector actually invoked
tier0_detector_skipped_total{camera,reason}     counter  # reason = calibrating | no_motion
tier0_regions_per_frame{camera}                 histogram# regions sent to detector / active frame
# results
tier0_detections_total{camera,label}            counter
tier0_tracks_started_total{camera}              counter
tier0_tracks_active{camera}                     gauge
tier0_events_published_total{camera}            counter  # NATS events emitted
# latency (mirror adapter_infer_latency_seconds naming)
tier0_stage_latency_seconds{camera,stage}       histogram# stage = decode|motion|region|detect|track
tier0_frame_latency_seconds{camera}             histogram# end-to-end per frame
# health / cost
tier0_worker_up{camera}                         gauge
tier0_restarts_total{camera}                    counter  # ffmpeg restarts
# plus prometheus_client process collector: process_cpu_seconds_total,
# process_resident_memory_bytes  -> CPU/RAM per worker process
```

**Derived KPIs (PR A):** motion-gate ratio = `detector_skipped / (runs + skipped)`;
CPU-per-camera (idle vs active); frames/s; end-to-end latency.

### 9b. Gate metrics (land with PR B / shadow mode)

```
gate_escalations_total{camera,model,reason}     counter
gate_suppressions_total{camera,reason}          counter
gate_decision_latency_seconds{camera}           histogram
# shadow mode = the miss-rate instrument (log-only gate beside the always-on pipeline)
gate_shadow_would_suppress_total{camera}        counter
gate_shadow_missed_total{camera}                counter  # would-suppress that actually mattered
```

**Derived KPIs (PR B):** expensive-call **reduction factor** = baseline calls /
gated calls (baseline calls come from KAI-C `adapter_infer_latency_seconds` count
+ `audit.jsonl`); **miss-rate** = `gate_shadow_missed / events_that_mattered`.

### 9c. Benchmark harness (the baseline-vs-gated comparison)

- A **fixed, documented clip corpus** (busy + idle segments; a labelled event set
  for recall/precision). Reproducible; committed or referenced, never ad-hoc.
- Run the *same* corpus under two configs: **A = baseline** (always-on full
  inference, no gate) and **B = Tier-0 + gate**. Collect Tier-0 `/metrics`, KAI-C
  call counts/latency, process CPU/RSS.
- On the **target hardware** (Pi 5, Intel N100): also record max sustained cameras
  @ target fps and wall energy (external meter / RAPL — out of software scope, but
  documented). **Never invent these numbers — measure on the real boxes.**
- Emit a comparison table (compute saved, call-reduction factor, miss-rate,
  cameras/box). This *is* Paper 01's evaluation section — build once, use twice.

### 9d. Wire the scrape (optional compose profile)

KAI-C already speaks `/metrics`; flip MediaMTX `metrics: yes`; add an optional
`prometheus` + `grafana` compose profile so all three are scrapeable. Opt-in, off
by default.

**Acceptance.** detect-pipeline `/metrics` scrapeable with the counters above; a
documented, reproducible benchmark producing a **baseline-vs-gated** table on real
Pi 5 / N100 hardware; no invented figures. Feeds both product observability and
the Paper 01 evaluation + the hardware deployment guides.

---

## 10. Tier-1 dispatch — close the loop (run the expensive model on escalations) — ✅ v1 LANDED

**Done (v1, flag-gated off).** `dispatch.py`: a declarative `DispatchRouter`
(caption default on person/vehicle; face/plate opt-in; custom = a row) + an
injectable `KaicDispatcher` that runs the routed adapter via KAI-C's governed
`POST /api/v1/infer/{adapter}` on the track's **retained best-frame crop**
(base64), async + best-effort + concurrency-capped. Wired into the worker
(enforce-only). Enable with `DETECT_DISPATCH_KAIC_URL`. **Still owed:** validate
shadow miss-rate on hardware before enabling; optional `opennvr://` URI handoff
instead of base64; per-trigger (not just per-class) routing keys.

**The gap (original).** PR B's gate *decides* which tracks are worth the expensive tier and
*audits* every decision, but it does **not** run anything. Even in `enforce`,
`GateResult.to_dispatch()` returns the escalations and nobody consumes them — so
today the gate emits decisions + audit records, and no VLM/face/plate is actually
invoked. This is the piece that turns "measured savings" into *delivered* savings.
(Deliberately out of PR B — it crosses the detect-pipeline↔KAI-C boundary and
needs the best-frame pixels; PR B is the decision + audit + measurement layer.)

**Scope.**
- A consumer (in KAI-C, or a small dispatcher) subscribes to gate escalations on
  `opennvr.inference.tier0.<camera>.gate` (`escalate: true`), and for each one runs
  the declared **expensive adapter** (VLM caption / face / plate) **through KAI-C**
  — governed, sovereignty-checked, audited — **once, on the best frame**, then
  publishes the Tier-1 result to the bus for the camera-agent/apps.
- **Best-frame pixel handoff (the real design piece):** Tier-0 already picks the
  best frame per track (`track.best`, a `ThumbCandidate` = label/box/score) but does
  **not** retain the *pixels*. Closing the loop needs the actual best-frame image
  (or a reference/URI to it) for the escalated track — align with KAI-C's existing
  frame-slicer / `opennvr://` RAM-disk URIs so the crop is passed, not the whole
  frame, and nothing extra is decoded.
- Only in `enforce` mode; `shadow`/`off` dispatch nothing (unchanged).
- Rate-limiting is already handled by the gate's cooldown — the dispatcher just
  acts on escalations as they arrive.

**Model-agnostic routing (do NOT hardcode the mapping).** The "which expensive
model runs" is a **declarative map**, keyed on trigger/class → adapter — never a
`switch`. The caption default is one editable row, not a special case:
- **Default (zero config):** `person → caption`, `vehicle → caption` (BLIP/moondream).
  A light, non-biometric default useful everywhere.
- **Opt-in rows (documented, not default):** `person → face`, `car → plate` — heavier
  and privacy/jurisdiction-sensitive, so sovereign-by-default = off unless enabled.
- **Custom models add a row**, not code: a custom adapter declares its trigger (via
  the contract / `trigger-policies.md`) and adds a routing rule; its escalations then
  run *it* through KAI-C, governed.
- **Honour the trigger mode** — the same flexibility as the per-camera `analyze=false`
  ("no crop/no pipeline on this stream"), but at the model level:
  `TriggerPolicy.none` = the adapter is *not* gated by Tier-0 (self-gates / raw stream);
  `always` = run every frame; a **custom trigger** = wake on the model's own cheap
  signal. The dispatcher must respect these, so a model is fully in control of its
  own gating.
- **All of it documented** (routing map + how to add a rule + how to add a custom
  trigger + `none`/`always` semantics) in `docs/design/trigger-policies.md` and the
  detect-pipeline README — a new model/skill is a config addition, never a core edit.

**Acceptance.** In `enforce`, a gate escalation causes exactly one governed,
audited run of the **routed** adapter on the best crop via KAI-C (default caption on
person/vehicle; face/plate opt-in), and the result lands on the bus; `shadow`/`off`
still run zero expensive inferences. A custom adapter with its own trigger + routing
row works without touching the gate core; `TriggerPolicy.none`/`always` are honoured.
This completes the end-to-end flow drawn in the architecture diagram (Gate →
expensive models → results), of which PR B ships the Gate + audit + measurement.

---

## 11. Surface Tier-0 / gate / Tier-1 metrics in the OpenNVR app (no runbook needed)

**Why.** Today the compute-gated numbers only exist at the detect-pipeline
`:9109/metrics` text endpoint — so validating shadow mode means curling a port or
running `bench.py` by hand (see `ENABLEMENT.md`). That's fine for a bring-up, but
it shouldn't be the *ongoing* way to see the win. The app **already** scrapes and
charts the AI adapters' `/metrics` (KAI-C polls each adapter every 60s →
`/api/v1/adapters/{name}/metrics` → `app/src/views/AIAdapters.tsx` renders CPU%
sparklines). The detect-pipeline emits the **same Prometheus shape** — including
`tier0_process_cpu_percent` / `tier0_process_resident_memory_bytes` in the exact
form the adapter cards already use — so surfacing it in-app is *wiring*, not a
redesign. Then a user compares before/after (gate off → shadow → enforce) in the
same UI where CPU/RAM already live, instead of reading a runbook.

**Scope (app-side wiring, additive).**
- **Scrape the detect-pipeline endpoint** the same way adapters are scraped: KAI-C
  (or the core backend) polls `:9109/metrics`, exposes it at a stable API path
  (e.g. `/api/v1/tier0/metrics`), cached like the adapter poll. No new format.
- **A "Compute-gated" panel** (its own card, or a section on the camera/AI view) showing:
  - **Resource use:** `tier0_process_cpu_percent` + `tier0_process_resident_memory_bytes`
    — the same sparkline component the adapter cards use, side-by-side with the
    expensive adapters so the trade is visible in one place.
  - **The gate working:** motion-gate ratio (`tier0_detector_skipped_total` /
    runs+skipped), `gate_escalations_total` vs `gate_suppressions_total`, and in
    shadow, `gate_shadow_would_suppress_total` (the risk-free "what we'd save").
  - **Expensive-call attributes:** `tier1_dispatch_total` (calls the gate actually
    fired), `tier1_dispatch_latency_seconds` (p50/p95 — what each costs),
    `tier1_dispatch_dropped_total` (backpressure), `tier1_dispatch_inflight`.
- **Optional richer feed via NATS:** the gate already audits every decision to
  `opennvr.inference.tier0.<cam>.gate`; the app could also live-tally escalate/suppress
  from that subject for a real-time view without waiting on the 60s poll.
- Everything read-only; nothing new to configure; shows nothing (or "gate off")
  when the pipeline isn't running.

**Acceptance.** A user can watch the gate's effect — CPU/RAM, motion-gate ratio,
escalations vs suppressions, and expensive-call count/latency — **in the app**,
next to the existing adapter CPU charts, across gate `off → shadow → enforce`,
without running `bench.py` or curling `/metrics`. `ENABLEMENT.md`'s manual steps
become the *bring-up* path, not the everyday one.
