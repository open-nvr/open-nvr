# smart-doorbell example app

The third producer-side first-party OpenNVR example. Polls a
doorbell camera, runs face recognition via the InsightFace adapter
through KAI-C, and fires alerts with severity based on whether the
visitor is registered family, a known friend, or a stranger.

The enrollment flow is **pure REST** — no shared filesystem,
no desktop tool. Snap a photo, run one CLI command, the face is
registered. Same flow Frigate / Shinobi force you to set up via a
web UI.

## What it does

```
┌─────────────┐   every poll_interval_seconds
│  Doorbell   │ ──────────────────────────────────┐
│   camera    │                                   │
└─────────────┘                                   ▼
                              ┌───────────────────────────────────┐
                              │ frame_sources.fetch (HTTP / file) │
                              └──────────────┬────────────────────┘
                                             │ frame JPEG bytes
                                             ▼
                              ┌───────────────────────────────────┐
                              │ KAI-C → InsightFace adapter       │
                              │   POST /api/v1/infer/insightface  │
                              │   params={task:"face_recognition"} │
                              └──────────────┬────────────────────┘
                                             │ FaceRead
                                             ▼
                              ┌───────────────────────────────────┐
                              │ classify: family / known / unknown │
                              └──────────────┬────────────────────┘
                                             │
                                             ▼
                              ┌───────────────────────────────────┐
                              │  AlertDispatcher (stdout/webhook  │
                              │  /NATS). Unknown-face alerts      │
                              │  carry a base64 JPEG snapshot in  │
                              │  the envelope so a small relay    │
                              │  can post it to Telegram/ntfy.    │
                              └───────────────────────────────────┘
```

A single `correlation_id` flows through every step so KAI-C's audit
log joins the chain end-to-end: alert → KAI-C inference event →
adapter audit line.

## Why the REST-only enrollment matters

Most NVRs make you upload faces through a desktop GUI or copy files
to a shared volume. This one needs neither — `python
smart_doorbell.py enroll --image alice.jpg --person-id alice` works
from any machine that can reach the adapter, including a phone or a
small Python script. Side effects:

* You can enroll over Tailscale / VPN without exposing the camera.
* You can script bulk enrollment from a folder of family photos.
* Re-enrolling (haircut, glasses, weight change) is idempotent —
  same `person_id`, new image, overwrites the embedding.
* The face DB persists at `OPENNVR_INSIGHTFACE_FACE_DB` on the
  adapter; it survives restarts but never holds raw images,
  only the 512-d embedding vectors.

## Who came to the door, and when

The feed and the stranger wall used to be in-memory only, so a restart
or a redeploy wiped every stranger the doorbell had ever seen. They are
now written to the app's durable store in core, which means "who came to
my door three days ago" has an answer that survives `docker compose up`.

Two settings govern how long the door remembers, both editable from the
App Catalog:

| Setting | Default | What it does |
| --- | --- | --- |
| `history_days` | 30 | How long a visit stays in the history. |
| `history_max` | 200 | Hard cap on remembered visits, whatever the age. |

Each visit costs a line of text — when, which camera, recognised or not,
and who. Only the most recent unrecognised visits also keep a thumbnail,
because those are the tiles anyone actually looks at; an older stranger
keeps the line and the wall shows **snapshot aged out** in place of the
picture. The visit still happened, and the caption is the answer to the
question — the photo was only ever the nicer half of it.

The full-size crop goes to the platform's evidence store, where alerts
already cite their pictures and where OpenNVR's own retention governs
it. A tile restored from history therefore cannot be enrolled from: the
full crop is not in this process, and enrolling from a 190 px wall
thumbnail would teach the adapter a worse face than the operator thinks
they are giving it. Enrol a stranger while the tile is fresh.

Why the identity is the app's own record rather than a claim on the
platform's visit rows: this app polls snapshots, so it has a frame and a
face but no `event_id`. Attaching a name to a platform visit would mean
guessing which visit the frame belonged to by matching timestamps, and a
guessed identity in the shared store is indistinguishable afterwards
from a measured one.

## The bell

An alert and a chime are different events, and treating them as one is
what makes doorbells annoying. Every face at the door is worth
**recording**; only some are worth **interrupting** somebody for. The
alert, the feed, the history and the operator inbox are unaffected by
anything in this section — it decides one narrower question: does
something ring, and what does it play.

