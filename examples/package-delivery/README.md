# Package Delivery

What is waiting at each door, who brought it, and who took it.

The question a person has about their doorstep is not "was a suitcase
detected" but "is my parcel still there, and if not, who has it". This
app answers that, and it gets a first-class page — **Deliveries** — with
the doors, what is waiting since when, today's deliveries with their
before/after photos, and the buttons a person actually needs: collected,
not a package, snooze, acknowledge, check now.

## How it works

Two kinds of evidence, each used for what it is good at.

**Tier-0, always on, for who and when.** The detection stream the
platform already produces says when a person walks up to the door and
leaves, and whether a van or a car stopped outside. That is the trigger:
somebody left the doorstep, so the doorstep may have changed. It costs
nothing extra — no frame is fetched and no model runs during the hours
in which nobody comes.

**A KAI-C skill, on demand, for what.** COCO, which Tier-0 runs, has no
package class at all. So when the trigger fires the app waits a few
seconds for the courier to clear the step, takes one snapshot, and asks
the best skill this box has to count the parcels inside the drawn porch
zone. It asks KAI-C what is registered and chooses, best first:

| Skill registered in KAI-C | How parcels are counted | Quality |
| --- | --- | --- |
| an adapter advertising `package_detection` | its detections inside the zone | good |
| an object detector whose classes include a box/parcel | those detections inside the zone | good |
| a VQA model (`vqa` — moondream, qwen-vl, …) | "How many parcels or boxes are on the doorstep? Answer with a number." | fair |
| none of the above | the COCO bag classes on the Tier-0 stream stand in (`suitcase`, `backpack`, `handbag`) | proxy |

The choice is re-made every five minutes, so installing a package
model this afternoon takes effect this afternoon, and the page says
which skill is counting and how far to trust it. On a stock install
the platform widens Tier-0 to the stand-in classes on the cameras
picked for this app (`tier0_labels` in the manifest), so the proxy works
without touching `DETECT_LABELS`.

Re-counts also run on a cadence — every 15 minutes while parcels wait,
hourly otherwise — to catch the collection nobody walked past the
camera for and the delivery Tier-0 missed.

### Delivered, collected, taken

A count going up after someone left is a delivery; a count going down is
a pick-up. The severity of a pick-up is decided by evidence, and every
reason is kept and shown on the page and in the alert:

- **a known face** at that door within the window — Smart Doorbell's
  `known_visitor` alert on the bus — makes it an owner pick-up;
- **the same person who brought it** taking it straight back is a
  courier correcting a mis-delivery, not a theft;
- **outside delivery hours**, a **quick grab** by somebody else within
  minutes of the delivery (the follow-the-van pattern), and a site that
  is **armed away** each weigh towards *taken*; a site that is *disarmed*
  weighs the other way;
- a vehicle that stopped outside is recorded as context — thieves drive
  too, so it decides nothing on its own;
- **nobody seen** at all (wind, a courier out of view, a count that
  wobbled) is reported gently, never as an accusation.

A pick-up with no identity information is reported as *unknown* at low
severity, not dressed up as theft. The person can say who it was from
the page; a *taken* alert is high severity and always fires.

Parcels are state, not events: a parcel waiting since 12:18 is one parcel
with one clock however many times a shadow moves over it. Reminders
come on the cadence you set, stop when acknowledged or snoozed, and stop
for good when it is collected — the next delivery starts them again.

### What the platform gives Home Assistant

Declared in the manifest, rendered by the platform: parcels waiting
(site and per door, as a count and as an occupancy sensor), deliveries
and parcels taken today, last delivery per door, and *Collected* and
*Acknowledge* buttons per door.

## Setup

Install from the App Catalog, pick the doors, and **draw the porch zone**
around the step where parcels are actually left. Nothing drawn means the
whole frame, which counts the plant pot and the doormat too; the page
says which doors still need one. Set the delivery hours for your area.

### The counter's grade, and what changes it

The page grades its own counts, so the grade is worth reading:

| what is registered with KAI-C | grade |
|---|---|
| the package detector that ships with this app | **good** |
| a VQA model (moondream, qwen-vl, …) | fair |
| nothing package-capable | proxy — COCO bags stand in |

**It should say *good* on a stock install.** The detector, its weights
and its KAI-C registration all come up with the app's compose profile:

```bash
docker compose -f docker-compose.yml -f docker-compose.apps.yml \
  --profile package-delivery up -d
```

That starts `package-detection-weights-init` (which copies a pre-baked
`yolov8n-package.onnx` into a volume), then the adapter, then a one-shot
registration under the task name `package_detection` — which is what the
app's picker matches on. Nothing to download by hand, and nothing
fetched at first boot, so it works on a filtered network.

If the page still says *fair* after five minutes, the detector is not
registered. In order of likelihood:

```bash
docker compose logs package-detection-register   # did registration succeed?
docker compose logs package-detection-adapter    # did the model load?
docker compose logs package-detection-weights-init
```

A "fair" grade is not the app rating the model poorly. It is the app
telling you a package detector is not answering, and counting with the
next best thing it could find.

### Accuracy, and fine-tuning for your porch

No general model knows *your* doorstep. A parcel in the rain on a dark
mat, a padded mailer against a stone step, a camera looking straight
down — each of these is where a stock detector wobbles, and where a
model trained on that one camera's frames does better. The app is
designed so that this is a skill you add, not a change to the app: a
`package_detection` adapter fine-tuned on frames from your own cameras
registers with KAI-C and is picked over everything else the next time
the app looks (within five minutes), with no restart.

The shipped `package-detection` adapter is exactly that recipe — the
openly licensed *package at front door* set (1,293 doorstep images,
MIT) and Roboflow's public *packages* set (CC0), fine-tuned on
YOLOv8n — and its `train_package_model.py` in the ai-adapter
repository merges frames from your own cameras into the next
training run (`package_data/site/`). For a
deployment where the counts matter — a building lobby, a business
receiving stock — plan on fine-tuning with a few hundred frames from
the actual cameras; the *Not a package* and *Collected* buttons on the
Deliveries page are the corrections such a set is built from.

Installing your own export takes one of two forms. Either bake it into
a weights image of your own and point the stack at it:

```bash
PACKAGE_DETECTION_WEIGHTS_IMAGE=registry.example.com/our-doorsteps:v3
```

or drop the file straight into the volume, which is quicker while you
are still iterating:

```bash
docker compose cp ./yolov8n-package.onnx \
  package-detection-adapter:/app/model_weights/yolov8n-package.onnx
docker compose restart package-detection-adapter
```

The filename matters — the adapter looks for `yolov8n-package.onnx`
inside `PACKAGE_DETECTION_WEIGHTS_DIR`, and a file under another name
is a model it cannot find and a grade that quietly stays *fair*.

If you would like help training a model for your site, or want your
adapter listed in the catalog, open an issue on the OpenNVR repository.

## Standalone

```bash
cp config.example.yml config.yml   # nats_url, cameras with porch zones
uv sync && uv run package-delivery --config config.yml
uv run pytest
```

`config.example.yml` documents every key. With `opennvr_url` set and no
`cameras:` list, cameras and zones come from the App Catalog. Set
`KAIC_URL` (or `kaic_url`) so the app can ask KAI-C what can count.

## Upgrading from 1.0

1.0 drove its own detector through KAI-C every three seconds and could
only see the COCO bag classes; it rated a pick-up by whether *anybody*
was seen, which told you nothing about who. 1.1 rides Tier-0, counts
on demand with whatever skill the box has, and rates pick-ups by
evidence. The `roi` per-camera key still loads (it is the `zone` now);
the 1.0 tuning knobs (`poll_interval_seconds`, `arrive_consecutive_hits`,
`gone_consecutive_misses`, `linger_alert_after_seconds`,
`pickup_person_lookback_seconds`, `dedup_window_seconds`) are ignored.
Alert kinds are `package_delivered`, `package_picked_up`,
`package_taken` and `package_reminder`.
