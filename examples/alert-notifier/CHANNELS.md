# Channels

Every place a notification can go, what each one can and cannot do, and
what to set. Read the two sections at the end before you decide this
does not work — "Getting out" and "Buttons that actually work" cover the
two things that surprise people.

## At a glance

| Channel | Account needed | Photo | Updates in place | Silent check | Notes |
|---|---|---|---|---|---|
| **`ntfy`** | **None** | yes | no | only with auth | The recommended default. Self-hostable. |
| `telegram` | Bot token | yes | **yes** | yes | Free, instant, group-able. Forum topics supported. |
| `pushover` | $5 one-off | yes | no | yes | **Emergency priority re-alerts until a human acknowledges.** |
| `discord` | Webhook URL | yes | **yes** | yes | No buttons — webhooks can't carry them. |
| `slack` | Webhook URL | no | no | no | Can't upload files. Can't receive a button press. |
| `teams` | Workflows URL | no | no | no | O365 connectors died 22 May 2026 — see below. |
| `gotify` | Self-hosted | link only | no | yes | Nothing leaves your network. |
| `matrix` | Access token | yes | **yes** | yes | The only channel whose send is idempotent. |
| `email` | SMTP | yes (inline) | no | yes | Works when every service above is down. |
| `webhook` | — | no | no | no | Home Assistant, sirens, SMS gateways, SIEMs, Apprise. |

"Silent check" is the column that matters most and gets read least: it
is whether this app can verify the credentials **without sending
anything to anyone**. Channels that have it get checked on a timer, so a
revoked token surfaces on a Tuesday afternoon rather than at 3am.

The ones that don't can only be verified by sending, which is what
**Send a test** is for — and why the page never shows them as healthy
until a human presses **I got it**. ntfy is in that group when the
topic has no auth, for a reason worth stating plainly: there the topic
name *is* the credential, so there is nothing that can be revoked and
nothing a check could discover. Publishing "checking this still works"
twice a day to prove it would be notification spam from the app whose
whole purpose is not sending notification spam.

---

## ntfy — start here

No account, no token, no signup. Install the ntfy app, subscribe to a
topic, and you're done.

```yaml
channels:
  phone:
    type: ntfy
    server: "https://ntfy.sh"      # or your own
    topic: "opennvr-7f3a91c4de2b18"
```

**The topic name is the password.** On the public server there is no
other access control: anyone who guesses your topic reads every security
alert from your site, including the snapshots. So the Notifications page
generates a long random topic rather than inviting you to type one, and
a short guessable topic like `home` or `cameras` is a privacy hole, not
a convenience. Self-hosting gets you real auth (`token:`, or
`username:`/`password:`).

Severity maps to ntfy's own 1-5 priority automatically: low → 2, medium
→ 3, high → 4, critical → 5 (which is the one that breaks through). You
never set both.

Messages are published as JSON rather than as ntfy's header API, for a
blunt reason: HTTP header values are ASCII, so a camera called "Cámara
Patio" — or any title at all, since the one this app composes contains
an em dash — would fail to encode and take the channel down
permanently. The one place the protocol forces headers on us is the
file upload below, and there every value is RFC 2047-encoded.

A snapshot is uploaded as the message body with the text in a header,
which is ntfy's file-upload form. The public server caps attachments at
15 MB and expires them after about three hours — fine for a
notification, not a place to keep evidence.

With a token or a username, ntfy gains a real silent check (its
`/auth` endpoint) and joins the channels verified on a timer.

Optional: `token`, `username` + `password`.

## Telegram

```yaml
channels:
  guardhouse:
    type: telegram
    bot_token: "123456:ABC-DEF..."   # from @BotFather
    chat_id: "-1001234567890"
    thread_id: ""                    # optional: a forum topic
```

**Don't go hunting for the chat id.** Add the bot to the group, then use
**Link a chat** on the Notifications page — it reads the id for you.

`thread_id` targets a forum topic, which makes "one group, one topic per
camera" a clean routing model without running ten bots.

Two Telegram facts the app handles so you don't have to: a photo
message's caption is capped at 1024 characters against 4096 for plain
text, so the text is shaped to whichever is being sent; and editing a
photo message needs `editMessageCaption` while a text one needs
`editMessageText`, so the app records which kind it sent. Calling the
wrong one is a 400 and a lost update.

The silent check verifies the token **and** the chat, because a bot
removed from a group has a perfectly valid token and cannot deliver a
thing.

## Pushover — the cheapest real escalation

