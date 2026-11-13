# Alert Notifier

The guard's phone — and the judgement about what deserves to be on it.

Every OpenNVR app raises its alerts on the bus. Core stores them, rings
open browsers, and can call a phone through its own alarm actions. This
app is the other delivery half: **ten places people actually read**, and
the decision about which alerts are worth interrupting someone for.

It gets a first-class page — **Notifications** — showing whether
delivery still works, what each rule would catch, and, for anything that
did not arrive, *why*.

## Delivering is the easy half

Roughly **94-98% of burglar-alarm activations are false**. In the far
better studied clinical literature, 80-99% of patient-monitor alarms are
false or clinically insignificant — and the useful finding there is that
tuning thresholds cut alarm volume by **over 80% without missing
events**.

So a notifier whose job description is "forward alerts" builds a system
its owner eventually mutes, and a muted system protects nobody. The job
is suppression: deliver the few that matter, once each, with enough
context to judge them in a second.

Five mechanisms do that, and three of them exist in no other NVR:

**Grouping.** Hold briefly, then send **one** notification for
everything that arrived on a camera in the window. "Person + car +
person at the front gate" is one event, not three, and later alerts
update that same message in place rather than stacking nine
notifications. This is Alertmanager's `group_wait`, which the entire NVR
field substitutes a blunt cooldown for — a cooldown either spams (too
short) or swallows the second, different event (too long).

One exception, deliberately: an alert **more serious** than the one
already sent gets a fresh notification rather than an edit. An edit
buzzes nobody, and quietly folding a critical into the message raised
for a medium one is how the alert that mattered reaches a phone that
never rings.

**Inhibition.** While a more serious alert is live on a camera, the
lesser ones behind it are noise — one person walks past and intrusion,
motion, line-crossing and occupancy each raise their own. It is strictly
one-directional: something *more* serious is never suppressed, so this
can only ever make the phone quieter, never lose the one that counted.

**Quiet hours that hold rather than drop.** What arrives overnight is
delivered as a single summary when the window ends. Nothing is lost,
nothing wakes anybody, and critical always breaks through — alarm
fatigue is the danger being designed against, but a fire at 3am is the
reason the system exists.

**Pauses that always expire**, with the countdown on the page. There is
no "mute for ever" in a security product; an invisible coverage gap is
the real dark pattern in this category.

**The site's own arm state.** While OpenNVR is disarmed — the family is
home, the alarm panel says so — nothing below the breakthrough severity
is delivered. That reads the platform's arm state rather than growing a
second schedule that drifts from the alarm panel. An arm state we
*cannot read* is never treated as disarmed: a core that is unreachable
must not be able to silence the alarms.

## Routing you can read top to bottom

A flat, **ordered, first-match** list. Not a nested policy tree: Grafana
shipped one, found that users were "not knowing where those alerts were
going", and added a flat path in 2024; Prometheus ships a separate web
app whose only purpose is simulating which receiver an alert hits. This
is an NVR with a dozen cameras, and a numbered list wins.

```yaml
rules:
  - name: "Barrier faults — any hour"
    match: { alert_types: ["barrier_fault"] }
    to: ["oncall"]
    ignore_quiet_hours: true      # a car is sitting at a gate that did not open

  - name: "Gates overnight"
    match: { cameras: ["gate-*"], from: "22:00", to: "06:00" }
    to: ["phone", "guardhouse"]

  - name: "Everything else"
    to: ["phone"]
```

