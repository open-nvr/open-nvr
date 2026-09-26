# Home Assistant: user guide

What you get from OpenNVR in Home Assistant, how to set it up, and what to
do when something doesn't show. For the network side (ports, WebRTC hosts,
RTSPS, mDNS) see [HOME_ASSISTANT.md](HOME_ASSISTANT.md).

## What you get

Every OpenNVR site appears in Home Assistant as **one device for the
server**, **one device per camera**, **one per zone**, and **one per app**
that declares entities (Occupancy, Guard Scan, …). The list below is the
same whichever way you connect; the native integration adds live video and
the media browser on top.

| Device | Entities |
|---|---|
| **Server** | CPU %, memory %, storage used % · unacknowledged alerts (on/off + count) · highest alert severity · *Acknowledge all alerts* button · **Site mode** as an alarm panel (armed/disarmed) when the token may change it, else a read-only sensor · *Manual event* button |
| **Each camera** | online · **motion** (binary sensor) · **detection** switch · **recording** switch and *recording problem* · last object seen · last plate read · object counts per class (`count.person`, `count.car`, …) and currently-present counts · PTZ buttons (up/down/left/right/zoom) and preset select · diagnostics: bitrate, detection fps, inference ms · an **event** entity that fires per detection and per alert |
| **Each zone** | occupancy (on/off) and head-count per class |
| **Each app** | whatever the app's manifest declares — Occupancy's counts and levels, Guard Scan's compliance rate, and so on |

Diagnostics (CPU, memory, bitrate, fps, inference ms, currently-present
counts) are created **disabled**; enable the ones you want in Home
Assistant. Commands — switches, buttons, PTZ, site mode — run **as the API
token you chose** and are written to OpenNVR's audit log.

## Two ways to connect

| | **MQTT discovery** (available now) | **OpenNVR integration** (native) |
|---|---|---|
| What you install in Home Assistant | nothing beyond its own **MQTT** integration | the `opennvr` custom integration (HACS) |
| Needs | an MQTT broker both sides can reach, an OpenNVR API token | an OpenNVR API token |
| Entities above | yes | yes |
| Live video, snapshots, media browser, services, notifications with pictures, Assist | no | yes |
| Status | shipped | built and tested; installable once `pyopennvr` is published on PyPI (see `integrations/home-assistant/RELEASING.md`) |

Use **one** of the two. Running both shows every entity twice; the native
integration raises a repair if it finds OpenNVR publishing MQTT discovery,
and another if the old `home-assistant-relay` app is still enabled.

## Setting up MQTT discovery

### 1. A broker
Any MQTT broker Home Assistant already uses works (Mosquitto add-on, an
existing broker). OpenNVR's containers must be able to reach it by name or
address. Give it a username and password; the broker is the trust
boundary — anyone who can publish to it can send every command the token
below allows.

### 2. An API token in OpenNVR
*Settings → API Tokens → New token.* The **Home Assistant** preset selects
`cameras.view`, `live.view`, `recordings.view`, `alerts.view`,
`settings.view`. What the token may do is exactly what Home Assistant may
do:

| Add scope | To get |
|---|---|
| `cameras.manage` | the detection / recording switches |
| `ptz.control` | PTZ buttons and presets |
| `settings.manage` | site mode as an alarm panel you can arm/disarm (without it: a read-only sensor) |
| `events.create` | the *Manual event* button |

Limit the token to the cameras Home Assistant should see, if you like.
Do **not** limit it to an address: commands arrive from the broker, not
from Home Assistant's IP, so an address-limited token is refused here.
Copy the secret — it is shown once — you will not need it for MQTT (OpenNVR
acts as the token itself), but keep it if you plan to use the native
integration later.

### 3. The MQTT integration in OpenNVR
*Settings → Integrations → Add → MQTT:*

| Field | Value |
|---|---|
| **Broker URL** | `mqtt://broker:1883`, or `mqtts://broker:8883` for TLS |
| **Username / Password** | the broker's. The password is encrypted at rest and shown masked afterwards; leave the mask alone to keep it, type a new one to change it |
| **Topic Prefix** | `opennvr` unless you have a reason to change it |
| **Home Assistant discovery** | on |
| **Act as API token** | the token from step 2 |
| **Discovery Prefix** | `homeassistant` unless you changed it in Home Assistant's MQTT integration |

**Test** publishes one message to the broker. **Save** starts the bridge:
within a few seconds Home Assistant's MQTT integration lists the devices.
Nothing else to configure in Home Assistant.

