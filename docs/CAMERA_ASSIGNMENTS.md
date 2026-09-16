# Cameras and apps — each app picks its cameras

> Guard Scan screens the entrance. Occupancy counts the entrance and the
> shop floor. ANPR reads plates at the gate. Each app is pointed at its
> cameras **in its own configuration**, and any camera can serve several
> apps at once.

This is the operator guide. The engineering design behind it is
[`docs/design/per-camera-assignment.md`](design/per-camera-assignment.md).

## What never depends on this

Every camera always does the **default work**: live streaming, recording,
and the always-on Tier-0 detection that feeds the event timeline. Nothing
on this page turns a camera off.

## The rule: every camera is available, an app uses only what it picked

* **Available** — every camera appears in every app's camera picker.
  Nothing is reserved, and one camera can be picked by as many apps as
  you like: one detection stream feeds them all at the cost of one.
* **Picked** — an app works on a camera *only* once that camera is picked
  for it. Until then the app reads nothing from it and computes nothing
  on it.

**Nothing picked means the app does not run.** That is where every newly
installed app starts. The app card in the catalog says *No cameras
picked*, and the app's own health line says the same, until you pick one.

## How to pick cameras for an app

App Catalog → the app → **Configure** → **Cameras**. Tick the cameras the
app should work on and press **Save**; Cancel discards the ticks. The
running app picks the change up within a few seconds, with no restart.
Unpicking a camera mid-screening drops the half-watched screening rather
than reporting it as incomplete.

* You need permission to **manage** a camera to pick or unpick it for an
  app; cameras you can't manage are shown but can't be ticked.
* Zones, tripwires and ROIs are drawn in the same form, and the zone
  editors only offer the cameras you picked. A zone on a camera the app
  doesn't use would never apply.
* For **ANPR**, giving a camera a gate role on the **Vehicles** page is the
  same pick. The Cameras section shows those roles, and warns before you
  unpick a camera that has one.
* **Uninstalling an app releases its picks.**

Some apps take no picks, because they read no camera data of their own:
**alert-notifier**, **gate-controller** and **home-assistant-relay** act on
other apps' alerts (already limited to the cameras those apps picked), and
the **OpenNVR Agent** shows each user their own cameras. These have no
Cameras section.

## What an app with picks may read

An app, using its own key, reads **only its picked cameras**: the camera
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
| `license_plate_recognition` | plate OCR runs on this camera, whether or not the ANPR app picked it |

Rules enforced by the server: skill names are lowercase snake_case, at
most 8 rows per camera, one row per skill, labels normalised and capped.
The dialog's **Used by** line lists the apps that picked the camera.

* **Tier-0**: an `object_detection` row with labels narrows that camera's
  detected classes — the global `DETECT_LABELS` applies everywhere else.
  With `DETECT_SKIP_UNASSIGNED=true` in `.env` (off by default), a camera
  whose claims are all detection-free skips Tier-0 entirely. Cameras
  picked by a detection app (occupancy, loitering, line-crossing,
  intrusion, abandoned-object, package-delivery, smart-doorbell,
  footage-search) are never skipped.
* **Plate OCR** runs on cameras carrying `license_plate_recognition` —
  from this section, or from ANPR's picks, which use that same name.

## For app developers

Declare nothing and read the picks:

* `OpenNVR().roster()` returns the picked cameras — `[]` when nothing is
  picked (**do nothing**), `None` when core could not be asked (**keep
  what you have**). Never treat those two alike.
* Override `on_cameras_update(camera_ids)` to react when picks change; it
  arrives on the live config poll, even though no config key changed.
* `Detector` subclasses get the filtering for free: events from cameras
  that weren't picked are dropped before `on_detections`. `FrameApp`
  polls exactly the picked cameras. Use `CoreSnapshotSource` for frames.
* Per-camera settings arrive keyed by camera id (`"3"`); the bus names the
  camera `"cam3"`. Look them up with `per_camera_value(mapping, camera)`.
* An app that reads no camera data declares `AppManifest(camera_picker=False)`.

`cameras_for_skill` / `filter_cameras_for_skill` still work — a pick is
stored as a claim named after the app id — but new apps should start from
`roster()`.
