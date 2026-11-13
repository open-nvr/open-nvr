# Ventuno Q — OpenNVR on the Hexagon NPU vs the CPU (measured 2026-10-30)

Board: Arduino Ventuno Q — Qualcomm Dragonwing IQ-8275 (QCS8275; the device tree reports the QCS8300 "Monaco"
platform: `arduino,monza qcom,monaco-monza qcom,qcs8300`), 8× Kryo (Cortex-A78C + Cortex-A55), Adreno 623 GPU,
Hexagon NPU rated by Arduino at up to 40 dense TOPS, 16 GB LPDDR5 (14 GB visible to Linux), 64 GB eMMC (55 GB usable).
Ubuntu 24.04.4 LTS, kernel 6.8.0-1084-qcom, Docker Engine 29.1.3, Compose 2.24.6 (the distro package — see the
end of this file). NPU runtime: ONNX Runtime 1.31.1 + onnxruntime-qnn 2.7.0 (QAIRT 2.51 bundled), HTP backend,
fp32 models run at fp16. Loaner from Indus Technologies.

The first section is the CPU baseline the port was measured against; the NPU sections follow.

Baseline stack: open-nvr main @ 012147e6, images 0.1.8, HW_ACCEL=cpu, detector yolo26n ONNX (CPU EP, fp32), enrichment off,
camera-agent installed but idle (Ollama granite4:micro resident, 0% CPU). Sources: 10 looping clips via the fakecams rig
(same box: ~0.7–1.1 cores of overhead that a real deployment would not pay). Decode: software, 2 threads/camera.
## 10 cameras — four consecutive 60 s windows (all identical within noise)
| metric | value |
|---|---|
| inferences/s, whole pipeline | 8.5–8.6 |
| wall latency per inference (under contention) | 1.05–1.13 s |
| frames processed per camera | ~0.9 fps |
| frames dropped per camera | ~1.1 fps (pipeline asks ~2 fps/camera) |
| detect-pipeline CPU | 600–630 % (of 800) |
| detect-pipeline RSS | ~975 MiB |
| host CPU | ~82 % user, ~12 % sys, ~4 % idle |
| mediamtx CPU | 45–55 % |
| fakecams rig CPU (test overhead) | 60–110 % |

The pipeline reports "detection exceeds this box's capacity" for every camera at this load; detector regions were cut to 1 of 8.

## Scaling — 60 s windows after a 100 s settle, all ten cameras restored afterwards
| active cameras | frames/s per cam (asked ~2) | dropped/s per cam | inferences/s total | wall ms per inference | detect-pipeline CPU | host idle |
|---|---|---|---|---|---|---|
| 1  | 1.98 | 0    | 1.8  | 164   | 330 % | 46 % |
| 2  | 1.99 | 0.01 | 4.0  | 286   | 447 % | 24 % |
| 4  | 2.01 | 0.01 | 7.9  | 284   | 439 % | 21 % |
| 10 | 0.93 | 1.07 | 8.6  | 1047–1127 | 600–630 % | 4 % |

Reading: the CPU ceiling is ~8–8.6 yolo26n inferences/s on this SoC. At the pipeline's ~2 fps per camera that is
four cameras with no drops; ten cameras get under half their frames detected on. Single-camera inference is 164 ms wall
with ORT intra-op threads uncapped (detect-pipeline still burns 3.3 cores for one camera: thread cap worth testing).
All numbers include the fakecams rig (~0.5 core) and software decode on the same box.

