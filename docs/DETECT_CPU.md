# Where Tier-0's CPU goes, and how we spend less of it

This page explains the strategy behind OpenNVR's detection CPU usage in one
place: what actually costs CPU, which savings are on by default (and why they
are safe), and which are opt-in dials (and what each one trades). The per-dial
reference lives in [`detect-pipeline/README.md`](../detect-pipeline/README.md);
this is the why.

## The one fact that drives everything: decode dominates

The detect pipeline looks like `decode → motion → regions → detector → track`,
and intuition says the neural network is the expensive part. It isn't. The
model only ever runs on small cropped regions, and only when motion or an
active track asks for them; motion itself runs on a ~100-pixel-tall grayscale
downscale. What runs unconditionally, at the camera's full frame rate, in
full resolution, is **video decode** — and video decode of a 25 fps stream
costs the same whether you analyze 25 frames a second or 2, because the
frame-rate filter that drops frames runs *after* they have been decompressed.

A 2880×1616 @ 25 fps main stream decodes ~116 million pixels per second.
The same camera's 704×576 substream decodes ~10 million — and if only
keyframes are decoded, ~0.4 million. Every mechanism below is some version of
the same idea: **decompress fewer pixels**.

## On by default — because they cannot lose anything

These ship enabled. All but the last are provably lossless for detection;
adaptive decode is default-on for a different reason — recording never goes
through the detector, so its trade is bounded to detector latency, never to
lost footage:

**The substream tap.** Tier-0 decodes the camera's low-res second stream, not
the recording-quality main stream (store the substream URL on the camera's
settings page). This is ~60× fewer pixels for typical cameras, and detection
accuracy is unaffected because the detector's input is a fixed small square
either way — only the resolution of saved evidence crops changes. Recording
always keeps the full main stream; the detector's diet has no effect on it.

**`DETECT_FPS=2`.** Frames analyzed per second. Detection work scales
linearly with it; 2 is enough granularity for presence, counting, and
tracking of people/vehicles at walking-to-driving speeds.

**`DETECT_DECODE_SKIP=nonref`.** Tells the *decoder* to skip frames that no
other frame references. By definition nothing depends on a dropped frame, so
this cannot corrupt video, and the analyzed rate stays far above
`DETECT_FPS`. It saves real CPU on streams that carry such frames (B-frames)
and is a no-op on streams that don't — never worse, sometimes better.

**`DETECT_DECODE_THREADS=2`.** ffmpeg's default is auto — up to 16 decoder
threads *per camera*, which on substream-sized video is pure scheduling
overhead multiplied across the fleet (Frigate pins 2 for the same reason).
Thread count never changes decoded output.

**`DETECT_DECODE_IDLE=nokey` (adaptive decode).** The pattern Blue Iris
ships as "limit decoding unless required," and the biggest saving on a
CPU-only box: while a camera's scene is quiet, decode *keyframes only*
(~one frame per GOP — near-zero cost) and keep watching motion at that
rate; the first motion box or live track flips the camera back to full
decode by respawning its ffmpeg against the local MediaMTX republish
(sub-second), and after `DETECT_DECODE_IDLE_AFTER` quiet seconds it idles
again. This is on by default because **recording is unaffected** — the
full main stream is always recorded, so nothing is ever lost; what the
default accepts is that the *detector's* reaction to a brand-new event on
a quiet scene can lag up to one GOP (~1–2 s), and an event briefer than
the keyframe interval can pass undetected while idle (it remains in the
recording). Set `DETECT_DECODE_IDLE=none` when sub-second detector
reaction matters more than CPU. `tier0_decode_idle{camera}` on `/metrics`
shows who is idling.

**Motion/stationary gating and bounded load** (`DETECT_STATIONARY_INTERVAL`,
`DETECT_MAX_REGIONS`, `DETECT_MAX_TRACKS`, `DETECT_LABELS`, `DETECT_CONF`).
The detector runs only where motion or a live track justifies it, parked
objects are re-verified on an interval instead of every frame, and worst-case
work per frame is capped no matter what the scene contains.

## Opt-in dials — because each one trades something, and the trade is yours

