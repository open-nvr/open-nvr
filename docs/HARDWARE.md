# Hardware acceleration — OpenNVR Accelerate

> One codebase, one setting, per-hardware image flavours. The code is the
> same in every flavour; a build argument picks the inference runtime and the
> vendor user-space, and `OPENNVR_ACCEL` picks the flavour at run time.

Only two parts of OpenNVR touch hardware: the **detect-pipeline** (video
decode and the always-on Tier-0 detector) and the **AI adapters** behind
KAI-C. Apps never do — they consume events — so acceleration is never an
app concern.

## Support matrix

| Hardware | Flavour / tag | Decode | Tier-0 detector | Adapters | Status |
|---|---|---|---|---|---|
| Any x86-64 or arm64 CPU | `cpu` (plain tag) | software | onnxruntime CPU | onnxruntime CPU | shipped |
| Intel iGPU — N100, Core i, Core Ultra, Arc | `intel` (`-intel`) | VA-API on the iGPU | OpenVINO on the iGPU | OpenVINO on the iGPU (`-intel` adapter images) | this release |
| Intel NPU — Core Ultra "AI Boost" | `intel` (`-intel`) | n/a (NPU does not decode) | OpenVINO on the NPU (default when present) | iGPU by default; `ADAPTER_ONNX_PROVIDERS=OpenVINOExecutionProvider:NPU` to move them | this release |
| Qualcomm Hexagon NPU — Dragonwing IQ8 / QCS8275, Snapdragon | `qualcomm` (`-qualcomm`) | software (ffmpeg has no path to the Qualcomm video core) | ONNX Runtime QNN plugin on the HTP (default when `/dev/fastrpc-cdsp` is present) | QNN on the HTP (`-qualcomm` adapter images: YOLO, pose, InsightFace, package detection, plate OCR; Parakeet stays on CPU) | this release |
| NVIDIA — desktop GPU, Jetson | `nvidia` (`-nvidia`) | NVDEC (`DETECT_HWACCEL=nvidia`) | CUDA / TensorRT | CUDA (`onnxruntime-gpu`) | decode shipped; images Phase 3 |
| AMD — iGPU, Radeon | later (`-rocm`) | VA-API (shipped) | ROCm | ROCm | on demand |
| Raspberry Pi, Rockchip | `cpu` | `rpi` / `rkmpp` decode | CPU | CPU | decode shipped |
| Coral, Hailo | — | — | via the accelerator adapter (own model format) | | follow-up |

"Shipped" means in a released image and selectable from `.env`; "this
release" is the current main branch.

## Selecting a flavour

```dotenv
# .env
OPENNVR_ACCEL=auto      # auto (default) | cpu | intel | qualcomm
```

On `auto`, `start.sh` picks `intel` on a Linux host with an Intel render node
(`/sys/class/drm/renderD*`, vendor `0x8086`), `qualcomm` on a Linux arm64 host
with the Hexagon compute DSP (`/dev/fastrpc-cdsp`), and `cpu` everywhere else —
the macOS and Windows Docker VMs expose no devices. The intel choice:

- swaps detect-pipeline for the `-intel` image via `docker-compose.accel-intel.yml`
  (`${CORE_TAG}-intel`; override with `DETECT_PIPELINE_IMAGE`),
- passes `/dev/dri` through, and `/dev/accel` too when `/dev/accel/accel0`
  exists (`docker-compose.accel-intel-npu.yml` — a separate overlay because
  compose refuses to start when a listed device is absent, and N100-class
  boxes have the iGPU but no NPU),
- swaps the ONNX adapters (YOLO, pose, InsightFace, plate OCR, package
  detection) for their `-intel` images with `/dev/dri` passed through, each
  on the device it is proven on (see "What runs where" below),
