# Face recognition as a platform route — implementation plan

Status: **planned** (2026-11-11). Owner: platform. First consumer: Attendly.

## Why

RFC-0002 decision 9: *"Detect X, then analyse the crop with Y" is one routing
row (`vehicle → fast_plate_ocr`, `person → insightface`), executed by Tier-1
dispatch, run once however many apps consume … plate/face routes stay opt-in
rows, never defaults.* Plates follow this today
(pipeline → dispatch → KAI-C → `plate.recognized.v1` → app). Faces do not:
every face app (Attendly, smart-doorbell) polls camera snapshots itself and
sends the whole frame to its adapter every second. That costs a full-frame
face detection per camera per second whether anyone is there, judges each
frame alone, and analyses a downscaled whole scene instead of the tracked
person's best crop from the main stream.

## Target flow

```
camera → detect-pipeline: motion → person detection → tracking → best crop per track
       → gate (per camera): escalate a person track to the face route
       → dispatch: crop → KAI-C /infer/<face adapter>  {task: face_recognition, track_id, label}
       → KAI-C: completion + normaliser → opennvr.events.face.recognized.v1.<cam>
       → app (scope events:face.recognized) for its picked cameras
```

## Decisions taken (2026-11-11, with the owner)

1. **Face route only.** Dispatch is off on every install today
   (`DETECT_DISPATCH_KAIC_URL` empty, `DETECT_GATE_MODE=shadow`). Turning on
   the face route must not change any other camera: the gate is enforced and
   dispatch runs **only for cameras whose skills include
   `face_recognition`** (a face app picked them); caption routes and every
   other camera stay exactly as today. Live sites upgrade in place.
2. **Classrooms: a platform face census, not the person route** (revised
   2026-11-11 after measuring). On real classroom CCTV (front-high camera,
   ~16 seated students, 360p upload) the person detector found ~25% of the
   students on the whole frame and ~50% on zoomed tiles; a student never
   detected as a person is never escalated, and would be marked absent with
   a visible face. Gates do not have this problem (one person, close).
   Classroom cameras therefore get a **scheduled whole-frame face pass run by
   the platform** (no person step) that publishes the same
   `face.recognized.v1` event; apps consume one event type for both. Design
   of the census waits for one minute of real 3 MP footage. Also measured:
   0 faces found in 11 s of that footage — students look down at books — so
   classroom recognition depends on looking repeatedly through the period,
   whatever the design.

## Work, by component

### P1 — detect-pipeline: the face route (opt-in, per camera)
- `dispatch.py`: `router_for_skills` adds `person → <face adapter>` when the
  camera's skills include `face_recognition`; `ADAPTER_TASKS[<face adapter>]
  = "face_recognition"`. The adapter name comes from the camera assignment
  (the app's `requires_adapters`), not a hard-coded name.
- Retry policy for faces (plates are once per track): re-dispatch a track
  when its best crop improves, until a result says *identified* or a per-track
  cap (e.g. 5 attempts) — the first crop can be the back of a head.
- Activation: dispatch is constructed when a KAI-C URL is configured; the
  gate is **enforced only on face-route cameras**, shadow elsewhere (today
  enforce/shadow is global). Default KAI-C URL is set in compose so a stock
  install with a face app works without hand edits; with no face app picked
  nothing is dispatched.
- `DETECT_SKIP_UNASSIGNED`: a camera picked only by a face app must not be
  skipped (`face_recognition` joins `DETECTION_SHAPED_SKILLS`).
- Tests: routing table, retry/cap, per-camera enforcement, no change for
  cameras without the skill.

### P2 — KAI-C: completion carries the track, face normaliser
- The infer request's `track_id` / `label` travel into the completion event
  (additive fields).
- `domain_events.py`: normaliser for the face adapter →
  `opennvr.events.face.recognized.v1.<cam>` with `person_id` (or null when
  unknown), `similarity`, `track_id`, face box in the crop, crop size,
  `observed_at`; evidence reference so an app can keep the image.
- `docs/EVENT_CONTRACTS.md`: the new contract, versioned like
  `plate.recognized.v1`.

### P3 — server: scope and wiring
- `events:face.recognized` scope: only apps that declare it (granted at
  install) can subscribe — faces are PII (RFC-0002 decision 6).
- A face app's pick projects `face_recognition` (and, for attendance
  cameras, the re-check profile) into the camera's skills.
- Compose: detect-pipeline gets the KAI-C URL; nothing else changes.

### P4 — SDK
- A typed helper for `face.recognized.v1` like the plate one, dropping
  cameras the app did not pick.

### P5 — Attendly (private repo `opennvr-pro-apps`)
- Manifest: `requires_tasks` / skills for face recognition,
  `requires_scopes: ["events:face.recognized"]`, app key + apps bus (it uses
  the site key and the platform bus today).
- Gates and staff attendance: subscribe instead of polling; per track, decide
  on the best result within a short window (best-of-N, threshold unchanged);
  cooldown per person as today.
- Classes: later, from the census (P7); until then the class worker stays
  as it is.
- Gallery enrolment stays as is (Attendly → KAI-C → its adapter).
- Fallback: the snapshot loop stays only for a camera the pipeline does not
  serve, and says so in the UI.

### P7 — classroom face census (after real footage)
- Platform skill for classroom cameras: whole-frame face pass at intervals
  through a class window, same `face.recognized.v1` event.

### P6 — verification and rollout
- End-to-end on the local stack: webcam gate, a classroom fake camera.
- Upgrade test as an in-place migration of the existing test stack
  (live clients): cameras without a face app must behave exactly as before;
  CPU before/after recorded.

## Progress

| Step | State | Notes |
|---|---|---|
| Plan | done | this file |
| Classroom miss-rate measurement | done | person route unfit for classes; see decision 2 |
| P1 detect-pipeline face route | done | baec2020 |
| P2 KAI-C track + normaliser + contract | done | ded90981 |
| P3 server scope + skills + compose | done | claim params.adapters projected; DETECT_FACE_KAIC_URL (face route only, default on); scope needed no change |
| P4 SDK helper | done | FaceRecognized typed payload |
| P5 Attendly consumer | done | private repo `attendly-face-route`: gates 5170cc2, detection-off fallback fe816c6, staff 3790692; enrol with an OpenNVR camera 5b661d7 |
| P6 end-to-end + upgrade test | done (local) | webcam gate: pipeline -> KAI-C -> event -> decision verified 2026-11-11; DB unchanged (no migration); cameras without a claim dispatch nothing |
| P7 classroom census | waiting | needs 1 min of real 3 MP classroom footage |
