# Attendly on OpenNVR — implementation and testing plan

Attendly is a classroom attendance application: students enrol their faces,
rooms are cameras, courses have sections, a weekly planner publishes classes,
and during each class the engine takes recognition snapshots and finalises a
PRESENT/ABSENT roster. It was built on AWS Rekognition and DynamoDB. This port
keeps the application (forms, flows, planner, finalisation rules, React UI)
and swaps only the lower layer for OpenNVR.

It runs the way the Camera Agent runs: its own container, its own web UI on
its own port, listed in the App Catalog with an "Open app" link that opens in
a new tab, logging in with the operator's OpenNVR account. OpenNVR must be up:
the app reads frames through core, detects and recognises through KAI-C and
the InsightFace adapter, and its compose profile starts that adapter.

## What is replaced

| Attendly (AWS)                              | Attendly on OpenNVR                                              |
|---------------------------------------------|------------------------------------------------------------------|
| Rekognition `index_faces` per photo         | InsightFace adapter `POST /faces/register` (`append=true`)       |
| Rekognition `detect_faces`                  | KAI-C `POST /api/v1/infer/insightface` task `face_detection`     |
| Rekognition `search_faces_by_image` per crop| KAI-C task `face_recognition` with `threshold` param             |
| Rekognition list / describe / delete faces  | adapter `GET /faces?category=`, `DELETE /faces/{id}[/samples/i]` |
| DynamoDB tables                             | SQLite (SQLAlchemy) on the app's `/data` volume                  |
| Own users table, JWT, TOTP                  | OpenNVR login proxied (`/api/v1/auth/login-json`, `/refresh`, `/me`) |
| Operator-typed RTSP URL + OpenCV capture    | OpenNVR camera roster + core snapshot endpoint                   |
| Vite dev server + nginx                     | FastAPI serves the built React app and the API on one origin     |

Thresholds change scale: Rekognition used 0–100 similarity (85 to match, 90
for duplicate checks); InsightFace uses cosine similarity 0–1 (0.5 to match,
0.65 for duplicate checks). Both are settings.

## Layout (open-nvr repo, branch `feat/attendly-app`)

```
examples/attendly/
  attendly_app.py            entrypoint: FastAPI app, contract surface, static UI, auth gate
  attendly/
    config.py                settings (env: OPENNVR_URL, KAIC_URL, OPENNVR_INTERNAL_API_KEY, ATTENDLY_*)
    manifest.py              AppManifest: id attendly, ui_mode external, ui_url http://{host}:9300/
    auth.py                  OpenNVR login/refresh/me proxy, token cache, role tiers
    cameras.py               camera roster + JPEG snapshot through the App SDK
    face/engine.py           FaceEngine: the seven Rekognition-shaped calls on InsightFace
    storage/db.py, models.py, repo.py
                             SQLite; repo keeps the old function names so routers port unchanged
    services/engine.py       class scheduler + per-class snapshot worker (polls core snapshots)
    services/streams_service.py, planner_publisher.py
    routers/                 students, recognition, attendance, streams, classrooms, courses,
                             schedules, planner, video_registration, registry (was aws_admin), cameras
    schemas.py, utils/
  web/                       the React app (Vite + Tailwind), built into web/dist by the Dockerfile
  tests/                     pytest (see below)
  Dockerfile                 node build stage + python runtime, port 9300
docker-compose.apps.yml      service `attendly`, profile `attendly`, depends on core + insightface-adapter
docker-compose.apps.yml      insightface-adapter / insightface-register gain the `attendly` profile
scripts/install.sh           Attendly listed next to the Camera Agent in the app picker
.github/workflows/ci.yml     attendly added to the examples pytest matrix
```

## Steps

1. **Storage.** `storage/models.py` + `repo.py` with the same function names the
   DynamoDB module exposed (`put_student`, `save_snapshot`, `finalize_attendance`,
   `streams_update`, `planner_template_list`, …) returning the same dict shapes.
   Soft deletes use `deleted_at`. Stable attendance row ids are kept
   (crc32 of `class_id:student_code`) so re-finalising overwrites.
2. **Face engine.** `face/engine.py` with `detect_faces`, `search_faces`,
   `index_face`, `list_faces`, `describe_collection`, `delete_faces`,
   `get_faces_by_external_id` + `delete_person`. Detection returns normalised
   `BoundingBox` so the crop code is unchanged. Enrolment tags entries with
   `category=attendly_student` and the student's name; a 422 from the adapter
   (no face / several faces) becomes a per-image validation error.
3. **Cameras.** `cameras.py` wraps `opennvr_app_sdk.OpenNVR`: `list_cameras()`
   and `snapshot(camera_id)` → BGR frame. Rooms store `camera_id`.
4. **Engine.** Same slot / attempt / aggregation / finalise rules. The worker
   no longer holds an RTSP capture: at each attempt it fetches one snapshot
   from core and runs detect → crop → recognise per face.
