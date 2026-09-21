# Gate Controller

Opens the barrier for allowed vehicles — and only for allowed vehicles.

The License Plate Recognition app decides **who** may enter and puts
every judgement on the bus as a contracted `access.decided.v1` fact.
This app is the other half: it knows **which wiring opens which gate**,
and it judges nothing. Deny, and any decision value it does not
recognise, actuate nothing. **Fail closed is the contract.**

It gets a first-class page — **Gates** — with what every barrier is
doing right now, Open and Hold controls, hold-open schedules, and a
line for every decision: who was let in, who was refused, and what the
gate actually did.

## What it speaks

Barriers accept a **momentary volt-free contact** — the same two
terminals a push button is wired to. Five ways to close it:

| Transport | For | Notes |
|---|---|---|
| **`dry_contact`** | A GPIO pin and a relay board | The universal answer: every barrier ever made takes a contact closure, and nothing on the network sits between the decision and the gate |
| **`http`** | Shelly, Tasmota, ESPHome, ControlByWeb, any IP relay | Name the product as a `profile`, give it a host |
| **`modbus`** | Industrial controllers, parking equipment | Write single coil; discrete input for position |
| **`onvif`** | ONVIF Profile C door controllers | Monitored *and* holdable with no extra wiring, no vendor code |
| **`mqtt`** | Sites already running a broker | Needs `paho-mqtt` |

Profiles fill in the URLs and the momentary timing for you:

```yaml
gates:
  "1": { name: "Main Gate",    transport: dry_contact, line: 17, active_low: true }
  "2": { name: "Visitor Lane", profile: shelly_gen2, host: "192.168.1.50" }
```

**[HARDWARE.md](HARDWARE.md) is the wiring guide** — per-vendor terminal
tables (FAAC, Nice, CAME, HySecurity, DoorKing, LiftMaster, Magnetic
AutoControl, Dahua), how to find the right terminals on a barrier we
have not listed, the relay products with documented APIs, and the
safety rules. Read it before connecting anything.

## What it does

**Live state per gate.** Open, closed, held, faulted — with "since
HH:MM". When the wiring can report the barrier's real position (a limit
switch, a Modbus discrete input, an ONVIF door monitor) the page shows
the truth; when it cannot, the page says **"state not reported"**
rather than inventing a confident "closed". A barrier that failed to
close is exactly the situation where a made-up state does harm.

**Faults are loud.** A fault means a car is sitting at a gate that did
not open, so a faulted gate sorts to the top of the page and raises a
high-severity alert. A failed open never starts the cooldown, so the
very next vehicle can retry.

**Hold open, honestly.** Manual holds from the page (15 minutes, an
hour, until released) and scheduled windows for the morning rush or a
delivery slot. Wiring that is momentary-only is **refused** rather than
faked by re-pulsing — re-pulsing a momentary input is how a barrier
ends up closing on a car. Holds are capped, so a gate nobody releases
goes back to deciding per vehicle instead of standing open all night.

**Manual control with attribution.** Open now, hold, release, and a
Test pulse for commissioning — each recorded with who did it.

**Local guards.** A plate is a weak credential; anyone can print one.
Because the decision arrives as an event rather than a function call,
this app can take a second look before a barrier moves: a confidence
floor, an allowed-reason list, and a repeat-plate rate limit that
catches the signature of a copied plate. All off by default — the plate
reader is still the decider.

**An audit that outlives the hardware.** Every decision is recorded
whether the relay answered, was in dry run, or was never wired.

**Home Assistant**, from the manifest: gate state and open/fault binary
sensors per gate, last-opened timestamps, opens and faults today, and
Open / Release buttons.

## Setup

1. Install from the App Catalog. It ships with **`dry_run: true`** so a
   fresh install cannot move hardware by surprise.
2. Turn on barrier mode in the License Plate Recognition app for the
   gate cameras — that is what publishes the decisions.
3. Map each gate camera to its wiring (Configure, applied live). See
   [HARDWARE.md](HARDWARE.md).
4. With the area clear, press **Test pulse** on each gate.
5. Turn `dry_run` off.

## Safety

This app **requests** an open. It is an accessory input, the same class
as the push button by the gate, and it is **not** a safety device.

Under **UL 325** (US) and **EN 12453** (Europe) entrapment protection
lives in the gate operator, which must monitor its own photo-eyes and
edges every cycle. Leave them wired, monitored and tested — if an
integration only works with a safety device disconnected, the
integration is wrong. Where an operator distinguishes a privileged
line-of-sight input from a soft one, always use the soft one
(LiftMaster `EXIT`, never `OPEN`; HySecurity `RADIO OPEN`). Never wire
into a hold-to-run control. HARDWARE.md has the detail.

## If the site already has an access-control panel

Then you probably should not be driving the barrier at all. An
HID/Mercury, Axis, 2N, Suprema, ZKTeco or eSSL panel already owns the
cardholder database, schedules, access levels and the audit trail;
opening the barrier around it makes every plate-admitted vehicle
invisible to the system of record. The right architecture there is to
feed the plate into the panel as a credential over OSDP or Wiegand and
let the panel decide — which is what Genetec does with an LPR camera,
where the barrier is configured as a *door*. These transports are for
barriers with no panel in front of them. See HARDWARE.md.

## Standalone

```bash
cp config.example.yml config.yml     # nats_url, gates
python gate_controller.py --config config.yml
pytest
```

`config.example.yml` documents every key.

## Upgrading from 1.0

1.0 pulsed an HTTP relay and nothing else. Your config still loads —
`relays:` is read as `gates:`, and a bare URL string is still a valid
gate — and the `access.decided.v1` contract is unchanged. New: `gates:`
with four more transports and vendor profiles, gate state and position
read-back, hold-open schedules and manual control, the local guards,
the Gates page, and Home Assistant entities. The two 1.0 alerts keep
their names; `barrier_held_open` and `barrier_refused` are new.
