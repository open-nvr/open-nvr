# home-assistant-relay example app

> **Deprecated.** It still works, but it only turns alerts into binary
> sensors. Two replacements cover far more (cameras, live video, controls,
> health, events, availability):
>
> - the **OpenNVR Home Assistant integration**
>   ([`integrations/home-assistant/`](../../integrations/home-assistant/));
> - **Home Assistant MQTT discovery**, built into OpenNVR: add an MQTT
>   integration under *Settings > Integrations* with discovery on, and Home
>   Assistant's own MQTT integration finds every device and entity.
>
> See [docs/HOME_ASSISTANT.md](../../docs/HOME_ASSISTANT.md#two-ways-in). Use
> one of them, not the relay alongside them: alerts would show twice.

**Every OpenNVR alert in your Home Assistant dashboard.** Subscribes
to NATS `opennvr.alerts.>`, maps each alert envelope onto a Home
Assistant entity, and publishes via MQTT discovery (recommended) or
HA's REST API.

The relay is the subscriber-side bridge that makes OpenNVR alerts
first-class citizens in your existing HA dashboards and automations.
You write the HA automation once — *"when binary_sensor.opennvr_front_porch_doorbell_visitor
turns on, push a notification to my phone"* — and every doorbell
alert from OpenNVR flows through it without further wiring.

## What it does

```
┌──────────────────────────────┐
│  OpenNVR producer apps:      │
│  smart-doorbell              │
│  package-delivery            │
│  intrusion-detection         │ ──┐
│  loitering-detection         │   │
│  license-plate-recognition   │   │
└──────────────────────────────┘   │
                                   │ opennvr.alerts.>
                                   ▼
                  ┌─────────────────────────────────────┐
                  │  NATS subscriber                    │
                  └──────────────────┬──────────────────┘
                                     │ Alert dict (§11.5)
                                     ▼
                  ┌─────────────────────────────────────┐
                  │  HaMapper                           │
                  │   per-source default rule           │
                  │   + operator overrides              │
                  └──────────────────┬──────────────────┘
                                     │ HaEntity
                                     ▼
                  ┌─────────────────────────────────────┐
                  │  Publisher (one of):                │
                  │   MqttPublisher (discovery + state) │
                  │   RestPublisher (POST /api/states)  │
                  └──────────────────┬──────────────────┘
                                     │
                                     ▼
                  ┌─────────────────────────────────────┐
                  │  Home Assistant                     │
                  │   binary_sensor.opennvr_front_…     │
                  │   sensor.opennvr_driveway_last_…    │
                  └─────────────────────────────────────┘
```

The mapper has built-in rules for each shipped OpenNVR producer
example:

| Alert source | HA entity | device_class | State |
|---|---|---|---|
| `smart-doorbell` | `binary_sensor.opennvr_<camera>_doorbell_visitor` | `occupancy` | `ON` (auto-off after window) |
| `package-delivery` | `binary_sensor.opennvr_<camera>_package` | `occupancy` | `ON` |
| `intrusion-detection` | `binary_sensor.opennvr_<camera>_intrusion` | `motion` | `ON` |
| `loitering-detection` | `binary_sensor.opennvr_<camera>_loitering` | `motion` | `ON` |
| `license-plate-recognition` | `sensor.opennvr_<camera>_last_plate` | (none) | plate text |
| anything else | `binary_sensor.opennvr_<camera>_<source>` | (none) | `ON` |

Operators override per source / per camera via the `mappings:` block
in `config.yml` — pin a specific entity_id, change device_class,
adjust auto-off window, point at a different camera id.

## Why MQTT discovery, not REST

MQTT discovery is HA's standard "third-party device" integration
pattern: publish a JSON config to a magic topic the first time you
see each entity, HA auto-creates it with the right device_class,
friendly name, and device card. Subsequent state changes flow on a
plain state topic. Zero HA UI clicks needed.

REST API works too but doesn't get a device card — entities surface
as "Set up via REST API" with no grouping. Use REST only if you
can't run an MQTT broker. The recommended path on HA OS / Supervised
is the bundled Mosquitto broker add-on.

## Honesty up front

Real-world limitations:

* **Auto-off is approximate.** Binary sensors hold ON for
  `auto_off_seconds` from the last alert, then flip OFF. If your
  doorbell fires three times in 90 seconds with a 30s window, the
  sensor goes ON-ON-OFF (because the timer resets on each refire),
  not ON-OFF-ON-OFF-ON-OFF. That's the right shape for HA
  automations but not a literal event log.
* **Snapshot bytes don't reach HA via this relay.** The mapper
  drops `evidence.snapshot_b64` to keep MQTT payloads under most
  brokers' defaults. If you want the snapshot inside HA, fork the
  relay to publish snapshots to a separate MQTT image topic (the
  HA MQTT Image platform supports this) or expose them via the
  OpenNVR HTTP server and reference the URL in the entity
  attributes.
* **One-way bridge.** This relay publishes OpenNVR → HA only. HA
  → OpenNVR (e.g. arm/disarm from a HA dashboard switch) is a
  separate example not yet shipped.
* **No JetStream / durable consumer.** Like the other NATS
  subscriber examples, the relay is fire-and-forget. A broker
  outage drops alerts that fired while the relay was disconnected.
  HA's MQTT retention picks up the slack for the LAST state but
  intermediate transitions are lost.
* **Discovery payloads aren't cleaned up.** When you remove an
  alert source from your OpenNVR stack, the HA entity sticks
  around as "unavailable" until you manually remove it from the
  HA MQTT integration UI. Standard MQTT discovery limitation,
  not specific to this relay.
* **Multi-instance OpenNVR against one HA collides on entity
  ids.** The device identifier is derived from
  ``mqtt.discovery_prefix`` (default ``homeassistant``), so two
  OpenNVR instances publishing through the same broker will share
  one HA device card and may overwrite each other's state.
  Operators with two NVRs should remap ``mqtt.discovery_prefix``
  per-instance (e.g. ``opennvr_primary`` and ``opennvr_secondary``)
  AND configure HA's MQTT integration to listen on both prefixes.
* **No availability topic.** When the relay process dies, HA
  shows the last retained state as if the entity were still live.
  Pair with HA's built-in entity-unavailable detection (configure
  ``expire_after`` on the relevant entities in HA) or fork the
  relay to publish an MQTT Last-Will payload on connect.