- fills the defaults an `.env` leaves at the CPU value: `DETECT_HWACCEL=vaapi`,
  `DETECT_ONNX_BACKEND=ort`,
  `DETECT_ONNX_PROVIDERS=OpenVINOExecutionProvider:NPU` when `/dev/accel/accel0`
  exists, else `:GPU`.
  An explicit operator value (`qsv`, a pinned `:NPU`) always wins.

`start.sh` prints the decision on every start:

```
Accelerate: intel flavour — iGPU decode (vaapi), detector on OpenVINOExecutionProvider:NPU, yolov8 on OpenVINOExecutionProvider:GPU, pose on OpenVINOExecutionProvider:NPU, other adapters on CPU unless set (NPU present)
```

### What runs where (intel defaults)

The default is an accelerator only where that model has been proven on
Intel hardware; everything else stays on CPU until it is. Each line is one
`.env` variable; `ADAPTER_ONNX_PROVIDERS` moves all five adapters at once.

| Service | Default on `intel` | Override | Status |
|---|---|---|---|
| Video decode | iGPU (`DETECT_HWACCEL=vaapi`) | `DETECT_HWACCEL` | proven, long runs |
| Tier-0 detector (yolo26n) | NPU, or iGPU without one | `DETECT_ONNX_PROVIDERS` | proven, long runs, 7–8 ms/frame |
| yolov8 adapter | iGPU | `YOLOV8_ONNX_PROVIDERS` | proven, 19–23 ms/frame |
| yolo_pose adapter | NPU; CPU without one | `YOLO_POSE_ONNX_PROVIDERS` | proven on the NPU, 19 ms/frame; resets a host on the iGPU |
| InsightFace adapter | CPU | `INSIGHTFACE_ONNX_PROVIDERS` | not yet proven on iGPU/NPU |
| Plate OCR adapter | CPU | `FAST_PLATE_OCR_ONNX_PROVIDERS` | not yet proven |
| Package detection adapter | CPU | `PACKAGE_DETECTION_ONNX_PROVIDERS` | not yet proven |

A wrong value never crashes a service: if the requested device cannot be
opened (not passed through, driver missing, plugin failure) the detector
and every adapter build their session again on CPU, log an error, and
report `accelerator_fallback_error` (adapters) or `requested != effective`
(the `tier0_detector_backend` metric) — see the next section.

`start.sh` writes what it resolved into `.env` — `COMPOSE_FILE`,
`COMPOSE_PROFILES`, the `DETECT_*` values it filled, the device group ids —
so a bare `docker compose up -d <service>` in the checkout rebuilds that
service with the same overlays and the same accelerator. Passing `-f` by
hand replaces that file set and drops the flavour: the detect-pipeline and
the adapters come back as the plain cpu images (measured: a Ventuno Q went
from 16 cameras on the HTP to 99 % CPU this way). To add an overlay, put it
in `OPENNVR_EXAMPLE_COMPOSE` / `OPENNVR_EXAMPLE_PROFILE` and run `./start.sh up`.

### What runs where (qualcomm defaults)

Same rule: the NPU only where the model is proven on it; the rest on CPU
until measured. One device (the HTP) is shared by everything on it and the
Tier-0 detector has first claim, so a busy site may prefer to keep one of
the adapters on CPU. Each line is one `.env` variable; `ADAPTER_ONNX_PROVIDERS`
moves all five adapters at once. `QNNExecutionProvider:gpu` is the Adreno GPU
through QNN, a second target for dynamic-shape graphs.