Match on severity, cameras (globs, so next month's camera is covered),
alert type, zone, producing app, title text, and day/time. The last row
is a **catch-all you cannot delete**, so "what happens to everything
else?" is never a mystery.

Two things make it legible rather than merely expressive. Each rule
shows **how many of your recent alerts it would have caught**, run
through the same matcher the live path uses — a preview that
reimplements the engine eventually disagrees with it, and a lying
preview is worse than none. And a rule that can **never fire** because a
broader one sits above it is flagged, instead of left as a coverage gap
you believe you closed.

## Ten channels

ntfy, Telegram, Pushover, Discord, Slack, Microsoft Teams, Gotify,
Matrix, SMTP email, and any webhook. **None of them adds a dependency** —
every one is HTTP through the `httpx` the app already had, or stdlib
`smtplib`. A notifier that cannot start is a notifier that does not
notify.

**[CHANNELS.md](CHANNELS.md)** has every channel's fields and its honest
limits. Three worth knowing up front:

- **ntfy is the recommended default** — no account, no token, no signup.
  Note that on the public server the topic name *is* the password, which
  is why the app generates a long random one instead of letting you type
  `home`.
- **Pushover is real escalation for $5.** A critical alert re-alerts on
  the device every `retry` seconds until a human acknowledges it. No NVR
  ships this.
- **A Microsoft Teams connector URL no longer works.** Office 365
  connectors were retired on 22 May 2026; use a Power Automate Workflows
  webhook.

You set a severity; each service's own urgency scheme is derived from it
(ntfy's priority, Pushover's emergency mode, Telegram's silent flag,
Gotify's 0-10). You never keep four settings in sync by hand.

## Does it still work?

The failure this app is most carefully built against is the one every
product in the category has: **push quietly stopped working three weeks
ago and nobody knew.**

**Three states, not two.** Healthy, Failing, and *never verified* — the
one everybody forgets. A channel that has never errored because it has
never been used is not known-good, and calling it healthy is the lie
that ends in silence.

**Silent checks.** Where a service allows it, credentials are verified
on a timer **without notifying anyone** — `getMe` plus `getChat` for
Telegram (a bot removed from a group has a perfectly valid token and
cannot deliver a thing), a metadata read for Discord, an SMTP connect
that hangs up before sending. A revoked token surfaces on a Tuesday
afternoon.

**A test sends a real alert.** The most recent one, its snapshot, the
actual template — not "Hello from Alert Notifier". A test that does not
exercise the photo path does not test the part that breaks.

**HTTP 200 is not proof a phone buzzed**, so a channel is not marked
confirmed until a human presses **I got it**.

**And when a channel does break, it is announced somewhere else** — on a
different healthy channel, and as an alert on the bus, which reaches the
inbox and core's own Twilio path that shares no code with any of this.
A notifier reporting its own outage through the broken channel would be
the exact failure it exists to prevent.

## Setup

1. Install from the App Catalog.
2. Add a channel. Start with **ntfy**: the page generates a topic and a
   QR code, you subscribe in the app, and your phone buzzes while you
   are still on the page.
3. Press **I got it** when it arrives.
4. Leave `min_severity` at **high** for a week and look at the
   suppressed count before lowering it.
5. Optionally set `base_url` to however a phone reaches your OpenNVR, to
   make notifications tappable.

Nothing is delivered until a channel is configured — a fresh install
cannot start pushing somewhere nobody chose.

## What it does not do

- **Buttons that act.** Acknowledging or muting *from* the notification
  needs the phone to reach this app, which sits on an internal network
  with no inbound route. A button that did nothing would be worse than
  none. The exception is Pushover's emergency acknowledgement, handled
  entirely by Pushover. CHANNELS.md → "Buttons that actually work".
- **The detection frame, usually.** Most apps upload their JPEG to the
  evidence store and put a *path* in the alert; this app has no route to
  that store, so those notifications travel without a photo and say so.
  Producers that inline a base64 frame (the doorbell) do get one.
- **On-call rotations and escalation chains.** A single Pushover
  emergency tier covers the prosumer case; a rota is a different
  product.
- **Per-user preferences.** Routing is per-rule, not per-person.

## Upgrading from 1.0

**1.0 delivered nothing.** Not rarely — zero, on every install. It
subscribed to `opennvr.events.alert.fired.v1.>` because
`docs/EVENT_CONTRACTS.md` said the SDK dual-publishes there. It does
not, and nothing in the platform ever has: `AlertDispatcher.fire` goes
to `NatsAlertChannel.send`, which publishes
`opennvr.alerts.{kind}.{name}.{camera}` and nothing else. So the app
installed cleanly, booted, reported healthy, showed `forwarded: 0`, and
an operator concluded it had been a quiet month. 2.0 subscribes to the
tree that actually carries alerts, and the doc has been corrected.

Your 1.0 config still loads: `telegram_bot_token` / `telegram_chat_id` /
`notify_webhook_url` are read as channels, and `min_severity` is
unchanged. The flood controls it had — a per-alarm cooldown and a
per-minute ceiling — are replaced by grouping and inhibition, which are
strictly better at the same job, and by pauses that now survive a
restart instead of living only in memory.

## Standalone

```bash
cp config.example.yml config.yml     # nats_url, channels
python alert_notifier.py --config config.yml
pytest
```

`config.example.yml` documents every key.
