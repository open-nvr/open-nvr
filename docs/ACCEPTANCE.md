# Acceptance suites — does this hardware (or this camera) carry OpenNVR well?

`tools/acceptance.py` measures a box running OpenNVR against the thresholds
we hold our own reference hardware to and writes a scored report. It is
read-only: it stores no credentials, changes no configuration, adds no
cameras. It is meant to be run by **us** on every new hardware class and
by **customers** on a box they are considering, and sent back as the
report. Nothing here runs in CI — the measurements need real hardware —
but the parsers, scorers and report are unit-tested (`tools/tests`) so the
tool does not rot between runs.

Two suites are planned. The **hardware** suite is here; the **camera**
suite (ONVIF/substream discovery, codec and GOP, main/sub field-of-view
match, keyframe-evidence viability, motion-gate noise floor, recording
continuity) follows once its checks have been exercised on real cameras.

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

## Growing it

A finding that explains a box or a camera becomes a check, not a note:
the GOP watch and the crop-match guard in the detect-pipeline, the clock
mask on an OSD camera, the MediaMTX reload crash — each is a measurement
with a threshold. Add it as a pure scorer with a test, wire the measurement
in the suite, and add its row to the table above.
