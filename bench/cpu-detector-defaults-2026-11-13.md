# The detector's frame rate and crop budget on a CPU-only host (2026-11-13)

What the Tier-0 detector costs on a CPU-only host, measured, and what
that says about the `DETECT_FPS` / `DETECT_MAX_REGIONS` defaults. The
companion measurement on the Qualcomm Hexagon (3 crops at 4 fps for two
busy cameras) is in `bench/rfc-0004-infer-once-log.md` and PR #392.

## Setup

* Host: Apple M1 Pro, Docker Desktop with 8 vCPU and 20 GB; the arm64
  `detect-pipeline:main` image (yolo26n, 640 px input, OpenCV DNN, the
  `cpu` flavour); the pipeline sizes 2 inference threads per camera on
  its own (8 cores / 4 cameras).
* Cameras: four fake cameras from `docker-compose.fakecams.yml`
  (`FAKECAM_MODE=transcode`, `FAKECAM_SUBSTREAM=360`), one folder each:
  crowd (1080p lobby crowd), lobby (384x288 CAVIAR corridor), parking
  (1080p parking lot), road (720p road with plates). Three of the four
  detect on the 360p substream; the lobby clip is below the substream
  width and runs as is. All four are BUSY the whole window (looping
  clips): this is the worst case, not a quiet night. The rig's own
  transcoders took ~0.75 core of the same host throughout.
* Method: `bench/cpu_detector_sweep.sh` recreates the pipeline per
  combination (shell env outranks `.env` for Compose), waits for the four
  workers, warms up 90 s, then measures 340 s: the pipeline's own
  counters as deltas (`tier0_*` at `:9109`) and `docker stats` samples
  every ~10 s. `bench/cpu_detector_sweep_report.py` renders the table.
  Per-crop cost from `bench/cpu_detector_microbench.sh` (the image's
  `detect_pipeline.bench` in a throwaway container, pipeline stopped).

## One crop costs half a core-second, whichever backend

Median latency of one yolo26n inference on the road clip (30 samples
after 10 warm-up frames):

| backend | 1 thread | 2 threads | 4 threads | 8 threads |
|---|---|---|---|---|
| cvdnn | 500 ms | 260 ms | 146 ms | 113 ms |
| ort | 532 ms | 278 ms | 158 ms | 205 ms |

Scaling is sub-linear (8 threads buy 4.4x), and onnxruntime is no faster
than OpenCV DNN here, so the backend is not a lever on CPU. The figure of
"~0.1 s per crop" in the regions PR was an x86 number; an ARM host is
about five times slower per crop.

## The sweep

Four busy cameras, 340 s per combination. "achieved" is the sum of the
four cameras' `tier0_processing_fps`; "dropped" is decoded frames the
detector never took; "shed" is the region budget controller's level at
the end against the configured cap; visits and detections are per minute
over all four cameras.