**`DETECT_DECODE_SKIP=nokey` (static).** The same keyframes-only decode,
permanently — simpler than adaptive, same trade all the time.

**`DETECT_DECODE_FAST=true`.** Skips the h264/h265 in-loop deblocking filter
(~10–20% of software decode). Deblocking exists for viewing quality;
detection is robust to the blockiness, but decoded pixels drift slightly from
the encoder between keyframes, so it is not bit-exact — hence opt-in. CPU
decode only; hardware decoders deblock in silicon for free.

**`DETECT_HWACCEL=vaapi|nvidia|qsv|rpi|rkmpp|jetson`.** Moves decode off the
CPU entirely. With an accelerator the pipeline also switches to the full-res
main stream automatically (`INFERENCE_TAP_STREAM=auto`) for full-res evidence
crops — hardware decode makes that affordable.

## The lever that costs nothing at all: the camera

Decode cost scales with the **source** frame rate before any of the above
applies. Most cameras let you set the substream's own encode settings: 5–10
fps and a 1–2 s keyframe interval is ideal for a detection feed (Tier-0
analyzes 2 fps regardless), and it also makes `nokey`/adaptive idle sampling
tighter. Do this first — it is the cheapest CPU you will ever save, and it
costs zero accuracy at the rates Tier-0 actually analyzes.

## Why not rewrite it in C/C++?

Because the hot loops already are: decode is ffmpeg (C), motion and the
detector run inside OpenCV (C++). Python orchestrates at ~2 frames per
second per camera, which profiles as noise. The savings in this domain are
architectural — decode less, decode later, decode only when something is
happening — not linguistic. The one genuinely native-code frontier is
compressed-domain motion detection (reading h264 motion vectors without
reconstructing pixels at all); it's research-grade and noted on the
[roadmap](ROADMAP.md) horizon, not needed while the dials above still leave
headroom.

## Verifying what a camera is actually doing

Every dial above is exported, so a dashboard can answer "is this camera
really running what I configured?" without reading container logs:

| Signal | Metric |
|---|---|
| The static decode dials in force | `tier0_decode_config{camera,skip,threads,fast,hwaccel,idle}` |
| Currently in cheap keyframe-only mode? | `tier0_decode_idle{camera}` (1 = idle) |
| Adaptive decode flapping? | `tier0_decode_mode_changes_total{camera}` |
| Decoding a main stream by mistake | `tier0_mainstream_fallback{camera}` |
| Detector work avoided by the motion gate | `tier0_detector_skipped_total{camera,reason}` |
| Detector work avoided by stationary gating | `tier0_stationary_skipped_total{camera}` |
| Where the load shedder settled | `tier0_regions_budget` vs `tier0_regions_configured` |
| What it all cost | `tier0_process_cpu_percent`, `tier0_frame_latency_seconds` |

`tier0_decode_config` is a Prometheus info-metric — its value is always 1
and the payload is the labels, so it joins onto the numeric series:

```promql
tier0_process_cpu_percent
  * on(camera) group_left(skip, hwaccel) tier0_decode_config
```

It reports the **effective** backend, not the requested one. A camera
configured for `vaapi` whose GPU turned out unusable reports
`hwaccel="cpu"` — that downgrade is silent apart from one startup warning,
and it is the difference between ~0.3 and ~2 cores. The same applies to an
invalid `DETECT_DECODE_SKIP`, which degrades to a safe default rather than
refusing to start. OpenNVR's own AI page shows this per camera beside the
load signals.

## If your detect container is still hot, in order

1. Is a camera decoding its main stream? (`tier0_mainstream_fallback` on
   `/metrics`, or a startup warning) → store the substream URL.
2. Lower the substream's encode fps in the camera itself (5–10 fps).
3. Check for perpetual motion: a burned-in OSD clock ticking every second
   defeats motion gating — disable the overlay or mask it.
4. Confirm adaptive decode is on (`DETECT_DECODE_IDLE=nokey`, the default)
   and check `tier0_decode_idle` — a camera that never idles has perpetual
   motion (see 3) or a genuinely busy scene.
5. `DETECT_DECODE_FAST=true` on CPU-only hosts.
6. Attach a hardware decoder (`DETECT_HWACCEL`) — the structural fix.
