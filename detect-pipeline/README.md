# detect-pipeline (OpenNVR Tier-0)

Always-on, low-risk detection for [compute-gated inference](../docs/design/compute-gated-inference.md).
Pulls a camera **substream from MediaMTX**, hardware-decodes it, and runs
`motion → region select → cheap detector → tracker → best-frame`. **Nothing is
gated here** (the gate + shadow mode are PR B); recording stays MediaMTX's job
and is never touched.

Ported CV logic (motion, region geometry, best-frame, tracker metric) is derived
from Frigate `6f80bcd19` (MIT) — see [`NOTICE`](NOTICE).

## Install

```bash
cd detect-pipeline
pip install -e .          # numpy + opencv-python-headless
```

## Run the tests

```bash
python -m pytest -q       # 92 unit/integration/smoke tests (real-ffmpeg tests run if ffmpeg present, else skip)
```

## Manual verification

Run the **real pipeline** on a video file (or RTSP URL) and eyeball an annotated
MP4 — motion boxes (yellow), detector regions (blue), tracked objects with IDs
(green), and a CALIBRATING banner while motion warms up.

```bash
# real object detection (YOLOv8 ONNX) — point at a model
python -m detect_pipeline --source people.mp4 --out annotated.mp4 --detector onnx --model yolov8n.onnx

# quick "is the whole chain alive?" on any clip with motion — no model, any OpenCV (default)
python -m detect_pipeline --source clip.mp4 --out annotated.mp4 --detector blob

# straight off a real OpenNVR-provisioned camera (production ffmpeg path,
# hwaccel decode, auto-probed resolution, bounded to 20s)
python -m detect_pipeline \
  --source "rtsp://<user>:<pass>@<camera-ip>:554/<substream-path>" \
  --out out.mp4 --detector onnx --model yolov8n.onnx --hwaccel vaapi --seconds 20
```

(`--detector hog` also works on **OpenCV 4.x only** — HOG was removed in OpenCV 5.)

### Against a real camera

`rtsp(s)://` sources use the **production ffmpeg FrameSource** (hardware decode,
TCP transport, auto-restart) — not OpenCV — so this run validates the real decode
path. Resolution is auto-probed with `ffprobe` (override with `--width/--height`).

Fastest first test — point at the **camera's own substream** directly
(plaintext, no TLS): use the camera's low-res RTSP URL (Dahua/CP-Plus
`subtype=1`, Hikvision/Uniview `…/N02`), with credentials in the URL.

For the **MediaMTX republish** (the production single-connection path): MediaMTX
serves operator RTSP as `rtsps://` on `127.0.0.1:8322` (localhost, basic auth,
TLS cert required) — run this on the OpenNVR host and pass the rtsps URL with
credentials.

Pick `--hwaccel` for the host: `vaapi` (Intel/AMD), `nvidia`, `qsv`, `rpi`,
`rkmpp`, `jetson`, or `cpu`.

It prints a summary, e.g.:

```
processed 300 frames @ 640x360 | detector=hog | unique tracks=4 | max concurrent=2 | wrote annotated.mp4
```

Open the annotated MP4 to confirm: motion only fires on real movement, the
detector runs on regions (not the whole frame), track IDs stay stable as objects
move, and the banner shows the detector standing down during lighting changes.

Detectors: `onnx` (YOLOv8/YOLO11 via cv2.dnn — the production default; pass
`--model yolov8n.onnx`), `hog` (people, no model download; the CLI default for a
zero-asset demo), `blob` (deterministic, for smoke checks), `stub` (motion +
regions only).

## As an OpenNVR service (integrated)

Ships as the `detect-pipeline` container in `docker-compose.yml`. It's an
**additive consumer**: it discovers cameras from opennvr-core's existing internal
endpoint, pulls the **same MediaMTX tap** the camera-agent uses (OpenNVR stays
the owner of the single camera connection), runs Tier-0 per camera, and publishes
detections to the existing `opennvr.inference.tier0.<camera_id>.completed` NATS
subjects. It changes nothing about ingest, recording, or serving.

