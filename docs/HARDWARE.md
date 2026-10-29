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
| NVIDIA — desktop GPU, Jetson | `nvidia` (`-nvidia`) | NVDEC (`DETECT_HWACCEL=nvidia`) | CUDA / TensorRT | CUDA (`onnxruntime-gpu`) | decode shipped; images Phase 3 |
| AMD — iGPU, Radeon | later (`-rocm`) | VA-API (shipped) | ROCm | ROCm | on demand |
| Raspberry Pi, Rockchip | `cpu` | `rpi` / `rkmpp` decode | CPU | CPU | decode shipped |
| Coral, Hailo | — | — | via the accelerator adapter (own model format) | | follow-up |

"Shipped" means in a released image and selectable from `.env`; "this
release" is the current main branch.

## Selecting a flavour

```dotenv
# .env
OPENNVR_ACCEL=auto      # auto (default) | cpu | intel
```

On `auto`, `start.sh` picks `intel` on a Linux host with an Intel render node
(`/sys/class/drm/renderD*`, vendor `0x8086`) and `cpu` everywhere else — the
macOS and Windows Docker VMs expose no devices. The intel choice:

- swaps detect-pipeline for the `-intel` image via `docker-compose.accel-intel.yml`
  (`${CORE_TAG}-intel`; override with `DETECT_PIPELINE_IMAGE`),
- passes `/dev/dri` through, and `/dev/accel` too when `/dev/accel/accel0`
  exists (`docker-compose.accel-intel-npu.yml` — a separate overlay because
  compose refuses to start when a listed device is absent, and N100-class
  boxes have the iGPU but no NPU),
- swaps the ONNX adapters (YOLO, pose, InsightFace, plate OCR, package
  detection) for their `-intel` images with `/dev/dri` passed through and
  `ADAPTER_ONNX_PROVIDERS=OpenVINOExecutionProvider:GPU` — the iGPU takes
  dynamic shapes and leaves the NPU to the always-on Tier-0 detector,
- fills the defaults an `.env` leaves at the CPU value: `DETECT_HWACCEL=vaapi`,
  `DETECT_ONNX_BACKEND=ort`,
  `DETECT_ONNX_PROVIDERS=OpenVINOExecutionProvider:NPU` when `/dev/accel/accel0`
  exists, else `:GPU`.
  An explicit operator value (`qsv`, a pinned `:NPU`) always wins.

`start.sh` prints the decision on every start:

```
Accelerate: intel flavour — iGPU decode (vaapi), detector on OpenVINOExecutionProvider:NPU (NPU present)
```

Running compose by hand instead of `start.sh`: add the overlay(s) with `-f`
and export the three `DETECT_*` values, or set them in `.env`.

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
| `intel` | same | `onnxruntime-openvino` (bundles OpenVINO) | + Intel OpenCL compute runtime and graphics compiler, Level Zero loader, NPU driver and compiler — pinned in `detect-pipeline/scripts/install-intel-runtime.sh` (the same script builds the adapter flavours in ai-adapter) | amd64 |

The NPU **firmware** is the host's job (`linux-firmware`, loaded by the `ivpu`
kernel driver — kernel 6.8 or newer); the container only needs the user-space
half. Check the host with `ls /dev/accel/accel0` and `journalctl -k | grep intel_vpu`.

## Is it really accelerated? (no silent fallbacks)

Every accelerator path degrades to CPU rather than failing, so each one also
tells you when it did:

| Question | Where to look |
|---|---|
| Is this camera decoding on the GPU? | `tier0_decode_config{camera,hwaccel="vaapi"}` — the **effective** backend; `cpu` next to a `vaapi` request means the render node was not reachable |
| Did core promote cameras to the main stream for a GPU that is not there? | `tier0_mainstream_fallback{camera}` |
| Is the detector on the NPU? | `tier0_detector_backend{requested,effective}` — `effective="CPUExecutionProvider"` beside an OpenVINO request means the device or its driver is missing; the log says the same once at startup |
| Is an adapter on the iGPU? | its `GET /hardware/evaluation`: `requested_providers`, `effective_providers`, `accelerator_in_use`; the adapter logs one warning at load when they differ |
| Is the iGPU busy at all? | `intel_gpu_top` on the host (`Video` engine = decode, `Render/3D` / `Compute` = inference) |

The two classic mistakes, both now visible: setting `DETECT_HWACCEL=vaapi`
without passing `/dev/dri` (decode stays in software while core hands the
pipeline the full-resolution stream), and requesting OpenVINO on a box whose
image has no Intel compute runtime (the detector quietly runs on CPU).

## Benchmark protocol

The same recipe on every hardware class, so rows compare:

1. Fake cameras from the rig (`docker-compose.fakecams.yml`), 720p H264 in
   copy mode so the rig itself costs no CPU.
2. `DETECT_FPS` at 2 and at 5.
3. Add cameras until the host sits at 70% CPU.
4. Report: cameras per box, host cores used, detector ms/frame, iGPU Video
   engine and NPU utilisation.

### First results — Intel Core Ultra 9 285K (Arrow Lake, 24 cores, iGPU + NPU)

Single-stream detector latency, yolo26n @ 640, 30-frame average, idle box:

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
- **One model can take a host down on the iGPU; the NPU is the pose
  device.** The same box reset four times at 48 °C, idle, with the test
  container capped at 8 cores — each time within 30 s of the pose adapter
  starting on `GPU`: with the dynamic export, with the input pinned to
  448, on the 6.8 GA kernel and on the 7.0 HWE kernel, never a line in the
  journal. The fixed-size yolov8n and yolo26n compile and run fine on that
  GPU (19–23 ms/frame), and the pinned pose model runs on the NPU at
  19 ms/frame. So `OPENNVR_ACCEL=intel` puts the pose adapter on the NPU
  when the host has one (`YOLO_POSE_ONNX_PROVIDERS` overrides; on a host
  without an NPU it follows `ADAPTER_ONNX_PROVIDERS`, so set it to
  `OpenVINOExecutionProvider:CPU` if that host resets). Open question for
  the iGPU: the 7.0 kernel asks for GuC firmware 70.53 and Ubuntu's
  `linux-firmware` ships 70.36. Move an Intel Core Ultra host to the HWE
  kernel (`linux-generic-hwe-24.04`) regardless — it is the stack Intel
  validates Arrow Lake graphics on — and keep the GA kernel as fallback.

## Roadmap

- **Phase 1 (this release):** `intel` flavour for the detect-pipeline, `OPENNVR_ACCEL`, this page.
- **Phase 2 (this release):** the ONNX adapters (YOLO, pose, InsightFace, plate OCR, package detection) on one shared provider resolver in the SDK, with `-intel` flavours and static compiles on accelerators (ai-adapter #69, #70).
- **Phase 3:** the `nvidia` flavour, a Hardware panel under Settings → AI, benchmark rows per hardware class.
- Later: ROCm, torch-based adapters per flavour, Coral / Hailo through the accelerator adapter.
