# Guard Scan Compliance

Checks that the guard wands every person entering — **left arm, right
arm, front, back** — and flags what the scanner finds.

A showroom screens everyone at the door with a hand-held metal detector.
Whether that screening actually happens, properly, on every person, is
the whole control, and it is exactly the thing nobody can verify
afterwards. This app rules on each screening and keeps the record.

## What it raises

Two kinds of alert, kept apart because they are two different problems
for two different people:

| Alert | Severity | It means |
|---|---|---|
| **Scanner-flagged person** | critical | The wand's red indicator lit on someone. A security matter. |
| **Improper scan** | high / medium | Surfaces were missed, or the order was wrong (if your site scores order). A staff matter. |
| **No scan performed** | high | Somebody walked in without being screened. |

Every alert carries photographs: the person's face, their full body, the
scene — **and the guard's face**, because a procedure alert is about the
guard, and a record that shows only the customer asks the manager to
take our word for who was careless.

**Every screening is recorded, not only the failures.** That is what
makes the compliance figure mean anything: it is complete scans over all
screenings. A store of only the alerts can say how many complaints there
were, never what share of the day went right. The **Entry Screening**
page shows the rate, the trend, the per-guard breakdown and the history.

## Setting it up

1. Install it from the App Catalog and pick your entrance camera (Configure → Cameras).
2. Draw the **scan zone** — where the person being screened stands.
   Anyone inside it is being scanned, so is not the guard.
3. Optionally draw the **guard post**, and set the guard's **uniform
   colour**. Both are worth more than any behavioural guess when the
   site can supply them: on the footage this was built against, the
   uniform matched 75–100% of the guard's frames and 0% of any
   customer's.
4. Leave the rest alone until you have watched it for a day.

Nothing needs editing on disk. Everything above is in the app's config
form.

## The procedure is a site decision

What counts as a proper scan differs between showrooms, so it is
configuration, not code. The `procedure` setting holds the surfaces that
must be covered, what each is worth, whether the **order** counts, and
the score bands:

```json
{
  "steps": [
    { "name": "left_arm",  "weight": 1.0 },
    { "name": "right_arm", "weight": 1.0 },
    { "name": "front",     "weight": 1.0 },
    { "name": "back",      "weight": 1.0 }
  ],
  "order": ["left_arm", "right_arm", "front", "back"],
  "order_weight": 0.0,
  "grades": [
    { "min": 100, "verdict": "compliant",  "severity": null,     "title": "Scan complete and correct" },
    { "min": 75,  "verdict": "partial",    "severity": "medium", "title": "Partial scan" },
    { "min": 1,   "verdict": "incomplete", "severity": "high",   "title": "Incomplete scan procedure" },
    { "min": 0,   "verdict": "no_scan",    "severity": "high",   "title": "Person entered without a scan" }
  ]
}
```

**`order_weight` is 0 by default, and that is deliberate.** On real
entrance footage the guards covered every surface but worked round
whichever side they happened to be standing on. Scoring the sequence
would have raised an alert on scans that were perfectly good. Set it to
`0.3` to let a wrong order pull a scan down to *partial*, or `1.0` to
make the sequence count as much as the coverage — but decide that with
the client, after watching a day of their own footage.

Weights bite too: `back` at weight 3 makes a missed back far more
serious than a missed arm.

## Who the guard is

The guard is **whoever reaches toward people**, not whoever stands
around longest. That sounds fussy until you watch a customer stand
perfectly still at the counter for two minutes while the guard moves
about: dwell-time picks the customer. On the reference footage the
guard's reach rate was 61% of frames and nobody else's exceeded 3%.

The choice is sticky — a challenger has to beat the incumbent by a clear
margin, for a while, before it changes hands — and it survives the
tracker renaming people, which it does constantly.

The app can tell one guard from another within a run but cannot learn
their **name**. That comes from the duty roster, resolved when the
screening is recorded, so a screening keeps the name that was true when
it happened.

## What it cannot do

- **The beep.** The wand also beeps; this reads only the light. Audio
  needs a camera microphone and an audio pipeline, and is the next
  phase. A wired signal from the wand itself would beat both.
- **Red things near the wand.** The light check looks only at the area
  around the guard's scanning hand — a showroom is full of red, and
  searching the whole frame would alarm on the furniture — but red
  clothing on the guard's own wrist can still fool it. Raise `led_ratio`
  if it does.
- **The back pass, from a bad angle.** When the guard steps behind the
  customer they can block the view. Mount the camera high and to the
  side of the screening spot.

## For developers

```
guard_scan/core.py      the screening logic — no platform, no camera
guard_scan/led.py       the wand's red indicator
guard_scan/settings.py  every threshold, in seconds
guard_scan_compliance.py  the app: frames in, alerts and events out
tests/                  the rules on a synthetic clock
```

The split is the point: `core.py` takes frames with keypoints and hands
back screenings, so the awkward cases — a two-minute screening, a wand
that leaves the customer for fifteen seconds, a tracker that renames
someone mid-scan — are pinned down by tests that need no camera, no
model and no core.

```bash
python -m pytest tests/ -q
```

Everything is measured in **seconds**. The prototype counted frames,
which quietly made the same rule stricter on a slow machine than a fast
one: "three frames of dwell" was 0.4s on one box and 0.1s on another, so
a guard who passed at the site failed on the demo laptop.