```yaml
channels:
  oncall:
    type: pushover
    token: "..."          # the APPLICATION token
    user_key: "..."
    retry: 60             # seconds between re-alerts (minimum 30)
    expire: 600           # give up after this (maximum 10800 = 3h)
```

A `critical` alert becomes Pushover's **emergency priority**, which
re-alerts on the device every `retry` seconds until somebody
acknowledges it or `expire` runs out. That is "keep waking someone up
until they tap", which no NVR ships, for a one-off $5. Everything below
critical maps to ordinary priorities, and low arrives quietly.

Pushover rejects an emergency message outright if `retry` or `expire` is
missing or out of range, so both are always sent and both are clamped —
a typo produces a working notification, not a silent rejection.

Attachments are capped at 5 MB.

## Discord

```yaml
channels:
  team:
    type: discord
    webhook_url: "https://discord.com/api/webhooks/..."
    thread_id: ""        # optional
```

Rich embeds, colour-coded by severity, with the snapshot uploaded as a
real attachment. Updates edit the message in place.

**No buttons.** Interactive components are restricted for non-application
webhooks, so a button here would render for nobody. The app does not
offer them rather than shipping something that looks like it works.

An edit deliberately does not re-upload the photo: the original
attachment stays on the message, and re-POSTing bytes on every update is
how you get rate-limited.

## Slack

```yaml
channels:
  security:
    type: slack
    webhook_url: "https://hooks.slack.com/services/..."
```

Block Kit formatting, severity emoji, and link buttons.

Three hard limits worth knowing before you make this your only channel.
An incoming webhook **cannot upload a file**, so the snapshot has to be
a URL Slack's own servers can fetch — which on a home NVR usually means
no photo at all. It **cannot receive a button press**: buttons render,
but handling a click needs a real Slack app with an interactivity
request URL. And it **cannot change which channel it posts to** — that
is fixed when you create the webhook.

There is also no way to check a Slack webhook without posting, so this
channel has no silent check.

## Microsoft Teams

**Office 365 connectors were retired on 22 May 2026.** A legacy
connector webhook URL no longer delivers anything. If yours stopped
working, that is why.

The replacement is a **Power Automate Workflows** webhook: in the
channel, *Workflows → "Post to a channel when a webhook request is
received"*, which generates a new URL.

```yaml
channels:
  ops:
    type: teams
    webhook_url: "https://prod-33.westeurope.logic.azure.com/workflows/..."
```

The app posts an Adaptive Card. A workflow created from the template
appends a "created by" footer to every message; copying the workflow and
saving it under your own name removes it. Webhooks can't customise the
bot's name or icon.

## Gotify

```yaml
channels:
  desk:
    type: gotify
    server: "http://gotify.lan:8080"
    token: "A..."         # an APPLICATION token, not a client token
```

Self-hosted, trivially simple, nothing leaves your network. Severity
maps to Gotify's 0-10 priority.

Gotify has no file upload, so a snapshot can only be a URL its client
can fetch — which on a LAN-only install is exactly the case the hosted
channels cannot serve. Set `base_url` for that to work.

## Matrix

```yaml
channels:
  matrix:
    type: matrix
    server: "https://matrix.example.com"
    access_token: "syt_..."
    room_id: "!abcdefgh:example.com"
```

The only channel here whose send is **idempotent**. Matrix requires a
client-chosen transaction id and guarantees the same id never posts
twice, so a retry after a timeout cannot produce a duplicate. The app
feeds it the message's dedup key and gets exactly-once delivery for
free.

The photo is uploaded to the media repository first. If that upload
fails the alert still goes out without it — losing the picture must
never lose the alert.

## Email

```yaml
channels:
  mail:
    type: email
    host: "smtp.example.com"
    port: 587
    encryption: starttls        # starttls | ssl | none
    username: "opennvr@example.com"
    password: "..."
    from: "opennvr@example.com"
    to: ["guard@example.com", "manager@example.com"]
```

The universal fallback, and the only channel that still works when every
service above has had a bad day. The snapshot is attached inline so it
renders in the client rather than arriving as a file nobody opens, and
an alert's updates thread into one conversation.

The silent check connects, negotiates TLS, authenticates and hangs up
without sending mail — the single best early warning of an expired app
password.

## Webhook

```yaml
channels:
  siren:
    type: webhook
    url: "http://192.168.1.50/relay/0?turn=on"
    method: GET                 # POST (default) | PUT | GET
    headers: {}
```

Anything with a URL: Home Assistant, a siren relay behind a Shelly, an
SMS gateway's HTTP API (Twilio, MSG91), a SIEM, n8n, Node-RED.

