# Using the camera's own analytics

Most IP cameras run line crossing, intrusion, loitering, tamper and
motion rules on board and announce them over ONVIF (pull-point events),
ISAPI (Hikvision) or the Dahua event stream. OpenNVR can use those
instead of, or as well as, running its own detection on the stream.

## The setting

Per camera, **Camera Settings → Events → Detection source**:

| Source | The camera's alarms | OpenNVR's detection on this camera |
|---|---|---|
| **OpenNVR** (default) | kept as history on the Events tab, no alerts | runs as configured |
| **Camera** | become alerts | paused (`detection_enabled` off) — the CPU is spent elsewhere; search metadata from this camera stops |
| **Both** | become alerts | runs as configured; the inbox shows the source of each alert (`camera · onvif`, `app · intrusion-detection`) |

Choosing Camera or Both subscribes to the camera's events at once and
again after every restart. The choice is greyed out when the camera does
not advertise an events service (the tab says so). The tab also lists the
analytics rules the camera advertises (ONVIF `GetRules`) and, for Camera
and Both, which alert types the camera's alarms may become — everything
ticked is the default.

API: `GET /api/v1/cameras/{id}/analytics-source` describes it;
`PUT /api/v1/cameras/{id}` with `analytics_source` (`opennvr` | `camera`
| `both`) and optionally `native_event_types` (a list of alert types)
changes it.

## What an alarm becomes

A native event is mapped to an OpenNVR alert type by keywords in its
topic or event type:

| Alert type | Severity | Matches (ONVIF / Hikvision / Dahua) |
|---|---|---|
| `line_crossing` | medium | `RuleEngine/LineDetector/Crossed`, `linedetection`, `CrossLineDetection`, tripwire |
| `intrusion` | high | `FieldDetector/ObjectsInside`, `fielddetection`, `regionEntrance`/`regionExiting`, `CrossRegionDetection` |
| `loitering` | medium | loitering, `WanderDetection` |
| `object_left` | medium | `unattendedBaggage`, `LeftDetection`, abandoned |
| `object_removed` | medium | `TakenAwayDetection`, object removal |
| `tamper_covered` | high | `TamperDetector`, `VideoBlind`, `SceneChange`, shelter alarm |
| `video_loss` | high | `VideoLoss` |
| `audio_alarm` | medium | `AudioAnomaly`, audio exception |
| `face_detected` | low | `FaceDetection` |
| `motion` | low | `CellMotionDetector/Motion`, `VideoMotion`, `VMD`, PIR |
| `camera_event` | low | anything else (`AlarmLocal`, …) |

Only the **active** edge of a condition raises; its end is not a second
alert. A condition the camera keeps re-announcing is one alert per type
per camera per second (the `alert_id` is derived from camera, type, rule
and second). The rule name the operator gave the line or field on the
camera rides in the evidence and the title: *Line crossed: Gate line at
Gate (camera)*.

The alert goes through the same path as an app's: the alerts inbox, the
bell, the timeline entity, and the alerts bus
(`opennvr.alerts.camera.<driver>.<camera>`), so automations, the
notifier and other apps see it exactly like an app alert, with
`source.kind = "camera"`.

## Vendor notes

- **ONVIF**: pull-point subscription; the parser keeps the full topic and
  the rule name from the `Source` items. Rules are read with `GetRules`;
  enabling or drawing them stays in the camera's own UI (writable rule
  config is not portable over generic ONVIF).
- **Hikvision**: ISAPI `alertStream`; `linedetection`, `fielddetection`,
  `regionEntrance`, `regionExiting`, `unattendedBaggage`, `loitering`,
  `VMD`, `shelteralarm`, `videoloss`, `facedetection`.
- **Dahua**: `eventManager.cgi?action=attach` with the IVS codes
  (`CrossLineDetection`, `CrossRegionDetection`, `LeftDetection`,
  `TakenAwayDetection`, `WanderDetection`, `ParkingDetection`,
  `SceneChange`, `AudioAnomaly`, `FaceDetection`) as well as
  `VideoMotion`, `VideoBlind`, `VideoLoss`, `AlarmLocal`.

## Why both

Edge rules are free and immediate but blind beyond the camera's own
picture; OpenNVR's detection is where cross-camera search, descriptors,
re-identification and the apps live. Milestone and Blue Iris make the
same per-camera choice; this setting follows them.
