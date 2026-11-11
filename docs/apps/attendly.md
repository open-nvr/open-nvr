# Attendly on OpenNVR

> Attendly is a first-party app from OpenNVR, distributed as a signed image
> (`ghcr.io/open-nvr/attendly`). Its source is not published; see
> [APP_LISTING_TERMS.md](../APP_LISTING_TERMS.md) for what that means for a
> catalog listing. Everything it needs from your site (the InsightFace
> adapter, the compose service, the catalog entry) is in this repository.

Classroom attendance on your OpenNVR cameras. Students enrol their faces by
photo or webcam; rooms are cameras; courses have sections; a weekly planner
publishes classes; during each class the app takes recognition snapshots that
finalise into a PRESENT/ABSENT roster you can export.

Attendly runs beside OpenNVR the way the Camera Agent does: its own
container, its own web UI on port 9300, a card in the App Catalog whose
**Open app** button opens that UI in a new tab, and sign-in with your OpenNVR
account. OpenNVR does the lower layer: detection and recognition go through
KAI-C to the InsightFace adapter, which the `attendly` compose profile starts.

### Where the faces come from

Gates and staff departments use OpenNVR's **face route**
(`docs/design/face-recognition-route.md`): the detect pipeline follows each
person on the camera, sends their best crop to Attendly's adapter through
KAI-C, and publishes the answer as `face.recognized.v1`; Attendly decides
from those events and takes no frames of its own. Saving a gate or a
department claims the `face_recognition` skill on its camera, naming
Attendly's adapter, with the saving admin's own login (core checks they may
configure that camera, and audits it); a camera nothing uses any more is
released. A gate or department falls back to taking its own snapshot every
second when its camera carries no claim, when AI detection is switched off
for that camera, or while the event bus is down. The Gates and Staff pages
show which source each one uses.

Classes still take scheduled snapshots of the room's camera: seated students
are poorly found by person detection, so classrooms wait for a whole-frame
face pass on the platform (the design doc's P7).

Enrolment can use the browser's webcam, uploaded photos, or an **OpenNVR
camera** (the gate's or the classroom's): enrolling with the camera that
will recognise the student is the most accurate.

## Run it

OpenNVR must be running. Attendly is a catalog app: pick it when the
installer asks which apps to run from the first start (answer **y** to
"Pick apps to run from the first start?" and choose **attendly**), install
it later from **Settings → App Catalog**, or from the repo root:

```bash
docker compose -f docker-compose.yml -f docker-compose.apps.yml --profile attendly up -d
```

That profile starts the app and the InsightFace adapter it needs. To keep
it in `./start.sh`'s hands, the installer writes (or you add) to `.env`:

```
OPENNVR_EXAMPLE_COMPOSE=docker-compose.apps.yml
OPENNVR_EXAMPLE_PROFILE=attendly
```

Then open **http://\<host\>:9300/** — or the Attendly card in the App Catalog,
whose **Open app** button opens the same page in a new tab. Sign in with your
OpenNVR account. For a reverse proxy or public hostname set
`ATTENDLY_PUBLIC_URL`; the card and the manifest carry that address.

## What you do in it

1. **Manage Rooms** — bind a classroom to one of OpenNVR's cameras.
2. **Manage Courses** — course codes and their sections.
3. **Register** — enrol a student. Details and
   consent first; then the webcam takes the photos itself: a small frame
   is checked about once a second (face boxes only, nothing kept), the
   page says what to change — distance, the oval, which way to turn — and
   says it out loud when Voice is on, and takes each angle's photo once
   the face is steady. Each photo is checked against everyone already
   enrolled, so a person registered twice is told at the first photo.
   The set is one person: if any photo already matches somebody else,
   none of the set is enrolled (a turned head can score under the
   duplicate threshold where the straight-on photo did not). A
   picture is cut from the first photo and shown in Student Data.
   (`/video-register`, the old page that streamed every webcam frame to
   the server, now opens this page.)
   Withdrawing consent deletes the faces; *Erase* also blanks name and
   e-mail and keeps only the counts (DPDP/GDPR).
4. **Manage Schedule** — the weekly timetable; *Publish Today* turns it into
   concrete class windows on each room.
5. **Attendance** — the day's classes (scheduled, running, decided,
   called off), each one's review — the mark per student and who set it,
   slots seen, first seen, corrections with a note, recompute, call off or
   restore, a one-off class, the evidence frames — and the finalised
   rosters with PDF/Excel export. Marks are PRESENT, LATE (first seen
   after `ATTENDLY_LATE_GRACE_MINUTES`, 10), ABSENT, EXCUSED (a person's
   mark) or Not watched.
6. **Report** — student × class for a course and date range, totals and a
   rate `(present + late) / (marked − excused)`, as Excel or CSV (cells
   escaped against formula injection); called-off classes are left out
   and a class too little watched counts for nothing.

During a class window the room's worker fetches a frame from core every
attempt interval (2 min), detects every face, recognises each crop, and
aggregates who was seen per 10-minute slot. At the end of the class a student
is PRESENT when seen in at least 60 % of the slots. Timing knobs are
`ATTENDLY_*` settings in `attendly/config.py`.

Two rules keep the record honest. A slot in which the camera could not be
looked at (no frame from core, recognition down) is recorded as *unwatched*
and counts against nobody; a class with no watched slot at all — an outage,
or a room that never got a worker — finalises as **Not watched** rather than
marking everyone absent. And finalisation runs after a short grace
(`ATTENDLY_FINALIZE_GRACE_SECONDS`, 30 s) and again when the room's worker
exits, so the last slot is always on disk before it is counted.