## Hexagon HTP probe (same yolo26n.onnx, fp32 model run at fp16 on the NPU via onnxruntime-qnn 2.7.0 / ORT 1.31.1, QAIRT 2.51 bundled)
| run | per inference |
|---|---|
| ORT CPU EP, 8 threads, probe container | 609 ms median (443 min) |
| QNN HTP, sustained_high_performance, fp16 | **8.6 ms median, 7.2 min, 11.8 p90** |
| HTP session build (graph finalisation mode 3) | 6.3 s |
Output agreement on a real parking-lot frame: same detections (cars), scores within 0.01, boxes within 0.5 px; mean |diff| 0.026 on raw outputs.
Host requirements found the hard way: /dev/fastrpc-cdsp AND /dev/dma_heap/{system,qcom,system}, libcdsprpc.so by its UNVERSIONED name (the wheel's HTP stub dlopens it), libdmabufheap.so.0; groups fastrpc (125) and dmaheap (989). Without the dma_heap nodes: QNN_DEVICE_ERROR_INVALID_CONFIG at device creation, silent fallback to CPU.

## In-stack on the NPU (qualcomm flavour, pool 1/2/4 identical), 16 cameras, 60 s windows
| pool | frames/s total | dropped | inferences/s | wall ms/inference (median per cam) | detect-pipeline CPU | host idle |
|---|---|---|---|---|---|---|
| 1 | 32.1 | 0 | 24.1 | 35 (26) | 294 % | 39 % |
| 2 | 32.1 | 0 | 24.1 | 25 (28) | 301 % | 37 % |
| 4 (auto) | 32.2 | 0 | 24.1 | 25 (25) | 281 % | 42 % |
Caveats recorded: the first NPU "run" (12 ms, 239 % CPU at 10 cams) was the STUB detector after `SessionOptions.add_provider` by name was refused — discarded. vehicles/tamper folders are transcoded by the rig (a core each) and were parked for these windows.

## In-stack on the NPU, 8 cameras (the ten minus the parked vehicles/tamper)
16.0 frames/s, 0 dropped, 15.7 inferences/s, wall 19–34 ms per camera (anpr and demo1, 1080p sources: 110–130 ms — letterboxing a large frame on a busy core), detect-pipeline 214 %, host 54 % idle.

## Adapters on the NPU (ai-adapter feat/accelerate-qualcomm images built on the box)
End-to-end `POST /infer` with a 1080p JPEG (crowd clip), 30 calls after warm-up, median. Includes JPEG decode, letterbox and HTTP on the ARM cores.
| adapter | device | /infer median | p90 | min | cold start to healthy |
|---|---|---|---|---|---|
| yolov8 (yolo26n) | HTP | 59–67 ms | 75–82 | 42 | 10 s (graph compile), 4 s with the cached graph |
| yolov8 (yolo26n) | CPU | 152 ms | 287 | 140 | 4 s |
`/hardware/evaluation`: verdict ok, `accelerator_in_use: true`, effective QNN. The HTP's own share is ~9 ms; the rest is JPEG decode and preprocessing on a busy CPU — the adapter path is now bounded by image handling, not inference.
First build trap: python:slim lacks libatomic1; the HTP backend dlopens it and the adapter silently served on CPU with the plugin listed (fixed in the Dockerfiles).
| yolo-pose (yolo11n-pose, pinned 448) | HTP | 48–69 ms | 65–93 | 32 | 8 s / 6 s cached |
| yolo-pose | CPU | 113 ms | 196 | 108 | 4 s |
| package-detection (yolov8n-package, pinned 416) | HTP | 55 ms | 61–65 | 36 | 8 s / 6 s cached |
| package-detection | CPU | 103 ms | 267 | 100 | 4 s |
| fast-plate-ocr (cct-s-v2, one 280×140 crop) | HTP | 79 ms | 123 | 59 | 142 s (model download + compile) |
| fast-plate-ocr | CPU | 276 ms | 396 | 230 | 140 s (model download) |
| fast-plate-ocr | QNN GPU (Adreno) | never became healthy in 240 s | | | |
Dynamic-axes exports (pose, package) only reach the HTP once pinned: the SDK writes `<model>.QNNExecutionProvider.fixed.onnx` before compiling. Parakeet: the box's legacy Docker builder skipped the heredoc weights bake (CI's BuildKit runs it); baked by hand for the measurement.
| fast-plate-ocr (model cache mounted) | HTP | 66–84 ms | 102–116 | 58 | 4 s cached graph |
| fast-plate-ocr (model cache mounted) | CPU | 272 ms | 363 | 224 | 4 s |
| parakeet (3 s 16 kHz clip) | HTP | loads; /infer 500: QNN partition fails at an Expand node (dynamic-length graphs) | | | 24 s |
| parakeet | CPU | 581 ms | 945 | 495 | 12 s |
| insightface (buffalo_l, 1080p frame) | HTP | 57–60 ms | 72–74 | 36 | 18–20 s (five graphs compiled), 8 s with cached graphs |
| insightface | CPU | 436–485 ms | 613–724 | 405 | 6 s |
Found on the way: derived files written beside the pack were globbed by InsightFace as models and broke the next start; the SDK now keeps them in `.opennvr-accel/` inside the model directory.

