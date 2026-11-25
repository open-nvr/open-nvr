# Gold Bar Desk

Watches an overhead camera on the desk where bullion bars are weighed and
trimmed, and keeps every bar accounted for: in the trays, on the precision
balance, and in the worker's hands.

| Alert | Severity | When |
|---|---|---|
| Possible missing bar | high | not every bar has been seen at once for 25 s |
| Extra bar on the desk | medium | more bars than the shift started with (a lookalike swapped in?) |
| Bar held away from the trays | medium | a bar close to the worker's body, off the trays and the balance, for more than a second |
| Desk camera blocked or moved | high | the view went dark or changed completely |

Every alert carries a photo of the desk with the bars boxed. Every weighing on
the precision balance is filed with a photo of the balance's display, taken a
second after the bar lands and again before it comes off. That captures the
reading before and after a trim, so it can be read back later. With the
overlay switched on (a superuser setting), the bars are boxed on the camera's
live view too.

## How the count stays honest

A worker trimming bars nearly always has one in hand, on the balance, or under
an arm. So the count on screen is usually one short, and an alarm on "the
count is low" would ring all day. But **hands and arms only ever hide bars,
they never add them.** So the missing-bar alarm asks a different question:
*has the full count been seen at least once in the last 25 seconds?* A bar
under an arm shows again as soon as the arm moves. A bar that has really gone
never does.

* **A hidden bar stays counted.** A bar that disappears from view is dropped only
  once its spot shows bare tray. A bar put back slightly off its old spot
  replaces the old one.
* **Part of a bar counts.** When an arm covers most of a bar, the gold still
  showing past it is counted in whole-bar units. Arms are far darker than
  polished gold, so they don't count.
* **Bars off the trays count:** one on the balance pan, or held in the handling
  zone.
* **Enough looks, not just enough time.** The full count shows only in brief
  moments between arms, and a machine that looks less often misses more of
  them. So the alarm also waits for 50 analysed frames without a full count
  (25 s at the intended 2 fps).
* **Too slow to tell: paused, not guessed.** Below 1 analysed frame a second
  the app can't tell a hidden bar from a missing one. On a loaded test box at
  0.3 fps it raised a false alarm on footage where nothing went missing. So
  below that rate the missing-bar alarm is paused, the desk's status reads
  **TOO SLOW TO VERIFY THE COUNT**, and the app's health turns amber. A false
  alarm on a gold desk is worse than an honest "can't check right now".
* **One stray frame doesn't clear an alarm.** Moving a bar briefly reads as one
  too many, so an alarm clears only when the full count shows in three frames
  running.
* **Sizes are relative to the bars themselves** (their median box on this
  camera), so nothing depends on resolution or camera height.

## Setting it up

1. **Camera.** Mount it overhead, looking down at the desk. **Full resolution
   matters:** a bar must be about 30 px wide to be segmented. OpenNVR hands apps
   a camera's substream when it has one, so either remove the desk camera's
   substream or set it to full resolution. The app says so on its status line
   if it gets a substream.
2. **Install** Gold Bar Desk in the App Catalog, and pick the desk camera.
3. **Set up** on the camera card, and draw these zones:
   * **Bar trays** (required: nothing is counted until it is drawn)
   * **Precision balance pan**: each bar placed here is a weighing
   * **Balance display**: photographed for every weighing
   * **Where bars are handled**: the desk off the trays, where a bar is held
     while worked on
   * **Close to the worker's body**: where the worker sits or stands
4. **Bars at the start of the shift.** Enter the issued count. With 0 the app
   learns it from the first 30 s, which is weaker: whatever is hidden at that
   moment is never expected back. With several desks, use **Start a new
   shift** with each desk's camera and count.

## Actions

* **Start a new shift** (optional camera and bar count): clear the alarms and
  count from now.
* **Acknowledge the count alarm**: once the bars have been checked.

## What it can't do

* **It can't see what it can't see.** One overhead camera mostly shows the
  worker's back, and moves to a pocket or sleeve may be out of view. A second
  camera facing the worker closes most of that gap.
* **It photographs the balance display; it doesn't read it.** The balance's own
  data output (RS-232/USB) would give exact weights per bar, and with them a
  check against swapped lookalikes and a reconciliation of trimmings.
* **It tells bars apart by colour and shape, not by serial number.** Two
  identical bars swapping places are invisible to it, by design.
* **The segmenter is general-purpose** (FastSAM, not trained on bars). On the
  reference footage it found every bar a person could see within one, per frame.
  Very different bars, trays or lighting may need the advanced thresholds.

## Compute

FastSAM-s runs in the app on CPU, on a crop of the trays: about half a second
per frame on two threads of a laptop CPU, when the box has nothing else to do.
Measured on one laptop (i7, no GPU) running the whole OpenNVR stack:

| Load | Per desk |
|---|---|
| core's own detection on 3 cameras + 2 desks + X-ray | 0.3–0.5 fps: **too slow**, alarm paused |
| 2 desks + X-ray, core detection off | ~1 fps: at the threshold |
| 1 desk + X-ray, core detection off | ~1.5 fps |

So plan on a machine with headroom for each desk. The desk's status line says
when it doesn't have enough.

onnxruntime reports usage to Microsoft from the moment it is imported. The
image sets `ORT_DISABLE_TELEMETRY=1`, which stops it; without it, the
platform's egress proxy refuses every attempt and raises an alert for each one.

## Developing

```
bar_desk/core.py        the rules: tracking, counts, alarms, weighings; no platform imports
bar_desk/segmenter.py   FastSAM-s through onnxruntime, decoded by hand
gold_bar_desk.py        the platform side: stream, zones, alerts, overlay, state, actions
tests/                  synthetic frames with a stub segmenter, and the decoder on built tensors
```

```bash
cd examples/gold-bar-desk
python -m pytest tests -q
```

To run it by hand, export the model first (`examples/fastsam-weights/`
shows how), put `FastSAM-s.onnx` next to the app, and use `config.example.yml`.