| backend | fps | cap | pipeline CPU % | mediamtx CPU % | crops/s | det ms/frame | frames/s achieved (target) | dropped | shed? (budget/configured) | capped frames | visits/min | person det/min | car det/min | spawns dropped |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| auto | 2 | 8 | 458.1 | 9.3 | 6.8 | 475 | 8.5 (8) | 155 | cam4:1/8, cam5:1/8, cam6:1/8, cam7:1/8 | 2119 | 27.6 | 135 | 527 | 171 |
| auto | 2 | 4 | 458.0 | 6.1 | 7.2 | 447 | 8.6 (8) | 53 | cam4:1/4, cam5:1/4, cam6:1/4, cam7:1/4 | 2281 | 26.5 | 142 | 612 | 197 |
| auto | 2 | 2 | 462.3 | 6.5 | 7.3 | 450 | 8.0 (8) | 26 | cam4:1/2, cam5:1/2, cam6:1/2, cam7:1/2 | 2312 | 23.4 | 145 | 579 | 189 |
| auto | 3 | 3 | 523.3 | 7.4 | 7.5 | 501 | 8.0 (12) | 1377 | cam4:1/3, cam5:1/3, cam6:1/3, cam7:1/3 | 2437 | 25.2 | 147 | 555 | 174 |
| auto | 3 | 2 | 509.0 | 6.5 | 7.9 | 478 | 8.3 (12) | 1248 | cam4:1/2, cam5:1/2, cam6:1/2, cam7:1/2 | 2574 | 23.5 | 148 | 619 | 208 |
| auto | 4 | 2 | 516.8 | 6.7 | 7.8 | 491 | 6.8 (16) | 2680 | cam4:1/2, cam5:1/2, cam6:1/2, cam7:1/2 | 2479 | 14.6 | 147 | 561 | 248 |
| auto | 5 | 1 | 503.4 | 14.1 | 6.1 | 634 | 7.1 (20) | 4693 | cam4:1/1, cam5:1/1, cam6:1/1, cam7:1/1 | 1910 | 6.8 | 112 | 602 | 257 |
| auto | 5 | 2 | 517.5 | 11.2 | 7.2 | 536 | 11.2 (20) | 4273 | cam4:1/2, cam5:1/2, cam6:1/2, cam7:1/2 | 2293 | 6.8 | 132 | 659 | 273 |
| auto | 5 | 4 | 520.5 | 10.7 | 7.5 | 511 | 7.7 (20) | 4140 | cam4:1/4, cam5:1/4, cam6:1/4, cam7:1/4 | 2402 | 9.8 | 141 | 608 | 280 |
| auto | 5 | 8 | 520.5 | 10.5 | 7.5 | 514 | 7.7 (20) | 4182 | cam4:1/8, cam5:1/8, cam6:1/8, cam7:1/8 | 2411 | 13.4 | 135 | 604 | 249 |

## What it says

1. **The host sustains about 8 detector frames a second in total, two per
   camera, whatever is asked.** Every combination landed between 6.8 and
   8.6 achieved frames a second at 460 to 520 % CPU, and every camera was
   shed to one crop per frame. That is the per-crop cost above times
   four workers at two threads each.
2. **Asking for more frames than that buys nothing and costs tracks.**
   At 2 fps the pipeline kept up (26 to 155 dropped frames in 340 s) and
   produced 23 to 28 visits a minute. At 3 fps it dropped over a thousand
   frames and held 25. At 4 and 5 fps it dropped three to five thousand
   frames, the dropped-spawn count rose by half, and visits fell to 7 to
   15 a minute: the frames that did get through arrived late and
   irregularly, and tracks broke between them.
3. **The crop cap barely matters under load.** Shed to one crop, 8, 4 and
   2 give the same rate and nearly the same visits at 2 fps. A lower cap
   settles faster: the controller starts at the configured cap, and the
   frames it drops before it reaches the floor are the 155 / 53 / 26 at
   caps 8 / 4 / 2.
4. **The region budget controller has one dial and the host needed the
   other.** It lowers crops per frame down to one but never the frame
   rate, so when one crop already exceeds the frame budget the pipeline
   can only drop frames. On a shared accelerator the same gap shows the
   other way: every camera meets its own frame budget while the device
   is saturated for everyone (the Hexagon measurement).

## Decision

* The CPU/GPU compose default goes back to `DETECT_FPS=2` with
  `DETECT_MAX_REGIONS=4` (the rate every release before 0.1.11 shipped;
  0.1.11's move to 5 was for the tracker's sake and was not measured on
  a CPU host). A quiet camera costs little at any rate, so the setting
  decides busy periods, and busy periods are where 5 fps on this class
  of host loses two thirds of the visits. A host with headroom raises it
  in `.env` or per camera, as before.
* The proper fix is a rate controller beside the region budget
  controller: when achieved frames fall under the target with the crop
  budget at its floor, lower the effective rate (to a floor of 1) and
  recover the way the region controller does. Then a strong host runs
  5 and a weak one 2 without anyone typing, and a shared accelerator
  gets the per-device budget the Hexagon measurement asked for.

Raw runs: `runs.jsonl` and the `docker stats` samples are kept off the
repo (the clips show people in an office). Re-run with
`COMBOS="2:4 5:4" bash bench/cpu_detector_sweep.sh` on any stack with
fake cameras registered.