### 4. Check
*Settings → Devices & services → MQTT* in Home Assistant should show
*OpenNVR* and one device per camera. In OpenNVR, `GET /api/v1/system/info`
reports `"mqtt_discovery": true` while the bridge is connected.

## Using it

**Automations on detections** — the camera's *motion* binary sensor and
*detection* event entity are the usual triggers:

```yaml
automation:
  - alias: Porch light on a person at night
    trigger:
      - platform: state
        entity_id: event.front_door_detection
    condition:
      - condition: template
        value_template: "{{ trigger.to_state.attributes.event_type == 'person' }}"
      - condition: sun
        after: sunset
    action:
      - service: light.turn_on
        target: { entity_id: light.porch }
```

**Arm when everyone leaves** — the site-mode alarm panel:

```yaml
    action:
      - service: alarm_control_panel.alarm_arm_away
        target: { entity_id: alarm_control_panel.opennvr_site_mode }
```

**Pause detection on a camera** — `switch.<camera>_detection`; **acknowledge
the inbox** — the *Acknowledge all alerts* button.

**Raw MQTT** (for other consumers): `opennvr/<site>/status` is `online` /
`offline` (broker Last Will); `opennvr/<site>/<key>/state` and
`.../attributes` hold every entity, retained; `opennvr/<site>/<key>/event`
carries detections and alerts as CloudEvents 1.0; `opennvr/alerts` carries
every alert as the webhook integrations receive it; publish to
`opennvr/<site>/<key>/set` to command. `<site>` is the first 12 characters
of the site id.

## Setting up the native integration (when released)

1. Home Assistant → HACS → *Custom repositories* → add the `hass-opennvr`
   repository → install **OpenNVR** → restart Home Assistant.
2. *Settings → Devices & services → Add integration → OpenNVR.* Enter
   `https://<opennvr-host>` and the API token from step 2 above. Turn off
   certificate verification unless you installed a trusted certificate
   (the integration raises a repair reminding you).
3. Choose the cameras. Leaving all selected means cameras added to OpenNVR
   later appear too.

You then also get: the camera entities with live view (WebRTC), snapshots,
a **media browser** for recordings and clips, the services `opennvr.ptz`,
`opennvr.create_event`, `opennvr.end_event`, `opennvr.export_recording`,
`opennvr.protect_recording`, `opennvr.ack_alerts`, `opennvr.search_events`,
the events `opennvr_alert` / `opennvr_media_ready` for phone notifications
with the picture and clip (a blueprint is shipped:
`blueprints/automation/opennvr/alert_notification.yaml`), reauthentication
when the token is revoked, and an Assist tool that answers "what's on the
driveway camera" from the same data.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| No devices appear | Home Assistant's MQTT integration must use the **same broker and discovery prefix**. Check OpenNVR's log for `MQTT bridge` lines: an unreachable broker, refused credentials, or a revoked token are named there. |
| Devices appear but no camera | The token can't see that camera (camera limit) or lacks `cameras.view`. |
| A switch/button does nothing | The token lacks the scope (`cameras.manage`, `ptz.control`, `settings.manage`, `events.create`). Commands are also rate-limited and never run from **retained** messages. |
| Everything is there twice | Two paths are on (MQTT discovery + native integration), or the deprecated `home-assistant-relay` app is enabled. The native integration raises a repair for both; keep one. |
| Entities go *unavailable* after a while | The token was revoked or expired — the bridge stops within a minute. Create a token and update the integration. |
| I deleted the integration but Home Assistant still lists the devices | OpenNVR clears its retained topics when you delete, disable, or move the integration; that needs the broker reachable at that moment. If it wasn't, delete the devices in Home Assistant by hand. |
| The broker password shows as `••••••••` | That is the mask; the stored password is unchanged. Type a new one only to replace it. |
| `mqtt_discovery` is `false` in `/system/info` | No MQTT integration is enabled with discovery on, or the bridge is reconnecting; the log says which. |

## Security notes

- The broker is the trust boundary: use credentials and, if the broker has
  them, ACLs so only Home Assistant may publish to `opennvr/<site>/+/set`.
- The token bounds everything: what is published is what the token may
  see; what runs is what it may do; every command is audited as
  `mqtt:<integration name>`.
- Broker/SMTP passwords and webhook secrets are encrypted at rest with
  `CREDENTIAL_ENCRYPTION_KEY` and never returned by the API.
- Revoking the token takes the devices offline within a minute; deleting
  the integration removes them from Home Assistant.
