# Implementation plan: attendance follow-ups

| | |
|---|---|
| **Builds on** | PR #791 (`SRB-attendance`): people, groups, timetable, sessions, marks, enrolment, the attendance app and its web UI |
| **Status** | Planned. Nothing started |
| **Revision** | r3 (2026-11-05): a second review against the code (section 12). r2: first review (section 11). r1: first draft |
| **Audience** | Whoever implements or reviews this work |

## 1. Scope

#791 takes attendance for classes. These follow-ups add what a campus also
asked for, with the policy and safety rules a first version lacked:

| Milestone | What | Why it is separate |
|---|---|---|
| **M0** | Enrolment polish: the oval check, voice instructions | Small, user-facing, no schema change |
| **S1** | The door-camera spike (ATT-102): how fast a camera can really be looked at | M1 and M2 both size themselves on its numbers |
| **M1** | Staff attendance: shifts, check-in and check-out | Same people, groups and cameras as classes; a different verdict |
| **M2** | Monitor gates: who came in or went out of a hostel block, with alerts | Needs fast looks at a door; opens nothing |
| **M3** | Meal bookings and a monitor-mode mess (served = recognised in a meal window) | Bookings are data with a lifecycle; nothing opens yet |
| **M4** | Barrier mode: a face match opens a flap barrier, and the mess spends a booking only when the barrier opened | Physical access. Needs M2's confirmation rules, liveness, and the platform's event-publish hardening |

A print/PDF button was planned for the external app's reports in r1: it already exists (`Reports.tsx`, "PDF (print)"), so it is not in this plan.

**Out of scope:**
- Payroll exports.
- Visitor management.
- Driving relays directly. The `gate-controller` app stays the only thing that touches hardware.

Milestones ship in order. Each is its own PR, stacked on #791 (or on main once #791 is merged).

## 2. Engineering rules (every issue)

- **Branches and commits:**
  - One issue, one `SRB-att-<id>-<slug>` branch, squashed to **one commit** that closes its issue. No Co-Authored-By trailer.
  - Push only with the owner's go-ahead, after a full retest and a review pass.
- **Schema:**
  - `server/models.py` plus an Alembic migration whose `down_revision` is the current head. `server/tests/test_migration_graph.py` stays green.
  - New columns are nullable or have a `server_default`.
  - Every migration is tried in place on a copy of the live database (test-as-migration), never only on a fresh one.
- **Permissions:**
  - Through `services/permission_catalog.py` (`UPGRADE_PATH_PERMISSIONS`), so fresh installs and upgrades get the same set.
  - New abilities go to admins only on upgrade.
- **Core decides, apps look.**
  - The app sends what it saw (`attendance.attempt.v1`, or a new contracted event).
  - Core owns rosters, policies and verdicts, as in #791.
  - A new event goes into `docs/EVENT_CONTRACTS.md` and into the SDK's typed events in the same issue.
- **Biometrics:**
  - Only people with consent on record are ever looked for.
  - Every new table that names a person is covered by erasure (`DELETE /attendance/people/{id}?erase=true`) and by removal.
- **Audit:** every state-changing route calls `audit_request`.
- **Live check:**
  - Each milestone ends with an in-place upgrade of the owner's test stack: a DB backup first, images built from the branch, recreated with `CORE_TAG`/`ADAPTER_TAG` overrides.
  - Then a scripted check through the attendance web proxy. The script is part of the milestone: an `attendance` e2e test in `tests/e2e`, not a file outside the repo.
- **Removal and erasure:** removing a person also clears their presence rows and cancels their future bookings; erasing keeps the counts and drops who they were.

## 3. M0: enrolment polish

