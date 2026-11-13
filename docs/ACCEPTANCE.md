# Acceptance suites — does this hardware (or this camera) carry OpenNVR well?

`tools/acceptance.py` measures a box running OpenNVR, or a camera attached
to it, against the thresholds we hold our own reference hardware and
cameras to, and writes a scored report. It is
read-only: it stores no credentials, changes no configuration, adds no
cameras. It is meant to be run by **us** on every new hardware class and
by **customers** on a box they are considering, and sent back as the
report. Nothing here runs in CI — the measurements need real hardware —
but the parsers, scorers and report are unit-tested (`tools/tests`) so the
tool does not rot between runs.

Two suites: **hardware** (is this box good enough?) and **camera** (does
this camera model behave with the pipeline?). Both write the same kind of
report and share the grading rule.

## Running the hardware suite

On the Linux host that runs OpenNVR, from the checkout (the folder with
`docker-compose.yml` and `.env`), with the stack up:

```bash
python3 tools/acceptance.py hardware                  # ~8 min: benches + a 5 min live window
python3 tools/acceptance.py hardware --minutes 30     # a longer live window: more truth
python3 tools/acceptance.py hardware --skip live      # benches only
python3 tools/acceptance.py hardware --clip ~/footage/lobby.mp4   # time the decode of YOUR footage too
```

Needs only `python3` (3.10+) and `docker compose`; no packages. It writes
`acceptance-reports/hardware-<host>-<UTC>.md` and `.json` (the folder is
gitignored) and prints the markdown. Exit code 0 for grade A or B, 1
otherwise, 2 when it could not run at all.

The benches run beside whatever the stack is doing. For the cleanest
detector and decode numbers run them on an idle box (no cameras, or
`--skip live` after stopping the cameras); the live window, by contrast,
wants the box loaded the way it will be in service.

## What it checks and how it scores

Each check is **pass**, **warn** or **fail** with the measured number
beside it, **unknown** when it could not be measured (no points either
way — a check the tool could not run never passes) or **info** when the
number is worth reporting but not judging. Points: pass 1, warn ½, fail 0
over the scored checks; A ≥ 90 %, B ≥ 75 %, C ≥ 60 %, else F.

| Check | What is measured | pass | warn | fail |
|---|---|---|---|---|
| Detector backend | `tier0_detector_backend`: the provider the detector asked for vs the one it got, against the flavour in `COMPOSE_FILE` | first effective provider is the requested one (CPU flavour: always, with a note) | — | an accelerated flavour running on the CPU (a silent fallback) |
| Detector latency | the in-image bench (`detect_pipeline.bench --latency`) with the service's own model, providers and input size on a synthetic 720p clip, converted to cameras the detector alone could serve at `DETECT_FPS` (× `DETECT_DETECTOR_POOL`) | ≥ 8 cameras | ≥ 4 | fewer; or the bench fell back to the CPU |
| Software decode cost | ffmpeg decode (its own `-benchmark` CPU time) of a 720p H.264 and a 1080p H.265 synthetic I/P-only clip (and `--clip`) in `full` and keyframe-only (`nokey`) modes with `DETECT_DECODE_THREADS`, as % of one core at real time; scored on 720p H.264 `full` — what the pipeline's default `-skip_frame nonref` costs on a camera stream, which has no B-frames to skip | ≤ 15 % of a core | ≤ 30 % | more |
| Host CPU over the window | `/proc/stat` busy % sampled every 5 s; plus a linear estimate of cameras at the 70 % ceiling | mean < 70 % | < 85 % | more (info when no camera was up) |
| Cameras keep their target fps | worst `tier0_processing_fps / tier0_target_fps` among cameras that are up | ≥ 0.9 | ≥ 0.7 | less |
| Frames decoded reach detection | `tier0_frames_dropped_total` and `tier0_worker_restarts_total` deltas over the window | 0 drops, 0 restarts | some | drops > 5 % of frames |
| Stream server stable | MediaMTX reload/close/error log lines over the window | 0 | any | — |
| Thermal headroom | hottest `/sys/class/thermal` zone over the window | < 80 °C | < 90 °C | hotter (info when no zone is readable) |
| Memory headroom | `/proc/meminfo` used share (caches excluded) at peak, swap in use | < 80 % | < 92 % | more |

The thresholds are constants at the top of `tools/acceptance.py`; change
them here and there together. Where they come from: 70 % is the ceiling
the benchmark protocol in [HARDWARE.md](HARDWARE.md) adds cameras up to;
15 % of a core for a 720p substream is three times what a 2020 mini-PC
measures; 8 cameras is the smallest site we size for.

## Reading a report

