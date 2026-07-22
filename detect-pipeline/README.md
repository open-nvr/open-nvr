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

**On by default.** Disable without a redeploy:

```bash
# .env
DETECT_PIPELINE_ENABLED=false     # container stays up but idle
DETECT_DETECTOR=onnx              # onnx (YOLOv8, default) | hog | blob | stub
DETECT_ONNX_BACKEND=cvdnn         # cvdnn (zero-dep CPU, default) | ort (ONNX Runtime)
DETECT_ONNX_PROVIDERS=            # ort EPs, e.g. OpenVINOExecutionProvider (Intel N100)
DETECT_HWACCEL=vaapi              # + uncomment devices: /dev/dri in compose
```

Entrypoint: `opennvr-tier0` (`detect_pipeline.run:main`). NATS is best-effort —
a broker outage never stops a worker.

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
      "stationary": false }     // settled object (the PR B gate will suppress these)
  ]
}
```

This is the **stable slice consumers code against.** It is intentionally the whole
"shareable" surface — the existing NATS bus + this schema. Apps get Tier-0's
benefit by subscribing here; no separate perception contract is needed. Wiring
consumers (camera-agent, apps) to consume/gate off these events is tracked for the
PR B era in [`FOLLOWUPS.md`](FOLLOWUPS.md).

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

## Not yet in PR A (deliberately)
- The gate (skip detection on stationary tracks) + shadow mode + audit → **PR B**.
- Kalman motion prediction in the tracker, and the learned per-camera region
  grid → documented follow-ups.
- The KAI-C-backed **accelerator** detector adapter (Coral/Hailo/OpenVINO/
  TensorRT) — the local ONNX detector is CPU via cv2.dnn until then.

OpenCV 4.x **and** 5.x are supported (the suite passes on both). One thing to
smoke-test on a host: real `yolov8n.onnx` inference through OpenCV 5's new DNN
engine (it auto-falls-back to the classic engine if needed).