| Id | Issue | Size | Notes |
|---|---|---|---|
| ATT-001 | **The oval check agrees with the oval on screen** | S | The guide says "Move your face into the oval" while the face is visibly centred. Check the box from `faces/check` against the frame actually sent (`CHECK_WIDTH`), against `check.frame`, and against the `<video>` element's `object-fit` crop and mirroring. Fix the centring maths and test `judge()` with real boxes. |
| ATT-002 | **Voice instructions** | M | `speechSynthesis` in `Register.tsx`. It reads: each angle's instruction when it starts, a changed hint, "Got it" after a photo, "All done" at the end. At most one sentence every ~2.5 s, and never mid-sentence. A "Voice" checkbox beside "Sound", remembered per browser. English always, Hindi when the browser has a `hi-IN` voice; ~15 strings in a small table. No audio files, no server change. The first `speak()` happens after the "Start the camera" click, which browsers require before speech plays. Chrome's "Google" voices are synthesised online, so only the instruction text is sent, never a name; a local Windows voice is preferred when there is one. Lands **after** ATT-001, or it would read the wrong oval hint aloud. |

**Done when:** an enrolment can be done by voice alone, with the screen out of the person's view. The oval check passes a face that is visibly centred and refuses one at the edge.

**State (2026-11-07):** ATT-002 has landed on #791 (with the webcam's per-photo duplicate check): English voice, a local voice preferred, a correction not repeated within 6 s. Still open from it: Hindi strings, and ATT-001, without which the voice can say the wrong oval hint.

## 3a. S1: the door-camera spike

ATT-102 (below, listed with M1) runs **before M1 and M2**: both size their look rate, M2 its two-look window, and M4 its latency budget on its numbers. If it fails (a walking person is missed even at the best rate snapshots allow), M1 and M2 switch to KAI-C's streaming inference (`/api/v1/infer/{adapter}/stream`, which exists) before any of their issues start.

## 4. M1: staff attendance (shifts)

A shift is a session, so it reuses everything #791 built. What differs is the verdict and the look rate.

