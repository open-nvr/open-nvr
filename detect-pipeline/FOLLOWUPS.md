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

## 2. KAI-C accelerator detector adapter (Coral / Hailo / OpenVINO / TensorRT)

The local `cv2.dnn` detector is **CPU**. The production win on cheap hardware is
running the model on an accelerator (OpenVINO on the N100 iGPU, Coral, Hailo, the
Pi 5 AI HAT, TensorRT on Nvidia). Land as a KAI-C-backed detector adapter that
declares its accelerator via the v1.1 `DetectorSpec`; the pipeline dispatches
region crops to it through KAI-C (governed + audited). Frigate-parity axis.

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

## 6. The gate itself → PR B

Stationary-object gate, shadow mode, per-camera `always_analyze`, critical-class
force-escalate, gate-decision audit. The whole point of Tier-0; PR A is the
always-on floor it sits on.

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
