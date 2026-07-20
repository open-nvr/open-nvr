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

**Phase 1 — gate + safety rails (ship together, never separately):**
1. Tier 0 always-on detector in KAI-C; Tier 1 dispatch gated on Tier-0 hits.
2. Per-camera sensitivity + "always analyze" override.
3. Critical-class force-escalate.
4. Heartbeat pass.
5. Shadow / calibration mode with the metric above.
6. Gate-decision audit records (score + threshold, per skip).

**Phase 2 — payoff:**
7. Natural-language standing rules compiled to primitives.
8. Dynamic FPS / compute reinvestment.
9. Adapter cost-tier + trigger conditions in the manifest.

Phase 1 items 1–6 are a single unit: the gate must not ship without the rails
that make it safe.

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
