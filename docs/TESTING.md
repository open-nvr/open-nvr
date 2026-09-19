# Testing OpenNVR

Two layers, with different jobs.

**Unit suites** — ~2,700 tests across `server/`, `kai-c/`, `detect-pipeline/`,
`sdk/`, `examples/*` and two script sidecars. Fast, no Docker, run per package.
See [CONTRIBUTING.md](../CONTRIBUTING.md#running-tests).

**The end-to-end suite** — `tests/e2e/`. Drives a real, isolated OpenNVR stack
through real user journeys: real MediaMTX, real Postgres, real Tier-0, real OCR.

```bash
python tests/e2e/run.py                 # smoke tier, warm stack (~40s)
python tests/e2e/run.py --fresh         # wipe and start clean
python tests/e2e/run.py -m media        # anything needing a publishing camera
python tests/e2e/run.py -m detection
python tests/e2e/run.py -m ""           # everything
python tests/e2e/run.py -- -k lifecycle -x    # args after -- go to pytest
```

Full details — how to add a test, how to read a failure — are in
[`tests/e2e/README.md`](../tests/e2e/README.md).

## What each layer is for

A unit test tells you a function is correct. It cannot tell you that MediaMTX's
webhook reaches core, that core resolves the file back to the right camera, or
that the adapter Tier-0 wants is registered. Those are the failures that reach
users, and they are all *between* components.

They are also, in this system, overwhelmingly **silent**. Tier-0 that never
finishes calibrating, an OCR adapter that unregistered when core restarted, an
app scoped to a camera that no longer exists — every one of them shows up as an
empty page and nothing in any log. `docs/FAKE_CAMERAS.md` is largely a
catalogue of them.

So the E2E suite is built around turning silence into a diagnosis: guards that
assert preconditions before the behaviour under test, waits that report the last
value they saw, and a failure bundle you can paste into an issue.

## Tiers

Markers, not directories, so a test joins a tier by declaring one.

| Marker | Tests | Needs | Typical runtime |
|---|---|---|---|
| `smoke` | 47 | nothing but the stack | ~1.5 min |
| `media` | 7 | a camera publishing a generated clip | ~3 min |
| `detection` | 4 | real footage, plus Tier-0 detecting an object | ~6 min |
| `lpr` | 3 | the above, plus the `fast_plate_ocr` adapter | several min |
| `ui` | 10 | Playwright against nginx | ~45 s |

**Generated clips cannot drive detection.** Tier-0 gates on motion and then
runs YOLOv8, which classifies real objects; a drawn rectangle is never a person
or a car, so no track and no visit is produced. Streaming, recording and
playback do not care what is in frame and use generated clips, which are tiny
and deterministic. Detection and LPR need real video — put a few clips in
`data/fake-cameras/` (or point `E2E_CLIP_SOURCE` at a folder) and those tiers
light up; otherwise they skip with a message saying exactly that.

## It cannot touch your real deployment

The E2E stack runs under its own compose project with its own container names,
host ports, subnet, volumes and recordings directory. You can run it while your
normal stack is up, and `--fresh` destroys only the E2E one.

That isolation is a dozen environment variables applied together, which is why
`run.py` exists rather than a documented compose command line. Compose isolates
volumes and networks by project name but **not** `container_name`, and it
*concatenates* `ports` across files so an override cannot remove a publication —
only move it. Get one wrong and the test stack adopts your real volumes.

## Known issues the suite documents

An `xfail` here is a real defect, recorded so the suite stays green and tells
you the moment it is fixed (it flips to XPASS). It is never a weakened
assertion.

| Test | Defect |
|---|---|
| `test_a_frame_can_be_extracted_from_past_footage` | `ffmpeg` is not installed in the `opennvr-core` image, so `GET /api/v1/recordings/frame` returns 502 on every install. `_extract_recording_frame` shells out to it; the runtime stage of the root `Dockerfile` installs supervisor, curl, gosu, libpq5 and a few X libraries, and no ffmpeg. Adding it should turn this XPASS. |

## Give the detection tier some CPU

Tier-0 is the first thing starved on a busy host, and it degrades in a way
that looks like a product failure. With another OpenNVR stack running
alongside on an 8-core laptop, frame latency of 13s against a 0.5s budget has
been observed, with the detector cutting its regions from 8 to 2 to keep up.

That matters because of how the motion gate works. On footage with continuous
motion the gate never settles, and Tier-0 force-opens it after 150 frames
rather than blocking forever. At its default 2 fps that is a little over a
minute on an idle host -- and several minutes on a starved one. Nothing is
broken in either case; the budget just has to clear it.

If the detection tier times out, check the frame counters before suspecting
the code:

```bash
docker exec opennvr_e2e_detect_pipeline python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:9109/metrics').read().decode())" | grep -E "frames_total|calibrating|detections_total"
```

`tier0_frames_total` **absent entirely** means no worker ever decoded a frame
-- a source problem, not a gate problem. Present but equal to the calibrating
count means the gate has not opened yet. Either way, raise the budget rather
than editing a test:

```bash
E2E_BUDGET_TIER0_CALIBRATED=900 python tests/e2e/run.py -m detection
```

## CI

The unit matrix runs on every PR (`.github/workflows/ci.yml`).
`.github/workflows/e2e.yml` runs the **smoke** tier on PRs that touch the
stack, and uploads the failure bundle as an artifact when it goes red.

The heavier tiers are opt-in via `workflow_dispatch`, or local. `detection` and
`lpr` cannot run on a hosted runner at all: they need real footage, and there
is nothing to point them at. A self-hosted runner with clips mounted and
`E2E_CLIP_SOURCE` set would work.
