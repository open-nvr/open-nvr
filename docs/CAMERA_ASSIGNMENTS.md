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
records — it tells the LPR capability *this is your camera*. Assigning
cameras 2–3 to `occupancy_counting` tells the counting app to watch
exactly those two and ignore the rest.

Think of skills as what the system **can do** (detect objects, read
plates, count occupancy, recognise faces) and an assignment as **where
each of those abilities should look**.

## The one rule that keeps old setups working

**Nothing assigned means nothing restricted.** A skill with no camera
assigned to it behaves exactly as before assignments existed: the
occupancy app watches every camera, the indexer indexes everything.
Restriction begins only when the *first* camera is assigned a given
skill — from that moment, the assignment list for that skill is the
whole truth. Un-assign the last camera and the restriction lifts again.

This is why upgrading changes nothing until you choose to use the
feature, and why an app never goes silently blind: "no assignment" can
never be misread as "watch nothing".

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

* **occupancy-counting** (on by default): when at least one camera is
  assigned `occupancy_counting`, the app scopes to exactly those
  cameras — picked up at boot *and* on its 5-minute discovery refresh,
  so assigning or un-assigning on the settings page takes effect within
  minutes, no restart. An explicit `cameras:` list in the app's own
  config always wins over assignments (the operator's written word is
  never second-guessed).
* **Any app built on the App SDK** can adopt the same behaviour with
  one call — `filter_cameras_for_skill(discovered, "my_skill")` /
  `cameras_for_skill(url, "my_skill")` — following the same
  `None` = "no restriction declared" contract.

## What's coming (the remaining slices)

* **Tier-0 per-camera classes**: `object_detection` + labels on a
  camera will narrow the always-on detector's classes for that camera
  ("camera 4 also wants trucks"), and a camera assigned nothing
  detection-shaped will be skippable entirely — a CPU saving, opt-in.
* **Catalog-UI validation**: the camera settings page will grey out a
  skill whose capability isn't installed, with a pointer to what to
  install ("LPR needs the plate adapter — not installed").
* **Retiring the per-model polling loop** in favour of assignments +
  the publish/subscribe path.

## For app developers

Read the design doc for the invariants, then copy the occupancy
pattern: fetch `discover_cameras()` once, pass the payload through
`filter_cameras_for_skill(...)`, and re-run the same scoping on your
refresh tick. Honour the contract: `None` from the filter means watch
everything you'd otherwise watch — never treat it as an empty list.