| Setting | Default | What it does |
| --- | --- | --- |
| `chime_enabled` | `true` | Off still alerts and records; nothing rings. |
| `quiet_hours` | *(none)* | `HH:MM-HH:MM` in which only an `alarm` tone rings. |
| `rechime_seconds` | 300 | Least gap between rings for the same caller. |
| `chime_tones` | see below | Tone per person category. |

The shipped table, which is opinionated on purpose:

| Category | Tone | Why |
| --- | --- | --- |
| `family`, `resident` | `none` | A bell that rings when the family comes home is the bell people stop hearing — and then it does not work for the stranger either. |
| `friend`, `staff` | `chime` | Announce, softly. |
| `contractor`, `visitor` | `ding_dong` | Somebody with business at the door. |
| `watchlist` | `alarm` | The one recognised face that must be louder than a stranger. |
| *(unknown)* | `ding_dong` | A caller nobody has enrolled. |

Three rules, each a thing real doorbells get wrong:

1. **Who it is decides the sound**, per the table above.
2. **Quiet hours silence the bell, not the alarm.** A delivery at 03:00
   goes in the log and does not wake the house; a stranger at 03:00
   does. Suppressing that would be a burglar alarm that observes
   bedtime, so only the `alarm` tone overrides the window — which is
   exactly the line between "someone is here" and "something is wrong".
3. **A bell that rings twelve times is noise.** `rechime_seconds` is
   separate from, and longer than, `dedup_window_seconds`: being told
   again in a feed costs nothing, being rung at again costs attention.
   A suppressed ring does not restart the clock, so somebody walking
   past during quiet hours cannot delay the ring that should follow it.

**Ringing is somebody else's job, deliberately.** This app decides; it
does not make a noise. The decision rides in the alert envelope, as
`evidence.chime` and a `chime:<tone>` tag, so whatever the household
actually rings — the alerts-subscriber relay into ntfy or Telegram, a
Home Assistant automation over OpenNVR's MQTT bridge, a webhook to a
smart speaker — plays the right tone at the right time. An app that
tried to own the speaker would work on exactly one deployment.

```json
"chime": { "tone": "ding_dong", "ring": false,
           "reason": "quiet hours (22:00-07:00)" }
```

When it decides **not** to ring it says why, in words, in the same
envelope and on the dashboard. A doorbell that silently chose to stay
quiet is indistinguishable from a broken one, and whoever is debugging
it in the morning has nothing else to go on.

## Honesty up front

Real-world failure modes the example does NOT yet handle:

* **Twins / siblings with similar embeddings.** Cosine similarity
  doesn't separate strong genetic resemblance reliably; the
  `recognition_threshold` is a global knob, not per-person.
* **Aggressive face-occlusion** (sunglasses, scarf, hat). The
  adapter may detect the face but recognition similarity drops
  below threshold → falls back to UNKNOWN. Set `dedup_window`
  appropriately so a family member walking past doesn't flood you.
* **Bad enrollment photos.** A frontal, well-lit JPEG is what
  InsightFace expects. A side profile gives a poor embedding and
  the person won't match consistently.
* **Spoofing** (printed photo, screen). v0.1 has no liveness
  detection. Planned follow-up.

## Quick start

> **On the compose stack you do none of this.** `docker-compose.apps.yml`
> provisions `insightface-adapter` (published as
> `ghcr.io/open-nvr/insightface-adapter`) and registers it with KAI-C
> alongside the app — `--profile smart-doorbell`, the installer's app
> picker, or one click from the App Catalog. The enrolled-faces DB and
> the ~280 MB `buffalo_l` model pack each live on a named volume. The
> steps below are the bare-metal developer path.

