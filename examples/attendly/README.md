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

## Roles

Attendly keeps no users. OpenNVR's roles map to three tiers: **viewer** reads
reports, **operator** enrols students and plans classes, **admin** also deletes
students and re-finalises classes.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OPENNVR_URL` | `http://opennvr-core:8000` | core (cameras, snapshots, login, catalog) |
| `KAIC_URL` | `http://opennvr-core:8100` | KAI-C (`face_detection`, `face_recognition`) |
| `OPENNVR_INTERNAL_API_KEY` | — | the site key, as the overlay passes it |
| `ATTENDLY_FACES_URL` | `http://insightface-adapter:9005` | the adapter's `/faces/*` (enrol, list, delete) |
| `ATTENDLY_RECOGNITION_THRESHOLD` | `0.5` | cosine similarity to count a match |
| `ATTENDLY_DUPLICATE_THRESHOLD` | `0.65` | similarity that flags a duplicate at enrolment |

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
