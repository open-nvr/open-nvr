# Authoring a trigger policy

> How to control *when* OpenNVR spends expensive compute on a stream, and how to
> add a new way to decide that. Companion to
> [`compute-gated-inference.md`](./compute-gated-inference.md) — read the
> "Tier-0 triggers are pluggable" section there first.

## What a trigger policy is

Compute-gated inference is one idea:

```
  cheap always-on TRIGGER  ->  GATE (policy)  ->  registered EXPENSIVE model  ->  audited bus -> agent acts
```

A **trigger policy** is the thing on the left that answers *"is this moment worth
waking the expensive model?"*. Object motion is the default one (security
cameras), but the set is **open** — a new model can bring a new trigger. We don't
know how many models will come or what will wake them, so the trigger is a
*declared capability*, never a hardcoded assumption.

A trigger has two separable parts. Keeping them separate is what makes the set
extensible:

1. **A signal producer** — the cheap, always-on computation that emits features
   (a motion field, a frame-delta, a scalar statistic, a schedule tick, another
   model's cheap output, a robot's own sensor reading).
2. **A predicate** — given those features plus context, decide **escalate?**.

## The built-in policies (the starter kit, not a whitelist)

| Policy | Cheap signal it runs on | Typical use |
|---|---|---|
| `motion` *(default)* | object motion + a small detector | security / CCTV |
| `scene_change` | frame-delta / contour change | microscopy, structural change |
| `interval` | a schedule (every N min/hours) | crop / vegetation survey, time-lapse |
| `field_statistic` | diffuse-motion / brightness / texture statistic | wind, rain, fog, smoke |
| `chained` | another (cheaper) model's output | "maybe-interesting" -> confirm |
| `always` | *(no gate — run every frame)* | domains that require every frame |
| `none` | *(pass-through — the model self-gates)* | model does its own gating internally |

`always` and `none` are legitimate values, not edge cases. Pick a built-in by
setting the model's declared `TriggerPolicy` (see the contract, alongside
`DetectorSpec` / `InputSpec` / `Accelerator`).

## Defining a NEW trigger — two levels

Choose the lowest level that expresses your trigger. Level 1 is safer and
auditable by construction; reach for Level 2 only when you need a signal the
platform doesn't already compute.

### Level 1 — declarative custom trigger (config, no code)

Write a predicate over features Tier-0 already exposes. No arbitrary code runs, so
it is sandboxable, cheap, and fully auditable (the exact reason it fired is
loggable).

Features available to the expression (indicative):

- `motion_fraction` — share of the frame in motion (0..1)
- `num_tracks`, `class_present("person")`, `zone_hit("porch")`
- `region_delta` — frame-to-frame change score
- `elapsed_since_last` — seconds since this model last ran on this camera
- any **named custom signal** a trigger adapter publishes (see Level 2)

Illustrative declaration (shape, not final schema):

```yaml
trigger:
  name: loading-bay-after-hours
  when: "class_present('truck') and zone_hit('bay') and elapsed_since_last > 30"
  cost: cheap          # required: it runs always-on
  audit: true          # fired AND suppressed decisions go to the bus
```

### Level 2 — trigger adapter (a plugin, for a genuinely new signal)

When your trigger needs a signal the platform doesn't compute — a robot's depth+IMU
fusion, a novelty detector, a bespoke measurement — ship a small **trigger
adapter**. It is a cheap always-on component that implements a minimal contract
and is **declared in the contract, discovered in the registry, exported as a
skill, and governed** exactly like a model adapter.

Minimal contract:

```
input :  a frame (or the features/sensor bundle it needs)
output:  { escalate: bool, score: float, reason: str, signals?: {name: value} }
```

Anything it puts in `signals` becomes a named feature that Level-1 declarative
triggers can then reference — so a plugin can *add vocabulary* the config layer
reuses.

## Rules every trigger must follow

These are non-negotiable regardless of level, because a trigger decides both when
compute is spent and what is recorded as an event:

- **It must be cheap.** It runs on every frame/tick. Declare its `cost`; a trigger
  that is as expensive as the model it gates defeats the purpose.