- **Grade A with the accelerator check passing** is a box we would ship
  on. The capacity estimate in the CPU check is the number to compare with
  the site's camera count.
- **A failed Detector backend** invalidates every detector number below
  it: fix the device pass-through first (HARDWARE.md, "Is it really
  accelerated?") and run again.
- **Decode cost** is the number that decides cameras per box on a CPU-only
  host; the `nokey` column is what an idle camera costs under adaptive
  decode, and the 1080p H.265 `full` column what a camera with no
  substream costs.
- **Unknown** rows mean the tool could not measure, usually because the
  stack or a camera was down; they are listed under the grade so a report
  with half its rows unmeasured cannot read as a pass.

## Running the camera suite

On the host, with the stack up, pointing at the camera's own RTSP URLs
(the tool probes through the detect-pipeline container; the report keeps
the URLs without their credentials):

```bash
python3 tools/acceptance.py camera --main 'rtsp://user:pass@10.0.0.5:554/main' \
    --sub 'rtsp://user:pass@10.0.0.5:554/sub' --camera-id 7 --quiet
```

- `--camera-id` is the camera's id in OpenNVR; with it the tool also reads
  the pipeline's counters for that camera over `--minutes` (default 5).
- `--quiet` says nothing moves in front of the camera during the run. The
  two gate checks are scored only then; otherwise they are reported as
  information, because an open gate on a busy scene is correct.
- `--seconds` (default 60) is how much stream is replayed for the keyframe
  and gate checks. `--sub` may be omitted for a camera without one.

Run it twice on a new model: once on a still scene with `--quiet` for the
gate floor, and once with someone walking through for the evidence check
(it reports "unknown" until something was tracked).

| Check | What is measured | pass | warn | fail |
|---|---|---|---|---|
| Streams reachable | `ffprobe` of the main and sub streams: codec, size, fps | both probe | main only (detection then decodes the main stream) | main fails |
| Sub stream fit for detection | the sub stream's height and fps | ≥ 480 px tall (the pipeline's `DETECT_SUB_MIN_HEIGHT`: below it an auto camera detects on the main stream) and ≥ `DETECT_FPS` | smaller or slower | — |
| Keyframe interval (evidence) | median gap between keyframes on the main stream over `--seconds` | ≤ `DETECT_EVIDENCE_NOKEY_MAX_GOP_S` × 1.15 (keyframe-only evidence applies) | ≤ 10 s (the pipeline switches that camera to full decode) | longer: "smart codec"; events can fall between keyframes of the recording |
| Main and sub show the same view | Pearson correlation of one keyframe from each stream, both at 64x64 luma | ≥ 0.9 | ≥ 0.7 (some main crops will be refused) | less: cross-stream evidence would be of the wrong place |
| Motion gate on a still scene | `--seconds` of the sub stream replayed through the pipeline's own motion detector, unmasked | ≤ 5 % of frames open | ≤ 25 % | more; when ≥ 80 % of the motion sits in one cell of a 10x10 grid the report names it as a burned-in clock/title and prints the mask polygon |
| Camera in the pipeline | `tier0_worker_up`, achieved/target fps over the window | up, ≥ 0.9 | ≥ 0.7 | down or less |
| Feed stayed up | `tier0_worker_restarts_total` over the window (adaptive-decode flips are reported beside it, not counted) | 0 | ≤ 2 | more |
| Gate in service | detector runs / (runs + no-motion skips) over the window, masks applied | ≤ 5 % (still scene) | ≤ 25 % | more (info without `--quiet`) |
| Main-stream evidence | `tier0_evidence_mismatch_total` over main-stream attempts (crops used + refused) in the window, from `tier0_evidence_crops_total` | refused ≤ 10 % | ≤ 30 % | more (a keyframe of a moving subject is of where it was: that camera wants full decode); unknown below 10 crops |

Where the numbers come from: the CP Plus used to build the suite measured
2.03 s between keyframes at a nominal 2 s (hence the 15 % allowance, the
same one the pipeline uses), 0.998 main/sub correlation (0.68 for a
simulated 80 % crop), and a gate open on 94 % of frames of an empty room
from its burned-in clock, 0 % with the clock masked.

Not yet in the suite: ONVIF discovery of the sub stream, recording
continuity, and a deliberate reconnect test — each is a measurement with a
threshold waiting for the camera that shows the problem.

## Growing it

A finding that explains a box or a camera becomes a check, not a note:
the GOP watch and the crop-match guard in the detect-pipeline, the clock
mask on an OSD camera, the MediaMTX reload crash — each is a measurement
with a threshold. Add it as a pure scorer with a test, wire the measurement
in the suite, and add its row to the table above.
