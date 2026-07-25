# Enabling compute-gated inference — rollout runbook

Everything ships **off by default** so it can never regress a deployment. This is
the staged path to actually *get the benefit* (fewer expensive inferences, more
cameras per box) — and the settings to lock in once you've validated it on your
own hardware. **Never enable straight to `enforce` — measure in `shadow` first.**

## The ladder: `off → shadow → enforce → +dispatch`

### 0. `off` (default) — Tier-0 only
```
DETECT_PIPELINE_ENABLED=true
DETECT_GATE_MODE=off
```
Always-on detection + events on the bus. No gating. This is where you confirm
Tier-0 itself runs at target fps on your hardware.

### 1. `shadow` — measure, risk-free
```
DETECT_GATE_MODE=shadow
DETECT_METRICS_PORT=9109
```
The gate computes and audits every escalate/suppress decision but **runs no
expensive model**. Let it run on real cameras for a representative period, then:

- **Read the metrics.** During bring-up, curl `:9109/metrics` or run
  `python -m detect_pipeline.bench` on a labelled clip set. The endpoint also emits
  process CPU/RAM (`tier0_process_cpu_percent`, `tier0_process_resident_memory_bytes`)
  and the expensive-call attributes (`tier1_dispatch_total`,
  `tier1_dispatch_latency_seconds`, …) in the same Prometheus shape the AI adapters
  use — so **surfacing them in the OpenNVR app, next to the existing adapter CPU
  charts, is the intended everyday view** (FOLLOWUPS #11); the curl/bench path is for
  the initial validation, not something you run forever. Look at:
  - **expensive-call reduction** = would-be gated calls vs always-on baseline
    (`gate_escalations_total` vs tracks-per-frame) — the compute you'll save.
  - **miss rate** = `gate_shadow_would_suppress_total` cross-checked against your
    labelled events — the risk.
  - **motion-gate ratio** = `tier0_detector_skipped_total / (runs + skipped)`.
- **Acceptance is your call, not an invented number.** Enable `enforce` only when
  the **miss rate on your labelled events is acceptable for your use case** and the
  reduction is worth it. If misses are too high, raise recall (lower thresholds,
  add `heartbeat`, add `critical_classes`) and re-measure — do not enforce yet.

### 2. `enforce` — act on the decisions
```
DETECT_GATE_MODE=enforce
DETECT_GATE_HEARTBEAT_S=30        # hard latency floor: a full pass at least this often
DETECT_GATE_COOLDOWN_S=30         # re-look at the same track at most once / 30s
DETECT_GATE_CRITICAL_CLASSES=person   # (example) always escalate these
```
The gate now suppresses redundant work. Still no expensive model runs *unless* you
also enable dispatch (next) — enforce alone just stops the always-on-everything
pattern.

### 3. `+dispatch` — close the loop (run the gated model)
```
DETECT_DISPATCH_KAIC_URL=http://kai-c:8100   # governed run via KAI-C
DETECT_DISPATCH_TASK=caption
```
Now an escalation actually runs the routed adapter (default **caption** on
person/vehicle; `face`/`plate` are opt-in) **once, on the best frame**, through
KAI-C — governed, audited, published to the usual adapter subject. Apps/agents
consume it unchanged.

## Recommended production posture (once validated)
```
# .env — validated compute-gated deployment
DETECT_PIPELINE_ENABLED=true
DETECT_DETECTOR=onnx
DETECT_ONNX_BACKEND=ort               # + the accelerator wheel/provider for your box
DETECT_ONNX_PROVIDERS=OpenVINOExecutionProvider,CPUExecutionProvider   # (N100 example)
DETECT_HWACCEL=vaapi                  # match your hardware

DETECT_GATE_MODE=enforce              # after shadow validation
DETECT_GATE_HEARTBEAT_S=30
DETECT_GATE_COOLDOWN_S=30
DETECT_GATE_CRITICAL_CLASSES=person

DETECT_DISPATCH_KAIC_URL=http://kai-c:8100
DETECT_DISPATCH_TASK=caption
DETECT_METRICS_PORT=9109
```
Per-camera overrides (set via the camera provider, not env):
- **Critical cameras** (gate, cash room, perimeter): `always_analyze=true` — the gate
  is disabled there, full pipeline every frame.
- **Quiet cameras**: leave gated; they get the biggest saving.

## Invariants that never change
- **Recording is never gated** — MediaMTX records regardless of any of this.
- **Roll back instantly** — set `DETECT_GATE_MODE=shadow` (or `off`) to stop
  acting/dispatching; no redeploy of the rest of the stack.
- **Metrics stay on** so you keep seeing the reduction/miss numbers in production.

## Confirming the benefit in production
After enforce+dispatch, expect on the metrics: `gate_escalations_total` ≪
tracks-processed (fewer expensive calls), lower CPU per camera, and more cameras
sustained per box. Record the real before/after numbers — they feed the hardware
guides and the paper. **Do not publish figures you haven't measured on the box.**
