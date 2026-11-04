# Attendance

Takes attendance from the cameras during scheduled classes or shifts. It
recognises the enrolled people in the room and OpenNVR marks them
**present**, **late** or **absent**. A Group can be a class section or a
work shift; the page calls it whatever you set.

The app only **looks**; OpenNVR **decides**. OpenNVR keeps the people, the
groups, the timetable, the sessions and the marks
(`server/routers/attendance.py`). The app asks which sessions are running,
looks at their cameras, and reports each look as an
[`attendance.attempt.v1`](../../docs/EVENT_CONTRACTS.md) event. You can
restart or reinstall the app without losing a mark.

## How a session is marked

- A session is cut into buckets, 5 minutes by default.
- Each camera of the room is looked at every 150 s, so every bucket gets at
  least two chances.
- A bucket counts only if a frame was actually looked at in it. If the
  camera or the app was down, that time is left out of everyone's ratio;
  nobody is marked absent for it.
- A person is **present** if recognised in at least 60% of the counted
  buckets. They are **late** if their first sighting came more than 10
  minutes after the start.
- If nothing was looked at during the whole session, it is flagged
  *no coverage*. If nothing has been looked at for 10 minutes while a
  session is running, an alert is raised.
- A person can correct any mark (present, late, absent, excused). A
  correction survives a recompute.

All of these numbers are the attendance policy (Attendance → Settings), and
a group can override them.

## Install

```sh
docker compose -f docker-compose.yml -f docker-compose.apps.yml --profile apps up -d attendance
```

This brings up the app and **its own InsightFace**
(`insightface-attendance-adapter`, registered with KAI-C as
`insightface_attendance`). It keeps a separate face DB and request queue
from the doorbell's: a school roster and a family's visitors must never
match each other, and a crowded classroom frame must not make the door
wait.

Then, in OpenNVR, **App Catalog → Attendance → Configure → Cameras**: pick
the classroom cameras. The app looks only at picked cameras, and only
during their sessions.

Everything else can be done in either of two places, which read and write
the same data:

- **The attendance web UI**: App Catalog → Attendance → **Open app**, or
  `https://<your OpenNVR host>:9218/` (`ATTENDANCE_WEB_PORT`). Its own
  pages, laid out for the people who take attendance: Register (details,
  consent, then photos or the webcam), People, Groups, Schedule,
  Attendance, Reports, Settings.
- **Attendance** in OpenNVR's own menu: the same, as tabs.

In both:

1. **Register** (or People): add people, record consent, enrol 3 to 5
   photos each (front, slight left and right, chin up and down, with
   glasses if they wear them).
2. **Groups** and **Schedule** (Timetable): who meets when, on which
   cameras.

## The web UI

The app serves its own pages (`web/`, React) on port 9218 inside the apps
network. `attendance-web`, a small nginx, puts them on the LAN over HTTPS
with OpenNVR's own certificate: the apps network has no route out, and a
browser only lets a page use the webcam over HTTPS.

Users sign in with their **OpenNVR account**: the same password, MFA code
and lockout. The app keeps no users and no session of its own. Every call
the page makes goes through the app to OpenNVR's attendance API as the
signed-in user (`attendance/webui.py`), so:

- what someone can do is their OpenNVR role: `attendance.view` to see,
  `attendance.manage` for groups, schedule, marks and settings,
  `attendance.enroll` for people and faces;
- every change is in OpenNVR's audit log under their name;
- the app passes on only the attendance routes and the two reads the page
  needs (the camera list and the user's permissions). Anything else is
  refused before it reaches OpenNVR;
- OpenNVR's device firewall still applies. A browser it has not approved
  is refused here exactly as on OpenNVR's own page.

The audit log shows the app's container address as the client IP, not the
browser's: OpenNVR does not take a forwarded address from an app.

## Consent and privacy

Faces are biometric data.

- Faces can be enrolled only after consent is recorded for that person.
- Withdrawing consent deletes their faces.
- Removing a person deletes their faces first. If the face service cannot
  confirm the deletion, the removal is refused.
- Only members of a session's group are reported. Faces of anyone else in
  the room are dropped in the app and never leave it.
- One evidence frame is kept per camera per bucket, and only when somebody
  was recognised. It follows the evidence store's retention.

The enrolled faces are stored unencrypted in the adapter's
`opennvr_insightface_attendance_faces` volume. Treat that volume as
personal data.

## Sizing

Measured on a 2013 laptop CPU (`buffalo_l`, a 1080p frame with 54 faces),
recognition costs about **170 ms per face**, so about 10 s per crowded
frame. Detection is about 0.3 s at the default detector size of 640. One
adapter instance therefore covers about **15 cameras** of 50 faces each at
the default 150 s interval.

For more cameras, either run more adapter instances or lengthen
`attempt_interval_s`. `ATTENDANCE_FACE_MODEL=buffalo_s` is ten times
cheaper but much less accurate. `ATTENDANCE_FACE_DET_SIZE=1280` finds
smaller faces at the back of a room at about 3.5× the detection cost.

Camera placement matters more than any threshold. Faces should be at
least 40 px tall, lit from the front, and seen at most about 30° off-axis.

## Limits

- There is no liveness check. A photo held up to the camera counts as a
  sighting. The evidence frames let a teacher spot it.

## Development

```sh
cd examples/attendance
PYTHONPATH=.:../../sdk/opennvr-app-sdk python -m pytest tests
```

`attendance/core.py` holds every decision with no platform in it, and is
tested on a synthetic clock.

The web UI:

```sh
cd examples/attendance/web
npm ci
npm run dev        # http://localhost:5174, against ATTENDANCE_WEB_URL (default https://localhost:9218)
npm run build      # into web/dist, which the app serves (the image builds it itself)
```

Without `web_port` in its config the app serves no UI and its manifest
does not offer one.