- **Its decisions are audited.** Both *fired* and *suppressed* land on the bus —
  this is what gives OpenNVR its "audit of non-events" property. A custom trigger
  inherits it for free.
- **The safety floor is independent of the trigger.** `recording never gated`,
  per-camera `always_analyze`, the `heartbeat` pass, and critical-class
  force-escalate all hold no matter what a trigger decides. A buggy custom trigger
  cannot silently blind the system.
- **The author owns correctness and tuning.** The platform guarantees governance,
  auditing, cost accounting, and the safety floor. It cannot validate that your
  bespoke novelty-detector is well-tuned — state that, and version the trigger.

## Worked example — a research/observation robot

A robot doing field observation has no "person/vehicle" events. It defines its own:
*"a specimen I have never catalogued appeared," "two tracked specimens came within
X cm," "a measurement crossed a threshold."*

- The robot's model declares a **Level-2 trigger adapter** ("novelty + proximity")
  that runs cheaply on each frame and emits `escalate` plus `signals:
  {novelty_score, min_pair_distance}`.
- Optionally, task operators layer **Level-1 declarative triggers** on top:
  `when: "novelty_score > 0.8 or min_pair_distance < 5"`.
- When it fires, the gate dispatches the **expensive** analysis model; the result —
  and every suppressed non-event — is governed and audited on the bus, and the
  agent acts.

The robot author never touches the gate core. They declare a trigger and a model;
OpenNVR runs, governs, and audits both. That is the property that keeps
compute-gating faithful to "bring your own model" — for cameras, microscopes,
crop surveys, weather, or robots we haven't imagined yet.

## From trigger to model — routing the dispatch

A trigger decides *when* to spend the expensive tier; **routing** decides *which*
model runs. Keep them separate and both declarative — that is what keeps the
system model-agnostic. (Implementation tracked in `FOLLOWUPS.md #10`.)

**The routing map** — escalations route to adapters by a small, editable map keyed
on the trigger / detected class, never a hardcoded `switch`:

```yaml
# illustrative, not final
dispatch:
  default: caption               # zero-config: person/vehicle -> caption (BLIP/moondream)
  rules:
    - when: {class: person},  run: [caption]   # face is opt-in (below)
    - when: {class: vehicle}, run: [caption]   # plate is opt-in
    # opt-in — heavier + privacy/jurisdiction-sensitive, OFF unless enabled:
    # - when: {class: person}, run: [face]
    # - when: {class: car},    run: [plate]
    # a CUSTOM model is just another row, keyed on its OWN trigger:
    - when: {trigger: scene_change}, run: [my-microscopy-adapter]
```

- **Default (zero config):** caption on `person`/`vehicle` — light, non-biometric.
- **Opt-in rows:** `face`, `plate` — heavier and privacy/jurisdiction-sensitive, so
  sovereign-by-default = off until an operator adds the row.
- **Custom model = a new row**, keyed on its own trigger; its escalations run *it*
  through KAI-C (governed) — never a core edit.

**A model controls its own gating** — the same flexibility `analyze=false` gives a
*stream*, but per *model*:

| Intent | Setting |
|---|---|
| don't run Tier-0 on this **stream** at all | `analyze=false` (per camera) |
| this **model** is not gated by Tier-0 (self-gates / raw stream) | `TriggerPolicy.none` |
| this **model** runs every frame | `TriggerPolicy.always` |
| this **model** wakes on its own cheap signal | a custom trigger (Level 1 / 2 above) |

So a model author is never boxed in: *gate me on motion, on my own signal, always,
or not at all* — and route my escalations to my own adapter. The gate core assumes
none of it.

## Status

Design-level. Tier-0 ships one trigger today — `motion` + object detection (the
CCTV default). Building the `TriggerPolicy` field on the contract and authoring
PR B's gate against this interface (not hardcoded motion) is tracked in
`detect-pipeline/FOLLOWUPS.md` (item 7). The non-object signal producers and the
trigger-adapter plugin mechanism are their own follow-ups; this guide fixes the
*shape* so they can be added without touching the gate core.