| Service | Default on `qualcomm` | Override | Status |
|---|---|---|---|
| Video decode | software | — | no ffmpeg path to the Qualcomm video core |
| Tier-0 detector (yolo26n) | NPU (`QNNExecutionProvider:htp`) | `DETECT_ONNX_PROVIDERS` | proven: 8.6 ms on the device, 16 cameras clean |
| yolov8 adapter | NPU | `YOLOV8_ONNX_PROVIDERS` | proven: `/infer` 59–67 ms end-to-end vs 152 ms on CPU (1080p JPEG; the HTP share is ~9 ms) |
| yolo_pose adapter | NPU | `YOLO_POSE_ONNX_PROVIDERS` | proven: `/infer` 48–69 ms end-to-end vs 113 ms on CPU; the dynamic export is pinned to 448 for the HTP |
| InsightFace adapter | NPU | `INSIGHTFACE_ONNX_PROVIDERS` | proven: `/infer` 57–60 ms vs 436–485 ms on CPU (buffalo_l; the dynamic detector pinned to 640, the recogniser's batch to 1); 18 s first start, 8 s with cached graphs |
| Package detection adapter | NPU | `PACKAGE_DETECTION_ONNX_PROVIDERS` | proven: `/infer` 55 ms vs 103 ms on CPU; dynamic export pinned to 416 |
| Plate OCR adapter | NPU | `FAST_PLATE_OCR_ONNX_PROVIDERS` | proven: 79 ms a plate vs 276 ms on CPU (single crop per call); the QNN GPU backend hung at load |
| Captioner (`caption-adapter`) | NPU: `CAPTION_ADAPTER=litertlm` (Gemma 4 E2B through LiteRT-LM) | `CAPTION_ADAPTER`, `CAPTION_ADAPTER_TAG` | proven: 2.5 s a photo warm, 4.3 s cold, image encoder on the NPU too; ~1.2 GiB resident; the detector sharing the NPU 38 → 44 ms. qwen3-vl 2B via Ollama on the CPU: 60–90 s, every caption past core's deadline (Ventuno Q, 2026-11-05) |
| Parakeet (speech) | CPU | `ADAPTER_ONNX_PROVIDERS` | measured: loads on the HTP, fails at inference (dynamic-length graphs, Expand node); CPU does 3 s of audio in 0.6 s |
| Whisper, BLIP, CLIP, open-vocab, Ollama | CPU | — | ctranslate2 / torch / Ollama: no Hexagon backend in those engines |

`start.sh` prints the decision on every start:

```
Accelerate: qualcomm flavour — software decode, detector on QNNExecutionProvider:htp (Hexagon NPU via /dev/fastrpc-cdsp); adapters: yolov8, yolo-pose, insightface, package-detection, fast-plate-ocr on the NPU (override <NAME>_ONNX_PROVIDERS or ADAPTER_ONNX_PROVIDERS in .env); captioner: litertlm (Gemma on the NPU)
```

The captioner is not an ONNX adapter: `litertlm` is Google's LiteRT-LM
running the NPU-compiled `gemma-4-E2B-it_Qualcomm_QCS8275.litertlm` (2.7 GB,
Apache-2.0, downloaded once into the `opennvr_litertlm_models` volume) with
six Qualcomm AI Runtime 2.50 libraries fetched once from Qualcomm's download
server into `opennvr_litertlm_npu` (their terms do not allow redistributing
them in the image). The model is compiled for the QCS8275's Hexagon v75; a
different SoC needs its own compiled bundle. The first start downloads for a
few minutes; `/health` on the adapter says `loading` until then. The measurements are in
[`bench/ventuno-q-npu-captioner-2026-11-05.md`](../bench/ventuno-q-npu-captioner-2026-11-05.md).

### Device shorthand

`DETECT_ONNX_PROVIDERS` is a comma-separated provider list; each provider takes
an optional `:device`, which becomes that provider's device option
(`device_type` for OpenVINO, `device_id` for CUDA/TensorRT):

| Setting | Runs on |
|---|---|
| `OpenVINOExecutionProvider:NPU` | the NPU, CPU fallback (default when the host has one) |
| `OpenVINOExecutionProvider:GPU` | the iGPU / Arc (default otherwise) |
| `OpenVINOExecutionProvider:AUTO:NPU,GPU,CPU` | OpenVINO's AUTO — **not recommended**: it starts on CPU while the accelerator compiles and can stay there; measured 17.0 ms/frame after 30 frames against 7.4 on the pinned NPU |
| `OpenVINOExecutionProvider:CPU` | OpenVINO's own CPU engine |
| `CUDAExecutionProvider:1` | the second NVIDIA GPU |

OpenVINO's multi-device forms keep their commas — a token that is not a
provider name continues the previous device.

### Model shapes on accelerators

OpenVINO compiles a graph for a device. A model exported with a **fixed**
input (yolo26n, yolov8n: `[1, 3, 640, 640]`) compiles once and is the
easy case. A model exported with **dynamic axes** (`[batch, 3, height,
width]` — Ultralytics `dynamic=True`, e.g. `yolo11n-pose`) is not: the NPU
plugin cannot run it at all, and on the Arrow Lake box the iGPU driver
hard-reset the host twice while compiling it for `GPU` (the fixed-size
models ran fine on the same device). So on an OpenVINO device other than
`CPU` the adapters compile static: the SDK passes `disable_dynamic_shapes`
(compile for the first inference's shapes; `ADAPTER_ONNX_DYNAMIC_SHAPES=1`
opts out), and an adapter that knows its size pins it with `reshape_input`
before the session is built — `yolo_pose` pins `[1, 3, 448, 448]` and
reports `input_shape_pinned` in `/hardware/evaluation`. Prefer fixed-size
exports for anything meant to run on an accelerator. (Pinning is what made
the pose model run on the NPU; it did not stop the iGPU reset described
under the benchmark findings — that one is driver-level.)

## What each flavour carries

| Flavour | Base | Inference wheel | Vendor user-space | Arch |
|---|---|---|---|---|
| `cpu` | `python:3.11-slim` | `onnxruntime` | Intel VA-API driver (iHD) for decode | amd64, arm64 |
| `qualcomm` | same | plain `onnxruntime` **plus** `onnxruntime-qnn` 2.x — a *plugin* execution provider (bundles the Qualcomm AI Runtime: `libQnnHtp`, the HTP v68–v81 skels) that the service and the adapter SDK register at startup (the same wheel builds the `-qualcomm` adapter flavours in ai-adapter) | none in the image; the host lends `/dev/fastrpc-cdsp`, the two `/dev/dma_heap` nodes, `libcdsprpc.so` and `libdmabufheap.so.0` through `docker-compose.accel-qualcomm.yml` | arm64 |
| `intel` | same | `onnxruntime-openvino` (bundles OpenVINO) | + Intel OpenCL compute runtime and graphics compiler, Level Zero loader, NPU driver and compiler — pinned in `detect-pipeline/scripts/install-intel-runtime.sh` (the same script builds the adapter flavours in ai-adapter) | amd64 |

The NPU **firmware** is the host's job (`linux-firmware`, loaded by the `ivpu`
kernel driver — kernel 6.8 or newer); the container only needs the user-space
half. Check the host with `ls /dev/accel/accel0` and `journalctl -k | grep intel_vpu`.

