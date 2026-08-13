# Recording layout — how recordings are named on disk

OpenNVR records through MediaMTX, which writes segments to a **templated
path**. The layout is configurable, so you can keep OpenNVR's default or
match an existing NVR's tree (so your scripts, backups, and tools address
files by path without change).

## Presets (`RECORDING_LAYOUT`)

| Preset | On-disk shape | Use it when |
|---|---|---|
| `nested` (default) | `<base>/<camera>/%Y/%m/%d/%H/%M/%S.<ext>` | Default; no change to existing installs |
| `date-hour` | `<base>/<camera>/%Y-%m-%d/%H/%M-%S.<ext>` | One folder per day and per hour; **path-addressable** retrieval. Pair with `RECORDING_SEGMENT_SECONDS=60` for one-minute clips (matches many NVRs and VMS systems) |
| `flat` | `<base>/<camera>/%Y-%m-%d_%H-%M-%S.<ext>` | Everything for a camera in one folder |
| `custom` | `RECORDING_PATH_TEMPLATE` verbatim | Any layout you need |

`<camera>` is the stream path (`cam-<id>` or `cam-<ip>` per
`MEDIAMTX_PATH_MODE`). Custom templates use `{camera}` plus MediaMTX
strftime tokens (`%Y %m %d %H %M %S %f`).

## One-minute, path-addressable clips (integrator layout)

```
RECORDING_LAYOUT=date-hour
RECORDING_SEGMENT_SECONDS=60
```

produces e.g. `…/cam-301/2026-08-13/14/23-05.mp4` — an external system can
compute the file path directly from a timestamp.

**Two things to know about segments:**

- Clips are named by their **start time** and are *approximately* the
  segment length — MediaMTX splits on keyframe boundaries, so a clip may
  start a second or two off the minute.
- A clip that starts near the end of an hour (e.g. `14:59:40`) lives in the
  `14/` folder but its footage runs a few seconds into `15:00`. Retrieval
  should treat a file as "the clip whose start ≤ your timestamp," not
  "exactly `HH/MM` = that minute."

## Timezone

Time tokens (`%H`, `%d`, …) follow the **MediaMTX container's timezone**.
Set `TZ` on the MediaMTX container for local-time folders; the default is
UTC. (First-class local-time handling across the UI and APIs is tracked
separately.)

## Changing the layout

The layout applies to **newly recorded** segments. Existing recordings keep
their original paths; retention and playback read both. Changing layout on a
running system is safe — it does not move or rewrite existing files.
