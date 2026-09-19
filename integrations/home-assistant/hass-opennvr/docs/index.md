# OpenNVR for Home Assistant

The OpenNVR integration brings an [OpenNVR](https://github.com/open-nvr/open-nvr) video recorder into Home Assistant:

- live cameras over WebRTC;
- detections, occupancy and alerts as sensors and events;
- controls such as detection, PTZ, arming and acknowledging;
- recordings in the media browser;
- phone notifications with pictures and clips.

It talks to OpenNVR over the local network. Changes are pushed as they happen, so no MQTT broker is needed.

Most entities are **described by the OpenNVR server**. When OpenNVR, or an AI app installed on it, adds a sensor or a control, it appears in Home Assistant without an integration update.

## Supported devices

- **OpenNVR servers** that speak contract version 1.x, which is reported in *Settings > System* and at `/api/v1/system/info`. A server that is too old, or a contract that is too new, raises a repair telling you which one to update.
- **Cameras:** every camera OpenNVR records, including turned-off ones. Pan/tilt/zoom controls appear for cameras OpenNVR knows to be PTZ.
- **AI apps** installed on OpenNVR that declare entities (for example abandoned-object detection).

## Installation

**Development status:** the integration is not installable by end users until its library, `pyopennvr`, is published on PyPI. Until then, use the development setup in the repository README.

Once it is published:

1. In HACS, add this repository as a custom repository (type *Integration*) and install **OpenNVR**, or copy `custom_components/opennvr` into your `config/custom_components` folder.
2. Restart Home Assistant.

### Create an API token in OpenNVR

In OpenNVR, open **Settings > API Tokens** and create a token with the Home Assistant preset. The secret is shown once, so copy it.

| Scope | Needed for |
|---|---|
| `settings.view`, `cameras.view` | Required: site information, cameras, entities, push updates |
| `live.view` | Live video and snapshots |
| `recordings.view` | Clips, the media browser, "last object" pictures |
| `alerts.view` | Alert sensors, alert events and notifications |
| `cameras.manage` | The detection switch and the camera motion-detection toggle |
| `alerts.manage` | Acknowledging alerts |
| `ptz.control` | PTZ buttons, presets and the `opennvr.ptz` action |
| `events.create` | Manual events (`opennvr.create_event`) |
| `settings.manage` | Arming and disarming (the alarm panel) |

You can limit the token to some cameras, or to Home Assistant's address. Revoking it cuts Home Assistant off at once.

### Add the integration

Go to **Settings > Devices & services > Add integration > OpenNVR**. OpenNVR servers that run the optional mDNS announcer (Linux hosts only) are offered automatically.

| Parameter | Description |
|---|---|
| URL | The address you open OpenNVR at, for example `https://192.168.1.20`. |
| API token | The token created above. It starts with `onvr_`. |
| Verify SSL certificate | Turn this off for OpenNVR's default self-signed certificate. A repair then suggests installing a trusted certificate. |
| Cameras | The cameras to add. Leave them all selected to include cameras added to OpenNVR later. |

The setup step lists any recommended scope the token lacks.

## Configuration options

**Settings > Devices & services > OpenNVR > Configure**:

| Option | Description |
|---|---|
| Cameras | Which cameras this entry shows. Removing a camera removes its entities. |
| Notification link lifetime | How long picture and clip links in notifications and action results stay valid (1–168 h, default 24 h). |

To change the address, SSL verification or token, use **Reconfigure**. When the token stops working, Home Assistant asks for a new one (reauthentication).

## Devices and entities

Devices: the **OpenNVR server**, one device per **camera**, one per **zone** (under its camera) and one per **AI app**.

| Platform | Examples |
|---|---|
| Camera | Live view (WebRTC), snapshots, motion-detection toggle (OpenNVR object detection), on/off (only where the site allows pausing recording) |
| Binary sensor | Occupancy (all and per label, per camera and per zone), motion, online, recording problem, unacknowledged alerts |
| Sensor | Counts per label, last plate, alert count and highest severity, storage, CPU and memory, detection FPS, inference time, bitrate |
| Event | Detections (per camera and per zone, one event type per label), alerts |
| Image | Last object seen on each camera |
| Switch | Object detection, recording (only where the site allows pausing recording), app toggles |
| Select / Button / Number | PTZ presets, PTZ moves, acknowledge all alerts, manual event, app controls and settings |
| Alarm control panel | Site mode: disarmed, armed home, armed away. Read-only without `settings.manage`. |
| Update | Server version. A newer release shows only if the OpenNVR operator enabled the update check. |

Diagnostic entities (FPS, inference time, bitrate, CPU, memory) are disabled by default.

## Actions

| Action | Fields | Returns |
|---|---|---|
| `opennvr.ptz` | `camera`, `action` (move, zoom, stop, preset), `argument` (up/down/left/right, in/out, or a preset name), `speed` | — |
| `opennvr.create_event` | `camera`, `label`, `sub_label`, `duration` | `event_id` |
| `opennvr.end_event` | `event_id` | — |
| `opennvr.export_recording` | `camera`, `start`, `end` (≤ 1 h), `with_hash` | `url`, `direct_url`, `expires_at`, and `sha256` and `bytes` when `with_hash` is set |
| `opennvr.protect_recording` | `event_id`, `pre_s`, `post_s` | — |
| `opennvr.ack_alerts` | `alert_ids`, or `source` and/or `severity` | `count` |
| `opennvr.search_events` | `query`, `camera`, `label`, `zone`, `plate`, `start`, `end`, `limit` | `results`, with a `thumbnail_url` on events |

With more than one OpenNVR site, pass `config_entry_id` to the actions that take no camera.

`query` is plain language ("red truck at the gate"). When OpenNVR runs the
**footage-search** app, it answers too, and its matches come back as
`kind: footage` with the detected objects and a caption.

## Assist

The integration adds an LLM API named **OpenNVR**. Select it (alongside
*Assist*, if you like) in a conversation agent's options, and ask things like
"did a white van come to the gate yesterday?", "what happened overnight?" or
"is the garage door open?". The agent gets five tools:

| Tool | What it does |
|---|---|
| `opennvr_search_events` | searches detections, alerts and footage (plain language, or by camera, object, zone, plate, time) |
| `opennvr_summarize_period` | counts per camera what was detected and alerted in a period (up to 31 days) |
| `opennvr_list_alerts` | lists alerts, optionally only unacknowledged ones |
| `opennvr_describe_camera` | describes a camera's current view, or answers a question about it, with OpenNVR's image model |
| `opennvr_ptz_goto_preset` | moves a PTZ camera to a saved preset |

The tools reach only cameras that are **exposed to the assistant**
(*Settings > Voice assistants > Expose*), shown by the integration, and
allowed by the token. Camera entities are not exposed by default, so expose
the ones Assist may talk about. Describing needs a caption or visual-question
model in OpenNVR (for example the Ollama VLM adapter) and the token's
`live.view`; each description is audited in OpenNVR and limited to a few a
minute. Moving to a preset needs `ptz.control`. Summaries need OpenNVR with
the `search_summary` feature. What the model says about a picture can be
wrong; the camera entity's snapshot is the ground truth.

## Media browser

**Media > OpenNVR** contains:

- **Alerts**, by severity;
- **Events**, by camera and label, with thumbnails;
- **Recordings**, by camera, day and hour.

Everything plays through Home Assistant's own address, so it also works away from home.

## Notifications

The integration fires two events for automations:

- `opennvr_alert`, when an alert lands;
- `opennvr_media_ready`, when its clip, or a detection's clip, can be played.

Both carry picture and clip links under Home Assistant's own address, so a phone can open them from anywhere it reaches Home Assistant.

The blueprint **OpenNVR alert notification** (`blueprints/automation/opennvr/alert_notification.yaml`) turns these into phone notifications. It filters by severity, camera and site mode, supports quiet hours and a cooldown, and offers *Acknowledge* and *Live view* buttons.

## How data is updated

The integration keeps one websocket open to OpenNVR, which pushes changes as they happen. Every 30 seconds it also reads the full state over REST. If the connection drops, it resumes where it left off; when OpenNVR can't replay what was missed, it sends a fresh snapshot. Entities become unavailable while the server is unreachable.

## Examples

Notify when a person is detected at the front door:

```yaml
triggers:
  - trigger: state
    entity_id: event.front_door_detection
conditions:
  - "{{ trigger.to_state.attributes.event_type == 'person' }}"
actions:
  - action: notify.mobile_app_phone
    data:
      message: Someone is at the front door
```

Arm the site when everyone leaves:

```yaml
triggers:
  - trigger: state
    entity_id: zone.home
    to: "0"
actions:
  - action: alarm_control_panel.alarm_arm_away
    target:
      entity_id: alarm_control_panel.opennvr_site_mode
```

Mark a doorbell press on the recording:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.doorbell
    to: "on"
actions:
  - action: opennvr.create_event
    data:
      camera: camera.front_door
      label: doorbell
      duration: 30
```

## Use cases

- Light the driveway when a car is detected there at night.
- Arm OpenNVR's alarm actions with the house alarm.
- Get a phone notification with the clip when an AI app raises an alert, and acknowledge it from the notification.
- Keep the recording around a doorbell press.
- Search last night's events by plate or label from a script.

## Known limitations

- Live view uses WebRTC. OpenNVR must advertise an address the viewing device can reach (`MEDIAMTX_WEBRTC_HOSTS`); a repair tells you when it doesn't.
- An RTSP stream (for recording clips inside Home Assistant) exists only if OpenNVR publishes RTSPS beyond itself (`RTSPS_BIND_HOST`, `MEDIAMTX_EXTERNAL_RTSPS_URL`). Live view doesn't need it.
- Detection events fire only for the labels OpenNVR lists for the camera.
- Recordings play as one-hour clips, and seeking depends on the OpenNVR version.
- mDNS discovery works only with OpenNVR on a Linux host. Elsewhere, enter the address.

## Troubleshooting

| Symptom | What to do |
|---|---|
| "Certificate could not be verified" | OpenNVR's default certificate is self-signed. Turn off *Verify SSL certificate*, or install a trusted certificate. |
| "Token lacks required scopes" | Create a token with at least `settings.view` and `cameras.view`. |
| Repair "OpenNVR refuses Home Assistant's address" | The token is limited to certain addresses. Add Home Assistant's address, or clear the limit. |
| Repair "OpenNVR live video may not reach your devices" | Set `MEDIAMTX_WEBRTC_HOSTS` to the OpenNVR host's LAN address. |
| Repair "clocks differ" | Enable NTP on both machines. |
| Entities unavailable | Check that OpenNVR is reachable from Home Assistant. **Download diagnostics** on the integration (token and addresses are removed) and attach it to an issue. |

## Removing the integration

1. Go to **Settings > Devices & services > OpenNVR**, open the menu, and choose **Delete**.
2. In OpenNVR, revoke the API token under **Settings > API Tokens**.
3. If you installed through HACS, remove the repository there. Otherwise delete `custom_components/opennvr`.