A student has one primary course and section and may be enrolled in more
(Student Data → edit → *Also enrolled in*); a class of any of them marks
them.

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
[`examples/gate-controller/config.docker.yml`](../../examples/gate-controller/config.docker.yml); its HARDWARE.md is the wiring
guide. The licence-plate app drives vehicle barriers the same way, so one
controller serves both.

### No barrier? Monitor mode

A hostel entrance with no flap barrier still gets the same camera and the
same gate, set to **Monitor only**. Nobody is stopped; every student the
camera recognises is logged as *came in* or *went out* and an OpenNVR alert
names them — "Asha Rao came in — Block B door" — in core's alert inbox and
wherever the alert-notifier app forwards (email, Slack, Telegram…). A day
scholar walking into a hostel, or a hostler at the wrong block, raises a
high-severity alert instead of a refusal. Set the gate's **direction** to
entry or exit when there is a camera on each, or to *both* for a single
camera, which alternates each student's state. The Gates page keeps a
**who is in / who is out** list per block with a one-click correction, and
alerts can be limited to refusals or switched off per gate.

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
| `ATTENDLY_RECOGNITION_ADAPTER` | `insightface_attendly` (overlay) | the KAI-C adapter name; the overlay runs Attendly's own InsightFace instance |
| `ATTENDLY_FACES_URL` | `http://insightface-attendly-adapter:9005` | the adapter's `/faces/*` (enrol, list, delete) |
| `ATTENDLY_DET_SIZE` | `1280` | the adapter's detector input (back rows in a 1080p frame); an adapter older than ai-adapter#81 ignores it |
| `ATTENDLY_IDENTIFY_MARGIN` | `0.05` | with `face_identify`, a match this close to the runner-up is ambiguous, not a hit |
| `ATTENDLY_MIN_FACE_PX` / `ATTENDLY_MAX_FACES` | `20` / `60` | the webcam guidance ignores faces smaller than this ("come closer"); a frame holds at most this many |
| `ATTENDLY_RECOGNITION_MIN_FACE_PX` | `60` | the smallest face a camera frame is named from (attendance, gates, staff). Smaller faces are counted as too small and logged, not matched: a wrong name is worse than none. Lower it only after measuring |
| `ATTENDLY_RECOGNITION_THRESHOLD` | `0.5` | cosine similarity to count a match |
| `ATTENDLY_DUPLICATE_THRESHOLD` | `0.6` | similarity at which an enrolment photo is somebody already enrolled; refuses the whole set |

| `ATTENDLY_GATE_RECOGNITION_THRESHOLD` | `0` (= recognition threshold) | a stricter match for admitting people |
| `ATTENDLY_FACE_ROUTE` | `true` | gates and departments take faces from OpenNVR's face route (`face.recognized.v1`); `false` = every one takes its own snapshots |
| `ATTENDLY_FACE_UNKNOWN_WINDOW_SECONDS` | `2` | an unknown face at a gate waits this long for a better crop of the same person before it is logged as unknown |
| `ATTENDLY_GATE_POLL_SECONDS` / `ATTENDLY_GATE_COOLDOWN_SECONDS` | `1` / `8` | how often a gate camera is looked at; how long the same person is not re-decided |
| `ATTENDLY_MEAL_WINDOWS` | `breakfast=07:00-09:30,lunch=12:00-14:30,dinner=19:00-21:30` | when the mess serves; a gate may override |
| `ATTENDLY_STAFF_POLL_SECONDS` | `60` | how often a department camera is looked at |
| `ATTENDLY_STAFF_WORK_START` / `ATTENDLY_STAFF_WORK_END` | `09:00` / `18:00` | the default working window for a new department |
| `NATS_URL` | `nats://nats:4222` (overlay) | the bus for `access.decided.v1`; blank = decisions are logged only |

Recognition uses the adapter's `face_identify` task (ai-adapter#81) when it
answers it — one call per frame, every face matched, a margin against the
runner-up — and detect-then-crop (one recognition call per face) on an
older adapter. The adapter is given each person's code, never their name.

Both thresholds are starting points. Tune them against real enrolments on
your cameras: lower the match threshold if known students go unrecognised,
raise it if strangers are matched. Changes take effect on restart.
| `ATTENDLY_PUBLIC_URL` | — | browser-reachable URL; blank = `http://{host}:9300/` |
| `ATTENDLY_AUTH_MODE` | `opennvr` | `none` opens every endpoint (loopback development only) |
| `ATTENDLY_DATABASE_URL` | `sqlite:////data/attendly.db` (image) | the app's own SQLite |
| `ATTENDLY_TIMEZONE` | `Asia/Kolkata` (the overlay follows `$TZ` when set) | class windows and meal hours are local times |
| `ATTENDLY_MAX_REGISTERED_STUDENTS` | `0` (no limit) | refuse enrolment past this many students |
| `ATTENDLY_MAX_WORKERS` | `50` | rooms that may hold a class at the same moment |
| `ATTENDLY_FINALIZE_GRACE_SECONDS` | `30` | wait after a class ends before its attendance is finalised |
| `ATTENDLY_LATE_GRACE_MINUTES` | `10` | first seen after this many minutes into the class → LATE |
| `ATTENDLY_MIN_COVERAGE_RATIO` | `0.5` | a class watched in fewer of its slots is flagged; its automatic marks are Not watched |
| `ATTENDLY_KEEP_EVIDENCE_FRAMES` | `true` | one frame per slot with a recognition goes to core's evidence store |
| `ATTENDLY_SAVE_DEBUG_FRAMES` | `false` | keep attempt frames and face crops under the data dir |