**On by default** in the full stack. The camera-agent-lite overlay puts this
service behind the `tier0` compose profile instead — the lite family is fully
CPU-bound and co-running an uncapped detector starves it — so the lite
quickstart does not start Tier-0; add `--profile tier0` to run both.
**Why is my CPU high? → check for a pinned stationary object, lower
`DETECT_FPS`, then configure the substream.** As of stationary-track
gating, an object that stops moving (parked car, person sitting still)
is re-verified every `DETECT_STATIONARY_INTERVAL`-th frame (default 10,
staggered per object; motion touching it re-checks immediately) instead
of feeding the detector every frame — an idle scene with parked objects
now costs decode+motion, not inference. If CPU is still high on a still
scene, look for perpetual motion sources: a camera-OSD timestamp burned
into the substream ticks every second and defeats motion gating — turn
the overlay off in the camera, or crop/mask it.

**Worst case is bounded by design.** A cluttered scene (a desk of wires
and boards) once drove the field failure this section exists for: at
confidence 0.25 across all 80 COCO classes, yolov8n hallucinated
"kite"/"banana" phantoms that confirmed into 181 standing tracks, and
re-verifying that population cost ~30 s of detector per frame. Four
guards now keep the worst case flat regardless of scene content, each
with an env dial and a metric (never a silent cap):

* `DETECT_LABELS` (default `person,car,truck,bus,motorcycle,bicycle,cat,dog`)
  — only these classes are tracked; `all` restores every COCO class.
* `DETECT_CONF` (default `0.4`) — detector confidence floor, plus
  `DETECT_MIN_SPAWN_SCORE` (default `0.5`): it takes solid evidence to
  *create* a track, weaker evidence still *matches* one (Frigate-style
  hysteresis).
* `DETECT_MAX_REGIONS` (default `8`) — hard per-frame budget of detector
  crops. Motion regions win slots first; track re-verifies round-robin
  the remainder across frames (skipped tracks coast, they don't age).
  Capped frames count on `tier0_regions_capped_total`.
* `DETECT_MAX_TRACKS` (default `50`) + `DETECT_TRACK_TTL` (default
  `300` s) — a hard ceiling on live tracks, and a wall-clock TTL so a
  track that is never positively re-detected always drains (frame-based
  miss counting stalls exactly when the pipeline is overloaded). Refused
  spawns show on `tier0_track_spawns_dropped`.

**Second dial: lower `DETECT_FPS`.**
Detection runs on every analyzed frame (the gate skips alarms, not
inference), so pipeline CPU scales almost linearly with the per-camera
analysis rate — `DETECT_FPS`, default 5. On CPU-only hosts (laptops; any
macOS/Windows Docker install, where the VM has no GPU) set `DETECT_FPS=1`
or `2`: a fraction of the CPU, at the cost of coarser motion/track
granularity and a smaller candidate pool for best-frame selection. An
explicit per-camera `fps` from the discovery endpoint still wins.

**Third dial: configure the substream.** Tier-0 decodes each
camera's *substream* (a low-res second stream every mainstream camera
provides). If a camera has no substream configured, Tier-0 falls back to
decoding the full main stream — that is the difference between ~0.3 and ~2
CPU cores per camera, and decode (not detection) is where the cost lives.
The service warns per camera at startup and exposes
`tier0_mainstream_fallback{camera=...}` on `/metrics` whenever it is on the
expensive path. Fix: set the camera's substream URL in OpenNVR (Camera
settings), or lower the main stream's resolution.

**To turn the measurements into savings** (enforce + Tier-1 dispatch), follow
the staged runbook in [ENABLEMENT.md](ENABLEMENT.md). Disable without a redeploy:

