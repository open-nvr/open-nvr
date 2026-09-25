# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""alert-notifier — the guard's phone.

An alarm nobody hears isn't one. Every OpenNVR app raises its alerts on
the bus; core stores them, rings open browsers, and — through its own
alarm actions — can call a phone. This app is the other delivery half:
the ten places people actually read, and the judgement about which
alerts are worth interrupting someone for.

The judgement is the product. Roughly 94-98% of burglar-alarm
activations are false, and the (far better studied) clinical literature
puts monitor-alarm false rates at 80-99% while showing that tuning cuts
volume by over 80% without missing events. A notifier that faithfully
forwards everything builds a system its owner mutes. So:

* a flat, ordered, first-match rule list with a pinned catch-all, rather
  than a nested policy tree nobody can trace;
* grouping — hold briefly and collapse a burst into ONE message that
  updates in place, instead of the cooldown timer the whole NVR field
  uses as a substitute;
* inhibition — while a person alert is live on a camera, the motion
  alert behind it is noise;
* quiet hours that HOLD rather than drop, with a severity that always
  breaks through;
* mutes that always expire, with the countdown on the page;
* the site's own arm state, so a disarmed house does not buzz.

And the thing every product in this category gets wrong: it checks
whether delivery still works. A revoked bot token is found on a Tuesday
afternoon by a silent probe, not at 3am by silence.

Run:
    python alert_notifier.py --config config.yml
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

import channels as ch
import routing
from opennvr_app_sdk import (
    Action,
    AlertSubscriber,
    AlertType,
    AppManifest,
    Entity,
    Param,
    StateView,
    alert_app,
)

logger = logging.getLogger("alert-notifier")

#: The subject alerts ACTUALLY travel on.
#:
#: 1.0 subscribed to ``opennvr.events.alert.fired.v1.>`` because
#: EVENT_CONTRACTS.md said the SDK dual-publishes there. It does not —
#: ``AlertDispatcher.fire`` goes to ``NatsAlertChannel.send``, which
#: publishes ``alert_subject()`` and nothing else. No code in the
#: platform has ever published on the domain tree, so 1.0 installed
#: cleanly, reported healthy, and delivered zero notifications for ever.
#: The doc has been corrected; this is the tree that carries alerts.
ALERT_SUBJECT_PATTERN = "opennvr.alerts.>"

#: Consecutive failures before a wobble is called an outage.
FAILING_ALERT_AFTER = 2

#: Retries per delivery, with backoff. Auth failures are never retried —
#: a revoked token does not come back.
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (2.0, 8.0)

#: Bound on the delivery queue. Past this the site is in an event storm
#: and the oldest held message is the right thing to lose.
QUEUE_DEPTH = 500

#: Alerts kept for the backtest ("this rule would have caught N").
HISTORY_DEPTH = 2000
#: How far back a backtest reaches into core's inbox. Enough to cover a
#: month on a busy site; a rule change is judged on a season, not on an
#: afternoon.
BACKTEST_LIMIT = 2000

#: Delivery log depth — what the operator pastes into a bug report.
LOG_DEPTH = 200

#: How long shutdown waits for queued notifications to go out.
SHUTDOWN_DRAIN_SECONDS = 10.0

#: Alerts a quiet-hours window may hold before the oldest are dropped.
#: Generous, because the contract of "hold" is that nothing is lost —
#: but not unbounded, because an overnight storm is real.
MAX_HELD = 500

SEVERITIES = ("info", "low", "medium", "high", "critical")


# ── Manifest ────────────────────────────────────────────────────────


