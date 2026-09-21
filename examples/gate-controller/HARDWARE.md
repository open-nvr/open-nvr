# Wiring Gate Controller to real hardware

Everything here answers one question: **what does your barrier accept as
"open now", and how do we send it?**

In the field the answer is nearly always a **momentary volt-free
contact** — the same two terminals a push button, a key switch or a
loop detector is wired to. So the fastest path for most sites is a
relay across those terminals, and the rest of this document is about
finding them and about the sites where something else is better.

> **Read the safety section before you connect anything.** This app is
> an accessory input, the same class as the button on the wall. It does
> not, and must not, participate in your gate's entrapment protection.

---

## Which transport do I want?

| Your situation | Transport | Why |
|---|---|---|
| A barrier and nothing else | **`dry_contact`** | A GPIO pin and a £3 relay board. Works with every barrier ever made, and nothing on the network sits between the decision and the gate. |
| You already run Shelly / Tasmota / ESPHome | **`http`** | Name the product as a profile, give it a host, done. |
| Industrial controller, PLC, parking equipment | **`modbus`** | One coil write. |
| The site has a door controller | **`onvif`** | Profile C — monitored and holdable with no extra wiring, no vendor code. |
| You already run a broker | **`mqtt`** | Needs `paho-mqtt`. |
| **The site already has an access-control panel** | **None — see [below](#if-the-site-already-has-an-access-control-panel)** | Usually you should not be driving the barrier at all. |

---

## 1. Barrier operators: finding the terminals

Three electrically different things get called "an open input". Only
the first two work with a plain relay:

| Type | What it is | Plain dry contact? |
|---|---|---|
| **Volt-free / dry N.O.** — FAAC, CAME, Nice, HySecurity, LiftMaster, DoorKing | Two terminals; closing them is the command. The barrier supplies its own logic voltage. | **Yes, directly.** |
| **24 V sourcing input** — Magnetic AutoControl MGC / MGC-Pro | The input activates when **+24 V DC is applied**. | **Yes, but** the relay must switch the barrier's own +24 V rail into the input. A bare closed contact does nothing. |
| **Serial / protocol only** — some Dahua / Hikvision modes, RS-485 boards | A command over RS-485 or an IP API. | **No.** Use `modbus`/`http`, or the board's parallel dry-contact terminals if it has them. |

### Verified operators

| Operator | Open input | Hold-open input | Position output |
|---|---|---|---|
| **FAAC 620/640 (624BLD)** | `OPEN` — N.O. | — | 624 Relay Card: RL2 *Opened*, RL3 *Closed*, N.O. 1 A @ 24 V; 624BLD `OUT1`–`OUT3` programmable |
| **Nice WIL** | N.O. step/open | **`Open-Timer` (t14)** — maintained holds the bar in an infinite pause | t9 *C.A. Indicator* — **24 V lamp output, not a contact** |
| **CAME ZL37 / ZL38** | **`2-3`** Open button (N.O.); `2-7` command button | **No latch.** `2-7` maintained is **hold-to-run** — see the warning below | `10-5` "barrier open" pilot lamp, 24 V 3 W — **lamp, not a contact** |
| **HySecurity / Nice HySecurity (Smart DC)** | **`RADIO OPEN`** (N.O.) — HySecurity's documented input for access control, not `OPEN` | Physical Hold Open toggle; free-exit latch via `EB`/`CB`/`DT` menu | **User Relay 1/2/3** — fn #1 Close-limit, #3 Open-limit, #8 gate-open-too-long |
| **DoorKing 1601 / 1602** | **Terminal 6 `UP`** (dry contact to t14 common) | **Yes — t6 maintained with SW1-6 OFF:** the arm stays up regardless of any down input or timer | **No.** t12/t13 are dry contacts but driven by **loop logic, not arm position** — leave this gate unmonitored |
| **LiftMaster / Chamberlain (CSL24U family)** | **`EXIT` — always. Never `OPEN`.** See the warning below | `EXIT` maintained holds an open gate and pauses the timer-to-close | Expansion board **`AUX RELAY 1/2`**, N.O.+N.C., 42 VDC / 5 A, with Open-Limit and Close-Limit functions |
| **Magnetic AutoControl MGC / MGC-Pro** | `IN1`–`IN5`, **+24 V applied, not volt-free**. Default IN1/IN2 = *Open low priority* | **`Open Service`** (MGC-Pro): boom stays up while 24 V is present | Assignable outputs: `Open`, `Closed`, `Opening`, `Closing`, `Boom angle`. Default NO1 = `Open` |
| **Dahua economic boom barrier** | **t10 `Open Barrier`**, t11 `Close`, t9 `COM` | not documented | **t7 `Opened`, t8 `Closed`, t9 `COM`** — a real two-state output |

### ⚠️ Two rules that are not style preferences

**LiftMaster: wire to `EXIT`, never `OPEN`.** On the CSL24U family,
`OPEN` is a *hard open* — a maintained switch there **overrides the
operator's external safeties** and resets the alarm condition, and it
is intended for line-of-sight use only. `EXIT` is the *soft* command,
documented for "telephone entry, external exit loop detector, or any
device that would command the gate to open" — which is exactly what
this app is. A plate reader is by definition not line-of-sight. Wiring
it to `OPEN` would let software defeat the entrapment protection.

**Never wire into a hold-to-run (dead-man) control.** CAME's `2-7` in
maintained-action mode, and the equivalent on other boards, moves the
barrier *only while the contact is held* and stops the instant it
opens. EN 12453 treats that as a safety control mode requiring a
present operator. A network stall or a crashed process would leave a
barrier mid-travel with nobody there.

### Vendors we could not verify

We did not find manufacturer wiring manuals with terminal tables for
**Nice (non-WIL barriers), BFT, Beninca, All-O-Matic, Viking, Elite,
Automatic Systems, Hikvision barrier gates**, or the Indian market
(**Godrej, Aditya, Spectra, Matrix, eSSL, Realtime**). They are not
unsupported — the great majority take a dry contact like everything
else — we simply will not print terminal numbers we have not read.

**If your barrier is not in the table**, the procedure is the same
everywhere:

1. Find the terminal block and its printed legend. Look for a group of
   command inputs (`OPEN` / `OP` / `APRE` / `START` / `P.P.` in Europe,
   `OPEN` / `CYCLE` / `RADIO` / `EXIT` in the US, `Open Barrier` /
   `Close Barrier` / `COM` on Chinese OEM boards) and a separate group
   of status outputs (`SCA`, `Opened`/`Closed`, `AUX RELAY`).
2. **Confirm it is volt-free** with a continuity test *before*
   connecting a relay — measure across the pair with the barrier
   powered. Voltage present means it is a sourcing input (the Magnetic
   case) and needs different wiring.
3. Try it with a wire link, briefly, before automating it. If a momentary
   short opens the barrier, a relay across the same pair will too.
4. `STOP` is **always** normally-closed. Never put our relay there.

Much of the Indian-market equipment is a rebadged Chinese controller
with the Dahua-style `Open` / `Close` / `COM` inputs and
`Opened` / `Closed` / `COM` outputs, or a licensed European board — so
one of the patterns above almost certainly applies.

### Reading the barrier's position

A gate is **monitored** when the wiring can tell us where the barrier
actually is. Without that, the page says *"state not reported"* rather
than inventing a confident "closed" — which matters, because a barrier
that failed to close is exactly when a made-up state does harm.

- **Real contacts** (wire to a GPIO input, a Modbus discrete input, or a
  door monitor): FAAC 624 relay card, HySecurity user relays,
  LiftMaster AUX RELAY, Magnetic assignable outputs, Dahua t7/t8.
- **Lamp outputs are not contacts.** Nice's C.A. indicator and CAME's
  `10-5` drive a 24 V bulb. Reading them needs an opto-isolator or a
  small 24 V relay — without one you will read nothing, or backfeed the
  board.
- **HySecurity limits are encoder-learned**, not mechanical switches.
  The contact is real, but a lost calibration mis-reports silently,
  where a broken physical switch simply reads "not open".

---

## 2. Dry contact — the universal path

A GPIO line driving an opto-isolated relay module, across the
barrier's open terminals.

```yaml
gates:
  "3":
    name: "Main Gate"
    transport: dry_contact
    line: 17              # BCM / line offset
    active_low: true      # most relay boards are
    pulse_ms: 500
    sense_line: 27        # optional: the operator's "gate open" contact
    hold_line: 22         # optional: a second channel on a hold input
```

Uses the kernel's libgpiod character device when the `gpiod` Python
binding is installed, and falls back to sysfs otherwise. The container
needs access to the GPIO device; without it you get a clear error at
startup rather than a failure at the first car.

`sense_line` is what makes the gate monitored. `hold_line` is what
makes *Hold open* possible — without it the app refuses to hold rather
than faking it (see [Holding](#3-holding-a-gate-open)).

---

## 3. IP relays over HTTP

**The rule: let the device time its own pulse.** Shelly's
`toggle_after`, Tasmota's `PulseTime` and an ESPHome `on_turn_on`
automation all run on the relay, so they survive the network dropping
mid-pulse. An "HTTP on, sleep, HTTP off" from this app does not — if
the off-call is lost, the contact stays closed and the barrier stays
up. We do that only as a fallback, and we shout when it fails.

### Shelly

Gen1 and Gen2+ are genuinely different APIs; use the right profile.

```yaml
gates:
  "3": { profile: shelly_gen2, host: "192.168.1.50", channel: 0 }
  "4": { profile: shelly_gen1, host: "192.168.1.51" }
```

| | Gen1 | Gen2+ (Plus / Pro / Gen3 / Gen4) |
|---|---|---|
| Momentary | `/relay/0?turn=on&timer=N` | `/rpc/Switch.Set?id=0&on=true&toggle_after=N` |
| State | `/relay/0` → `ison` | `/rpc/Switch.GetStatus?id=0` → `output` |

Both profiles are **latching-capable** (there is an off URL), so *Hold
open* works, and both are **monitored**.

### Tasmota

```yaml
gates:
  "3": { profile: tasmota, host: "192.168.1.52", channel: 0 }
```

Set the pulse on the device: `PulseTime1 105` = 500 ms (`1..111` are
0.1 s steps; `112..64900` are seconds offset by 100, so `115` = 15 s).

**Tasmota gates are deliberately unmonitored.** There are long-standing
reports that querying `Power` *during* an active `PulseTime` window can
defeat the auto-off and leave the relay latched on
([#7810](https://github.com/arendst/Tasmota/issues/7810),
[#4093](https://github.com/arendst/Tasmota/issues/4093)). On a lamp
that is a curiosity; on a barrier it means holding the boom up. So the
profile ships with no status probe. If you need a monitored gate, use
Shelly, ESPHome or a dry contact with a sense line.

### ESPHome

Put the timing in the device:

```yaml
# esphome device config
switch:
  - platform: gpio
    pin: GPIO5
    id: gate_relay
    name: "gate"
    on_turn_on:
      - delay: 500ms
      - switch.turn_off: gate_relay
```

```yaml
# gate-controller
gates:
  "3": { profile: esphome, host: "192.168.1.53", switch: "gate",
         self_timed: true }
```

ESPHome's `interlock` is software-only — if you drive open and close
relays from one ESP, use a hardware SPDT interlock as ESPHome itself
recommends.

### Commercial boards

**ControlByWeb / Xytronix** (X-320, WebRelay, X-301) is the one to reach
for if you are buying: its manual documents a per-request pulse
duration, discrete-input read-back *and* a Modbus slave, which covers
every need from one product. `state.json?pulseTime1=5&relay1=2` — note
that `pulseTime1` must come **before** `relay1=2`.

```yaml
gates:
  "3":
    transport: http
    url: "http://192.168.1.60/state.json?pulseTime1=1&relay1=2"
    status_url: "http://192.168.1.60/state.json"
    status_open_when: '"relay1":"1"'
    self_timed: true
```

**Sonoff** is a hardware brand, not an API — its stock firmware speaks
the eWeLink cloud protocol. Reflash with Tasmota or ESPHome and use
those profiles.

**KMtronic** and **Denkovi** boards are widely used and we could not
confirm their URL shapes against a manufacturer source, so there is no
profile for them. `transport: http` with an explicit `url` works fine.

---

## 4. Modbus TCP

```yaml
gates:
  "3":
    name: "Truck Gate"
    transport: modbus
    host: "10.0.4.12"
    port: 502
    unit: 1
    coil: 0            # write 0xFF00 / 0x0000 — function 0x05
    sense_input: 0     # optional: discrete input — function 0x02
    hold_coil: 1       # optional: a maintained coil on a hold input
```

Function codes used: **0x05** write single coil (the pulse) and **0x02**
read discrete inputs (the position). One command opens one connection
and closes it — embedded slaves cap concurrent connections hard
(ControlByWeb allows two and drops an idle one after 50 s), so a
long-lived socket per barrier is how a site runs out of them.

**Waveshare Modbus POE ETH Relay**: default IP `192.168.1.254`, port
502, unit `0x01`, coils `0x0000`–`0x0007`. It also has native "Flash
ON" registers at `0x0200`–`0x0207` (value × 100 ms) for a device-timed
pulse — preferable if your variant supports it. Discrete-input
read-back is **not** documented on every variant; check yours before
relying on `sense_input`.

Generic industrial boards (Eletechsup and the AliExpress class)
commonly default to unit 1 / port 502 / coils from `0x0000`, but this
is **unverified** — take the addresses from your board's own table.

---

## 5. ONVIF Profile C

The vendor-neutral path. A conformant door controller gives us a
momentary open, a real hold, and position feedback with no extra
wiring — the only transport here that manages all three out of the box.

```yaml
gates:
  "3":
    name: "Visitor Lane"
    transport: onvif
    url: "http://10.0.0.9/onvif/door_control"
    username: "opennvr"
    password: "…"
    door_token: "Door1"
```

- `AccessDoor` is the momentary open; `UnlockDoor` / `LockDoor` are the
  hold and release; `GetDoorState` is the position.
- **Profile C conformance does not guarantee position feedback.**
  `DoorMode` (the *commanded* mode) is mandatory, but
  `DoorPhysicalState` (what the door is actually doing, from a monitor
  contact) is **conditional** — only devices with a door monitor report
  it. This app answers "not reported" in that case rather than passing
  the commanded mode off as the real position.
- Profile C models a **door**, not a barrier. If the installer mapped
  `LockDoor`/`UnlockDoor` to a lock relay rather than the boom, hold
  will not do what you expect — check before relying on it.
- Check conformance per model **and per firmware version** in the
  [ONVIF conformant products database](https://www.onvif.org/conformant-products/)
  rather than trusting a datasheet.

**Axis**: its door controllers conform to Profile C, and Axis also says
plainly that new features land in VAPIX first and that Axis-specific
features are VAPIX-only. Profile C is the right default (portable, no
lock-in); VAPIX is the escape hatch if you need something Profile C
does not carry.

---

## 6. MQTT

```yaml
gates:
  "3":
    transport: mqtt
    host: "10.0.0.20"
    topic: "gate/main/set"
    payload_on: "ON"
    payload_off: "OFF"
    state_topic: "gate/main/state"     # optional, makes it monitored
```

Needs `paho-mqtt` (`pip install paho-mqtt`). Without it the app says so
at startup rather than failing at the first car. Most MQTT relays also
speak HTTP, which needs no extra dependency.

---

## Running in the shipped compose

The stack puts apps on a network that is **`internal: true` by
default** — no route to the LAN or the internet — and everything
outbound goes through an egress proxy that asks core, per destination,
whether this app may reach it (`docs/APP_NETWORK.md`). That proxy is
HTTP `CONNECT`: it can carry HTTP and HTTPS, and it **cannot carry raw
TCP**. What that means per transport:

| Transport | Works out of the box? | What it needs |
|---|---|---|
| **`dry_contact`** | Yes — it uses no network at all | Pass the GPIO device into the container (see below) |
| **`http`** | Yes | Allow the relay's host for this app in the App Catalog (the green chips on the app card) |
| **`onvif`** | Yes | Same — allow the door controller's host |
| **`modbus`** | **No** | Raw TCP. Needs `APPS_EGRESS_ENFORCED=false` in `.env`, then `docker compose down` and up (a network's internal flag cannot change in place) |
| **`mqtt`** | **No** | Same as Modbus |

The app says this in the fault message rather than leaving you with
"Network is unreachable", but it is worth knowing before you choose a
transport. If you are on a compose install and want Modbus or MQTT,
decide up front whether you are comfortable giving apps a normal
bridge — it is a deployment-wide switch, not a per-app one.

**GPIO into the container.** `dry_contact` needs the device node and
the privileges to drive it:

```yaml
# docker-compose.override.yml
services:
  gate-controller:
    devices:
      - /dev/gpiochip0:/dev/gpiochip0
    group_add:
      - "${GPIO_GID:-997}"      # the host's `gpio` group
```

`getent group gpio` gives the gid. For the libgpiod path rather than
sysfs, the image also needs `pip install gpiod` — add it in a
derived image or use sysfs, which needs nothing.

**Running the app outside compose** sidesteps all of this: it is a
single Python process that needs the bus URL and its config, and on a
box that already has the barrier wired to its own GPIO that is often
the simpler deployment.

## If the site already has an access-control panel

**Then you probably should not be driving the barrier at all.**

If there is an HID/Mercury, Axis, 2N, Suprema, ZKTeco or eSSL panel on
site, it already owns the cardholder database, the schedules, the
access levels, anti-passback and — most importantly — the audit trail.
Opening the barrier around it with a relay makes every plate-admitted
vehicle **invisible to the system of record**, which is usually a
compliance failure rather than an inelegance.

The right architecture there is to feed the plate into the panel as a
**credential**, over **OSDP** (preferred: RS-485, and with Secure
Channel it is encrypted and supervised) or **Wiegand** (SIA
AC-01-1996.10; unencrypted, unsupervised, trivially replayed at the
reader head). The panel then decides, opens, logs and schedules — the
same way a Nedap long-range reader or any other credential source is
integrated. That is also what Genetec does with an LPR camera: the
barrier is configured as a *door*, and the plate is a credential.

`gate-controller`'s direct transports are for barriers that have **no
panel in front of them**. Emitting OSDP/Wiegand from OpenNVR is a
different piece of work (a reader emulator, not this app) and is not
built yet — if you need it, say so on the tracker.

---

## Safety — read this

This app **requests** an open. It is an accessory input, the same class
as the push button by the gate. It is **not** a safety device and must
never be treated as one.

**United States — UL 325** (with ASTM F2200 for the gate itself):

- Two independent means of entrapment protection are required in each
  entrapment zone. The operator's inherent force sensing counts as one;
  an independent external device (photo eye or edge) is the second.
- Since 2016 those external devices are **monitored** — they must be
  connected and operational for the operator to run at all. Never
  bypass or jumper them to make an integration work.
- Activation controls must be at least **six feet (1.83 m)** from any
  moving part of the gate; reset controls must be within line of sight.

**Europe — EN 12453:2017+A1:2021** (safety in use), with **EN 12604**
(mechanical, including preventing a boom falling), **EN 12445** (the
force test method), **EN 12978** (safety devices and their monitoring)
and **EN 13241** (the product standard).

**In practice, for this app:**

- Leave the operator's photo-eyes and safety edges wired, monitored and
  tested. If an integration only works with a safety device
  disconnected, the integration is wrong.
- Wire to the *soft* open input where the operator distinguishes them
  (LiftMaster `EXIT`, HySecurity `RADIO OPEN`), never the privileged
  line-of-sight one.
- Never wire into a hold-to-run control.
- Commission with `dry_run: true` first — the page and the log behave
  exactly as they will in production, with nothing connected.
- Use the *Test pulse* button on the Gates page, with the area clear,
  before the first vehicle relies on it.

Holds are capped (`max_hold_minutes`, default 120, hard ceiling 12 h)
so a gate that nobody releases returns to deciding per vehicle instead
of standing open all night.

---

## Contributing hardware

If you wire this to something not listed — especially an Indian,
African or South-American barrier, where our coverage is thinnest —
please open a PR or an issue with the make and model, the terminal
names you used, whether the input was volt-free or sourcing, and
whether you found a position output. A profile is a dozen lines in
`transports.py` and it saves the next person an afternoon with a
multimeter.
