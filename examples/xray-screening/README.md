# X-ray Screening

Reads a baggage X-ray scanner's screen as a camera. For every bag that goes
through, it records how many trays there were and what they were made of, and
flags the scans a person should look at.

| Alert | Severity | When |
|---|---|---|
| Dense object the X-ray cannot see through | high | a solid near-black shape; precious metal looks like this |
| Small dense item | medium | compact, very dark, about jewellery size |
| Too cluttered to clear on screen | medium | so much metal and electronics that something small could hide in it |

Each alert carries the scan with the shapes boxed (red: dense object, orange:
small dense item). The same boxes appear on the scanner camera's live view
while that bag is on screen, once a superuser turns the app's overlay on.

**It never says "gold".** A lead-lined pouch or a steel weight looks the same
on screen, and an operator who learns to trust "gold" will believe "no gold"
from a tray the X-ray cannot read. It says *look here*.

## Connecting a scanner

The app needs the scanner's screen as a camera:

* **A real scanner:** most have a video output (VGA/HDMI/DVI) for a second
  monitor. An HDMI-to-RTSP encoder or a capture card on that output becomes an
  ordinary camera in OpenNVR.
* **Testing without one:** put exported scan images in a folder under
  `data/fake-cameras/`. The fake-camera rig plays a folder of stills as a
  slideshow, one scan every 4 s (see `docs/FAKE_CAMERAS.md`).

Then, in the App Catalog:

1. Install **X-ray Screening**, and pick the scanner's camera.
2. Optional: **Set up** on the camera card, and draw round the part of the
   screen that shows the scan, leaving out the scanner's menus and status bar.
   If you skip this, the whole picture is read.

## How it reads the screen

* **One event per bag.** A scan is read once the picture has held still for
  0.6 s after changing. So a bag scrolling in isn't read half-drawn, and one
  left on screen is read once. Any change counts, however small, because two
  near-empty trays are still two bags.
* **Repeats aren't recounted.** The same image shown again within an hour
  (a replayed recording, or an operator calling a scan back up) is skipped.
  A real belt never shows the same bag twice.
* **Colours are the scanner's own:** orange organic, green mixed, blue metal,
  near-black where the beam did not get through.
* **Sizes are fractions of the scan's height squared.** The height of a scan is
  the belt, the same for every bag, while its width depends on how many trays
  went through together. So thresholds hold on a 576 px export and a 1080p
  capture alike, and for one tray or two.
* **Trays:** a scan wider than 0.8× its height is two trays side by side.

## What it can't do

* **It has never seen gold.** The thresholds were set on everyday metal
  (phones, watches, tools, tins): dense shapes on those are flagged, phones
  and watches are not. Before relying on it, run test bags with known gold
  (a ring, a chain, a coin, a small bar, and gold hidden in a shoe, under a
  phone and inside a tin) and tune **Dense object size** and **Smallest dense
  item** on the results.
* **It reads one screen.** It reads what the scanner chose to show, at the
  screen's resolution. It is not the scanner's raw data.
* **A noisy capture card** can make one bag count as several. Raise **How much
  the screen must change** if counts run high.

## App page

The app page shows scans, trays and flagged scans in total and per scanner,
the last 12 flagged scans as thumbnails, and a log of recent scans.

## Developing

```
xray/core.py          the analysis and the one-event-per-bag watcher; no platform imports
xray_screening.py     the platform side: stream, alerts, overlay, state
tests/                synthetic scans: no scanner images ship with this repo
```

```bash
cd examples/xray-screening
python -m pytest tests -q
```