## Verified from the CI-published images (open-nvr main @ 603305ed, ai-adapter main @ fc660e2, same evening)

Everything above was measured on images built on the board. After the merges the stack was brought up from a fresh
`main` checkout with `./start.sh up` and the GHCR-built images (`detect-pipeline:main-qualcomm`, the five adapters as
`latest-qualcomm`; the pinned `0.1.8-qualcomm` names were re-pointed at them with `docker tag`, since no
`0.1.8-qualcomm` tag exists until the next release). The installer selected `qualcomm` on its own; detect-pipeline logged
`running on QNNExecutionProvider(htp)`; the yolov8 adapter's `/hardware/evaluation` inside the stack reported the
accelerator in use.

Adapters, `/infer` end-to-end on a 1080p JPEG (plate OCR: one plate crop), median of 30 after a warm-up call, each image
run twice — once with the overlay's default `QNNExecutionProvider:htp`, once forced to `CPUExecutionProvider`:

| adapter (published image) | NPU median | NPU p90 | CPU median | CPU p90 | first call NPU / CPU |
|---|---|---|---|---|---|
| yolov8 | 77 ms | 86 | 141 ms | 187 | 167 / 264 ms |
| yolo-pose | 64 ms | 94 | 108 ms | 123 | 155 / 296 ms |
| package-detection | 60 ms | 69 | 100 ms | 161 | 118 / 247 ms |
| insightface (buffalo_l) | 62 ms | 76 | 443 ms | 538 | 338 / 632 ms |
| fast-plate-ocr | 62 ms | 102 | 248 ms | 345 | 140 / 314 ms |

In the stack, 16 fake cameras at 2 fps, software decode, 60 s window after a 100 s settle: 0 dropped frames, 26 ms wall
per inference, ~20 inferences/s (four static scenes trigger no detector runs; two cameras had not delivered frames yet),
detect-pipeline at 280 % CPU, host 46 % idle. Same picture as the on-box builds above.

Found on the way, both fixed on main the same day:

* Compose 2.24.6 (Ubuntu 24.04's `docker-compose-v2`) left `opennvr-core` on `opennvr_apps` only — 1,900
  "could not translate host name db" errors in 45 minutes with a green health check — and `nats` without
  `opennvr_apps` (docker/compose#11533, fixed upstream in 2.26.1). `start.sh` now warns below 2.26.1 and
  re-attaches missing networks after every `up` (PR #738).
* The MediaMTX signing key pair tore itself on the very first boot (two threads generated it at once); key creation
  is now locked and atomic and a torn pair heals on load (PR #737).

Still open: the detect-pipeline's weights volume is mounted read-only, so the compiled QNN graph is cached under the
container's temp dir and recompiled (~6 s) on every detect-pipeline start instead of once; a writable cache volume is
the follow-up. Hardware video decode (Iris VPU), an INT8 detector export and a 24 h soak remain on the list.
