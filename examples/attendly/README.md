# Attendly on OpenNVR

Classroom attendance on your OpenNVR cameras. Students enrol their faces by
photo or webcam; rooms are cameras; courses have sections; a weekly planner
publishes classes; during each class the app takes recognition snapshots that
finalise into a PRESENT/ABSENT roster you can export.

Attendly runs beside OpenNVR the way the Camera Agent does: its own
container, its own web UI on port 9300, a card in the App Catalog whose
**Open app** button opens that UI in a new tab, and sign-in with your OpenNVR
account. OpenNVR does the lower layer: frames come from core's snapshot path,
detection and recognition go through KAI-C to the InsightFace adapter, which
the `attendly` compose profile starts.

## Run it

OpenNVR must be running. From the repo root:

```bash
docker compose -f docker-compose.yml \
               -f docker-compose.apps.yml \
               -f docker-compose.attendly.yml \
               --profile attendly up -d
```

or let the installer pick it: `./scripts/install.sh` lists **attendly** next to
**camera-agent**, which writes to `.env`:

```
OPENNVR_EXAMPLE_COMPOSE=docker-compose.attendly.yml
OPENNVR_EXAMPLE_PROFILE=attendly
```

Then open `http://<host>:9300/` — or OpenNVR → Apps → Attendly → **Open app**.
Set `ATTENDLY_PUBLIC_URL` in `.env` when the app sits behind a reverse proxy
or a public hostname; the catalog card and the manifest carry that address.

## What you do in it

1. **Manage Rooms** — bind a classroom to one of OpenNVR's cameras.
2. **Manage Courses** — course codes and their sections.
3. **Image Register / Video Register** — enrol a student from photos or the
   webcam. The face model refuses frames with no face or several faces; a
   face already enrolled for another student is reported as a duplicate.
4. **Manage Schedule** — the weekly timetable; *Publish Today* turns it into
   concrete class windows on each room.
5. **Attendance** — finalised rosters per class, with PDF/Excel export.

During a class window the room's worker fetches a frame from core every
attempt interval (2 min), detects every face, recognises each crop, and
aggregates who was seen per 10-minute slot. At the end of the class a student
is PRESENT when seen in at least 60 % of the slots. Timing knobs are
`ATTENDLY_*` settings in `attendly/config.py`.

## Hostel gates, the mess, and staff

Three more jobs on the same cameras and the same face database:

* **Hostel flap barriers.** Enrolment asks whether a student is a *day
  scholar* or a *hostler*, and which block a hostler lives in. On the
  **Gates** page add a gate of kind *hostel* per block entrance, bound to the
  camera at the barrier. The gate's worker looks at the camera every second,
  takes the largest face (the person at the barrier), recognises it, and
  decides: a hostler of that block is allowed, everyone else refused. Every
  decision is logged on the page.
* **Mess entry against meal bookings.** Students book meals for the next day
  (the **Meal Bookings** page, a CSV import, or your existing booking system
  calling `POST /meals/bookings` with an OpenNVR token). A gate of kind
  *mess* admits a student only inside a meal window and only with an unused
  booking for that meal; passing consumes the booking, so the QR code at the
  counter is replaced by the face at the barrier.
* **Staff attendance.** The **Staff** page enrols staff from photos (their
  own `ST` codes, their own category in the face database) and defines
  departments: a camera plus a working window. Inside the window the camera
  is checked every minute; the first sighting of the day is the check-in,
  the last one the check-out. **Staff Attendance** shows the day per
  department, with ABSENT for anyone not seen, and exports CSV.

### Driving the barrier

Attendly decides; it never touches a relay. Each gate decision goes on the
platform bus as the contracted `access.decided.v1` event (keyed by the gate
camera's handle, with the student code in `plate_text` and the name in
`owner`), and OpenNVR's **gate-controller** app closes the barrier's contact
on `allow` over the wiring your site has: a GPIO dry contact, a Shelly-style
IP relay, Modbus, an ONVIF door controller or MQTT. Start it with the
`gate-controller` profile and list each barrier under its camera handle in
`examples/gate-controller/config.docker.yml`; its HARDWARE.md is the wiring
guide. The licence-plate app drives vehicle barriers the same way, so one
controller serves both.

Two shortcuts for simpler sites: a gate can carry an **HTTP trigger URL**
(attendly GETs it on allow, for a barrier whose own relay takes one), and a
barrier controller with its own camera can POST a JPEG to
`/gates/{id}/check` and act on the JSON decision.

