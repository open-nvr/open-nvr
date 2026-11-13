# ANPR on the Ventuno Q: where the time goes when a road camera is busy (2026-11-11)

Box: Ventuno Q (QCS8275, Hexagon HTP v75, 8 ARM cores), ONNX Runtime 1.31.1
in the detector and the adapters, Gemma captions on the HTP through LiteRT-LM.
Load camera: a fake 1080p plate clip (`fake-plates-road`, no substream) picked
for the LPR app; the real CP Plus camera on its substream throughout.

## The symptom

The dashboard's NPU tile read 100 % and the host CPU rose as soon as the
plate reader was in use. Both are real effects with different causes.

## The NPU figure

The tile is summed client-observed inference latency over wall time
(`accelerator_usage.py`). With three producers on one device each one's
latency includes the wait for the others, so the sum ran past 100 and the
tile capped it. During a plate burst, per 24 s window:

| producer | share of wall time |
|---|---|
| detector | 117 % |
| plate reader | 72 % |
| captioner (litertlm) | 74 % |

By Little's law that sum is the mean number of requests in flight (2.6):
the device was never idle AND requests were queued behind each other. The
tile now carries that as `inflight` and says "queued ×2.6".

## What actually slows down, and why

Reproduced without the camera: core → KAI-C → adapter on one real vehicle
frame, two plate reads in flight (~2/s), 75 s, with and without captions
running back to back on the HTP. CPU from the containers' cgroup counters.

| run | plate read median | detector mean | plate adapter | caption adapter | detect pipeline |
|---|---|---|---|---|---|
| quiet, no load | — | 24–26 ms | 0.03 cores | — | 0.28 cores |
| plates alone | 71–77 ms | 20–23 ms | 0.12 cores | — | 0.3 cores |
| plates + captions | 370 ms | 124–138 ms | 0.12 cores | 0.27 cores | 0.3 cores |

The captioner holds the HTP about 1 s per caption. While it runs, the
detector's inference takes 5× longer and a plate read 5× longer, at low
CPU everywhere. That is the contention. QNN's `qnn_context_priority=high`
on the detector made no difference (106–108 ms burst mean with and without):
LiteRT-LM is not a QNN session and does not yield to one.

The CPU rise is the 1080p fake camera decoded on the CPU (no substream):
detect pipeline 36 % quiet → 110–160 % while it was active, and core's sweeps.

## The plate OCR graph

The OCR export has a symbolic batch, so QNN had compiled 2 of 377 nodes;
the rest ran on the CPU. ai-adapter PR #89 pins the batch to 1 and the
whole graph compiles for the HTP. Standalone: 62 → 10 ms per read, 501 → 1 ms
of CPU (tight loop, thread-pool spin included in the before number). Through
the production path nothing moved (77 → 80 ms; 370 → 364 ms with captions):
the wait for the shared device sets the latency, not this graph.

## Calls per vehicle

373 plate reads for 62 vehicle visits on the road clip: 2.2 early attempts
per visit (fragmented tracks re-confirm) plus the sweep's full 4 looks
whenever two reads did not agree. The LPR app's `scan_mode` was `accurate`;
`auto` or `fast` cut that to ≤ 3 without a code change.

## Plate models on the clip (six known UK-style plates)

| model | run | visits | with a plate | exact | wrong / extra |
|---|---|---|---|---|---|
| indian-plates-v1 | 10 min | 32 | 12 | H644LX, R183JF | L656XH→L656XHH, 66HH07→66HH070/66HHH70 |
| cct-s-v2-global-model | 5.5 min | 30 | 7 | 66HH07 ×4, L656XH | — |

K884RS read on both models at 0.88–1.00 and is not in the clip's list: most
likely a real plate the ground truth lacks. Each model is right for its own
plate format; the real camera's plates are Indian, so the box runs
`indian-plates-v1`.

## What would reduce the contention

1. Fewer plate reads per vehicle on a road camera: `scan_mode: auto`.
2. Captions yield to the detector during a burst: the enrichment gate
   already drops when its queue is full; a rate cap or a "detector slow"
   signal from the detect pipeline would keep the HTP free for detection
   while vehicles keep coming. Not built; needs a design pass.
3. A substream for any 1080p camera (the CPU half).