MANIFEST = AppManifest(
    id="alert-notifier",
    name="Alert Notifier",
    version="2.0.0",
    category="notifications",
    # No camera picker: this app acts on other apps' alerts, which those
    # apps already limited to the cameras they were given.
    camera_picker=False,
    provides=["notifications"],
    has_ui=True,
    summary=(
        "Alerts on the phones people actually read: ntfy, Telegram, "
        "Pushover, Discord, Slack, Teams, Gotify, Matrix, email or any "
        "webhook. Ordered routing rules, burst grouping, quiet hours, "
        "and a check that delivery still works."),
    requires_tasks=[],
    requires_scopes=["events:alert.fired"],
    subscribes=ALERT_SUBJECT_PATTERN,
    params=[
        Param("channels", dict, default={}, label="Channels",
              group="Delivery",
              description=(
                  "Where notifications go: {name: {type, …}}. Types: ntfy "
                  "(no account — the recommended default), telegram, "
                  "pushover (emergency priority re-alerts until someone "
                  "acknowledges), discord, slack, teams, gotify, matrix, "
                  "email, webhook. See CHANNELS.md for each one's fields.")),
        Param("rules", list, default=[], label="Routing rules",
              group="Delivery",
              description=(
                  "An ordered list, first match wins: [{name, match: "
                  "{min_severity, cameras, alert_types, zones, sources, "
                  "days, from, to}, to: [channel, …]}]. The last row is a "
                  "catch-all you cannot delete, so 'everything else' is "
                  "always visible. Leave empty and every alert at or above "
                  "min_severity goes to every channel.")),
        Param("min_severity", str, default="high", label="Notify at or above",
              group="Delivery", choices=list(SEVERITIES),
              description=(
                  "The floor, applied before the rules. Default high — "
                  "unknown vehicles, watchlist hits, barrier faults. The "
                  "info and low chatter stays in the alerts inbox, where it "
                  "is still searchable.")),
        Param("group_wait_seconds", float, default=20.0,
              label="Collapse bursts for", group="Noise control",
              description=(
                  "Hold this long, then send ONE notification for "
                  "everything that arrived on a camera in the window — "
                  "'person + car + person at the front gate' is one event, "
                  "not three. Later alerts update that message in place "
                  "where the channel allows it. 0 sends immediately.")),
        Param("inhibit_seconds", float, default=60.0,
              label="Hush lesser alerts for", group="Noise control",
              description=(
                  "While a more serious alert is live on a camera, suppress "
                  "the lesser ones behind it — the motion and line-crossing "
                  "alerts about the same person. Never suppresses something "
                  "MORE serious. 0 is off.")),
        Param("quiet_hours", dict, default={}, label="Quiet hours",
              group="Noise control",
              description=(
                  "{enabled, windows: [{days, from, to}], mode, "
                  "breakthrough}. mode: hold (default — delivered as a "
                  "summary when the window ends, never lost), silent, or "
                  "drop. breakthrough defaults to critical, which always "
                  "gets through.")),
        Param("respect_site_mode", bool, default=True,
              label="Follow the site's arm state", group="Noise control",
              description=(
                  "When the site is disarmed — the family is home, the "
                  "alarm panel says so — deliver nothing below the "
                  "breakthrough severity. Alerts are still recorded. This "
                  "is the platform's own arm state, not a second schedule "
                  "to keep in sync.")),
        Param("timezone", str, default="UTC", label="Time zone",
              group="Noise control",
              description="Zone for quiet hours and time-scoped rules, "
                          "e.g. Europe/London or Asia/Kolkata."),
        Param("base_url", str, default="", label="OpenNVR address",
              group="Delivery", advanced=True,
              description=(
                  "How a phone reaches this OpenNVR, e.g. "
                  "https://nvr.example.com. Turns the notification into "
                  "something tappable. Leave empty if OpenNVR is not "
                  "reachable from a phone — an empty link is better than a "
                  "dead one.")),
        Param("probe_hours", float, default=12.0, label="Check channels every",
              group="Delivery", advanced=True,
              description=(
                  "How often to silently verify each channel's credentials "
                  "without notifying anyone. This is what catches a revoked "
                  "token before the night it matters. 0 is off.")),
        Param("dry_run", bool, default=False, label="Dry run",
              group="Delivery", advanced=True,
              description=(
                  "Decide and record everything, send nothing — for "
                  "tuning the rules against live alerts without a phone "
                  "buzzing.")),
    ],
    state_schema=[
        StateView(name="pushed", label="Delivered", kind="metric",
                  path="delivered_total"),
        StateView(name="suppressed", label="Suppressed", kind="metric",
                  path="suppressed_total",
                  description="Below the bar, grouped, inhibited, muted, "
                              "quiet hours, or disarmed."),
        StateView(name="failures", label="Delivery failures", kind="metric",
                  path="failure_total"),
        StateView(name="channels", label="Channels", kind="table",
                  path="channels"),
        StateView(name="recent", label="Recent notifications", kind="log",
                  path="recent", limit=20),
    ],
    emits=[
        AlertType("notification_channel_failing", severity="high",
                  description=(
                      "A notification channel stopped accepting deliveries. "
                      "Raised onto the bus so it reaches the inbox and "
                      "core's own alarm actions — the one failure this app "
                      "cannot report through itself.")),
        AlertType("notification_channel_recovered", severity="low",
                  description="A channel that was failing is delivering again."),
    ],
    actions=[
        Action("test", "Send a test",
               params=[Param("channel", str, default="", label="Channel",
                             description="Leave blank to test every channel.")],
               description=(
                   "Send a real notification — the most recent alert, its "
                   "snapshot, the actual template — to one channel, or to "
                   "all of them when left blank. A test that does not "
                   "exercise the photo path does not test the thing that "
                   "breaks.")),
        Action("confirm", "I got it",
               params=[Param("channel", str, required=True, label="Channel")],
               description=(
                   "Mark a channel verified because a human saw the "
                   "message arrive. HTTP 200 is not proof a phone buzzed.")),
        Action("check", "Check channels now",
               description=(
                   "Verify every channel's credentials without sending "
                   "anything to anyone.")),
        Action("mute", "Pause alerts",
               params=[Param("minutes", float, default=60.0, label="Minutes"),
                       Param("camera", str, default="", label="Camera",
                             description="Leave blank to pause every camera.")],
               confirm=True,
               description=(
                   "Stop delivering for a while — everything, or one "
                   "camera. Always expires; the page shows the countdown.")),
        Action("unmute", "Resume alerts",
               params=[Param("camera", str, default="", label="Camera")],
               description="End a pause early."),
        Action("backtest", "Preview rule matches",
               description=(
                   "How many recent alerts each rule would catch, run "
                   "through the same matcher the live path uses.")),
    ],
    entities=[
        Entity("delivered_today", "sensor", "Notifications sent",
               state_path="today.delivered", state_class="total_increasing",
               icon="mdi:bell-ring"),
        Entity("suppressed_today", "sensor", "Notifications suppressed",
               state_path="today.suppressed", state_class="total_increasing",
               icon="mdi:bell-sleep"),
        Entity("channels_failing", "sensor", "Channels failing",
               state_path="health.failing", icon="mdi:bell-alert"),
        Entity("notifications_healthy", "binary_sensor",
               "Notification delivery", state_path="health.problem",
               device_class="problem"),
        Entity("quiet_hours", "binary_sensor", "Quiet hours",
               state_path="quiet.active", icon="mdi:sleep"),
        Entity("muted", "binary_sensor", "Alerts paused",
               state_path="mute.active", icon="mdi:bell-off"),
        Entity("pause", "button", "Pause alerts", action="mute",
               icon="mdi:bell-off"),
        Entity("resume", "button", "Resume alerts", action="unmute",
               icon="mdi:bell"),
        Entity("send_test", "button", "Send a test notification",
               action="test", icon="mdi:bell-check"),
    ],
    description=(
        "The delivery half of alerting, and the judgement about what "
        "deserves to interrupt someone.\n\n"
        "Ten channels, no new dependencies: ntfy (no account at all — the "
        "recommended default), Telegram, Pushover (whose emergency "
        "priority re-alerts until a human acknowledges, which is real "
        "escalation for the price of a coffee), Discord, Slack, Microsoft "
        "Teams via Power Automate Workflows, Gotify, Matrix, SMTP email, "
        "and any webhook — Home Assistant, a siren relay, an SMS "
        "gateway, a SIEM, an Apprise instance for the other hundred.\n\n"
        "Routing is a numbered list you read top to bottom: when THIS, "
        "send there; first match wins; the last row is a catch-all you "
        "cannot delete, so 'everything else' is never a mystery. Each "
        "rule shows how many of your recent alerts it would have caught, "
        "and a rule that can never fire because a broader one sits above "
        "it is flagged rather than left as a silent gap.\n\n"
        "Noise control is the point. A burst on one camera is held "
        "briefly and collapses into one notification that updates in "
        "place. A lesser alert behind a more serious one on the same "
        "camera is hushed. Quiet hours hold rather than drop, and "
        "critical always breaks through. Pauses always expire, with the "
        "countdown on the page. And the site's own arm state is honoured, "
        "instead of this app growing a second schedule that drifts from "
        "the alarm panel.\n\n"
        "Channels are checked on a timer without notifying anyone, so a "
        "revoked token surfaces on a Tuesday afternoon. When one does "
        "break, the app says so through a DIFFERENT healthy channel and "
        "raises an alert on the bus — because a notifier reporting its "
        "own outage through the broken channel is the failure it exists "
        "to prevent."),
    author="OpenNVR",
    website="https://github.com/open-nvr/open-nvr",
    license="AGPL-3.0",
    contact="https://github.com/open-nvr/open-nvr/discussions",
    use_cases=[
        "Watchlist and unknown-vehicle hits on the guard's phone, one "
        "notification per car rather than one per detector",
        "A barrier fault straight to the maintenance group, at any hour",
        "Overnight perimeter alerts held until 07:00, except critical",
        "Quiet while the family is home, loud the moment the site arms",
        "High-severity events into Slack, Teams or a SIEM",
    ],
)


# ── Config ──────────────────────────────────────────────────────────


@dataclass
class AppConfig:
    nats_url: str
    nats_token: str | None = None
    subject_pattern: str = ALERT_SUBJECT_PATTERN

    channels: dict[str, Any] = field(default_factory=dict)
    rules: list[Any] = field(default_factory=list)
    min_severity: str = "high"
    group_wait_seconds: float = 20.0
    inhibit_seconds: float = 60.0
    quiet_hours: dict[str, Any] = field(default_factory=dict)
    respect_site_mode: bool = True
    timezone: str = "UTC"
    base_url: str = ""
    probe_hours: float = 12.0
    dry_run: bool = False

    # App contract (spec §03).
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None


def migrate_1_0(raw: dict[str, Any]) -> dict[str, Any]:
    """1.0's three flat keys → a channel each.

    1.0 had ``telegram_bot_token`` / ``telegram_chat_id`` /
    ``notify_webhook_url``. An operator who upgrades must not have to
    rewrite their config to keep the alerting they meant to have, so the
    old shape is read and translated. (What they actually had was
    nothing — see ALERT_SUBJECT_PATTERN — but the INTENT is in that file
    and it is honoured.) An explicit ``channels:`` entry of the same
    name always wins.
    """
    channels = dict(raw.get("channels") or {})
    token = str(raw.get("telegram_bot_token") or "").strip()
    chat = str(raw.get("telegram_chat_id") or "").strip()
    if token and chat and "telegram" not in channels:
        channels["telegram"] = {"type": "telegram", "bot_token": token,
                                "chat_id": chat}
    hook = str(raw.get("notify_webhook_url") or "").strip()
    if hook and "webhook" not in channels:
        channels["webhook"] = {"type": "webhook", "url": hook}
    return channels


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"config file {path} did not parse to a dict")
    nats_url = raw.get("nats_url")
    if not nats_url:
        raise ValueError(
            "config: nats_url is required — this app consumes the "
            "platform's alerts from the bus")
    return AppConfig(
        nats_url=str(nats_url),
        nats_token=raw.get("nats_token") or None,
        subject_pattern=str(raw.get("subject_pattern")
                            or ALERT_SUBJECT_PATTERN),
        channels=migrate_1_0(raw),
        rules=list(raw.get("rules") or []),
        min_severity=str(raw.get("min_severity") or "high"),
        group_wait_seconds=float(raw.get("group_wait_seconds", 20.0)),
        inhibit_seconds=float(raw.get("inhibit_seconds", 60.0)),
        quiet_hours=dict(raw.get("quiet_hours") or {}),
        respect_site_mode=bool(raw.get("respect_site_mode", True)),
        timezone=str(raw.get("timezone") or "UTC"),
        base_url=str(raw.get("base_url") or "").strip().rstrip("/"),
        probe_hours=float(raw.get("probe_hours", 12.0)),
        dry_run=bool(raw.get("dry_run", False)),
        contract_port=(int(raw["contract_port"])
                       if raw.get("contract_port") is not None else None),
        contract_bind_host=raw.get("contract_bind_host"),
        contract_host=raw.get("contract_host"),
        opennvr_url=raw.get("opennvr_url"),
        opennvr_token=raw.get("opennvr_token"),
    )


