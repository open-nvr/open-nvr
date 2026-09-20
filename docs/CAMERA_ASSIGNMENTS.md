# Cameras and apps — each app has its selected cameras

> Guard Scan screens the entrance. Occupancy counts the entrance and the
> shop floor. ANPR reads plates at the gate. Each app's cameras are
> selected **in its own configuration**, and any camera can serve several
> apps at once.

This is the operator guide. The engineering design behind it is
[`docs/design/per-camera-assignment.md`](design/per-camera-assignment.md).

## What never depends on this

Every camera always does the **default work**: live streaming, recording,
and the always-on Tier-0 detection that feeds the event timeline. Nothing
on this page turns a camera off.

## The rule: every camera is available, an app uses only what is selected

* **Available** — every camera appears in every app's camera list.
  Nothing is reserved, and one camera can be selected for as many apps as
  you like: one detection stream feeds them all at the cost of one.
* **Selected** — an app works on a camera *only* once that camera is
  selected for it. Until then the app reads nothing from it and computes
  nothing on it.

**No cameras selected means the app does not run.** That is where every
newly installed app starts. The app card in the catalog says *No cameras*,
and the app's own health line says *No cameras selected*, until you select
one.

## How to select cameras for an app

App Catalog → the app → **Configure** → **Cameras** → **Select cameras**.
The dialog shows every camera as a thumbnail, grouped by location, with
search, an *Online only* filter and *Select all* per location; a camera
another app already uses says so (that is fine — apps share cameras).
Press **Done**, then **Save**; Cancel discards the change. The running
app picks it up within a few seconds, with no restart.

The Cameras section then lists just this app's cameras, each with its
picture, whether it is online, and which of its zones or lines are drawn
yet. The ✕ on a card removes that camera (again, on Save).

**Set up** on a card (**Edit** once something is set) opens that camera
with everything the app needs drawn or sampled on it — Guard Scan's scan
zone, guard post and uniform colour, a line-crossing tripwire, an ROI —
one tab each, on the camera's picture, with the camera's other shapes
shown faintly for reference. A tab that is still empty offers **Copy from**
another camera that has one, as a starting point. **Done** keeps them in
the form; **Save** applies them.
Removing a camera mid-screening drops the half-watched screening rather
than reporting it as incomplete.

* You need permission to **manage** a camera to add it to or remove it from
  an app; cameras you can't manage are shown locked, with the reason.
* Zones, tripwires, ROIs and sampled colours are set up per camera, from
  its card — never on a camera the app doesn't use, where they would never
  apply. A colour is per camera because lighting is.
  Settings for the whole app (thresholds, alerts, schedules) stay in the
  form below.
* For **ANPR**, giving a camera a gate role on the **Vehicles** page
  selects it too. The Cameras section shows those roles, and warns before
  you remove a camera that has one.
* **Uninstalling an app releases its selected cameras.**

Some apps have no camera selection, because they read no camera data of
their own: **alert-notifier**, **gate-controller** and
**home-assistant-relay** act on other apps' alerts (already limited to the
cameras selected for those apps), and the **OpenNVR Agent** shows each user
their own cameras. These have no Cameras section.

## What an app may read

An app, using its own key, reads **only its selected cameras**: the camera
list, snapshots, stream grants (each scoped to that one camera's path),
recordings, events and evidence, plate data, its own per-camera settings,
and the boxes it may draw on the live view. A camera's own RTSP URL, with
its credentials, is never handed to an app.

One gap remains: the event bus is not yet partitioned per camera, so an
app can still *subscribe* to other cameras' inference events. The SDK
drops them before an app's rules see them; per-camera bus permissions are
a follow-up.

## The camera's Assignments section

The camera edit dialog (Cameras → ✎ → **Assignments**) tunes **platform
detection** for that camera. It no longer points apps at cameras — a row
naming an installed app is refused with a pointer to that app's
configuration.

| You type | It means |
|---|---|
| `object_detection` + labels `person, truck` | Tier-0 detects only people and trucks on this camera |
| `license_plate_recognition` | plate OCR runs on this camera, whether or not it is selected for the ANPR app |

Rules enforced by the server: skill names are lowercase snake_case, at
most 8 rows per camera, one row per skill, labels normalised and capped.
The dialog's **Used by** line lists the apps this camera is selected for.

* **Tier-0**: an `object_detection` row with labels narrows that camera's
  detected classes — the global `DETECT_LABELS` applies everywhere else.
  An app selected for the camera adds the classes its manifest's
  `tier0_labels` names on top of either (the narrowing still stands for
  what it names; see [tier0-consumption.md](tier0-consumption.md)).
  With `DETECT_SKIP_UNASSIGNED=true` in `.env` (off by default), a camera
  whose claims are all detection-free skips Tier-0 entirely. Cameras
  selected for a detection app (occupancy, loitering, line-crossing,
  intrusion, abandoned-object, package-delivery, smart-doorbell,
  footage-search) are never skipped.
* **Plate OCR** runs on cameras carrying `license_plate_recognition` —
  from this section, or from being selected for ANPR, which stores that
  same name.

## For app developers

In code a selection is called a *pick* (`picked_cameras`,
`camera_picked()`). Declare nothing and read them:

* `OpenNVR().roster()` returns the selected cameras — `[]` when none are
  selected (**do nothing**), `None` when core could not be asked (**keep
  what you have**). Never treat those two alike.
* Override `on_cameras_update(camera_ids)` to react when the selection
  changes; it arrives on the live config poll, even though no config key
  changed.
* `Detector` subclasses get the filtering for free: events from cameras
  that aren't selected are dropped before `on_detections`. `FrameApp`
  polls exactly the selected cameras. Use `CoreSnapshotSource` for frames.
* Per-camera settings arrive keyed by camera id (`"3"`); the bus names the
  camera `"cam3"`. Look them up with `per_camera_value(mapping, camera)`.
* An app that reads no camera data declares `AppManifest(camera_picker=False)`.

`cameras_for_skill` / `filter_cameras_for_skill` still work — a selection
is stored as a claim named after the app id — but new apps should start
from `roster()`.
