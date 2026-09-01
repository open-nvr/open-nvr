# Fake cameras from video files — testing LPR (and everything else) without hardware

> Drop `.mp4` files in a folder, get real RTSP cameras. OpenNVR records them,
> Tier-0 detects on them, and the license-plate app reads plates off them —
> the whole production path, no camera required.

This is a **lab rig**. It runs MediaMTX with no authentication and plaintext
RTSP, pinned to the internal Docker network. Never expose it to a LAN.

## How it works

One container (`opennvr_fakecams`) runs MediaMTX plus one supervised `ffmpeg`
per video file, each looping its file forever and publishing it as its own
path. File name becomes stream name:

```
./data/fake-cameras/gate-entry.mp4   ->   rtsp://172.28.90.10:8554/gate-entry
./data/fake-cameras/exit-lane.mp4    ->   rtsp://172.28.90.10:8554/exit-lane
```

Those URLs are ordinary RTSP, so OpenNVR treats them like any other camera:
provisioned into the stack's own MediaMTX, recorded, and analysed.

## 1. Add your clips

Copy the videos into `./data/fake-cameras/`, or point the rig at a folder you
already have by setting `FAKECAM_VIDEO_DIR` in `.env`:

```dotenv
FAKECAM_VIDEO_DIR=D:/footage/lpr-samples
```

`.mp4 .m4v .mkv .mov .avi .ts .webm` are picked up, including one level of
subfolders. **One file = one camera**, so for an LPR test use clips that
actually contain readable plates, ideally a few seconds of approach per
vehicle rather than a single frame.

## 2. Start the rig

```bash
docker compose -f docker-compose.yml -f docker-compose.fakecams.yml \
    --profile fakecams up -d fakecams
docker logs opennvr_fakecams        # one "publishing …" line per file
```

Sanity-check a stream from the host before going further:

```bash
ffplay rtsp://127.0.0.1:8554/gate-entry
```

Note the two different addresses: `127.0.0.1:8554` works from the **host**
(that is the published loopback port); OpenNVR itself must use
`172.28.90.10:8554`, because it connects from inside a container.

## 3. Register them as cameras

ONVIF **discovery will not find these** — the rig is a bare RTSP server with no
ONVIF device service, and it lives on the Docker network rather than your LAN.
Add them manually (Cameras → Add → **Manual** tab, paste the RTSP URL, leave
credentials blank), or let the script do it:

```bash
docker exec -i opennvr_core python - < scripts/fakecams/register_fake_cameras.py
```

It asks the rig which streams are live, creates one camera per stream, and
assigns each the `license_plate_recognition` skill so the LPR app scopes to
them. Re-running it is safe — streams that already have a camera are skipped.

Knobs (pass with `docker exec -e VAR=…`):

| Variable | Default | Meaning |
|---|---|---|
| `FAKECAM_SKILL` | `license_plate_recognition` | Skill assigned to each new camera; `""` to assign none |
| `FAKECAM_PREFIX` | `fake-` | Camera-name prefix |
| `FAKECAM_IP` | `172.28.90.10` | Address stored on the camera records |
| `FAKECAM_PORT` | `8554` | RTSP port |

## 4. Watch the plates come in

```bash
docker logs -f opennvr_license_plate_recognition     # alerts
docker logs -f opennvr_detect_pipeline               # "tier0 camN: started (WxH)"
```

Plate reads also land in the operator UI's alerts inbox and the Vehicles page.

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

## If plates never appear

Fake cameras get you working video in; they cannot fix a plate chain that is
not wired up. Check in this order:

1. **Is the rig publishing?** `docker logs opennvr_fakecams`
2. **Is the camera's path live in the stack's MediaMTX?**
   `docker exec opennvr_mediamtx sh -c 'curl -s http://127.0.0.1:9997/v3/paths/list'`
3. **Is Tier-0 actually detecting?** The trap here is the motion gate — if
   every frame is skipped, no tracks form and nothing downstream ever runs:
   ```bash
   docker exec opennvr_detect_pipeline python -c "import urllib.request;\
   print([l for l in urllib.request.urlopen('http://127.0.0.1:9109/metrics').read().decode().splitlines()\
   if 'skipped' in l or 'frames_total' in l])"
   ```
   If `tier0_detector_skipped_total{reason="calibrating"}` equals
   `tier0_frames_total`, the detector has never run. Try `FAKECAM_MODE=transcode`
   and a higher `DETECT_FPS`; the motion gate needs a comparatively quiet frame
   (<5% of the frame moving) to finish calibrating, which continuous-motion or
   corrupt-at-the-seam footage never provides.
4. **Is the OCR adapter registered?**
   ```bash
   docker exec opennvr_core sh -c 'curl -s -H "X-Internal-Api-Key: $INTERNAL_API_KEY" \
     http://127.0.0.1:8100/api/v1/adapters'
   ```
   If `fast_plate_ocr` is absent, plate reads 404 silently. KAI-C's registry is
   in-memory, so it is lost on every `opennvr-core` restart — re-run the
   registrar: `docker compose -f docker-compose.yml -f docker-compose.apps.yml \
   --profile apps up fast-plate-ocr-register`
5. **Is the skill assigned to a live camera?** A skill assignment left on a
   *deleted* camera still counts as a restriction, which scopes the LPR app to
   a camera that no longer exists and silently ignores every live one.