## Roles

Attendly keeps no users. OpenNVR's roles map to three tiers: **viewer** reads
reports, **operator** enrols students and staff, plans classes, books meals and
configures gates, **admin** also deletes students, staff, gates and
departments and re-finalises classes.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OPENNVR_URL` | `http://opennvr-core:8000` | core (cameras, snapshots, login, catalog) |
| `KAIC_URL` | `http://opennvr-core:8100` | KAI-C (`face_detection`, `face_recognition`) |
| `OPENNVR_INTERNAL_API_KEY` | — | the site key, as the overlay passes it |
| `ATTENDLY_FACES_URL` | `http://insightface-adapter:9005` | the adapter's `/faces/*` (enrol, list, delete) |
| `ATTENDLY_RECOGNITION_THRESHOLD` | `0.5` | cosine similarity to count a match |
| `ATTENDLY_DUPLICATE_THRESHOLD` | `0.65` | similarity that flags a duplicate at enrolment |

| `ATTENDLY_GATE_RECOGNITION_THRESHOLD` | `0` (= recognition threshold) | a stricter match for admitting people |
| `ATTENDLY_GATE_POLL_SECONDS` / `ATTENDLY_GATE_COOLDOWN_SECONDS` | `1` / `8` | how often a gate camera is looked at; how long the same person is not re-decided |
| `ATTENDLY_MEAL_WINDOWS` | `breakfast=07:00-09:30,lunch=12:00-14:30,dinner=19:00-21:30` | when the mess serves; a gate may override |
| `ATTENDLY_STAFF_POLL_SECONDS` | `60` | how often a department camera is looked at |
| `ATTENDLY_STAFF_WORK_START` / `ATTENDLY_STAFF_WORK_END` | `09:00` / `18:00` | the default working window for a new department |
| `NATS_URL` | `nats://nats:4222` (overlay) | the bus for `access.decided.v1`; blank = decisions are logged only |

Both thresholds are starting points. Tune them against real enrolments on
your cameras: lower the match threshold if known students go unrecognised,
raise it if strangers are matched. Changes take effect on restart.
| `ATTENDLY_PUBLIC_URL` | — | browser-reachable URL; blank = `http://{host}:9300/` |
| `ATTENDLY_AUTH_MODE` | `opennvr` | `none` opens every endpoint (loopback development only) |
| `ATTENDLY_DATABASE_URL` | `sqlite:////data/attendly.db` (image) | the app's own SQLite |
| `ATTENDLY_TIMEZONE` | `Asia/Kolkata` (`$TZ` in the overlay) | class windows are local times |
| `ATTENDLY_SAVE_DEBUG_FRAMES` | `false` | keep attempt frames and face crops under the data dir |

## Development

```bash
cd examples/attendly
uv sync --extra dev
uv run pytest -q                      # 41 tests, no OpenNVR needed
ATTENDLY_AUTH_MODE=none ATTENDLY_LISTEN_HOST=127.0.0.1 uv run python attendly_app.py --port 9300
cd web && npm ci && npm run dev       # Vite on 5173 against the app on 9300 (see .env.example)
```

The image builds the React app in a node stage and installs exactly what
`uv.lock` says: `docker build -f examples/attendly/Dockerfile .` from the repo
root. There is no node on the dev box, so that build is the frontend's type
check.

## How it is wired

```
browser ──► attendly :9300 ──► /api/auth/*  ──► core /api/v1/auth/{login-json,refresh,me}
                      │ ──► /cameras, snapshots ──► core /api/v1/internal/…  (site key)
                      │ ──► detect / recognise  ──► KAI-C /api/v1/infer/insightface
                      │ ──► enrol / list / delete ──► insightface-adapter /faces/*
                      └──► SQLite on /data (students, rooms, courses, planner, snapshots, rosters)
core ──► attendly /health, /manifest, /state   (the catalog card; self-registered on boot)
```

Student codes (`AT00001`…) are the adapter's `person_id`; every entry attendly
writes carries `category=attendly_student`, which keeps it apart from the
Smart Doorbell's household in the deployment's one shared face database.
Names, courses and sections live in attendly's SQLite.

See `IMPLEMENTATION_PLAN.md` for what was replaced from the AWS version and
the testing plan.