```bash
# .env
DETECT_PIPELINE_ENABLED=false     # container stays up but idle
DETECT_FPS=5                      # frames analyzed /s /camera (1-30) — the main CPU dial
DETECT_STATIONARY_INTERVAL=10     # re-verify stationary tracks every Nth frame (0 = every frame)
DETECT_DETECTOR=onnx              # onnx (YOLOv8, default) | hog | blob | stub
DETECT_ONNX_BACKEND=cvdnn         # cvdnn (zero-dep CPU, default) | ort (ONNX Runtime)
DETECT_ONNX_PROVIDERS=            # ort EPs, e.g. OpenVINOExecutionProvider (Intel N100)
DETECT_HWACCEL=vaapi              # + uncomment devices: /dev/dri in compose
DETECT_GATE_MODE=shadow           # shadow (default: measure only) | off | enforce (act) — see ENABLEMENT.md
DETECT_GATE_HEARTBEAT_S=0         # >0: force a periodic escalate even on static scenes
DETECT_GATE_CRITICAL_CLASSES=     # e.g. person,weapon — always escalate, bypass suppression
DETECT_GATE_COOLDOWN_S=30         # re-escalate the same track at most once per N seconds
DETECT_METRICS_PORT=9109          # Prometheus /metrics (0 to disable)
DETECT_CV_THREADS=2               # cv2 intra-op thread cap (0 = uncapped); compose also
                                  # exports OMP_NUM_THREADS with the same value
DETECT_DISPATCH_KAIC_URL=         # set to enable Tier-1 dispatch (#10); empty = off
DETECT_DISPATCH_TASK=caption      # default task for routed adapters
# always_analyze is per-camera (gate config), deliberately not a global env.
```

Entrypoint: `opennvr-tier0` (`detect_pipeline.run:main`). NATS is best-effort —
a broker outage never stops a worker — but it **must authenticate**: the compose
bus runs with `--auth $INTERNAL_API_KEY`, and the publisher connects with that
token (override with `NATS_TOKEN`). An unauthenticated connect is rejected
(`Authorization Violation`) and every event is dropped.

## Event schema (what it publishes)

Tier-0 reuses OpenNVR's **existing** inference bus — no new contract. Consumers
(the camera-agent, apps, dashboards) subscribe to the same subject convention
adapters already use:

```
opennvr.inference.tier0.<camera_id>.completed
```

Payload (`schema = opennvr.tier0.v1`), published only for frames that produced
tracks (a 5 fps stream of empty results would be bus noise — set
`publish_empty` to change):

```jsonc
{
  "schema": "opennvr.tier0.v1",
  "adapter": "tier0",
  "camera_id": "cam_3",
  "seq": 44120,                 // frame counter since the stream (re)started
  "ts": 1837.42,                // monotonic seconds when the frame was read
  "calibrating": false,         // motion detector still warming up / lighting flash
  "tracks": [
    { "id": 7, "label": "person", "score": 0.88,
      "box": [x1, y1, x2, y2],  // full-frame pixels
      "stationary": false,      // settled object (the PR B gate will suppress these)
      "best": true }            // a best-frame crop is fetchable for this track
  ]
}
```

This is the **stable slice consumers code against.** It is intentionally the whole
"shareable" surface — the existing NATS bus + this schema. Apps get Tier-0's
benefit by subscribing here; no separate perception contract is needed.

