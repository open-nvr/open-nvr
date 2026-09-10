# Per-camera skill assignment — give each camera a job

> Camera 1 reads license plates. Cameras 2 and 3 count people. Camera 4
> watches for trucks. Declared once, on the camera's settings page, and
> honoured by everything that cares.

This is the operator guide to camera assignments. The engineering design
behind it is [`docs/design/per-camera-assignment.md`](design/per-camera-assignment.md).

## What an assignment is — and is not

Every camera in OpenNVR always does the **default work**: live
streaming, recording, and the always-on Tier-0 detection that feeds the
event timeline. None of that ever depends on assignments.

An **assignment** is *additional, specialized attention* layered on top:
it declares what a camera is *for*, so the capabilities ("skills") that
can serve that purpose point themselves at the right cameras. Assigning
camera 1 to `license_plate_recognition` doesn't change what camera 1
records — it tells the LPR capability *this is your camera*, and it is
what turns plate reading ON for that camera. Assigning cameras 2–3 to
`occupancy_counting` tells the counting app to watch exactly those two
and ignore the rest.

Think of skills as what the system **can do** (detect objects, read
plates, count occupancy, recognise faces) and an assignment as **where
each of those abilities should look**.

## The one rule: eligible everywhere, computing nowhere

An unassigned camera is **offered to every skill and used by none.**

* **Eligible** — it appears in every app's camera picker, so a fresh
  install shows you your whole fleet and you choose where each skill
  looks. Assign a camera to one skill and it stops being offered to the
  others: a camera has a job, not five.
* **Adopted** — a skill's inference runs on a camera *only* once that
  camera carries the skill. Until then it costs nothing.

> **This reverses the earlier rule, and it matters on upgrade.** "No
> camera assigned" used to mean "no restriction declared", so an app
> with no assignments watched the entire fleet. That made the
> least-configured install the most expensive one, and let a newly
> installed app read every camera nobody had offered it. **After
> upgrading, assign your cameras.** An app pointed at nothing now
> watches nothing — plate reads stop on unassigned cameras until you
> assign them.

What never depends on assignments: streaming, recording, and the
always-on Tier-0 detection behind the event timeline. Un-assigning a
camera turns a skill off; it never turns the camera off.

## How to assign

Open the camera's **edit dialog** (Cameras → ✎) and use the
**Assignments** section: each row is a skill name, plus optional labels
that narrow it.

| You type | It means |
|---|---|
| `license_plate_recognition` | this camera is for LPR |
| `occupancy_counting` | this camera is counted by the occupancy app |
| `object_detection` + labels `person, truck` | this camera cares specifically about people and trucks |

Rules enforced by the server: skill names are lowercase snake_case, at
most 8 assignments per camera, one row per skill, labels normalized and
capped. The vocabulary is deliberately open — you can declare an
assignment before its adapter or app is installed; validation against
what's actually installed arrives with the catalog-UI integration.

## What honours assignments today

* **occupancy-counting** (on by default): the app scopes to exactly the
  cameras assigned `occupancy_counting` — picked up at boot *and* on its
  5-minute discovery refresh, so assigning or un-assigning on the
  settings page takes effect within minutes, no restart. Assign none and
  it counts nowhere. An explicit `cameras:` list in the app's own config
  always wins over assignments (the operator's written word is never
  second-guessed).
* **License Plate Recognition**: plate OCR runs **only** on cameras
  carrying `license_plate_recognition`. This is the expensive one — one
  full OCR call per vehicle visit — and it is now the operator's switch,
  per camera. Giving a camera a gate role on the Vehicles page assigns
  the skill as part of the same action; clearing the role releases it.
* **Any app built on the App SDK** can adopt the same behaviour with
  one call — `filter_cameras_for_skill(discovered, "my_skill")` /
  `cameras_for_skill(url, "my_skill")`. Both return `[]` when nothing
  carries the skill: **watch nothing**. `cameras_for_skill` returns
  `None` only when core could not be asked at all — unknown, so keep
  whatever roster you already had rather than acting on a blip.
* **Tier-0 (the always-on detector)**: an `object_detection` assignment
  with labels narrows that camera's detected classes ("camera 4 wants
  person + truck") — the global `DETECT_LABELS` still applies to every
  camera without a declaration, and a label change on the settings page
  restarts just that camera's worker within one reconcile tick. And with
  `DETECT_SKIP_UNASSIGNED=true` in `.env` (off by default), a camera
  whose assignments are all detection-free — say an LPR-only camera —
  skips Tier-0 analysis entirely, a straight CPU saving. Cameras with no
  assignments are never skipped.

The Assignments editor also **suggests and annotates**: typing in a
skill row offers every skill the install knows (canonical adapter tasks
plus installed catalog apps), and a skill whose capability isn't
actually served shows an amber note naming what to install ("no
registered adapter advertises license_plate_recognition — register one
on the AI Adapters page"). It annotates, it never blocks — the
vocabulary stays open, and when adapter status can't be determined
(KAI-C unreachable) nothing is flagged.

(The old per-model polling loop — a Start button that drove live
inference on a timer per camera — has been retired: live detection now
flows one way, Tier-0 → event bus → apps, and assignments are how you
point it. Recording analysis is unaffected.)

## For app developers

Read the design doc for the invariants, then copy the occupancy
pattern: fetch `discover_cameras()` once, pass the payload through
`filter_cameras_for_skill(...)`, and re-run the same scoping on your
refresh tick. Honour the contract: `None` from the filter means watch
everything you'd otherwise watch — never treat it as an empty list.