5. **Auth.** `auth_mode: opennvr` (default): `/api/auth/login-json` and
   `/refresh` proxy core; every API call carries the OpenNVR bearer which is
   validated via `/me` (60 s cache, 120 s grace). Tiers: viewer reads,
   operator writes, admin deletes students and re-finalises. The video
   registration WebSocket takes the token as a query parameter.
   `auth_mode: none` exists for local development on loopback only.
6. **Contract surface.** `/health`, `/manifest`, `/state` are open so the App
   Catalog can probe the app. On boot the app self-registers with
   `POST /api/v1/apps/register` using the site key, exactly like the Camera
   Agent, with `ui_mode="external"` and `ui_url="http://{host}:9300/"`
   (or `ATTENDLY_PUBLIC_URL` when set). The catalog's "Open app" button opens
   that URL in a new tab; the manifest `website` carries the same URL.
7. **Frontend.** Same pages. Changes: same-origin API base, global bearer
   interceptor, login against the proxy, MFA page removed (OpenNVR owns MFA),
   rooms pick a camera from the OpenNVR roster instead of typing RTSP,
   `/aws/*` calls become `/registry/*`, copy text no longer mentions AWS.
8. **Packaging.** Dockerfile (node 20 build stage, python 3.11 runtime, uv
   lock), compose overlay with `depends_on` core healthy + adapter healthy +
   adapter registered, `/data` volume, port 9300 published like 9100.
9. **Listing.** install.sh offers "attendly" beside "camera-agent"; the
   compose profile is `attendly`; CHANGELOG entry; README in examples/attendly.

## Testing plan

Unit and API tests (pytest, no network, no OpenNVR needed) under
`examples/attendly/tests`, run by `uv run pytest -q` and by the CI examples
matrix:

- `test_repo.py` — students (code sequence AT00001…, email uniqueness among
  live rows, soft delete), planner courses/templates, streams schedule and
  finalised flag, snapshots → finalise: ratio and threshold, ABSENT rows for
  rostered-but-unseen students, idempotent re-finalise, class listing order.
- `test_face_engine.py` — respx-mocked KAI-C and adapter: bbox
  normalisation, recognised vs not, append enrolment and the 422 paths,
  sample-id grouping on delete, list/describe shapes.
- `test_contract.py` — `/health`, `/manifest` (validates as an SDK
  `AppManifest`, `ui_mode=external`, `{host}` placeholder, port 9300),
  `/state` counts, self-registration payload.
- `test_auth.py` — gate: open paths, 401 without bearer, viewer vs operator
  vs admin by method/path, cached `/me`, grace during core outage; login
  proxy: token pass-through, MFA-missing → 202, `/me` mapping.
- `test_api_flow.py` — end-to-end through FastAPI with a fake face engine and
  fake camera source: course → room (camera from roster) → register_new
  student (two photos, one duplicate rejected) → schedule class → manual
  snapshot recognises the student → finalise → attendance report lists
  PRESENT/ABSENT; planner publish expands templates into schedules.
- `test_engine.py` — `StreamWorker._recognize_codes` crops per detected face,
  dedups codes, filters by course/section; `StreamEngine.reload_streams`.