# ── Channel health ──────────────────────────────────────────────────


@dataclass
class Health:
    """What we know about one channel's ability to deliver.

    Three states, not two. "Unverified" — never delivered, never checked
    — is the one every product forgets: a channel that has never errored
    because it has never been used is not known-good, and calling it
    healthy is the lie that ends in silence at 3am.
    """

    state: str = "unverified"
    last_ok: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0
    consecutive_failures: int = 0
    delivered: int = 0
    failed: int = 0
    confirmed: bool = False
    last_probe: float = 0.0
    build_error: str = ""

    def ok(self, now: float) -> None:
        self.state = "healthy"
        self.last_ok = now
        self.consecutive_failures = 0
        self.last_error = ""

    def bad(self, message: str, now: float) -> None:
        self.state = "failing"
        self.last_error = str(message)[:400]
        self.last_error_at = now
        self.consecutive_failures += 1

    def summary(self, now: float) -> str:
        if self.build_error:
            return f"misconfigured — {self.build_error}"
        if self.state == "failing":
            return f"failing — {self.last_error}"
        if self.state == "unverified":
            return "never verified — send a test"
        if not self.confirmed:
            return f"accepted {_ago(now, self.last_ok)}, unconfirmed"
        return f"healthy — last delivered {_ago(now, self.last_ok)}"


def _ago(now: float, then: float) -> str:
    if not then:
        return "never"
    delta = max(0.0, now - then)
    if delta < 90:
        return f"{int(delta)}s ago"
    if delta < 5400:
        return f"{int(delta // 60)}m ago"
    if delta < 172800:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


# ── Delivery work ───────────────────────────────────────────────────


@dataclass
class Job:
    """One message to one set of channels, queued for the worker."""

    message: ch.Message
    channels: list[str]
    group_key: str
    is_update: bool = False
    #: A test bypasses dry run and the whole suppression stack: the
    #: operator asked for it and is standing there waiting.
    test: bool = False


# ── The app ─────────────────────────────────────────────────────────