On Qualcomm the split is the same: the Hexagon firmware and the fastrpc daemon
(`cdsprpcd`) are the host's — Qualcomm's Ubuntu ships them — and the container
carries the user-space in the wheel. Check the host with `ls -l /dev/fastrpc-cdsp`
(group `fastrpc`; set `FASTRPC_GID` in `.env` if yours is not 125) and
`systemctl is-active cdsprpcd`; the DMA-BUF heaps (`/dev/dma_heap/system`,
`qcom,system`, group `dmaheap`) go through too — without them the runtime
refuses to create the device (`QNN_DEVICE_ERROR_INVALID_CONFIG`). The HTP runs fp16 and int8 only: the stack's
fp32 `yolo26n.onnx` is run with `enable_htp_fp16_precision=1` (the flavour's
default), which the plugin applies at graph finalisation; an INT8 export is the
next step on this platform, not a requirement.

## Measured: Qualcomm Hexagon HTP (Arduino Ventuno Q, QCS8275, 2026-10-30)

The board: Arduino Ventuno Q — Qualcomm Dragonwing IQ-8275 (QCS8275, on the
QCS8300 "Monaco" platform per its device tree), 8× Kryo (Cortex-A78C +
Cortex-A55), Adreno 623, a Hexagon NPU Arduino rates at up to 40 dense TOPS,
16 GB LPDDR5, 64 GB eMMC; Ubuntu 24.04.4, kernel 6.8 (qcom). Runtime: ONNX
Runtime 1.31.1 with onnxruntime-qnn 2.7.0 (QAIRT 2.51), HTP backend, fp32
models run at fp16. The full log, including the CPU baseline and the
re-measurement from the CI-published images, is
[`bench/ventuno-q-benchmark-2026-10-30.md`](../bench/ventuno-q-benchmark-2026-10-30.md).