Build checks: `docker build -f examples/attendly/Dockerfile .` compiles the
React app (there is no node on the dev machine, so the image build is the
frontend's type/bundle check) and installs from `uv.lock`.

Integration (manual, needs a running OpenNVR with one camera):
`OPENNVR_EXAMPLE_COMPOSE=docker-compose.apps.yml`,
`OPENNVR_EXAMPLE_PROFILE=attendly`, `./start.sh`, open the catalog → Attendly
→ Open app → log in with the OpenNVR admin → add a room from the camera list →
enrol a student from the webcam → schedule a class for now → watch
`/registry/engine_status` and the Attendance page.

## Known limits

- One recognition call per face crop (the adapter answers for one face per
  request); same call count as the Rekognition version.
- The InsightFace face database is shared across the deployment; attendly's
  entries are separated by `category`. Student names and metadata live in the
  app's SQLite, keyed by `student_code` = adapter `person_id`.
- InsightFace model packs are research-licensed (see the adapter README).
  Attendly is free to use; a commercial deployment would need its own
  licence from the model authors.

---

# Phase 2 — hostel gates, mess gates, staff attendance

Three new jobs on the same lower layer (core snapshots, KAI-C detection and
recognition, the InsightFace adapter's face database). Nothing new is
installed; the profile already starts everything these need.

## How a flap barrier is driven

Attendly **decides**; it does not touch relays. Every decision at a gate is
published on the platform bus as the contracted `access.decided.v1` event
(docs/EVENT_CONTRACTS.md), keyed by the gate camera's handle. OpenNVR's
**gate-controller** app subscribes to exactly that event and closes the
barrier's contact over whichever wiring the site has (dry contact / GPIO,
Shelly-style IP relay, Modbus, ONVIF door controller, MQTT). Run it with
the `gate-controller` profile and list each barrier under its camera handle
in its config; its HARDWARE.md is the wiring guide. The LPR app drives
vehicle barriers the same way, so one controller serves both.

The event's payload is the contract's: `plate_text` carries the credential
(the student code), `decision` is `allow`/`deny`, `reason` says why
(`hostler`, `booked` on allow; `unknown`, `day_scholar`, `wrong_block`,
`not_booked`, `already_used`, `no_meal_window` on deny), `owner` is the
name, `unit` the hostel block or the meal, `confidence` the similarity.
Consumers actuate only on a literal `allow`.

**No barrier:** a gate in `monitor` mode publishes nothing on the access
bus. Each recognised student becomes a movement (`in`/`out` by the gate's
`direction`, or alternating for `toggle`) that moves a `hostel_presence`
row, and an OpenNVR alert (`hostel_in`, `hostel_out`; `gate_denied` when
the policy would have refused) goes on `opennvr.alerts.app.attendly.<cam>`
through the SDK's `NatsAlertChannel`. `alert_on` per gate: `all`, `deny`,
`none`. The manifest declares these alert types in `emits`.

For a barrier with its own built-in relay that takes an HTTP trigger, a
gate can also carry an `open_url`: attendly GETs it on allow. And a barrier
controller that has its own camera can ask attendly directly:
`POST /gates/{id}/check` with a JPEG returns the decision (and publishes it).

## Data

| Table | Purpose |
|---|---|
| `students` + `residence`, `hostel_block` | `hostler` or `day_scholar`; the block a hostler lives in |
| `staff` | `ST00001`-coded people enrolled under `category=attendly_staff` |
| `departments` | a staff attendance camera with a working window |
| `staff_attendance` | one row per staff per day: first seen, last seen, sightings |
| `gates` | kind `hostel` (block) or `mess` (meal windows), its camera, enabled, optional `open_url` |
| `access_events` | every decision, with who, why and similarity |
| `meal_bookings` | `(student, date, meal)` unique; `consumed_at` once the student passed the mess gate |

Student and staff codes share the adapter's one face database (prefixes
`AT` and `ST`), so a gate recognises either and looks the code up in the
right table.

## Services

* `services/access.py` — the gate policy and the `GateWorker`: while a
  gate is enabled it polls its camera (`gate_poll_seconds`, default 1 s),
  takes the **largest** detected face (the person at the barrier),
  recognises it, decides, records an `access_events` row, publishes the
  event, and ignores the same person for `gate_cooldown_seconds`.
  `GateEngine` keeps one worker per enabled gate and reloads from the
  database every 15 s.
* `services/staff_attendance.py` — `StaffWorker` per department: within
  the working window it polls the camera every `staff_poll_seconds`
  (default 60 s) and upserts the day's row for every staff member it
  recognises. A staff member never seen that day is ABSENT in the report.
* Meal bookings have no worker: the mess gate's policy reads them. Bookings
  come from the Meal Bookings page, a CSV import, or the existing booking
  system calling `POST /meals/bookings` with an OpenNVR token (the QR step
  is replaced by the face at the barrier).

## API

| Prefix | Routes |
|---|---|
| `/students` | `residence`, `hostel_block` on create / update / register_new / video init |
| `/staff` | list, create, update, delete (admin), `register_new` (photos), `{code}/register` |
| `/departments` | CRUD; camera from the roster |
| `/staff-attendance` | `?date=&department_id=` rows with ABSENT synthesised; `/engine_status` |
| `/gates` | CRUD, `/status`, `/events?gate_id=&limit=`, `{id}/check` (JPEG → decision) |
| `/meals` | `bookings?date=&meal=`, create (one student, one or more meals), `bookings/import` (CSV), delete, `summary?date=` |

Role gate: GET → viewer; DELETE /staff and /gates → admin; other mutations → operator.

## UI

Register and Video Register gain a residence selector (hostler + block).
New pages: **Staff** (people + departments, photo enrolment), **Staff
Attendance** (date + department, CSV export), **Gates** (barriers, live
decision feed), **Meal Bookings** (date, bookings, add, CSV import).

## Testing

Unit (no OpenNVR): policy table for hostel and mess decisions; the gate
worker on a fake clock — largest face wins, cooldown, event row, publish
called with the contract payload; the staff worker upserting first/last
seen inside and outside the window; the meal booking uniqueness and
consumption; every new router through the fakes.

Live: enable the `gate-controller` profile with a `dry_run` config for the
hostel camera handle, enrol a hostler and a day scholar, walk both past
the camera, and watch the Gates page and gate-controller's page agree;
book a meal for one student and repeat at the mess gate inside and outside
the meal window; add a department and confirm staff rows appear during
the window and ABSENT rows after it.