class AlertNotifier(AlertSubscriber):
    """Alerts on the bus → the right people, once, with a photo."""

    manifest = MANIFEST

    # -- lifecycle ----------------------------------------------------

    def setup(self) -> None:
        self._lock = threading.RLock()
        # Before anything that formats a time: _apply sets the real
        # value, but the counters are initialised first and they roll
        # on the SITE's midnight.
        self._tz = str(getattr(self.cfg, "timezone", "") or "UTC")
        self._nvr: Any = None
        self._nvr_tried = False
        self._camera_names: dict[str, str] = {}
        self._camera_names_at = 0.0
        self._alerts: Any = None

        self._delivered = 0
        self._suppressed = 0
        self._failures = 0
        self._queued_dropped = 0
        self._today = {"day": self._today_key(), "delivered": 0,
                       "suppressed": 0}
        self._recent: deque[dict[str, Any]] = deque(maxlen=LOG_DEPTH)
        self._history: deque[routing.Incident] = deque(maxlen=HISTORY_DEPTH)
        self._held: list[routing.Incident] = []
        self._held_dropped = 0
        self._site_mode = ""
        self._site_mode_at = 0.0

        self._channels: dict[str, ch.Channel] = {}
        self._health: dict[str, Health] = {}
        self._muting = routing.Muting()
        self._queue: queue.Queue[Job | None] = queue.Queue(maxsize=QUEUE_DEPTH)
        self._worker: threading.Thread | None = None
        self._stopping = threading.Event()

        self._apply(vars(self.cfg), initial=True)
        self._restore_state()
        self._start_worker()

    def _start_worker(self) -> None:
        """Delivery runs on its own thread.

        Ten channels × an 8 s timeout is 80 s. Doing that on the asyncio
        loop would stall the NATS subscription behind one unreachable
        webhook, so the loop only ever decides; the thread sends.
        """
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._drain, name="alert-delivery", daemon=True)
        self._worker.start()

    # -- config -------------------------------------------------------

    def _apply(self, config: dict[str, Any], *, initial: bool = False) -> None:
        """Build channels, rules and the noise controls from config.

        Everything is rebuilt together because the pieces reference each
        other — a rule names a channel — and a half-applied config is
        how a rule ends up routing to a channel that no longer exists.
        Live state (health, mutes, groups, counters) is carried across.
        """
        cfg = self.cfg
        for key in ("min_severity", "timezone", "base_url", "dry_run",
                    "respect_site_mode", "group_wait_seconds",
                    "inhibit_seconds", "probe_hours"):
            if key in config:
                setattr(cfg, key, config[key])
        if "channels" in config:
            cfg.channels = dict(config["channels"] or {})
        if "rules" in config:
            cfg.rules = list(config["rules"] or [])
        if "quiet_hours" in config:
            cfg.quiet_hours = dict(config["quiet_hours"] or {})
        if not initial and ("telegram_bot_token" in config
                            or "notify_webhook_url" in config):
            cfg.channels = migrate_1_0({**config, "channels": cfg.channels})

        self._min_rank = ch.severity_rank(cfg.min_severity)
        self._tz = str(cfg.timezone or "UTC")
        self._base_url = str(cfg.base_url or "").strip().rstrip("/")

        built: dict[str, ch.Channel] = {}
        health = dict(self._health)
        configured: set[str] = set()
        for raw_name, spec in (cfg.channels or {}).items():
            name = str(raw_name)
            configured.add(name)
            entry = health.setdefault(name, Health())
            if isinstance(spec, dict) and spec.get("enabled") is False:
                entry.build_error = "disabled in config"
                continue
            try:
                built[name] = ch.build_channel(name, spec)
                entry.build_error = ""
            except ValueError as exc:
                # A misconfigured channel must not stop the others, and
                # must be VISIBLE rather than quietly absent.
                logger.error("channel %s is misconfigured: %s", name, exc)
                entry.build_error = str(exc)
                entry.state = "failing"
        for name, channel in self._channels.items():
            if built.get(name) is not channel:
                try:
                    channel.close()
                except Exception:  # noqa: BLE001
                    pass
        self._channels = built
        # Only channels the CONFIG still names — which includes the ones
        # that failed to build, because a misconfigured channel must stay
        # visible rather than silently vanish. Keeping the health of a
        # REMOVED or renamed channel is what had to stop: it left a
        # permanently failing row on the page and held Home Assistant's
        # problem sensor on for a channel nobody could fix because it no
        # longer existed.
        self._health = {n: health.get(n, Health()) for n in configured}
        for name, entry in self._health.items():
            if not entry.build_error and entry.state == "failing" \
                    and not entry.last_error:
                # A build error that has since been fixed: the state
                # was set by the failed build, so it would otherwise
                # read "failing — " with no cause, for ever.
                entry.state = "unverified"

        try:
            self._rules = routing.parse_rules(cfg.rules, built)
        except ValueError as exc:
            logger.error("rules rejected (%s) — falling back to "
                         "'everything at or above the bar, everywhere'", exc)
            self._rules = self._default_rules(built)
        if not cfg.rules and built:
            # No rules at all: an operator who configured channels and
            # nothing else means "send them there". Keyed on the CONFIG
            # being empty rather than on the rule list having one entry,
            # because a single rule the operator marked catch_all also
            # collapses to one entry — and replacing that with
            # "everything, everywhere" would quietly widen their routing.
            self._rules = self._default_rules(built)

        try:
            self._quiet = routing.parse_quiet_hours(cfg.quiet_hours)
        except ValueError as exc:
            logger.error("quiet_hours rejected (%s) — running without", exc)
            self._quiet = routing.QuietHours()

        self._inhibitor = routing.Inhibitor(_nonneg(cfg.inhibit_seconds, 60.0))
        grouper = routing.Grouper(_nonneg(cfg.group_wait_seconds, 20.0))
        if not initial:
            # Carry the in-flight work across. A rebuilt Grouper starts
            # empty, and an alert sitting inside a collapse window when
            # the operator saves ANY config change would simply cease
            # to exist: never delivered, never suppressed, never logged.
            grouper.adopt(self._grouper)
        self._grouper = grouper

    @staticmethod
    def _default_rules(built: dict[str, ch.Channel]) -> routing.RuleSet:
        """Everything that clears the bar, to every channel.

        The shape a first install wants, and it is a REAL rule in the
        list rather than hidden behaviour — the page shows it, and
        editing it is how an operator learns the model."""
        if not built:
            return routing.RuleSet([])
        return routing.RuleSet([
            routing.Rule(name="All alerts", channels=sorted(built),
                         matcher=routing.Matcher())])

    def on_config_update(self, config: dict[str, Any]) -> None:
        with self._lock:
            self._apply(config)

    # -- the sink -----------------------------------------------------

    def on_alert(self, alert: dict[str, Any], subject: str) -> None:
        """One alert off the bus. Decides; never sends."""
        with self._lock:
            inc = routing.parse_incident(
                alert, camera_names=self._camera_names)
            self._remember(inc)
            self._roll_day()
            job = self._decide(inc)
        if job is not None:
            self._enqueue(job)

    def _remember(self, inc: routing.Incident) -> None:
        """Keep the alert for the backtest — WITHOUT its photo.

        The backtest reads severity, camera, type, source, zones and
        time. Holding 2000 incidents that each may carry a 2 MB inline
        JPEG would retain gigabytes to answer a question about
        metadata."""
        import dataclasses

        self._history.append(dataclasses.replace(inc, image=None))

    def _decide(self, inc: routing.Incident) -> Job | None:
        """The suppression stack, in order. Called with the lock held."""
        local = routing.local_now(self._tz, inc.at)

        if inc.rank < self._min_rank:
            return self._suppress(inc, "below the severity bar")

        disarmed = self._site_suppresses(inc)
        if disarmed:
            return self._suppress(inc, disarmed)

        muted = self._muting.muted(inc, inc.at)
        if muted:
            return self._suppress(inc, f"{muted} is paused")

        rule = self._rules.match(inc, local=local)
        if rule is None:
            return self._suppress(inc, "no rule matched")
        if not rule.channels:
            return self._suppress(inc, f"rule {rule.name!r} routes nowhere")

        if not rule.ignore_quiet_hours:
            verdict = self._quiet.verdict(inc, local)
            if verdict == "drop":
                return self._suppress(inc, "quiet hours (dropped)")
            if verdict == "hold":
                if len(self._held) >= MAX_HELD:
                    # An overnight storm must not grow without bound.
                    # The OLDEST goes, and the operator is told in the
                    # morning summary how many were dropped.
                    self._held.pop(0)
                    self._held_dropped += 1
                self._held.append(inc)
                return self._suppress(
                    inc, f"quiet hours — held for the "
                         f"{self._quiet.ends_at(local) or 'morning'} summary")
            # "silent" still delivers; each channel lowers its own
            # urgency from the message's severity.

        inhibited = self._inhibitor.inhibits(inc)
        if inhibited:
            return self._suppress(inc, f"inhibited — {inhibited}")
        self._inhibitor.observe(inc)

        live = [c for c in rule.channels if c in self._channels]
        if not live:
            return self._suppress(
                inc, f"rule {rule.name!r} names no channel that loaded")

        group, is_update = self._grouper.add(inc, rule, live)
        if group is None:
            # Either held for the collapse window (tick will bring it
            # back) or already represented in a message we sent.
            return None
        return self._job_for(group, is_update)

    def _job_for(self, group: routing.Group, is_update: bool) -> Job:
        message = routing.render(
            group, base_url=self._base_url, tz_name=self._tz,
            actions_enabled=bool(self._base_url))
        return Job(message=message, channels=list(group.channels),
                   group_key=group.key, is_update=is_update)

    def _suppress(self, inc: routing.Incident, why: str) -> None:
        self._suppressed += 1
        self._today["suppressed"] += 1
        # The REASON is kept, not just the count: "why didn't I get that
        # alert" is the question this product exists to answer.
        self._note(f"[{inc.severity}] {inc.title}", inc.severity,
                   detail=why, kind="suppressed")
        return None

    def _site_suppresses(self, inc: routing.Incident) -> str:
        if not self.cfg.respect_site_mode:
            return ""
        if self._site_mode != "disarmed":
            return ""
        floor = self._quiet.breakthrough or "critical"
        if inc.rank >= ch.severity_rank(floor):
            return ""
        return "the site is disarmed"

    # -- delivery -----------------------------------------------------

    def _enqueue(self, job: Job) -> None:
        try:
            self._queue.put_nowait(job)
            return
        except queue.Full:
            pass
        # Full means an event storm or a wedged channel. Drop the OLDEST
        # rather than the newest: in a storm the most recent alert is
        # the one somebody needs. Retried, because this is a
        # read-modify-write against a live consumer — if the worker
        # empties the queue in between, the naive version drops the
        # NEWEST instead, which is the exact opposite of the intent.
        for _ in range(5):
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self._queued_dropped += 1
            except (queue.Empty, ValueError):
                pass
            try:
                self._queue.put_nowait(job)
                return
            except queue.Full:
                continue
        self._queued_dropped += 1
        logger.warning("delivery queue is wedged — dropped a notification")
        with self._lock:
            self._note("DROPPED a notification — the delivery queue is full",
                       "high", kind="failed",
                       detail="a channel is not responding, or the site is "
                              "in an event storm")

    def _drain(self) -> None:
        while not self._stopping.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                self._queue.task_done()
                break
            try:
                self._deliver(job)
            except Exception:  # noqa: BLE001
                logger.exception("delivery worker: unhandled error")
            finally:
                self._queue.task_done()

    def _deliver(self, job: Job) -> None:
        """Send one job to its channels. Runs on the worker thread."""
        if self.cfg.dry_run and not job.test:
            self._record(job, {name: "dry run" for name in job.channels},
                         ok=True)
            return
        results: dict[str, str] = {}
        for name in job.channels:
            with self._lock:
                channel = self._channels.get(name)
            if channel is None:
                results[name] = "no such channel"
                continue
            results[name] = self._deliver_one(channel, job)
        # ALL, not any: a rule routing to the pager and Slack where the
        # pager 401s is not a delivered notification, and a green line
        # on the page is how that goes unnoticed.
        self._record(job, results,
                     ok=bool(results) and all(
                         v in ("sent", "updated", "dry run")
                         for v in results.values()))

    def _deliver_one(self, channel: ch.Channel, job: Job) -> str:
        with self._lock:
            health = self._health.setdefault(channel.name, Health())
        handle = self._handle_for(job.group_key, channel.name)
        is_update = job.is_update
        # Buttons only where they render. A Discord webhook cannot carry
        # components at all, so sending them is payload the service
        # silently drops.
        message = (job.message if channel.can_act
                   else job.message.without_actions())
        last = ""
        for attempt in range(MAX_ATTEMPTS):
            try:
                if is_update and handle and channel.can_edit:
                    channel.edit(message, handle)
                    self._on_ok(channel.name, health)
                    return "updated"
                new_handle = channel.send(message)
                if new_handle:
                    self._remember_handle(job.group_key, channel.name,
                                          new_handle)
                self._on_ok(channel.name, health)
                return "sent"
            except ch.AuthFailed as exc:
                # Never retried: a revoked token does not heal, and
                # hammering it is how an account gets locked.
                self._on_fail(channel.name, health, str(exc), permanent=True)
                return f"auth: {exc}"
            except ch.NotSupported as exc:
                # An edit this channel cannot do becomes a fresh send
                # rather than a lost update.
                if is_update:
                    is_update = False
                    handle = ""
                    continue
                last = str(exc)
                break
            except ch.Throttled as exc:
                last = str(exc)
                if attempt + 1 >= MAX_ATTEMPTS:
                    break
                wait = exc.retry_after or RETRY_BACKOFF_SECONDS[
                    min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                if self._stopping.wait(min(wait, 30.0)):
                    break
            except ch.DeliveryError as exc:
                last = str(exc)
                if attempt + 1 >= MAX_ATTEMPTS:
                    break
                if self._stopping.wait(RETRY_BACKOFF_SECONDS[
                        min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]):
                    break
            except Exception as exc:  # noqa: BLE001 — a channel bug
                logger.exception("channel %s raised", channel.name)
                last = f"internal error: {exc}"
                break
        self._on_fail(channel.name, health, last or "delivery failed")
        return f"failed: {last}"

    def _handle_for(self, group_key: str, channel: str) -> str:
        with self._lock:
            group = self._grouper.sent_group(group_key)
            return (group.handles.get(channel) or "") if group else ""

    def _remember_handle(self, group_key: str, channel: str,
                         handle: str) -> None:
        with self._lock:
            group = self._grouper.sent_group(group_key)
            if group is not None:
                group.handles[channel] = handle

    def _on_ok(self, name: str, health: Health) -> None:
        now = time.time()
        with self._lock:
            was = health.state
            health.ok(now)
            health.delivered += 1
            self._delivered += 1
            self._today["delivered"] += 1
        if was == "failing":
            self._raise_alert(
                "notification_channel_recovered",
                f"Notification channel '{name}' is delivering again",
                "The channel accepted a delivery after failing.", "low")

    def _on_fail(self, name: str, health: Health, why: str, *,
                 permanent: bool = False) -> None:
        now = time.time()
        with self._lock:
            was = health.state
            health.bad(why, now)
            health.failed += 1
            self._failures += 1
            streak = health.consecutive_failures
        logger.warning("channel %s failed: %s", name, why)
        # Exactly once, when the streak crosses the threshold. Firing on
        # the first blip would make a single 503 from Slack raise a
        # high-severity alert and post a notice to another channel —
        # the alarm-fatigue pattern this whole app is written against.
        # A rejected credential is exempt: it never heals, so waiting
        # for a second one only delays the truth.
        if streak == FAILING_ALERT_AFTER or (
                permanent and was != "failing"):
            self._report_broken(name, why)

    def _report_broken(self, name: str, why: str) -> None:
        """Say a channel is broken — somewhere OTHER than that channel.

        The failure mode this whole app is built against is "push
        stopped working three weeks ago and nobody knew". Reporting it
        through the broken channel reproduces it exactly, so this goes
        two ways that do not depend on the failing channel: an alert
        onto the bus, which reaches the inbox and core's own alarm
        actions, and a one-line notice on any OTHER healthy channel.
        """
        self._raise_alert(
            "notification_channel_failing",
            f"Notification channel '{name}' is not delivering",
            f"{why}. Alerts routed only to this channel are not reaching "
            f"anyone. Check it on the Notifications page.", "high")
        with self._lock:
            # Anything not itself broken. A backup channel that has
            # never been used is "unverified", which is the NORMAL state
            # of a backup channel — requiring "healthy" here means that
            # on day one, when the primary's token is wrong, the notice
            # goes nowhere at all. This is a best-effort notice; trying
            # costs nothing.
            others = [c for n, c in self._channels.items()
                      if n != name
                      and self._health.get(n, Health()).state != "failing"
                      and not self._health.get(n, Health()).build_error]
        if not others:
            return
        notice = ch.Message(
            title="A notification channel is failing",
            body=f"'{name}' stopped accepting deliveries: {why}",
            severity="high", when=self._clock(),
            url=f"{self._base_url}/notifications" if self._base_url else "",
            dedup_key=f"channel-failing:{name}")
        try:
            others[0].send(notice)
        except Exception as exc:  # noqa: BLE001 — best effort by design
            logger.warning("could not report the failure on %s: %s",
                           others[0].name, exc)

    def _raise_alert(self, alert_type: str, title: str, description: str,
                     severity: str) -> None:
        """Put this app's own trouble on the bus.

        Deliberately NOT through our own channels: core stores it, rings
        open browsers, and can act on it through a Twilio path that
        shares not one line of code with anything in channels.py."""
        dispatcher = self._alerts
        if dispatcher is None:
            return
        try:
            from opennvr_app_sdk import Alert

            dispatcher.fire(Alert(
                title=title, description=description, camera_id="",
                severity=severity, alert_type=alert_type,
                tags=[f"type:{alert_type}"]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not raise %s: %s", alert_type, exc)

    def _record(self, job: Job, results: dict[str, str], *, ok: bool) -> None:
        level = job.message.severity if ok else "high"
        detail = ", ".join(f"{n}: {v}" for n, v in results.items()) or "nowhere"
        # No "sent"/"FAILED" prefix: ``kind`` already carries the verb
        # and every surface renders it, so putting it here too produces
        # "Sent sent Person at the door".
        head = job.message.subject()
        if job.is_update:
            head = f"updated · {head}"
        with self._lock:
            self._note(head, level, detail=detail,
                       kind="delivered" if ok else "failed")

    def _note(self, message: str, level: str, *, detail: str = "",
              kind: str = "info") -> None:
        self._recent.appendleft({
            "message": message, "time": time.time(), "level": level,
            "detail": detail, "kind": kind})

    # -- periodic work ------------------------------------------------

    def tick(self, now: float | None = None) -> list[Job]:
        """Flush due groups, expire things, deliver the quiet-hours
        summary. Returns the jobs it produced so tests can drive it
        without a delivery thread."""
        now = time.time() if now is None else now
        jobs: list[Job] = []
        with self._lock:
            self._roll_day()
            for group in self._grouper.flush_due(now):
                jobs.append(self._job_for(group, False))
            self._grouper.prune(now)
            self._inhibitor.prune(now)
            jobs.extend(self._quiet_summary(now))
        for job in jobs:
            self._enqueue(job)
        return jobs

    def _quiet_summary(self, now: float) -> list[Job]:
        """When the quiet window ends, deliver what it held.

        The contract of ``hold`` is a promise: every suppressed alert
        was logged as "held for the 07:00 summary", so the alerts must
        actually arrive. Two ways that promise used to be broken, both
        fixed here:

        The list was emptied BEFORE checking there was anywhere to send
        it, so a config change that left no usable channel deleted the
        night's alerts outright, and the page then showed "0 holding".
        Nothing is taken from ``_held`` now until a job exists.

        And the summary went to every channel regardless of routing, so
        an alert the rules sent only to the maintenance webhook was
        broadcast to the family's phones in the morning. Held alerts
        are re-routed through the same rules instead, producing one
        summary per destination set.
        """
        if not self._held:
            return []
        local = routing.local_now(self._tz, now)
        if self._quiet.active(local):
            return []
        if not self._channels:
            # Nowhere to send it. KEEP the alerts: the operator may be
            # mid-way through fixing a channel, and deleting the
            # night's alerts because of a typo is the worst thing this
            # function could do.
            logger.warning(
                "%d alerts are held from quiet hours but no channel is "
                "configured — keeping them", len(self._held))
            return []

        by_target: dict[tuple[str, ...], list[routing.Incident]] = {}
        for held in self._held:
            rule = self._rules.match(held, local=local)
            live = tuple(sorted(c for c in (rule.channels if rule else [])
                                if c in self._channels))
            if not live:
                continue
            by_target.setdefault(live, []).append(held)
        if not by_target:
            logger.warning(
                "%d alerts are held from quiet hours but no rule routes "
                "them to a channel that loaded — keeping them",
                len(self._held))
            return []

        routed = {id(i) for group in by_target.values() for i in group}
        self._held = [i for i in self._held if id(i) not in routed]
        dropped, self._held_dropped = self._held_dropped, 0

        jobs: list[Job] = []
        for index, (targets, held) in enumerate(sorted(by_target.items())):
            ordered = sorted(held, key=lambda i: (-i.rank, i.at))
            lead = ordered[0]
            body = f"Most serious: {lead.title}" + (
                f" on {lead.where}" if lead.where else "")
            if dropped:
                body += (f" · {dropped} more were dropped — the hold "
                         f"reached its {MAX_HELD}-alert limit")
            jobs.append(Job(
                message=ch.Message(
                    title=f"{len(held)} alerts during quiet hours",
                    body=body, severity=lead.severity,
                    when=self._clock(now),
                    url=f"{self._base_url}/alerts" if self._base_url else "",
                    dedup_key=f"quiet-summary:{int(now)}:{index}",
                    message_id=f"quiet-{int(now)}-{index}",
                    group=len(held),
                    members=[(i.severity, i.title, i.where) for i in ordered]),
                channels=list(targets),
                group_key=f"quiet-summary:{int(now)}:{index}"))
        return jobs

    def probe_all(self, *, force: bool = False) -> dict[str, str]:
        """Silently verify credentials.

        This is the feature that separates "we send notifications" from
        "notifications arrive": a bot token revoked on Tuesday is found
        on Tuesday, by a call that wakes nobody.
        """
        now = time.time()
        interval = _nonneg(self.cfg.probe_hours, 12.0) * 3600.0
        out: dict[str, str] = {}
        with self._lock:
            items = list(self._channels.items())
        for name, channel in items:
            with self._lock:
                health = self._health.setdefault(name, Health())
            if not channel.can_probe:
                out[name] = "no silent check available"
                continue
            if not force and (interval <= 0
                              or now - health.last_probe < interval):
                continue
            health.last_probe = now
            try:
                channel.probe()
            except ch.NotSupported as exc:
                out[name] = str(exc)
                continue
            except ch.AuthFailed as exc:
                out[name] = f"auth: {exc}"
                self._on_fail(name, health, str(exc), permanent=True)
                continue
            except ch.DeliveryError as exc:
                # A transport blip is not a broken credential. Recorded
                # without condemning the channel, or every flaky minute
                # of wifi becomes a "channel failing" alert.
                out[name] = f"unreachable: {exc}"
                logger.info("probe of %s could not reach it: %s", name, exc)
                continue
            except Exception as exc:  # noqa: BLE001
                out[name] = f"internal error: {exc}"
                continue
            out[name] = "ok"
            with self._lock:
                health.state = "healthy"
                health.last_ok = max(health.last_ok, now)
                health.consecutive_failures = 0
                health.last_error = ""
        return out

    def _clock(self, stamp: float | None = None) -> str:
        """HH:MM in the SITE's zone, not the container's."""
        return routing.clock(self._tz, stamp)

    def _today_key(self) -> str:
        """The site's calendar day, so the daily counters roll at the
        site's midnight rather than the container's."""
        return routing.local_now(self._tz).strftime("%Y-%m-%d")

    def _roll_day(self) -> None:
        today = self._today_key()
        if self._today.get("day") != today:
            self._today = {"day": today, "delivered": 0, "suppressed": 0}

    # -- core lookups -------------------------------------------------

    @property
    def nvr(self) -> Any:
        """The core client, built once and never fatal.

        Everything it provides here is an improvement, not a
        requirement: camera names make a notification readable, site
        mode makes it respectful, durable state makes a pause survive a
        restart. None of it may stop an alert reaching a phone.
        """
        if self._nvr is None and not self._nvr_tried:
            self._nvr_tried = True
            try:
                from opennvr_app_sdk import OpenNVR

                self._nvr = OpenNVR(self.cfg.opennvr_url or None, timeout=8.0,
                                    token=self.cfg.opennvr_token or None)
            except Exception as exc:  # noqa: BLE001
                logger.info("core API unavailable (%s) — running without "
                            "camera names, site mode and durable pauses", exc)
        return self._nvr

    def refresh_context(self, now: float | None = None) -> None:
        """Camera names and the site's arm state, on a slow cadence."""
        now = time.time() if now is None else now
        nvr = self.nvr
        if nvr is None:
            return
        if now - self._camera_names_at > 300.0:
            self._camera_names_at = now
            try:
                names = {str(c.id): (c.name or f"camera {c.id}")
                         for c in (nvr.roster() or [])}
                if names:
                    with self._lock:
                        # Both spellings: alerts carry 'cam3' or '3'.
                        self._camera_names = {
                            **names,
                            **{f"cam{k}": v for k, v in names.items()},
                            **{f"cam-{k}": v for k, v in names.items()}}
            except Exception as exc:  # noqa: BLE001
                logger.debug("camera roster unavailable: %s", exc)
        if self.cfg.respect_site_mode and now - self._site_mode_at > 30.0:
            self._site_mode_at = now
            try:
                body = nvr.site_mode() or {}
            except Exception as exc:  # noqa: BLE001
                logger.debug("site mode unavailable: %s", exc)
                return
            mode = str(body.get("mode") or "") if isinstance(body, dict) else ""
            with self._lock:
                # Unknown is NOT disarmed. A core we cannot reach must
                # never be able to silence the alarms.
                self._site_mode = mode

    def _restore_state(self) -> None:
        nvr = self.nvr
        if nvr is None:
            return
        try:
            saved = nvr.state.get("mutes") or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not read saved pauses: %s", exc)
            return
        self._muting.restore(saved, time.time())

    def _persist_mutes(self) -> None:
        nvr = self.nvr
        if nvr is None:
            return
        try:
            nvr.state.set("mutes", self._muting.snapshot())
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not save pauses: %s", exc)

    def _build_alert_dispatcher(self) -> None:
        if self._alerts is not None:
            return
        try:
            from opennvr_app_sdk import build_dispatcher, set_default_source

            set_default_source(kind="app", name="alert-notifier",
                               version=MANIFEST.version)
            self._alerts = build_dispatcher(
                webhook_url=None, nats_alerts_url=self.cfg.nats_url,
                nats_alerts_token=self.cfg.nats_token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cannot raise our own alerts: %s", exc)

    # -- actions ------------------------------------------------------

    def on_action(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        if name == "test":
            return self.action_test(str(params.get("channel") or "").strip())
        if name == "confirm":
            return self.action_confirm(str(params.get("channel") or "").strip())
        if name == "check":
            return {"ok": True, "results": self.probe_all(force=True)}
        if name == "mute":
            return self.action_mute(params)
        if name == "unmute":
            return self.action_unmute(str(params.get("camera") or "").strip())
        if name == "backtest":
            return self.action_backtest()
        return {"ok": False, "error": f"unknown action {name!r}"}

    def action_test(self, channel: str) -> dict[str, Any]:
        """Send a REAL notification, built from a real alert.

        Not "Hello from Alert Notifier". The most recent alert, its
        snapshot, the actual template — because a test that does not
        exercise the photo path does not test the part that breaks, and
        a test that looks nothing like a real notification tells the
        operator nothing about what 3am will look like.
        """
        with self._lock:
            targets = [channel] if channel else sorted(self._channels)
            missing = [t for t in targets if t not in self._channels]
            if missing:
                return {"ok": False,
                        "error": f"no such channel: {', '.join(missing)}"}
            if not targets:
                return {"ok": False,
                        "error": "no channels are configured yet"}
            sample = self._history[-1] if self._history else None
            message = self._sample_message(sample)
        self._enqueue(Job(message=message, channels=targets,
                          group_key=message.dedup_key, test=True))
        return {"ok": True, "sent_to": targets,
                "using": ("your most recent alert" if sample
                          else "a synthetic alert (none have arrived yet)"),
                "next": "Confirm it arrived with 'I got it' — HTTP 200 is "
                        "not proof a phone buzzed."}

    def _sample_message(self, sample: routing.Incident | None) -> ch.Message:
        if sample is None:
            sample = routing.Incident(
                title="Test notification",
                description="If you can read this, delivery works.",
                severity="high", camera_name="(no alerts have arrived yet)",
                at=time.time())
        group = routing.Group(key=f"test:{int(time.time() * 1000)}",
                              rule="test", channels=[], members=[sample],
                              opened_at=sample.at)
        message = routing.render(group, base_url=self._base_url,
                                 tz_name=self._tz,
                                 actions_enabled=bool(self._base_url))
        message.body = (message.body + " · test from OpenNVR").strip(" ·")
        return message

    def action_confirm(self, channel: str) -> dict[str, Any]:
        with self._lock:
            names = [channel] if channel else list(self._channels)
            # Validated, or a typo permanently invents a phantom channel
            # row that nobody can remove.
            missing = [n for n in names if n not in self._channels]
            if missing:
                return {"ok": False,
                        "error": f"no such channel: {', '.join(missing)}"}
            confirmed = []
            for name in names:
                health = self._health.setdefault(name, Health())
                if health.state == "failing" or health.build_error:
                    # It is still failing; a human saying they saw a
                    # message does not change that.
                    continue
                health.confirmed = True
                health.state = "healthy"
                health.last_ok = health.last_ok or time.time()
                confirmed.append(name)
        return {"ok": True, "confirmed": confirmed,
                "skipped": [n for n in names if n not in confirmed]}

    def action_mute(self, params: dict[str, Any]) -> dict[str, Any]:
        asked = _number(params.get("minutes"), 60.0)
        camera = str(params.get("camera") or "").strip()
        now = time.time()
        with self._lock:
            until = self._muting.mute(camera or "*", asked, now)
        self._persist_mutes()
        scope = f"camera {camera}" if camera else "all alerts"
        left = (until - now) / 60.0
        with self._lock:
            self._note(f"{scope} paused for {int(left)} min", "medium",
                       kind="muted", detail=f"by {self._who()}")
        return {"ok": True, "scope": scope, "minutes": round(left, 1),
                "until": self._clock(until),
                "note": (f"Capped at {routing.MAX_MUTE_MINUTES // 60}h — a "
                         f"pause that never ends is a coverage gap."
                         if asked > routing.MAX_MUTE_MINUTES else "")}

    def action_unmute(self, camera: str) -> dict[str, Any]:
        with self._lock:
            cleared = self._muting.unmute(camera or "*")
        self._persist_mutes()
        scope = f"camera {camera}" if camera else "all alerts"
        if cleared:
            with self._lock:
                self._note(f"{scope} resumed", "low", kind="muted",
                           detail=f"by {self._who()}")
        return {"ok": True, "scope": scope, "was_paused": cleared}

    def action_backtest(self) -> dict[str, Any]:
        """"If I changed this rule, what would have fired?"

        Answered from the platform's alert inbox — the alerts THIS app
        raised, which core has kept whether or not this process was
        running when they happened.

        It used to be answered from an in-memory deque, and said so in
        its own output: "it does not read history it was not running
        for." Which made the answer worth very little on the occasion
        it mattered most, because the reason somebody backtests a rule
        is usually that something went wrong recently — and a redeploy
        or a crash between then and now emptied the evidence.
        """
        history, source = self._backtest_history()
        with self._lock:
            counts = self._rules.backtest(history, tz_name=self._tz)
            shadowed = [
                {"rule": self._rules.rules[i].name,
                 "covered_by": self._rules.rules[j].name}
                for i, j in self._rules.shadowed()]
        return {
            "ok": True,
            "alerts_considered": len(history),
            "since": self._clock(history[0].at) if history else "",
            "matches": counts,
            "never_fires": shadowed,
            "source": source,
        }

    def _backtest_history(self) -> tuple[list[routing.Incident], str]:
        """Past alerts to replay, newest-first from core, oldest-first out.

        Falls back to the in-memory deque when core cannot be reached,
        and SAYS which one it used. An operator reading "12 would have
        fired" needs to know whether that was measured against a month
        or against the twenty minutes since the last restart; the old
        version left them to guess, and the number looks identical
        either way.
        """
        try:
            rows = self.nvr.alerts.inbox(limit=BACKTEST_LIMIT)
        except Exception:  # noqa: BLE001 — a backtest is not worth a crash
            logger.warning("backtest: inbox unavailable", exc_info=True)
            rows = None
        if not rows:
            with self._lock:
                local = list(self._history)
            return local, ("the alerts this app has seen since it started "
                           "— core's inbox could not be read")
        history = [self._incident_from_row(r) for r in rows]
        history = [i for i in history if i is not None]
        history.sort(key=lambda i: i.at)
        return history, f"core's alert inbox ({len(history)} alerts)"

    @staticmethod
    def _incident_from_row(row: dict[str, Any]) -> "routing.Incident | None":
        """One inbox row as the shape the rules match against.

        Deliberately not a second parser: every field here is one the
        rules already read, and anything they do not read is left off
        rather than guessed at. ``image`` is absent by design — a
        backtest counts matches, it does not re-deliver.
        """
        when = row.get("observed_at") or row.get("fired_at") or ""
        at = 0.0
        if when:
            try:
                parsed = dt.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                at = parsed.timestamp()
            except (TypeError, ValueError):
                return None
        return routing.Incident(
            title=str(row.get("title") or "Alert"),
            description=str(row.get("description") or ""),
            severity=str(row.get("severity") or "high"),
            camera_id=str(row.get("camera_id") or ""),
            camera_name=str(row.get("camera_name") or ""),
            alert_type=str(row.get("alert_type") or ""),
            source=str(row.get("source_name") or ""),
            zones=list(row.get("zones") or []),
            tags=list(row.get("tags") or []),
            fired_at=str(row.get("fired_at") or ""),
            alert_id=str(row.get("alert_id") or ""),
            correlation_id=str(row.get("correlation_id") or ""),
            at=at,
        )

    def _who(self) -> str:
        try:
            user = self.current_user()
        except Exception:  # noqa: BLE001
            return "an operator"
        return (getattr(user, "username", None)
                or getattr(user, "name", None) or "an operator")

    # -- contract surface ---------------------------------------------

    def state_snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            channels = []
            for name in sorted(set(self._health) | set(self._channels)):
                health = self._health.get(name, Health())
                channel = self._channels.get(name)
                channels.append({
                    "name": name,
                    "type": channel.kind if channel else "—",
                    "address": channel.address if channel else "—",
                    "state": ("misconfigured" if health.build_error
                              else health.state),
                    "status": health.summary(now),
                    "confirmed": health.confirmed,
                    "delivered": health.delivered,
                    "failed": health.failed,
                    "last_ok": health.last_ok,
                    "last_error": health.last_error or health.build_error,
                    "can_attach": bool(channel and channel.can_attach),
                    "can_edit": bool(channel and channel.can_edit),
                    "can_probe": bool(channel and channel.can_probe),
                })
            failing = sum(1 for c in channels
                          if c["state"] in ("failing", "misconfigured"))
            unverified = sum(1 for c in channels
                             if c["state"] == "unverified")
            local = routing.local_now(self._tz, now)
            counts = self._rules.backtest(list(self._history), tz_name=self._tz)
            shadow = dict(self._rules.shadowed())
            rules = []
            for index, rule in enumerate(self._rules.rules):
                rules.append({
                    "position": index + 1,
                    "name": rule.name,
                    "reads": rule.describe(),
                    "to": list(rule.channels),
                    "enabled": rule.enabled,
                    "catch_all": rule.catch_all,
                    "would_match": counts.get(rule.name, 0),
                    "never_fires": (self._rules.rules[shadow[index]].name
                                    if index in shadow else ""),
                })
            mutes = {k: round(v, 1)
                     for k, v in self._muting.active(now).items()}
            return {
                "delivered_total": self._delivered,
                "suppressed_total": self._suppressed,
                "failure_total": self._failures,
                "dropped_total": self._queued_dropped,
                "today": {"delivered": self._today["delivered"],
                          "suppressed": self._today["suppressed"]},
                "health": {
                    "failing": failing,
                    "unverified": unverified,
                    "problem": bool(failing
                                    or (unverified and self._channels)),
                },
                "channels": channels,
                "rules": rules,
                "quiet": {
                    "enabled": self._quiet.enabled,
                    "active": self._quiet.active(local),
                    "mode": self._quiet.mode,
                    "breakthrough": self._quiet.breakthrough,
                    "ends_at": self._quiet.ends_at(local),
                    "holding": len(self._held),
                    "windows": [w.describe() for w in self._quiet.windows],
                },
                "mute": {"active": bool(mutes), "scopes": mutes},
                "site_mode": self._site_mode or "unknown",
                "respect_site_mode": bool(self.cfg.respect_site_mode),
                "grouping": {"wait_seconds": self._grouper.wait,
                             "pending": self._grouper.pending,
                             "inhibit_seconds": self._inhibitor.window},
                "min_severity": self.cfg.min_severity,
                "timezone": self._tz,
                "dry_run": bool(self.cfg.dry_run),
                "base_url": self._base_url,
                "queue_depth": self._queue.qsize(),
                "alerts_seen": len(self._history),
                "recent": list(self._recent),
            }

    def ui_html(self) -> str:
        """The in-catalog view. The Notifications page is the real one."""
        state = self.state_snapshot()
        rows = "".join(
            f"<tr><td>{_esc(c['name'])}</td><td>{_esc(c['type'])}</td>"
            f"<td><b>{_esc(c['state'])}</b></td>"
            f"<td>{_esc(c['status'])}</td>"
            f"<td>{c['delivered']}</td><td>{c['failed']}</td></tr>"
            for c in state["channels"]) or (
                "<tr><td colspan=6>No channels configured — nothing is "
                "being delivered.</td></tr>")
        rules = "".join(
            f"<li>{_esc(r['reads'])} <small>({r['would_match']} recent"
            + (f", never fires — covered by {_esc(r['never_fires'])}"
               if r["never_fires"] else "") + ")</small></li>"
            for r in state["rules"])
        log = "".join(
            f"<li>{_esc(e['message'])}"
            + (f" <small>{_esc(e['detail'])}</small>"
               if e.get("detail") else "") + "</li>"
            for e in state["recent"][:15])
        banner = ""
        if state["health"]["failing"]:
            banner += (f"<p><b>{state['health']['failing']} channel(s) are "
                       f"not delivering.</b></p>")
        if state["mute"]["active"]:
            banner += "<p><b>Alerts are paused.</b></p>"
        if state["quiet"]["active"]:
            banner += (f"<p>Quiet hours until "
                       f"{_esc(state['quiet']['ends_at'] or 'later')} — "
                       f"{state['quiet']['holding']} held.</p>")
        if state["dry_run"]:
            banner += "<p><b>Dry run</b> — deciding, sending nothing.</p>"
        return (
            "<h2>Alert Notifier</h2>" + banner
            + f"<p>Today: {state['today']['delivered']} delivered, "
              f"{state['today']['suppressed']} held back.</p>"
              "<table border=1 cellpadding=4><tr><th>Channel</th><th>Type</th>"
              "<th>State</th><th>Status</th><th>Sent</th><th>Failed</th></tr>"
            + rows + "</table><h3>Rules</h3><ol style='padding-left:1.2em'>"
            + rules + "</ol><h3>Recent</h3><ul>" + log + "</ul>")

    # -- the loop -----------------------------------------------------

    async def run(self, *, once: bool = False) -> None:
        self._build_alert_dispatcher()
        tasks: list[asyncio.Task] = []
        if not once:
            tasks.append(asyncio.create_task(self._tick_loop()))
        try:
            await super().run(once=once)
        finally:
            for task in tasks:
                task.cancel()
            self._shutdown()

    async def _tick_loop(self) -> None:
        """Group flushes need a second's resolution; probing and core
        lookups are network calls and go in a thread on a slow cadence."""
        last_slow = 0.0
        while True:
            await asyncio.sleep(1.0)
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                logger.exception("tick failed")
            now = time.time()
            if now - last_slow >= 30.0:
                last_slow = now
                try:
                    await asyncio.to_thread(self._slow_work)
                except Exception:  # noqa: BLE001
                    logger.exception("background refresh failed")

    def _slow_work(self) -> None:
        self.refresh_context()
        self.probe_all()

    def _shutdown(self) -> None:
        """Deliver what is still held, then stop the worker.

        A collapse window in flight at shutdown is a real notification
        somebody is owed; dropping it silently is the bug this whole
        file is written against."""
        try:
            with self._lock:
                jobs = [self._job_for(g, False)
                        for g in self._grouper.flush_all()]
            for job in jobs:
                self._enqueue(job)
            # Wait for the queue to drain, but BOUNDED. queue.join() has
            # no timeout, so if the worker has died — or was never
            # started — shutdown blocks for ever and the process has to
            # be killed. A few seconds to deliver what is owed is worth
            # it; an unkillable process is not.
            deadline = time.monotonic() + SHUTDOWN_DRAIN_SECONDS
            while (self._queue.unfinished_tasks
                   and self._worker is not None
                   and self._worker.is_alive()
                   and time.monotonic() < deadline):
                time.sleep(0.05)
            if self._queue.unfinished_tasks:
                logger.warning(
                    "%d notification(s) were still queued at shutdown",
                    self._queue.unfinished_tasks)
        finally:
            self._stopping.set()
            worker, self._worker = self._worker, None
            if worker is not None:
                try:
                    self._queue.put_nowait(None)
                except queue.Full:  # pragma: no cover
                    pass
                worker.join(timeout=5.0)
            for channel in self._channels.values():
                try:
                    channel.close()
                except Exception:  # noqa: BLE001
                    pass


# ── Helpers ─────────────────────────────────────────────────────────


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)








def _number(value: Any, default: float) -> float:
    """A usable POSITIVE number, or the default. For a duration where
    zero is meaningless — a pause of zero minutes is not a pause.

    ``float('nan')`` compares false against every bound, so an unchecked
    NaN sails through ``min``/``max`` and lands in a timestamp that can
    never be reached — which for a pause means "silent for ever"."""
    num = _finite(value)
    return default if num is None or num <= 0 else num


def _nonneg(value: Any, default: float) -> float:
    """Same, but zero is a legitimate setting: a collapse window of 0
    means "send immediately", an inhibit window of 0 means "off", and a
    probe interval of 0 means "don't check". Those must not silently
    become the default."""
    num = _finite(value)
    return default if num is None or num < 0 else num


def _finite(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num in (float("inf"), float("-inf")):
        return None
    return num


def main(argv: list[str] | None = None) -> int:
    return alert_app(AlertNotifier, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