`POST` and `PUT` send the whole alert plus a ready-made string, so the
far end can use either without parsing ours:

```json
{
  "message": "[HIGH] Person at gate\nloitering 8s · zone Driveway\nFront Door · 02:14:03",
  "title": "Person at gate",
  "severity": "high",
  "camera": "Front Door",
  "when": "02:14:03",
  "url": "https://nvr.example.com/alerts",
  "group_size": 3,
  "dedup_key": "Gates overnight|cam3",
  "alerts": [{"severity": "high", "title": "Person at gate", "camera": "Front Door"}]
}
```

`GET` sends no body at all, for a dumb relay that switches on whatever
request arrives.

### The other 120 services

For anything not in the table — Mattermost, Rocket.Chat, Signal,
Pushsafer, Google Chat, SNS, and about a hundred more — run
[Apprise](https://github.com/caronc/apprise)'s API container and point a
`webhook` channel at it. Apprise normalises to a lowest common
denominator, so you lose Pushover's receipts, ntfy's priority headers
and Telegram's in-place editing; that is exactly why the ten above are
implemented natively and Apprise is the escape hatch rather than the
whole design.

---

## Getting out

**This is the most common reason a channel that is configured correctly
still fails.**

In the shipped compose, apps sit on an internal network with no route to
the internet. The only way out is a proxy that asks OpenNVR core whether
*this app* may reach *this host*. A host nobody has allowed does not
fail with "forbidden" — it fails with a connection error, which reads
exactly like the service being down. (The app spots this and says so in
the error text, but it is worth knowing first.)

The catalog listing already declares the hosts of the public services:

```
ntfy.sh   api.telegram.org   api.pushover.net
discord.com   hooks.slack.com
```

**Teams is not in that list**, because a Workflows URL is on a
per-tenant host (`prod-33.westeurope.logic.azure.com`) that the catalog
cannot know in advance — add yours. Everything else is likewise a host only you know — a self-hosted ntfy or Gotify,
your Matrix homeserver, your SMTP server, your webhook target — so add
it to this app's **allowed hosts** in the App Catalog. Rules are hosts,
not URLs: `ntfy.lan`, `192.168.1.0/24`, optionally with a port.

Two cases the proxy cannot help with at all:

- **SMTP.** The proxy speaks HTTP `CONNECT`; it cannot carry a raw SMTP
  conversation. An SMTP server outside the internal network needs
  `APPS_EGRESS_ENFORCED=false`, or a relay inside it.
- **A device on your LAN** (a siren relay, a local Gotify) is on the
  other side of the same wall, for the same reason.

## Buttons that actually work

A notification with **Acknowledge** and **Mute 1h** on the lock screen
is the best thing in this category, and this app only offers the parts
that genuinely work.

A tappable button needs the *phone* to reach something. This app runs on
an internal Docker network with no inbound route at all, so a button
that called back into it would render, be tapped, and do nothing. Worse
than absent.

So: set `base_url` to however a phone reaches your OpenNVR
(`https://nvr.example.com`) and notifications carry a **View in
OpenNVR** link. Leave it empty — which is the default, because most home
installs are not reachable from the internet — and there is no button at
all, because an empty link is better than a dead one.

Acknowledging and muting **from** the notification is not shipped for
the same reason, with one exception that needs no inbound route at all:
**Pushover emergency priority**, where acknowledging on the device is
handled entirely by Pushover's own infrastructure. That is why it is the
recommended channel for anything you genuinely need someone to wake up
for.

## Severity, once

You set a severity. Each channel's own urgency scheme is derived from
it — ntfy's `X-Priority`, Pushover's `priority` (and its emergency
retry/expire), Gotify's 0-10, Telegram's silent flag, Discord's embed
colour, Teams' card colour. You never set two of them and keep them in
sync.

| Severity | ntfy | Pushover | Gotify | Telegram |
|---|---|---|---|---|
| `info` / `low` | 2 | -1 quiet | 2 | silent |
| `medium` | 3 | 0 | 5 | normal |
| `high` | 4 | 1 high | 8 | normal |
| `critical` | 5 max | **2 emergency** | 9 | normal |

An unrecognised severity ranks as **low**, deliberately: something the
platform cannot classify must not be able to ring a phone at 3am, and it
is in the alerts inbox either way.

## Secrets

Tokens and webhook URLs never appear in the UI, the delivery log, or an
error message. A Discord or Slack webhook URL *is* a credential — anyone
holding it can post as you — so the page shows `discord.com/…/tok…`
rather than the URL, and error bodies are truncated hard, because some
services echo the whole request back and the request contains the token.
