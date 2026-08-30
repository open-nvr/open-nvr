# alert-notifier

The guard's phone. Consumes OpenNVR's contracted `alert.fired.v1`
stream and pushes the alerts that matter to people: **Telegram** (free,
instant, group-able — the guard house gets a phone that beeps) and/or
any **generic webhook** — Slack/Teams incoming hooks, SMS gateway HTTP
APIs (Twilio, MSG91), a siren relay, a SIEM.

Flood-safe by design: a severity bar (default `high`), a per-alarm
repeat cooldown (one alarm, one push — not fifty for a flapping
camera), and a global per-minute ceiling. Delivery failures are
counted, visible in the catalog, and never start the cooldown — the
next firing retries.

Setup: create a bot with @BotFather, add it to the guard-house group,
paste the token and chat id into the app's Configure form (applied
live). Run: `python alert_notifier.py --config config.yml`, or install
from the App Catalog.