```bash
# 1. Start the InsightFace adapter (in the ai-adapter repo).
#    On first boot the adapter downloads the buffalo_l model pack into
#    ~/.insightface inside the container (InsightFace's default root).
#    Mount a host directory there so the download happens once.
cd ai-adapter
docker build -f adapters/insightface/Dockerfile -t opennvr/insightface-adapter:local .
OPENNVR_ADAPTER_TOKEN=$(openssl rand -hex 16)
mkdir -p face-db model-cache
docker run --rm -d --name insightface -p 9005:9005 \
  -e OPENNVR_ADAPTER_TOKEN=$OPENNVR_ADAPTER_TOKEN \
  -v $(pwd)/face-db:/data \
  -v $(pwd)/model-cache:/root/.insightface \
  opennvr/insightface-adapter:local

# 2. Start KAI-C and register the adapter
cd ../open-nvr/kai-c
INTERNAL_API_KEY=$(openssl rand -hex 32)
AI_SOVEREIGNTY=local_only INTERNAL_API_KEY=$INTERNAL_API_KEY \
  python -m uvicorn main:app --host 0.0.0.0 --port 8100 &
curl -X POST http://localhost:8100/api/v1/adapters/register \
  -H "X-Internal-Api-Key: $INTERNAL_API_KEY" -H "Content-Type: application/json" \
  -d '{"name":"insightface","url":"http://127.0.0.1:9005"}'

# 3. Configure
cd ../examples/smart-doorbell
cp config.example.yml config.yml
# edit config.yml: kaic_api_key, adapter_token, camera frame_url

# 4. Enroll family — one REST call per person
python smart_doorbell.py enroll \
  --config config.yml \
  --person-id alice --name "Alice Smith" --image ~/photos/alice.jpg \
  --category family

python smart_doorbell.py enroll \
  --config config.yml \
  --person-id bob --name "Bob Jones" --image ~/photos/bob.jpg \
  --category family

# 5. Sanity check
python smart_doorbell.py list-faces --config config.yml --category family

# 6. Start the daemon
python smart_doorbell.py daemon --config config.yml
```

You'll see lines like:

```
2026-05-22T18:10:43+00:00 INFO  smart-doorbell: started: 1 cameras, poll=1.0s, threshold=0.50
ALERT [LOW] 2026-05-22T18:11:02+00:00 camera=front-door title='Known visitor at front-door: Alice Smith' correlation_id=a4f1b... alert_id=alrt_8c2d31
ALERT [HIGH] 2026-05-22T18:12:54+00:00 camera=front-door title='Unknown visitor at front-door' correlation_id=8d3f5... alert_id=alrt_91e2bb
```

## Telegram / ntfy / Discord delivery

The example fires alerts to **stdout** (always), **webhook** (any
URL), and **NATS** (any subscriber on `opennvr.alerts.>`). Unknown
faces carry a base64 JPEG snapshot in `evidence.snapshot_b64`, so:

* **Telegram bot** — point `webhook_url` at a small relay that
  reads `evidence.snapshot_b64` and POSTs to
  `https://api.telegram.org/bot<TOKEN>/sendPhoto`. ~15 lines of
  Python or [n8n](https://n8n.io/) / [Node-RED](https://nodered.org/).
* **ntfy** — POST the snapshot as a [ntfy attachment](https://docs.ntfy.sh/publish/#attachments).
* **Discord** — Discord webhooks accept `multipart/form-data`
  with a `file` part; same shape as the Telegram relay.
* **Home Assistant** — subscribe to `opennvr.alerts.app.smart-doorbell.>`
  via the `home-assistant-relay` example (coming next) and the
  doorbell becomes an HA event automatically.

## Operate

| Mode | Command |
|---|---|
| Daemon (production) | `python smart_doorbell.py daemon --config config.yml` |
| One cycle then exit (testing) | `python smart_doorbell.py daemon --once --config config.yml` |
| Enroll | `python smart_doorbell.py enroll --config config.yml --person-id ID --name "Display" --image FILE --category family` |
| List | `python smart_doorbell.py list-faces --config config.yml [--category family]` |
| Delete | `python smart_doorbell.py delete-face --config config.yml --person-id ID` |
| Verbose | `python smart_doorbell.py daemon --config config.yml --log-level DEBUG` |

SIGINT / SIGTERM stops cleanly — the in-flight cycle finishes,
dispatcher drains.

### The People page

Enabling the app lights **Applications → Smart Doorbell** in the main
navigation (the manifest `provides: ["people"]`). That page is the face
directory for a home, a gated premises or an office:

* **Directory** — everyone enrolled, with photo, category, notes, an
  optional *valid until* date, and when the door last saw them. Search
  by name, id or notes; filter by category.
* **Add person** — name, category, notes, expiry, and a photo from one
  of three places: **Upload** a file, snap one from **a door camera**,
  or take one on **this device** (below).
* **Guided capture from this device.** The browser's own camera — a
  laptop or phone webcam — walked through four poses: straight on, a
  quarter turn left, a quarter turn right, and chin lifted for the angle
  a camera above a door sees. Each pose is captured, retakeable by
  clicking its thumbnail, and skippable; the set enrols together, the
  first pose creating the person and the rest appended as samples.

  This exists because a single front-on portrait is the one pose a door
  camera almost never gets: people arrive at an angle, look at the lock,
  glance down at a parcel. It also means a household can be enrolled at
  a desk in a couple of minutes, before anyone has walked past the door.

  Four is where it stops on purpose. Past four the gains come from
  different *light* rather than different angles, and the light at the
  operator's desk is not the light at the door — which is what the
  strangers wall and *Add a photo* are for.

  Requirements, and the failure modes named rather than swallowed:
  browsers hand a page the camera only on a secure origin, which
  OpenNVR's own nginx provides (https, self-signed by default); reached
  over plain http through some other proxy, the dialog says so instead
  of offering a button that does nothing. A blocked permission, a
  machine with no camera, and a camera already held by a video call are
  each reported as themselves. The preview is mirrored so that "turn
  left" is followable, but the frame stored is the true, unmirrored one
  — face embeddings are not mirror-invariant, so enrolling flipped
  samples would cost accuracy against unflipped door footage. The
  camera is released the moment the dialog closes. If some poses are
  rejected (no face found in that frame), the ones that landed are kept
  and the dialog says how many — three of four is a usable person.
* **Strangers at the door** — every unrecognised face the camera took,
  newest first. Click one, and *Enrol this person* turns that snapshot
  into a known face: no photo to go and find.
* **Edit / Remove** — rename, move to another category, set or clear an
  expiry, add a note.
* **More than one photo per person.** Each person holds a set of face
  samples and a match is the best of them, so the porch camera's
  evening, 30-degree view of Alice can sit beside her daytime selfie.
  *Add a photo* on a person appends; the strangers wall offers *This
  is…* so a capture the door missed becomes exactly the sample it
  needed. Five samples from the door camera itself is where recognition
  gets reliable; the adapter keeps up to 32 per person, oldest dropped
  first. "Start over" on the editor replaces the set (a bad first
  enrolment).

Categories and what the door does with them: `family`, `resident`,
`friend` are greeted (low); `staff`, `contractor`, `visitor` are noted
(info); `watchlist` alarms (high) on every sighting; anyone whose
`valid_until` has passed raises `expired_pass` (high) instead of a
greeting. Notes and expiry live in the adapter's face DB as metadata,
so they survive app restarts and image upgrades.

### In the App Catalog

The app page (Settings → App Catalog → Smart Doorbell) carries the same
surfaces in their generic form:

* **Live** — enrolled-face count, known visitors vs strangers since
  start, a per-camera table (ok / waiting / stalled / error, with the
  fetch error spelled out), a thumbnail wall of the latest strangers,
  and the recent-visitor feed.
* **Dashboard** — the same, as one page (`GET /ui`, proxied and
  sandboxed by core).
* **Quick actions** — *Enroll a face* (name + photo + category),
  *Enrolled faces*, *Remove a face*.
* **Config form** — `recognition_threshold`, `dedup_window_seconds` and
  the snapshot knobs apply live; cameras still need a restart.

## Layout

```
examples/smart-doorbell/
├── smart_doorbell.py              CLI + SmartDoorbell driver
├── face_recognition_pipeline.py   Testable pipeline (no daemon loop)
├── alerts.py                      Alert envelope + stdout/webhook/NATS dispatchers
├── chime.py                       When the door rings, and with what
├── visit_log.py                   Who came to the door, kept across restarts
├── frame_sources.py               file:// + http(s):// frame fetchers
├── config.example.yml             Operator config with every option
├── pyproject.toml                 Minimal deps (httpx, PyYAML, nats-py)
├── Dockerfile                     Slim container image
├── README.md                      you are here
└── tests/
    ├── test_face_recognition_pipeline.py   (15 tests)
    └── test_smart_doorbell.py              (16 tests)
```

## Tests

```bash
uv pip install -e ".[dev]"
PYTHONPATH=. pytest tests/
```

31 tests total. The tests stub the recognition client (no KAI-C
needed) and exercise the parser, dedup window, severity routing,
snapshot attachment.

## Why this is a template

Copy this folder, rename for your task, and replace the predicate.
For a `smart-doorbell` the predicate is "the recognised-face DB
returned a match." For other tasks:

* `intruder-after-hours` — recognised-face DB + restricted hours
* `package-delivery` — vehicle/package detection + porch state machine
* `lost-pet-finder` — pet face/breed adapter + watchlist matching

Everything else — KAI-C call, correlation_id, audit trail, alert
dispatch, frame fetching, SIGINT handling, dedup — is the template.
