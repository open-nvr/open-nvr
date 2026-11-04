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

Then, in OpenNVR:

1. **App Catalog → Attendance → Configure → Cameras**: pick the classroom
   cameras. The app looks only at picked cameras, and only during their
   sessions.
2. **Attendance → People**: add people, record consent, enrol 3 to 5
   photos each (front, slight left and right, with and without glasses).
3. **Attendance → Groups** and **Timetable**: who meets when, on which
   cameras.

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
