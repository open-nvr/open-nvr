# Fake cameras from video files — test OpenNVR without hardware

> Drop video files in a folder, get real RTSP cameras. OpenNVR streams them,
> records them, detects on them and runs apps against them exactly as it would
> real hardware — because as far as the stack is concerned, they *are* cameras.

Useful any time you need a camera and don't have one, or need a *specific*
scene you can replay on demand:

* developing or reviewing anything camera-facing without a camera on your desk
* live view, recording, playback, timeline and retention behaviour
* Tier-0 detection and the event timeline
* any installed app — occupancy counting, line crossing, intrusion, LPR
* reproducing a bug from a saved clip, deterministically, as many times as you like
* demos on a laptop with no network cameras in reach

This is a **lab rig**. It runs MediaMTX with no authentication and plaintext
RTSP, pinned to OpenNVR's internal Docker network. Never expose it to a LAN.

## How it works

One container (`opennvr_fakecams`) runs MediaMTX plus one supervised `ffmpeg`
per video file, each looping its file forever and publishing it as its own
path. File name becomes stream name:

```
./data/fake-cameras/gate-entry.mp4   ->   rtsp://172.28.90.10:8554/gate-entry
./data/fake-cameras/lobby.mp4        ->   rtsp://172.28.90.10:8554/lobby
```

Those URLs are ordinary RTSP, so OpenNVR treats them like any other camera:
provisioned into the stack's own MediaMTX, recorded, and analysed.

## Getting the rig onto your own branch

The rig lives on the `fake-camera` branch and is **purely additive** — five new
files, zero changes to anything that already exists — so it drops onto any
branch without conflicting with your work.

```bash
git fetch origin fake-camera

# from whatever branch you are working on:
git checkout origin/fake-camera -- \
    docker-compose.fakecams.yml \
    scripts/fakecams \
    docs/FAKE_CAMERAS.md \
    data/fake-cameras/README.md
```

That drops the files in and stages them. Then pick one:

**Keep it out of your commits** (the usual choice — it is a test tool, not part
of your feature):

```bash
git reset                                     # unstage; files stay on disk
printf '%s\n' \
  'docker-compose.fakecams.yml' \
  'scripts/fakecams/' \
  'docs/FAKE_CAMERAS.md' \
  'data/fake-cameras/' >> .git/info/exclude    # never offered as untracked
```

**Or commit it to your branch**, if teammates on that branch should have it:
just `git commit`.

Line endings are handled — `.gitattributes` already pins `*.sh` to LF, so
`entrypoint.sh` checks out correctly on Windows and the container can run it.

To pick up later fixes, re-run the same `git fetch` + `git checkout` pair; it
overwrites those five files in place and touches nothing else.

## 1. Add your clips

Copy the videos into `data/fake-cameras/`, or point the rig at a folder you
already have by setting `FAKECAM_VIDEO_DIR` in `.env`:

```dotenv
FAKECAM_VIDEO_DIR=D:/footage/samples
```

`.mp4 .m4v .mkv .mov .avi .ts .webm` are picked up, including one level of
subfolders. **One file = one camera**, and the file name becomes the camera
name — so name them how you want them to appear.

Pick clips that suit what you're testing. Detection and app testing want a
scene where something actually happens; a static clip is fine for checking
streaming, recording or playback.

### Where to get test clips

**Your own recordings are the best source.** OpenNVR already writes 60-second
mp4 segments under `recordings/<camera>/<date>/<hour>/`. Copy a few into
`data/fake-cameras/` and you can replay a real scene from a real camera, over
and over, deterministically — ideal for chasing a bug you saw once.

**Free stock footage** — direct download, no account, permissive license:

| Source | Good for |
|---|---|
| [Pexels — cctv](https://www.pexels.com/search/videos/cctv/) · [traffic](https://www.pexels.com/search/videos/traffic/) | Surveillance-style scenes, streets, vehicles. Pexels License: free for commercial use, no attribution required. |
| [Pixabay — traffic](https://pixabay.com/videos/search/traffic/) · [street](https://pixabay.com/videos/search/street/) | ~1,900 traffic clips. Pixabay Content License, no attribution required. |
| [Mixkit — cctv](https://mixkit.co/free-stock-video/cctv/) | ~210 CCTV clips in 4K/HD, no watermark, under the Mixkit License. |

Useful search terms on any of them: `cctv`, `surveillance`, `security camera`,
`traffic camera`, `parking lot`, `street`, `pedestrians`.

> **Stock footage is shot to look good, not to be analysed.** Expect handheld or
> panning shots, cinematic depth of field, and vehicle plates that are blurred
> or simply too small to read. That is fine for exercising streaming, recording
> and playback, and usually fine for detection — but for OCR-style testing you
> need footage where the detail is legible in a paused frame. Prefer clips from
> a locked-off camera; a static viewpoint is also what Tier-0's motion gate
> expects.

**Annotated research datasets** — realistic fixed-camera traffic, if you need
ground truth:

* [UA-DETRAC](https://ubmdfl.cse.buffalo.edu/index.php?page=downloads) — 100
  traffic-surveillance videos, ~10 hours, 960×540 at 25 fps, across varied
  weather and lighting, with vehicle bounding boxes.

These typically ship as numbered JPEG frame sequences rather than video, so
assemble a clip first:

```bash
ffmpeg -framerate 25 -i img%05d.jpg -c:v libx264 -pix_fmt yuv420p scene.mp4
```

Check the license of any dataset before using it for anything beyond local
testing — several are academic-use-only.

## 2. Start the rig

```bash
docker compose -f docker-compose.yml -f docker-compose.fakecams.yml \
    --profile fakecams up -d fakecams
docker logs opennvr_fakecams        # one "publishing ..." line per file
```

Sanity-check a stream from the host before going further:

```bash
ffplay rtsp://127.0.0.1:8554/gate-entry
```

Note the two different addresses: `127.0.0.1:8554` is the published loopback
port, for tools on the **host**. OpenNVR must use `172.28.90.10:8554`, because
it connects from inside a container.

## 3. Register them as cameras

ONVIF **discovery will not find these** — the rig is a bare RTSP server with no
ONVIF device service, and it lives on the Docker network rather than your LAN.
Add them manually (Cameras → Add → **Manual** tab, paste the RTSP URL, leave
credentials blank), or let the script do it:

```bash
docker exec -i opennvr_core python - < scripts/fakecams/register_fake_cameras.py
```

It asks the rig which streams are live and creates one camera per stream.
Re-running it is safe — streams that already have a camera are skipped.

Knobs (pass with `docker exec -e VAR=…`):

| Variable | Default | Meaning |
|---|---|---|
| `FAKECAM_PREFIX` | `fake-` | Camera-name prefix |
| `FAKECAM_SKILL` | *(none)* | Skill to assign to every new camera — see below |
| `FAKECAM_IP` | `172.28.90.10` | Address stored on the camera records |
| `FAKECAM_PORT` | `8554` | RTSP port |

`FAKECAM_SKILL` matters only when you are testing a capability that scopes
itself by per-camera assignment (see [`CAMERA_ASSIGNMENTS.md`](CAMERA_ASSIGNMENTS.md)).
By default nothing is assigned, which is what you want most of the time —
"nothing assigned" means "no restriction", so every app still sees the cameras.
Set it when you specifically want the fake cameras claimed by one capability:

```bash
docker exec -e FAKECAM_SKILL=license_plate_recognition -i \
    opennvr_core python - < scripts/fakecams/register_fake_cameras.py
```

## 4. Confirm it's working

```bash
# the rig is serving
docker logs opennvr_fakecams

# OpenNVR is pulling each camera (expect ready=true)
docker exec opennvr_mediamtx sh -c 'curl -s http://127.0.0.1:9997/v3/paths/list'

# Tier-0 picked them up
docker logs -f opennvr_detect_pipeline     # "tier0 camN: started (WxH)"
```

The cameras should now appear in Live View and start recording.

## Encoding modes

`FAKECAM_MODE` in `.env`:

* `auto` (default) — H.264 sources are stream-copied untouched; anything else
  (HEVC, VP9, …) is re-encoded to H.264.
* `copy` — always stream-copy. Cheapest, and fails outright on non-H.264 input.
* `transcode` — always re-encode. **Prefer this if detection isn't firing**:
  stream-copy looping emits corrupt packets at each loop seam ("Invalid NAL
  unit size", "Missing reference picture" in the logs), which can wedge the
  consumer's motion detector. Transcoding rebuilds clean keyframes there.

Transcodes get an explicit frame rate, taken from the source when it reports a
plausible one and 15 fps otherwise (clips with irregular timestamps otherwise
make libx264 duplicate frames up to absurd rates). Override with
`FAKECAM_FPS=10` to cut CPU further.

## Tearing it down

```bash
docker compose -f docker-compose.yml -f docker-compose.fakecams.yml \
    --profile fakecams down fakecams
```

Delete the cameras themselves in the UI (Cameras → 🗑), which stops their
recording and frees the disk their segments took.

## Gotchas

* **Recording is always on.** Every camera records continuously and the
  start/stop routes are deliberately disabled (`server/routers/recordings.py`),
  so you cannot turn it off per camera. If your retention policy is unset
  (`retention_days = 0`, `min_free_space_gb` empty — the default), fake cameras
  will fill the disk indefinitely. Set **Settings → Recording →
  `min_free_space_gb`** before running several of them.
* **All fake cameras share one IP.** That is why the register script passes
  `?force=true` — the duplicate guard would otherwise reject everything after
  the first camera. Adding by hand in the UI hits the same "already added"
  prompt; confirm past it.
* **Never delete `scripts/fakecams/entrypoint.sh` while the overlay exists.**
  It is bind-mounted, and Docker silently creates an empty *directory* in place
  of a missing bind source — the container then dies with exit 127.
* **The static IP assumes the default subnet.** If you have overridden
  `OPENNVR_DOCKER_SUBNET`, set `FAKECAM_IP` to an address inside it.
* **CPU.** Every fake camera costs a decode (and an encode, if transcoding) on
  the rig, plus Tier-0 on the stack side. Half a dozen 1080p clips will be felt
  on a laptop — trim clips, or set `FAKECAM_FPS`.
* **The host port is optional.** `127.0.0.1:8554` exists only so you can
  `ffplay` a stream. On Windows an unbindable port aborts *every* publication
  for the container (#298) — if the rig's ports won't bind, delete that block
  in `docker-compose.fakecams.yml`.

## Troubleshooting

### The stream isn't reaching OpenNVR

1. **Is the rig publishing?** `docker logs opennvr_fakecams` — expect one
   `publishing …` line per file. No lines means no video files were found in
   the folder bound to `/videos`.
2. **Is the camera's path live in the stack's MediaMTX?**
   ```bash
   docker exec opennvr_mediamtx sh -c 'curl -s http://127.0.0.1:9997/v3/paths/list'
   ```
   Look for `"ready": true` on your camera's path.
3. **400 Bad Request in the detect-pipeline logs** usually means the camera was
   deleted or re-added and its MediaMTX path was torn down. It resolves itself
   once the worker picks up a refreshed URL.

### Detection never fires

Tier-0 gates the detector behind a motion detector that must first *calibrate*.
If it never calibrates, the detector never runs, no tracks form, and nothing
downstream — timeline events, apps, enrichment — ever happens.

```bash
docker exec opennvr_detect_pipeline python -c "import urllib.request; \
print([l for l in urllib.request.urlopen('http://127.0.0.1:9109/metrics').read().decode().splitlines() \
if 'skipped' in l or 'frames_total' in l])"
```

If `tier0_detector_skipped_total{reason="calibrating"}` equals
`tier0_frames_total`, that's your problem. The gate needs a comparatively quiet
frame (under 5% of the frame moving) to finish calibrating, which
continuous-motion footage — or footage corrupted at the loop seam — never
provides. Try `FAKECAM_MODE=transcode` first, then a higher `DETECT_FPS`.

### An app sees nothing

* **Check its camera scope.** Apps that scope by assignment follow the rule in
  [`CAMERA_ASSIGNMENTS.md`](CAMERA_ASSIGNMENTS.md): nothing assigned means no
  restriction, but *one* assignment makes that list the whole truth. A stale
  assignment left on a **deleted** camera still counts — which silently scopes
  the app to a camera that no longer exists.
* **Check the app's adapter is registered**, if it depends on one:
  ```bash
  docker exec opennvr_core sh -c 'curl -s -H "X-Internal-Api-Key: $INTERNAL_API_KEY" \
    http://127.0.0.1:8100/api/v1/adapters'
  ```
  KAI-C's adapter registry is in-memory, so adapters registered by an app's
  one-shot init container are **lost on every `opennvr-core` restart**. Re-run
  that app's registrar from the apps overlay to restore it.

### Testing LPR specifically

LPR is the longest chain in the stack, so it is worth naming the extra links
beyond the generic checks above. A plate read needs *all* of:

1. Tier-0 producing a **vehicle visit with an evidence crop** (see "Detection
   never fires" — this is the usual blocker),
2. the `fast_plate_ocr` adapter registered with KAI-C (see "An app sees
   nothing"),
3. clips with plates that are genuinely legible at that resolution and angle —
   if you can't read it in a paused frame, neither can the OCR.

Every one of those fails silently: the Vehicles page simply stays empty.
