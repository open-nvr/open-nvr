# Camera Tamper

Tells you when a camera can no longer see what it was pointed at:
**covered** (a hand, a bag, spray paint), **defocused** (a smeared or
refocused lens), **moved** (re-aimed, knocked, a mast in the wind) or
**frozen** (a stream that stopped changing). No model runs: each check is
a few milliseconds of arithmetic on a small greyscale copy of the frame,
against a reference view the app learns from the camera itself.

It is a frame-polling app on the App SDK: every `poll_interval_seconds` it
fetches one frame per picked camera through core, scores it, and alerts on
the transition. The camera's own ONVIF tamper alarm, where the camera has
one, still arrives through core's native-alarm route; this app covers the
cameras that have none and the faults the camera cannot judge about itself.

## What you see

The app's page in the catalog shows cameras watched, cameras tampered
now, tamper alerts today, a focus gauge per camera (sharpness now as a
share of the reference view's), a per-camera table (condition, how long,
focus, match with the reference, alerts today), today's alerts by
condition, a recent-events log and a gallery pairing each alert's frame
with the camera's reference view.

## What it emits

| Alert | When | Severity |
|---|---|---|
| `tamper_covered` | most of the picture is near black, or it has no contrast at all | `alert_severity` |
| `tamper_defocused` | sharpness has dropped below `blur_ratio` of the reference | `alert_severity` |
| `tamper_moved` | the picture's edges no longer line up with the reference view (plain brightness correlation forgives a zoom of the same scene; edge correlation does not) | `alert_severity` |
| `tamper_frozen` | `freeze_polls` frames in a row that did not change (same bytes, or a mean pixel change under `frozen_mad`) | `alert_severity` |
| `tamper_cleared` | the camera looks normal again after an alert | low |

A condition has to hold for `confirm_seconds` before it alerts, so a hand
passing the lens or a headlight flare does not page anyone, and the same
condition on the same camera stays quiet for `cooldown_seconds` after an
alert. A camera that stays "moved" for `rebaseline_seconds` was re-aimed on
purpose: the new view becomes the reference and the condition clears.

## Quick start (compose)

```bash
docker compose -f docker-compose.yml -f docker-compose.apps.yml \
  --profile camera-tamper up -d camera-tamper
```

Then pick its cameras: App Catalog → Camera Tamper → Configure → **Cameras**.
Nothing picked, nothing polled.

## Standalone

```bash
cp config.example.yml config.yml      # list cameras with a frame_url each
python camera_tamper.py --config config.yml
python camera_tamper.py --config config.yml --once   # one pass, then exit
```

## Testing

```bash
uv sync --extra dev && uv run pytest -q
```

The tests drive the app with synthetic frames (a textured scene, then the
same scene blurred, blacked out, shifted, repeated) through the SDK's
`RecorderChannel`, so they need no broker, no core and no camera. The
repository's fake-camera rig has clips for the same four conditions
(`data/fake-cameras/tamper/`, synthesised with ffmpeg) for an end-to-end
run with `tools/usecase_eval.py`.