## Quick start (MQTT, recommended)

```bash
# 1. Make sure you have an MQTT broker reachable from both
#    OpenNVR and HA. On HA OS, install the Mosquitto add-on and
#    use the same auth credentials below. On bare-metal, run any
#    standalone mosquitto.

# 2. Make sure NATS is reachable too — the OpenNVR docker-compose
#    bundles it on port 4222 by default.

# 3. Configure
cd examples/home-assistant-relay
cp config.example.yml config.yml
# edit config.yml:
#   - nats_url + (optional) nats_token
#   - backend: mqtt
#   - mqtt.host / port / username / password

# 4. Run
uv sync --extra dev
python home_assistant_relay.py --config config.yml
```

Then fire any OpenNVR alert. On the first alert per (camera_id,
source) pair, the HA MQTT integration auto-discovers the entity —
no further config needed in HA. Open *Settings → Devices & Services
→ MQTT* and you'll see "OpenNVR" as a device with each entity
underneath.

## Quick start (REST fallback)

```bash
# 1. In Home Assistant, create a long-lived access token:
#    Settings → Profile → Long-Lived Access Tokens → Create.

# 2. Configure
cd examples/home-assistant-relay
cp config.example.yml config.yml
# edit config.yml:
#   - backend: rest
#   - rest.url: http://homeassistant.local:8123
#   - rest.token: <the token>

# 3. Run
uv sync --extra dev
python home_assistant_relay.py --config config.yml
```

## Example HA automation

Once the relay is running and HA has discovered the entities, a
typical automation is one HA UI form, no code:

```yaml
# In configuration.yaml or via the HA UI builder.
- alias: "Notify on unknown visitor at front porch"
  trigger:
    - platform: state
      entity_id: binary_sensor.opennvr_front_porch_doorbell_visitor
      to: "on"
  condition:
    - condition: template
      value_template: "{{ state_attr(trigger.entity_id, 'severity') == 'high' }}"
  action:
    - service: notify.mobile_app_my_phone
      data:
        message: >
          {{ state_attr(trigger.entity_id, 'title') }} —
          {{ state_attr(trigger.entity_id, 'description') }}
```

The `severity`, `title`, `description`, `correlation_id`, and other
fields land in HA as attributes, so automations can branch on any of
them. The full §11.5 alert envelope (minus the snapshot blob) is
preserved.

## Operate

| Mode | Command |
|---|---|
| Daemon | `python home_assistant_relay.py --config config.yml` |
| One alert + exit | `python home_assistant_relay.py --config config.yml --once` |
| Verbose logs | `python home_assistant_relay.py --config config.yml --log-level DEBUG` |

## Configure

See `config.example.yml` for the full set. Key knobs:

| Field | Default | Effect |
|---|---|---|
| `nats_url` | required | NATS broker URL (`nats://host:port`). |
| `subject_pattern` | `opennvr.alerts.>` | NATS subject filter. Use `opennvr.alerts.app.smart-doorbell.>` to scope to one source. |
| `backend` | `mqtt` | `mqtt` (recommended) or `rest`. |
| `mqtt.host` / `port` | `127.0.0.1` / `1883` | MQTT broker. |
| `mqtt.discovery_prefix` | `homeassistant` | Matches HA's default. Change only if you remapped it. |
| `mqtt.auto_off_seconds` | `30` | Window for binary sensors to hold ON. `0` disables. |
| `mqtt.qos` / `retain` | `1` / `true` | HA recommends both. |
| `rest.url` / `token` | required when backend=rest | HA host + long-lived access token. |
| `mappings[]` | `[]` | Override per source / per camera. See `config.example.yml`. |

## Tests

```
cd examples/home-assistant-relay
uv sync --extra dev
uv run pytest -q
```

Tests cover:

* Mapper: default rules for all 5 shipped OpenNVR sources, fallback
  for unknown sources, camera-id slug normalisation (incl. unicode),
  snapshot_b64 stripped from attributes, override precedence
  (explicit > template > default, camera-specific > wildcard),
  malformed override skip-and-log, `parse_overrides` validation
* Publishers: MQTT discovery published once per entity, state +
  attributes published on each refire, `publish_off` writes OFF
  for binary_sensor and empty for sensor, idempotent close. REST
  POSTs to the right `/api/states/<entity_id>` path, 4xx returns
  False, idempotent close.
* Daemon: alert → mapper → publisher flow, skip path for
  unmappable alerts, failure-counter increments, auto-off
  scheduling (zero window skips, positive window schedules a task,
  task fires and calls `publish_off` after the window), and the
  config loader (required fields, qos validation, REST token
  required, mapping overrides parsing).
