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

PowerShell is the same command on one line, or via the launcher's compose args.

Sanity-check a stream from the host before going further:

```bash
ffplay rtsp://127.0.0.1:8554/gate-entry
```

## 3. Register them as cameras

Either add each URL by hand (Cameras → Add → paste the RTSP URL, leave
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

Plate reads also land in the operator UI's alerts inbox. If nothing fires,
check in this order: the rig is publishing (`docker logs opennvr_fakecams`),
the camera's path is `ready` in the stack's MediaMTX, Tier-0 started a worker
for it, and the `fast_plate_ocr` adapter is registered (AI Models page).

## Encoding modes

`FAKECAM_MODE` in `.env`:

* `auto` (default) — H.264 sources are stream-copied untouched; anything else
  (HEVC, VP9, …) is re-encoded to H.264.
* `copy` — always stream-copy. Cheapest, and fails outright on non-H.264 input.
* `transcode` — always re-encode. Use when a source's keyframe interval is so
  long that streams take many seconds to start.

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

* **All fake cameras share one IP.** That is why the register script passes
  `?force=true` — the duplicate guard would otherwise reject everything after
  the first camera. Adding by hand in the UI hits the same "already added"
  prompt; confirm past it.
* **The static IP assumes the default subnet.** If you have overridden
  `OPENNVR_DOCKER_SUBNET`, set `FAKECAM_IP` to an address inside it.
* **CPU.** Every fake camera costs a decode (and an encode, if transcoding) on
  the rig, plus Tier-0 on the stack side. Half a dozen 1080p clips will be felt
  on a laptop — trim clips, or set `FAKECAM_FPS`.
* **The host port is optional.** `127.0.0.1:8554` exists only so you can
  `ffplay` a stream. On Windows an unbindable port aborts *every* publication
  for the container (#298) — if the rig's ports won't bind, delete that block
  in `docker-compose.fakecams.yml`.