**Best frame.** When a track has `"best": true`, its best-frame crop (the sharpest /
largest / most-confident frame Tier-0 already selected) is fetchable at
`GET :<DETECT_METRICS_PORT>/best_frame?camera=<cam>&track=<id>` (JPEG; omit `&track=`
for the camera's most-recent best). A consumer that needs to run a vision model
should use this instead of grabbing an arbitrary live frame — more accurate, cheaper.

**For app authors**, the App SDK wraps both of these (`snapshot_from_event` for
counts/presence, `BestFrameClient` for the best frame) so they're one shared
implementation, not per-app glue — see the SDK's
`docs/tier0-consumption.md`. The camera-agent is the reference consumer (FOLLOWUPS #8).

## Detector

The default detector is **YOLOv8 ONNX** (`onnx_detector.py`), loading the same
`yolov8n.onnx` the stack builds for the `yolov8-adapter`. The decode (YOLOv8/YOLO11
`(1, 4+nc, N)` → transpose → NMS) is a pure, unit-tested function; the net/session
is injectable so tests need no model file.

### Pluggable inference backend — plug in an accelerator for more speed

The model runs through a **pluggable backend** (preprocessing + decode are shared,
so the backend is interchangeable):

- **`cvdnn`** *(default)* — OpenCV's `cv2.dnn`. Already a dependency, so **nothing
  extra to install**, runs anywhere. CPU only; the OpenCV 5 engine is much faster
  than 4.x. This is the zero-dependency floor.
- **`ort`** — **ONNX Runtime** with selectable *execution providers*. On the
  **same ONNX model**, this is the on-ramp to hardware acceleration:

| Hardware | Install | Select |
|---|---|---|
| Intel N100 / iGPU / NPU | `pip install onnxruntime-openvino` | `DETECT_ONNX_PROVIDERS=OpenVINOExecutionProvider` |
| Nvidia / Jetson | `pip install onnxruntime-gpu` | `DETECT_ONNX_PROVIDERS=TensorrtExecutionProvider,CUDAExecutionProvider` |
| Apple Silicon | `pip install onnxruntime` | `DETECT_ONNX_PROVIDERS=CoreMLExecutionProvider` |
| Any CPU | `pip install 'detect-pipeline[onnxruntime]'` | `DETECT_ONNX_BACKEND=ort` |

```bash
# .env — Intel N100 example
DETECT_DETECTOR=onnx
DETECT_ONNX_BACKEND=ort
DETECT_ONNX_PROVIDERS=OpenVINOExecutionProvider,CPUExecutionProvider
```

CPU is always appended as a fallback, so a missing accelerator provider degrades
instead of failing. CLI equivalent: `--backend ort --providers OpenVINOExecutionProvider`.

**Coral (EdgeTPU)** and **Hailo** are *also* backends on this same seam, but they
need their own model format + SDK (not ORT providers), so they land with the
KAI-C accelerator adapter — see `FOLLOWUPS.md`. **YOLO26 / RF-DETR** (NMS-free)
are a detector-family follow-up: they need a different decode, independent of the
backend chosen here.

## The gate (PR B)

The **gate** (`gate.py`) decides which Tier-0 tracks are worth the *expensive*
Tier-1 model — so the costly model runs **once per event on the best frame**, not
every frame. It is **off by default** (PR A behavior unchanged); `shadow` computes
and audits every escalate/suppress decision without enforcing (so you *measure*
the miss rate first); `enforce` actually gates.

> **Rolling it out?** Follow [`ENABLEMENT.md`](ENABLEMENT.md) — the staged
> `off → shadow → enforce → +dispatch` path, the validation criteria, and the
> recommended production flags once you've measured it on your hardware.

- **Safety rails:** `always_analyze` (disable the gate for critical cameras),
  `critical_classes` (always escalate), a `heartbeat` pass (bound worst-case
  latency), stationary suppression, and a per-track cooldown.
- **Audit of non-events:** every decision — escalations *and* suppressions — is
  published to `opennvr.inference.tier0.<camera>.gate` (`opennvr.tier0.gate.v1`).
- **Trigger-agnostic:** authored against `TriggerPolicy` (motion is the shipped
  default; scene-change/interval/… plug in on the same seam).

### Metrics & measuring the win

The service exposes Prometheus text at `:9109/metrics` (`DETECT_METRICS_PORT`):
`tier0_frames_total`, `tier0_detector_runs_total` vs `tier0_detector_skipped_total{reason}`
(the motion-gate ratio), `gate_escalations_total{reason}` / `gate_suppressions_total{reason}`,
`gate_shadow_would_suppress_total`, and `tier0_frame_latency_seconds`.

**Process resource use** (same shape the AI adapters already export, so the app can
show detect-pipeline CPU/RAM next to them): `tier0_process_cpu_percent`,
`tier0_process_resident_memory_bytes` — sampled from `/proc` on each scrape (Linux).

**Operator health — "is it running and keeping up?"** (the capacity signal for a
fixed box): `tier0_worker_up{camera}`, `tier0_processing_fps{camera}` vs
`tier0_target_fps{camera}` (a ratio well under 1 = the box can't keep up with that
camera), and `tier0_worker_restarts_total{camera}` (repeated feed restarts = an
unhealthy camera). Plus `tier0_stage_latency_seconds{camera,stage}` — per-stage time
(`decode|motion|region|detect|track`) so you can see *where* a frame's time goes, not
just the end-to-end total.

**Model-benchmarking signals** (compare two detectors of the same kind on speed +
output): `tier0_detector_latency_seconds{camera,model}` (pure detector inference
time — region loop only, excluding decode/motion/track), `tier0_detector_runs_total{camera,model}`,
and `tier0_detections_total{camera,label}` (per-class output volume). `model` is the
detector identity — the onnx model file's basename (e.g. `yolov8n`) or an explicit
`DETECT_MODEL_ID`. Swap the model, keep the same clip, and the two `model` series are
directly comparable. **Accuracy** (mAP / precision-recall / miss-rate) is *not* a live
metric — production has no ground truth; get it from `bench.py --model-id` on a
labelled clip set (see #1/#9c) and record it beside these speed numbers.

**Expensive-model (Tier-1) calls** — the attributes of the AI calls the gate
dispatches: `tier1_dispatch_total{camera,adapter}` (calls issued),
`tier1_dispatch_errors_total{camera,adapter}`, `tier1_dispatch_dropped_total{camera,adapter}`
(shed under backpressure), `tier1_dispatch_inflight` (gauge), and
`tier1_dispatch_latency_seconds{adapter}` (histogram — how long the expensive model
takes). Together these show *how often* the costly path runs and *what it costs*.

The **benchmark harness** quantifies the expensive-tier saving — baseline
(always-on) vs gated — with a miss rate:

```bash
python -m detect_pipeline.bench --source people.mp4 --detector blob --model-id yolov8n --repeat 5
# [yolov8n] fps=… | frames=… | expensive calls: baseline=… gated=… (Nx fewer) | events=… missed=… (miss-rate …)
# [yolov8n] fps over 5 runs: mean=… std=…    ← --repeat reports variance so a small delta isn't noise
```

`--model-id` tags the row (A/B two builds of the same family); `--repeat N` reports
the Tier-0 throughput (fps) as mean ± std across runs.

Run it on your real clip set / hardware — **do not publish invented numbers**.

### Tier-1 dispatch (#10) — closing the loop

When enabled, an **enforce** escalation actually runs the expensive model: the
worker routes the track (declarative class→adapter map, `dispatch.py`) and sends
its **best-frame crop** to KAI-C's governed `POST /api/v1/infer/{adapter}` — once,
best-effort, concurrency-capped. KAI-C runs it (sovereignty + audit), and publishes
to the existing `opennvr.inference.<adapter>.<camera>.completed` subject, so
apps/agents consume it unchanged. **Off by default** — set `DETECT_DISPATCH_KAIC_URL`
to enable; it still only fires in `enforce` (`shadow`/`off` dispatch nothing).

- **Default routing:** `caption` on person/vehicle (light, non-biometric). `face`/`plate`
  are opt-in rows. A custom model adds a row keyed on its own class/trigger, and
  `TriggerPolicy.none`/`always` let a model opt out/in — see
  [`docs/design/trigger-policies.md`](../docs/design/trigger-policies.md).
- **Discipline:** validate the shadow-mode miss rate on real hardware before flipping
  `enforce` + dispatch.

## Not yet (deliberately)
- Accelerator detector adapters (Coral/Hailo/RKNN), RF-DETR/YOLO26, and the
  on-hardware capacity/energy numbers → follow-ups (`FOLLOWUPS.md`).
- Kalman motion prediction in the tracker, and the learned per-camera region
  grid → documented follow-ups.
- The KAI-C-backed **accelerator** detector adapter (Coral/Hailo/OpenVINO/
  TensorRT) — the local ONNX detector is CPU via cv2.dnn until then.

OpenCV 4.x **and** 5.x are supported (the suite passes on both). One thing to
smoke-test on a host: real `yolov8n.onnx` inference through OpenCV 5's new DNN
engine (it auto-falls-back to the classic engine if needed).
