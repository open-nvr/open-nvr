# Ventuno Q — a vision-language captioner on the Hexagon NPU (measured 2026-11-05)

Board: Arduino Ventuno Q — Qualcomm Dragonwing IQ-8275 (QCS8275), 8× Kryo (Cortex-A78C + Cortex-A55),
Hexagon NPU (HTP v75), 16 GB LPDDR5, Ubuntu 24.04, kernel 6.8 (qcom). Same loaner as
[`ventuno-q-benchmark-2026-10-30.md`](ventuno-q-benchmark-2026-10-30.md), which covers the detector.
The Tier-0 detector (yolo26n, ONNX Runtime QNN, HTP) was running on one real office camera
(CP Plus, H.265 sub stream 704×576 at 5 fps) throughout; every number below was taken with it live.

## Why

OpenNVR describes a visit in words (a caption and two visual questions per visit, plus on-demand
"describe" from Search). On a box without a GPU that was the one job no model could keep up with:

| captioner | runs on | one 124 KB evidence photo | outcome |
|---|---|---|---|
| qwen3-vl 2B instruct via Ollama (`CAPTION_ADAPTER=ollamavlm`) | 4 ARM cores | 60–90 s | every call outran KAI-C's 30 s proxy timeout; the box sat at 100 % CPU for an hour writing zero captions |
| moondream 0.5B int8 in-container (`CAPTION_ADAPTER=moondream`, the CPU default), measured 2026-11-06 on this board | ARM cores | 16.0 / 17.1 / 17.4 s a caption, 14.5 s a visual question; 2.0 GiB RSS | works, but a busy camera outruns it; the caption was correct ("a man in a green shirt and blue jeans works on a computer…") |

The Hexagon can run a language model, but only one compiled ahead of time for the exact SoC. The only
vision-language bundles published for the QCS8275 are Google's LiteRT-LM releases of Gemma
(`litert-community/gemma-4-E2B-it-litert-lm`, file `gemma-4-E2B-it_Qualcomm_QCS8275.litertlm`,
2.72 GB, Apache-2.0, QAIRT 2.50). The ONNX route the detector uses does not apply: a text decoder has
dynamic shapes, which is what stopped Parakeet on the HTP.

## Runtime

LiteRT-LM `litert-lm-api` 0.18.0 with the Linux arm64 NPU build of the native library
(`native-v0.18.0-e`), LiteRT's Qualcomm dispatch library, and six Qualcomm AI Runtime 2.50 libraries
(`libQnnHtp`, `libQnnHtpPrepare`, `libQnnSystem`, `libQnnTFLiteDelegate`, `libQnnHtpV75Stub`,
`libQnnHtpV75Skel`). The image encoder runs on the NPU as well as the decoder. One detail worth
knowing: the runtime inside the pip wheel leaves the HTP in its default power mode on Linux and was
about three times slower than the native bundle, so the adapter swaps the bundled library in.

## Standalone (the spike, 11:26 UTC)

Google's hackathon example driven from a shell script, one process, model loaded per run.

| measurement | value |
|---|---|
| text only: load / generate / RSS | 4.75 s / 0.73 s / 932 MiB |
| image caption, first call after load (cold) | 4.28 s |
| image caption, second call (warm) | 2.49 s |
| RSS with the image encoder loaded | 1.2 GiB |
| detector latency on the live camera while captioning | 43.9 ms mean (baseline ~38 ms) |
| host load average during the runs | ~2 |

The caption was correct ("casual t-shirt, sitting on a chair, using a laptop").

## Through the OpenNVR adapter (`litertlm-adapter`, 12:00–13:00 UTC)

The production path: the engine stays loaded, one conversation per request, the photo downscaled to
768 px on its longest side before the model (the detect stream is 704×576; a 1080p evidence crop
scales to 768×432). HTTP from inside the container.

| measurement | value |
|---|---|
| `scene_caption`, 124 KB evidence photo, warm, 3 runs | 1.66 / 1.66 / 1.81 s wall (model 1601–1695 ms) |
| `visual_qa` ("how many people, what colours") | 2.50 s wall (model 2444 ms) |
| adapter RSS after load | 1.3 GiB (VmSize 5.6 GB, the model file mapped) |
| adapter CPU at idle | 0.3 % |
| engine load at container start (model already on disk) | about 6 s to `/health` = ok |

Through core, which grabs a fresh frame from the camera's RTSP stream, asks KAI-C, and audits the call
(`scene_description.describe`):

| path | value |
|---|---|
| caption, including the frame grab from a new RTSP session | 5.58 s |
| visual question, session warm | 2.31 s |

After the switch the detector on the live camera averaged 32.8 ms over 42,000 inferences with the
captioner idle; the box load average sat at 2.3 on 8 cores.

## What is not measured here

- Caption quality against a labelled set. The captions read well on office photos; a scored comparison
  against moondream and qwen3-vl (the camera-agent `vqa_eval` tool) is the next step.
- Gemma 4 E4B, the larger bundle for the same chip. Same adapter, a different model path; expected
  slower and better, not yet timed on this board.
- Sustained throughput with every visit captioned on a busy camera, and the NPU's effect on the
  detector at that rate. The spike's 38 → 44 ms was one caption at a time.
- Any other SoC. The bundle is compiled for the QCS8275; a QCS6490 or QCS9075 needs its own file.

## How to reproduce

```
OPENNVR_ACCEL=qualcomm                       # or auto on a box with /dev/fastrpc-cdsp
CAPTION_ADAPTER=litertlm                     # start.sh fills this on the qualcomm flavour
CAPTION_ADAPTER_TAG=<release>-qualcomm
```

First start downloads the 2.7 GB model from Hugging Face and the six Qualcomm libraries from
Qualcomm's download server (pinned SHA-256; Qualcomm's terms do not allow redistributing them in the
image). Then `docker exec opennvr_core python3` with `services.scene_description.describe(camera, "bench", None)`
times the full path; `/infer` on the adapter container times the model alone.