**Model** (one migration, `ATT-101`):
- `attendance_groups.mode`: a column, `class` (today's bucket rule) or `shift`, `server_default 'class'`. It cannot live in `policy`: `attendance_policy.validate_overrides` accepts numbers only and refuses unknown keys, by design.
- `attendance_marks`: `last_seen_at` (nullable) and `left_early` (bool, default false).
- `attendance_timetable_slots`: a slot may end after midnight (`end_time < start_time` means the next day). The session's `local_date` is the day the shift starts. Three places refuse this today, and all three change together:
  - `SlotIn._order` ("a night shift is two slots");
  - `materialize` in `attendance_scheduler.py`, which reads `end <= start` as a DST gap and cancels the session, in both of its branches;
  - `_local_window`, which must put the end on the next day.
- New policy keys (numbers, so they fit the validator): `early_leave_minutes` (30), `checkin_before_minutes` (60), `checkout_after_minutes` (60).

**Check-in happens before the shift starts.** Today a look counts only inside `[start_at, end_at)`: the consumer drops anything else as `no_session`, and `/internal/app/attendance/active` names a session only while it runs. A person who arrives at 08:50 for a 09:00 shift would never be seen. For `mode: shift`, the **watch window** is `[start - checkin_before, end + checkout_after)`. `/active` and the consumer use the watch window; the verdict uses the shift's own times.

**Verdict** for `mode: shift` (`services/attendance_scheduler.finalize`):
- PRESENT if seen at all in the window; LATE if the first sighting is after `late_grace_minutes`.
- `left_early` if the last sighting is more than `early_leave_minutes` (new key, default 30) before the end.
- `first_seen_at` / `last_seen_at` are check-in and check-out.
- The coverage floor still applies: a camera that was down is not an absence.

**Look rate:**
- A door camera sees a person for about 2 s. #791 looks every 150 s, and the policy floor is 30 s.
- `attempt_interval_s` may go down to **3 s for `mode: shift` only**: door frames hold one or two faces, about 0.2–0.4 s of adapter time each. The policy bounds become mode-dependent (today one global 30–1800).
- **Measured first:** the camera snapshot route took about 2 s per frame on the owner's camera 2 before #796. ATT-102 measures the real rate after #796; if 3 s cannot be held, M1 and M2 change frame source (section 10).
- The #796 capture fix keeps frequent snapshots live and cheap. ATT-102 measures the real cost on a door camera first.
- Thinning, so a door camera does not write 28,800 rows a day:
  - Empty looks go out at most once per bucket (the coverage heartbeat).
  - A person goes out at most twice per bucket: the first look they are in, at once (check-in), and the last one, when the bucket closes (check-out). Staff sitting in view of a camera all day would otherwise be a sighting every 3 s.
  - Check-out is therefore accurate to one bucket. Shift groups default to 2-minute buckets.
- Back-to-back shifts (06:00-14:00, 14:00-22:00) have overlapping watch windows. A look in the overlap is linked to both sessions, which the consumer already allows. Each person's check-out is taken only inside their own session's window. This is a test case in ATT-103.

| Id | Issue | Size |
|---|---|---|
| ATT-101 | Model: `mode` column, `last_seen_at`, `left_early`, overnight slots (in `SlotIn`, `materialize`, `_local_window`), the new policy keys; migration; erasure covers them | M |
| ATT-102 | Spike: measure snapshot + `face_identify` on a door camera: frames per second actually reached, adapter CPU, misses for a walking person, and face-to-decision latency. Its numbers set ATT-104's interval and M2's two-look window | S |
| ATT-103 | Shift verdict in `finalize`, with tests: overnight shift, late, early leave, partial coverage | M |
| ATT-104 | App and core: the watch window (`/active`, the consumer), the per-session look interval, empty looks thinned to one per bucket | M |
| ATT-105 | UI (both): group mode, a shift day view (in / out / late / left early), check-in/out columns in the report and CSV/XLSX | M |

**Done when:** a 22:00–06:00 shift on a door camera gives each person one row with check-in and check-out. A person who walks past once at normal speed is caught (measured in ATT-102). The live stack's existing class groups are unchanged.

## 5. M2: monitor gates (no barrier)

A **gate** is a camera at an entrance with a direction (`in`, `out`, or `both`) and an **area** (a hostel block). It opens nothing. It records movements, keeps who is inside each area, and raises alerts.

**Model** (`ATT-201`):
- `attendance_areas` (name; the groups allowed in it).
- `attendance_gates` (camera, area, direction, mode = `monitor` | `barrier`, alert setting, enabled).
- `attendance_movements` (person or unknown, gate, at, direction, decision, reason, evidence path).
- `attendance_presence` (person, area, inside, since).
- Movements are kept 90 days, like attempts. Presence is current state only.

**Decision rule** (`services/access_policy.py`, a pure function like #798's `decide()`):
- A person may be in an area if they are an active, consented member of one of its groups.
- Anyone else is `deny` with a reason: `not_allowed`, `unknown_face`, `not_consented`, or `inactive`.

**Recognition is stricter than for attendance.** A movement is recorded only when the same person matches in **two consecutive looks**, close together in time, and each look must pass all of:
- similarity ≥ `gate_threshold` (default 0.6, not 0.5);
- a margin to the runner-up ≥ 0.08 (`second_similarity`, which `face_identify` already returns);
- the face at least `min_face_px` wide.

The largest face is not assumed to be "the person at the gate": every qualifying face is decided.

The two-look window is `max(3 s, 2.5 x the look interval ATT-102 measured)`. At the ~2 s per snapshot seen before #796, "within 3 s" could never be met.

**One look is not nothing.** Someone walking briskly may be in view for one look only. In a monitor gate, a single qualifying look is recorded as an **unconfirmed movement**. It is shown in the log, but it does not change presence and raises no alert. Presence and alerts need the two-look confirmation, and barrier mode (M4) acts only on confirmed movements.

**Direction `both`:**
- Toggles the person's state, with a hold of 60 s per person per gate.
- That fixes #798's in-out-in flapping while someone lingers.

**Where it runs:**
- The attendance app watches gate cameras as well. A new internal route, `/internal/app/attendance/gates`, says which cameras, how often, and who may be looked for. Gates come first in each tick.
- **Gates get their own adapter instance.** On the shared one, a crowded classroom frame (~10 s on this CPU) would hold every gate's look behind it. The gate instance serves the **same gallery read-only**: it reloads the face DB when the file changes and refuses enrolment. That is new ai-adapter work (ATT-206), because two writers on one face DB file would corrupt it.
- Core decides. The app publishes a new contracted `attendance.sighting.v1` (camera, at, matches with similarity and margin, track hint). Core applies the two-look rule and writes the movement.
- Alerts (`hostel_in`, `hostel_out`, `gate_denied`) go to core's inbox, so the alert-notifier forwards them.
- **Strangers:** a face that matches nobody is a movement with no person. Its frame is kept only when the gate's alerts include refusals, for 30 days, and never as a face crop: it is someone who never consented.
- **Permission:** a new `attendance.access` (areas, gates, presence corrections; later meals), admins only on upgrade.
- **Forged sightings:** until the platform's event-publish permissions are narrowed, another app could publish `attendance.sighting.v1` and fake movements and alerts, as it can fake attendance today. Nothing opens in M2, so this is accepted here and closed before M4.

| Id | Issue | Size |
|---|---|---|
| ATT-201 | Model + migration: areas, gates, movements, presence; erasure and removal cover them | M |
| ATT-202 | `access_policy.decide()` + the two-look confirmation, pure and fully unit-tested | M |
| ATT-203 | `attendance.sighting.v1` contract (docs + SDK type); the app watches gate cameras at the rate S1 measured, gates first in each tick | M |
| ATT-204 | Core consumer: movements, presence, alerts; per-gate alert setting (all / refusals / none) | M |
| ATT-205 | UI: areas, gates, a live "who is in" per area with a manual correction (audited), the movement log | M |
| ATT-206 | ai-adapter: a read-only replica mode for InsightFace (reload the face DB on change, refuse writes); compose: a gate adapter instance on the attendance gallery | M |

**Done when:**
- A printed photo held up to the camera is recorded as at most one sighting, never a movement. This is tested with a photo, in ATT-102's setup.
- A student who lingers produces one movement, not a sequence.
- The "who is in" list matches a scripted walk-through.

## 6. M3: meal bookings and the mess gate

**Model** (`ATT-301`):
- `attendance_meals`: site meal windows (`breakfast 07:00–09:30` …), and per-area overrides. A window that crosses midnight is allowed and tested; #798's string comparison silently never matched one.
- `attendance_meal_bookings`: person, date, meal, status `booked` / `pending` / `used` / `cancelled`, `used_at`, `correlation_id`. Unique on (person, date, meal).

- A meal that crosses midnight (dinner 20:00-00:30) belongs to the date its window starts.

**In M3 the mess has no barrier.** A confirmed recognition at a mess gate inside a meal window, against a `booked` booking, marks it `used` (one conditional UPDATE, so two lanes can never both win it). It is flagged `unconfirmed` in the day's report: nothing physical proved the meal was served. Someone with no booking is a refusal alert, as at a hostel gate.

**With a barrier (M4), the booking is spent only when the barrier opened.** This is the fix for #798's lost meals:
1. The mess decides `allow` against a `booked` booking: `booked → pending`, with the decision's correlation id, in one conditional UPDATE.
2. The `gate-controller` already raises a `barrier_opened` alert with the same `correlation_id`, on `opennvr.alerts.>`. Core's alert intake moves the booking `pending → used`.
3. A `pending` booking with no confirmation after 15 s goes back to `booked`. This covers the controller's cooldown, an unwired gate, or NATS being down. A `barrier_fault` alert with that correlation id returns it at once. The student is not charged a meal they did not get.
   - **Dry run:** the `gate-controller` still raises `barrier_opened` in dry run, with `evidence.dry_run = true` (the opened path in `gate_controller.py`). Such a confirmation does **not** spend the booking.
   - **Late confirmation:** a `barrier_opened` that arrives after the revert spends the booking if it is still `booked` with the same correlation id.
   - The decision carries a fresh `correlation_id`. The controller threads the envelope's id into its alert (`correlation = event.get("correlation_id")`), and its alerts reach core's inbox on `opennvr.alerts.>` (`services/alerts_inbox.py`), on the core bus the controller already uses.
4. A monitor-only mess gate (no barrier) spends the booking on the decision. It is flagged as unconfirmed in the report.

**Bookings in:**
- The page.
- A CSV import (with formula-injection guard, as for the report).
- `POST /attendance/meals/bookings` for a booking system, with an API token scope `attendance.meals` (no full operator login).

| Id | Issue | Size |
|---|---|---|
| ATT-301 | Model + migration: meal windows, bookings; erasure covers them | M |
| ATT-302 | Booking API, CSV import, `attendance.meals` token scope, permissions | M |
| ATT-303 | Monitor-mode mess: a confirmed recognition in a meal window spends a booking (one conditional UPDATE), flagged unconfirmed; a missing booking is a refusal alert | M |
| ATT-304 | UI: bookings per day and meal, import, the day's served/unserved counts | M |

**Done when:** two mess cameras that both recognise the same student in the same second spend one booking, not two (a concurrency test). A student with no booking raises a refusal alert. The day's report shows booked, served (unconfirmed) and not served.

## 7. M4: barrier mode

Barrier mode publishes `access.decided.v1` for M2/M3 decisions, so the existing `gate-controller` opens the flap. It is the last milestone on purpose:

- **The platform's event-publish permissions must be narrowed first.** Today any app that declares `provides` may publish any domain event, so any such app could publish an `allow`. This is tracked separately and must land before barrier mode can be enabled.
- **Liveness.**
  - The two-look rule stops a photo shown once. It does not stop a phone video.
  - ATT-401 adds a `face_liveness` task to the InsightFace adapter: a passive anti-spoof model, after a licence check. Barrier mode requires it to pass.
  - Until then the barrier toggle stays disabled, with the reason shown.
- **Contract.** `access.decided.v1` is plate-shaped. Add `credential: "face"` and `person_ref` (additive). `plate_text` carries the reference number and `owner` the display name, so the controller's logs read well. No `gate-controller` code change. **Core is the producer**: it publishes on the core bus, where the `gate-controller` subscribes (`nats://nats:4222`).
- **Latency budget:** look, identify, apps bus, core's decision, core bus, relay. Target at most 2 s from a face at the gate to the pulse, measured in ATT-403 on top of ATT-102.
- **Tailgating:** a face gate decides who is at the flap, not how many pass. One person per opening is the flap barrier's job (its own sensors), not the software's. The UI says so where barrier mode is enabled.

| Id | Issue | Size |
|---|---|---|
| ATT-401 | ai-adapter: `face_liveness` task (model choice + licence review first) | L |
| ATT-402 | Barrier mode: publish `access.decided.v1` (additive fields), admin-only toggle, refused until liveness and the event-scope hardening are in | M |
| ATT-403 | e2e: decision → `gate-controller` (dry run) → `barrier_opened` → booking used; spoof photo → no event | M |
| ATT-404 | The mess with a barrier: `booked → pending` on the decision, `pending → used` on `barrier_opened` (same correlation id, not in dry run), back to `booked` on `barrier_fault` or after 15 s, a late confirmation still spends | M |

**M4 done when** (beside ATT-403): with `pulse_cooldown_seconds` at 5 and two students 3 s apart, the second is not charged; a NATS outage spends no bookings; a dry-run gate spends none.

## 8. Dependencies and order

```
#791 merged ─┬─ M0 (any time; ATT-001 before ATT-002)
             └─ S1 spike (ATT-102) ─┬─ M1
                                     └─ M2 ── M3 ── M4
                                                    ├─ needs: event-publish hardening (platform)
                                                    ├─ needs: ATT-401 liveness
                                                    └─ uses: gate-controller's barrier_opened alert (exists today)
```

## 9. Open decisions (owner)

1. **Voice languages (ATT-002):** English only, or English + Hindi?
2. **Gate threshold and two-look window (M2):** 0.6 and 3 s are starting points. Tune on the owner's cameras before M4.
3. **Liveness model (ATT-401):** which model, and whether its licence allows commercial use.
4. **Where gates live:** in the attendance app (as planned, one face DB and one roster) or a separate app. A separate app means a second adapter instance or sharing the attendance gallery.

## 10. Risks

- **Door-camera recall.** If ATT-102 shows snapshots miss walking people even at 3 s, M1 and M2 need a different frame source: KAI-C streaming inference on the sub-stream. That is a larger change, decided at the spike.
- **Adapter capacity.** Even with its own instance (ATT-206), a gate adapter serves every gate's look in turn. More than a handful of busy gates on this CPU may need a second gate instance; ATT-102 gives the per-look cost to size it.
- **Disk.** Movements and door-camera attempts grow fast. Thinning (ATT-104) and the 90-day prune are part of the issues, not follow-ups.

## 11. Review of r1 (what changed)

r1 was checked against the code. These claims were wrong or missing:

| r1 said | The code says | r2 |
|---|---|---|
| Shift `mode` can go in the group's `policy` JSON, no migration | `validate_overrides` accepts numbers only and refuses unknown keys | A `mode` column with a migration |
| Overnight slots need only `SlotIn` relaxed | `materialize` reads `end <= start` as a DST gap and cancels the session | Change `SlotIn`, `materialize` and `_local_window` together |
| A shift is watched from its start | The consumer drops looks outside `[start, end)` and `/active` names a session only while it runs | A watch window around the shift for check-in and check-out |
| Two looks within 3 s | The snapshot route took ~2 s per frame on camera 2 | The window comes from ATT-102's measured rate |
| Gates share the attendance adapter, with priority | One crowded classroom frame is ~10 s on the single adapter slot | A read-only replica adapter instance for gates (ATT-206) |
| `dry_run` returns a pending booking | `gate-controller` raises `barrier_opened` in dry run too, with `dry_run: true` | Dry-run confirmations never spend; a fault returns at once; a late confirmation still spends |
| No new permission for gates and meals | Every vertical guards its routes with its own permission | `attendance.access` |
| (missing) | A stranger's frame at a gate is biometric data of someone who never consented | Kept only for refusal alerts, 30 days, never as a crop |
| (missing) | Barrier latency and tailgating were not addressed | A 2 s budget measured in ATT-403; tailgating stated as the barrier's job |

## 12. Second review (r2 to r3)

| r2 said | What was wrong | r3 |
|---|---|---|
| M0 adds a print/PDF button to the external app | It already has one: `Reports.tsx`, "PDF (print)" | Dropped |
| M3 spends a booking when the barrier opened | Nothing publishes a barrier decision until M4: M3 could not have shipped | M3 is monitor-mode (spent on recognition, flagged unconfirmed); the barrier confirmation is ATT-404 in M4 |
| The spike sits inside M1 | M2 (two-look window) and M4 (latency) need its numbers too | S1, before M1 and M2 |
| Only empty looks are thinned | A person sitting in view would be a sighting every 3 s all shift | At most first and last per person per bucket; 2-minute buckets for shifts |
| Two looks or nothing | A brisk walker seen once would vanish from the log | A single look is an unconfirmed movement: shown, no presence change, no alert |
| Gate cameras at 1-2 s | Contradicted r2's own measured-rate rule | The rate S1 measured |
| (missing) | Overlapping watch windows of back-to-back shifts | Linked to both sessions; check-out within the person's own window; a test case |
| (missing) | Forged `attendance.sighting.v1` before the platform hardening | Accepted for M2 (nothing opens), closed before M4 |
| (missing) | A dinner window across midnight: which date is the booking? | The date the window starts |
| The live check reuses a scratch script | That file is outside the repo | Each milestone's check is an e2e test in `tests/e2e` |

Checked and true: `second_similarity` is the runner-up **person's** score (`face_db.match_many`), so the margin rule is sound. KAI-C has the streaming endpoint the fallback names. Core raises its own alerts (the coverage alert), and attempts are pruned at 90 days.