The stack's own `yolo26n.onnx` (fp32, 640×640), run through the QNN plugin
with the flavour's defaults (`htp_performance_mode=sustained_high_performance`,
`enable_htp_fp16_precision=1`, graph finalisation mode 3):

| | per inference | notes |
|---|---|---|
| CPU, the shipped `cvdnn` path, 1 camera | 164 ms | 8 ARM cores, ORT threads uncapped |
| CPU, ONNX Runtime CPU provider, 8 threads | 609 ms median | plain `onnxruntime` 1.31 on this SoC |
| **Hexagon HTP, fp16** | **8.6 ms median** (7.2 min, 11.8 p90) | session build 6.3 s, once per session |

Output agreement on a real frame: the same detections, scores within 0.01,
boxes within half a pixel. The whole graph runs on the HTP; nothing is
partitioned back to CPU.

In the stack (looping test clips through the fake-camera rig on the same
board, software decode, Tier-0 asking ~2 fps per camera, 60 s windows after a
100 s settle):

| cameras | flavour | frames detected on | dropped | wall ms / inference | detect-pipeline CPU | host idle |
|---|---|---|---|---|---|---|
| 4 | `cpu` | 2.0 fps each | none | 284 | 439 % | 21 % |
| 10 | `cpu` | 0.9 fps each | **half** | 1047–1127 | 600–630 % | 4 % |
| 8 | `qualcomm` | 2.0 fps each | none | 19–34 (two 1080p clips 110–130) | 214 % | 54 % |
| 16 | `qualcomm` | 2.0 fps each | none | 25 (median per camera 25–28) | 281–300 % | 37–42 % |

The CPU ceiling on this board is ~8.5 inferences/s: four cameras clean, ten
over capacity. On the HTP sixteen cameras run clean at ~24 inferences/s with
the detector pool at 1, 2 or 4 sessions alike (the HTP is one device; the
pool size changes nothing) and the board 40 % idle — decode and
preprocessing on the ARM cores are now the limit, not inference. The ~25 ms
wall figure includes letterboxing and decode on a busy CPU; the HTP itself
takes ~9 ms. Benchmark overhead to subtract: the fake-camera rig (~0.6
cores) and mediamtx (~0.5 cores) ran on the same board.

## Is it really accelerated? (no silent fallbacks)

Every accelerator path degrades to CPU rather than failing, so each one also
tells you when it did:

| Question | Where to look |
|---|---|
| Is this camera decoding on the GPU? | `tier0_decode_config{camera,hwaccel="vaapi"}` — the **effective** backend; `cpu` next to a `vaapi` request means the render node was not reachable |
| Did core promote cameras to the main stream for a GPU that is not there? | `tier0_mainstream_fallback{camera}` |
| Is the detector on the NPU? | `tier0_detector_backend{requested,effective}` — `effective="CPUExecutionProvider"` beside an OpenVINO request means the device or its driver is missing; the log says the same once at startup |
| Is the detector on the Hexagon NPU? | the same metric and log line. A *plugin* provider keeps listing itself after its device failed, so for QNN `effective` comes from whether the provider compiled the graph: the compiled-graph cache file (`.opennvr-accel/yolo26n.onnx.QNNExecutionProvider.ctx.onnx` inside the model's directory, or under the temp dir) exists after the first session. `detect-pipeline` logs `compiled the graph, cached at …` once, then `loaded the compiled graph …` for every later session — the compile is ~6 s on a Hexagon v73, the load well under a second |
| Is an adapter on the iGPU? | its `GET /hardware/evaluation`: `requested_providers`, `effective_providers`, `accelerator_in_use`; the adapter logs one warning at load when they differ |
| Is the iGPU busy at all? | `intel_gpu_top` on the host (`Video` engine = decode, `Render/3D` / `Compute` = inference) |

The two classic mistakes, both now visible: setting `DETECT_HWACCEL=vaapi`
without passing `/dev/dri` (decode stays in software while core hands the
pipeline the full-resolution stream), and requesting OpenVINO on a box whose
image has no Intel compute runtime (the detector quietly runs on CPU).

## Benchmark protocol

`python3 tools/acceptance.py hardware` runs the measurable part of this
protocol on the box itself and scores it — accelerator really in use,
detector ms/frame, decode cost per stream, and a live window of the
running stack — into a report ([ACCEPTANCE.md](ACCEPTANCE.md)). The
recipe below is what the rows in this page were produced with:

1. Fake cameras from the rig (`docker-compose.fakecams.yml`), 720p H264 in
   copy mode so the rig itself costs no CPU.
2. `DETECT_FPS` at 2 and at 5.
3. Add cameras until the host sits at 70% CPU.
4. Report: cameras per box, host cores used, detector ms/frame, iGPU Video
   engine and NPU utilisation.

Detector ms/frame per device comes from `bench.py --latency`, run inside the
`-intel` image on the box itself, against a clip or one camera's substream. The
three compose files are what start.sh adds on an NPU host; without the last one
the container has no `/dev/accel` and every row is a CPU fallback. It prints the
provider that really ran and exits 2 on a fallback:

```bash
docker compose -f docker-compose.yml -f docker-compose.accel-intel.yml \
  -f docker-compose.accel-intel-npu.yml run --rm --no-deps detect-pipeline \
  python -m detect_pipeline.bench \
  --source "rtsp://…/substream" --max-frames 200 --detector onnx \
  --model /app/model_weights/yolo26n.onnx \
  --providers OpenVINOExecutionProvider:NPU --latency 30
```

Repeat with `:GPU`, `:CPU` and `--providers CPUExecutionProvider --threads 0`
for the other rows. Over a remote link, run it on the remote box so that video
stays on its LAN; a run on your laptop measures your laptop.

### First results — Intel Core Ultra 9 285K (Arrow Lake, 24 cores, iGPU + NPU)

Single-stream detector latency, yolo26n @ 640, 30-frame **mean** from a hand-run
probe, idle box. (`bench.py --latency`, added later, reports the **median**; a
re-run with it will differ by the statistic, not the hardware — re-measure the
whole table rather than mixing rows.)

| Device | ms/frame |
|---|---|
| onnxruntime CPU, all cores | 10 |
| OpenVINO CPU | 15.5 |
| iGPU | 7.7 |
| **NPU** | **7.4** |
| OpenVINO AUTO | 17.0 (still on CPU after 30 frames) |

Whole stack under load — 720p30 H264 fake cameras in copy mode plus four real
352x288 cameras, `DETECT_FPS=2`, every catalog app running (guard-scan,
occupancy, line-crossing, loitering, intrusion, abandoned-object,
footage-search, doorbell, plate recognition), 90–120 s windows, container CPU
averaged every 5 s:

| | cpu flavour | intel flavour |
|---|---|---|
| Cameras | 9 × 720p + 4 × CIF | 7 × 720p + 2 × CIF (two of each were down on the site link) |
| Decode backend (effective, all cameras) | cpu | vaapi, iGPU Video engine ~9 % busy |
| Detector backend (effective) | onnxruntime CPU | OpenVINO **NPU** |
| detect-pipeline, cores | **6.5** | **1.1** |
| …per 720p camera, cores | ~0.7 | ~0.15 |
| yolo-pose adapter (guard-scan), cores | 14.3 | 12.6 |
| guard-scan app, cores | 1.4 | 2.1 |
| MediaMTX + rig + core + db, cores | 0.3 | 0.3 |
| Host total, of 24 cores | 72 % (17.3) | 40 % (9.7) |
| Analysed rate per camera | 2.7 fps | 2.8 fps |

What the numbers say:

- **Decode and the Tier-0 detector together drop about 6×** when they move
  to the iGPU and NPU — from ~0.7 to ~0.15 core per 720p camera. On a box
  that gives Tier-0 nothing else to do, that is the difference between
  roughly 30 and 150 cameras of substream analysis.
- **The adapters are the ceiling now.** Pose estimation for the guard-scan
  app alone costs 12–14 cores on CPU and does not change with the flavour,
  because adapters are CPU-only until Phase 2 (ai-adapter #68). That is the
  Phase 2 motivation in one row, and the app side of it (an app should not be
  able to peg the box; a per-track inference budget) is tracked separately.
- **Thermal headroom is part of sizing.** Under this load the package ran
  at 95–99 °C and the box hard-rebooted — nothing in the journal, two-minute
  boot-to-trip cadence, i.e. a thermal or power trip. The accelerators add
  package power on top of CPU load; a desktop-class CPU in a closed cabinet
  needs real airflow. Watch `x86_pkg_temp` (`/sys/class/thermal`) during a
  benchmark; the Settings → AI → Hardware panel (Phase 3) will show it.
- **A platform can reset under accelerator load; the symptom is not a
  software one.** The same box, idle at 48 °C with the test container
  capped at 8 cores, reset within 30 s of the pose adapter starting on
  `GPU` — four times: dynamic export, input pinned to 448, 6.8 GA and 7.0
  HWE kernels — and once more running InsightFace's detector on the `NPU`.
  Never a line in the journal, nothing in the EFI pstore (so no kernel
  panic), no watchdog: the board reset itself. Meanwhile the fixed-size
  yolov8n and yolo26n run fine on that GPU (19–23 ms/frame) and the pinned
  pose model runs on the NPU at 19 ms/frame. Which model trips it is a
  matter of which load pattern the power delivery or firmware cannot take,
  not of shapes or drivers. On such a board: update the BIOS (this one was
  a year old), check the PSU, lower the RAPL limits as a test, and keep the
  accelerators to the models that are proven on it. `OPENNVR_ACCEL=intel`
  puts the pose adapter on the NPU when the host has one
  (`YOLO_POSE_ONNX_PROVIDERS` overrides; without an NPU it follows
  `ADAPTER_ONNX_PROVIDERS`); any adapter can be pinned to
  `OpenVINOExecutionProvider:CPU` the same way. Move an Intel Core Ultra
  host to the HWE kernel (`linux-generic-hwe-24.04`) regardless — it is the
  stack Intel validates Arrow Lake on — and keep the GA kernel as fallback.

## Roadmap

- **Phase 1 (this release):** `intel` flavour for the detect-pipeline, `OPENNVR_ACCEL`, this page.
- **Phase 2 (this release):** the ONNX adapters (YOLO, pose, InsightFace, plate OCR, package detection) on one shared provider resolver in the SDK, with `-intel` flavours and static compiles on accelerators (ai-adapter #69, #70).
- **Phase 3:** the `nvidia` flavour, a Hardware panel under Settings → AI, benchmark rows per hardware class.
- Later: ROCm, torch-based adapters per flavour, Coral / Hailo through the accelerator adapter.
