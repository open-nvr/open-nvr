# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
alert-notifier — the guard's phone.

An alarm nobody hears isn't one. This app subscribes to the platform's
contracted alert stream (``alert.fired.v1``, docs/EVENT_CONTRACTS.md)
and pushes the ones that matter to people: a Telegram chat (free,
instant, group-able — the guard house gets a phone that beeps) and/or
any generic webhook (Slack/Teams incoming hooks, SMS gateways like
Twilio/MSG91 via their HTTP APIs, a siren relay, a SIEM).

Pure consumer, zero inference, zero core access: severity gate in,
message out. Flood-safe by design — a camera fault that fires fifty
alerts must not send fifty pushes:

* ``min_severity`` forwards only alerts at or above the bar
  (default ``high`` — unknown vehicles, watchlist hits, barrier
  faults; the info/low chatter stays in the UI inbox);
* per (camera, title) repeat cooldown — the same alarm re-firing
  within the window is counted, not re-sent;
* a global per-minute ceiling as the last line.

Run:
    python alert_notifier.py --config config.yml
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx  # module attribute on purpose: tests monkeypatch alert_notifier.httpx
import yaml

from opennvr_app_sdk import (
    AlertSubscriber,
    AppManifest,
    Param,
    StateView,
    alert_app,
)

logger = logging.getLogger("alert-notifier")

#: The contracted domain alert stream (EVENT_CONTRACTS.md) — every
#: first-party app's alerts land here regardless of which app fired.
ALERT_SUBJECT_PATTERN = "opennvr.events.alert.fired.v1.>"

#: HTTP budget per delivery attempt.
DELIVERY_TIMEOUT_SECONDS = 5.0

#: Severity ladder for the gate (unknown severities rank as "low").
SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def severity_rank(value: Any) -> int:
    return SEVERITY_RANK.get(str(value or "").lower().strip(), 1)


MANIFEST = AppManifest(
    id="alert-notifier",
    name="Alert Notifier",
    version="1.0.0",
    category="notifications",
    summary=(
        "Pushes the alerts that matter to people: Telegram to the "
        "guard's phone and/or any webhook (Slack, Teams, SMS gateways, "
        "sirens). Severity-gated and flood-safe."
    ),
    requires_tasks=[],
    requires_scopes=["events:alert.fired"],
    subscribes=ALERT_SUBJECT_PATTERN,
    params=[
        Param("min_severity", str, default="high",
              description=(
                  "Forward alerts at or above this severity "
                  "(info/low/medium/high/critical). Default high: "
                  "unknown vehicles, watchlist hits, barrier faults.")),
        Param("telegram_bot_token", str, default="",
              description=(
                  "Telegram bot token (from @BotFather). With chat_id "
                  "set, alerts land in that chat — add the bot to the "
                  "guard-house group.")),
        Param("telegram_chat_id", str, default="",
              description="Telegram chat id (user, group, or channel)."),
        Param("notify_webhook_url", str, default="",
              description=(
                  "Generic POST target: gets the full alert JSON plus "
                  "a ready-made 'message' string. Works with Slack/"
                  "Teams incoming hooks, SMS gateway HTTP APIs, "
                  "sirens, SIEMs.")),
        Param("repeat_cooldown_seconds", float, default=300.0,
              description=(
                  "The same (camera, title) re-firing within this "
                  "window is counted, not re-sent — one alarm, one "
                  "push.")),
        Param("max_per_minute", int, default=20,
              description="Global push ceiling per minute (flood guard)."),
    ],
    state_schema=[
        StateView(name="forwarded", label="Pushed", kind="metric",
                  path="forwarded_total"),
        StateView(name="suppressed", label="Suppressed", kind="metric",
                  path="suppressed_total",
                  description="Below the bar, in cooldown, or over the ceiling."),
        StateView(name="failures", label="Delivery failures", kind="metric",
                  path="failure_total"),
        StateView(name="recent", label="Recent notifications",
                  kind="log", path="recent", limit=12),
    ],
    description=(
        "The delivery half of alerting. Detectors and apps raise "
        "alerts on the bus; this app decides which ones deserve a "
        "human's attention right now and pushes them — Telegram for "
        "the guard's phone (free, instant, works in a group), a "
        "generic webhook for everything else: Slack or Teams incoming "
        "hooks, SMS gateway HTTP APIs (Twilio, MSG91), a siren relay, "
        "a SIEM.\n\n"
        "Flood-safe: a severity bar (default high), a per-alarm repeat "
        "cooldown, and a global per-minute ceiling mean the guard "
        "hears one beep per real event — never fifty for a flapping "
        "camera.\n\n"
        "Every delivery failure is counted and visible in the catalog; "
        "settings apply live from the Configure form."
    ),
    author="OpenNVR",
    website="https://github.com/open-nvr/open-nvr",
    license="AGPL-3.0",
    contact="https://github.com/open-nvr/open-nvr/discussions",
    use_cases=[
        "Unknown-vehicle and watchlist alarms on the guard's phone (Telegram)",
        "SMS via any HTTP gateway (Twilio, MSG91) through the webhook",
        "Barrier faults straight to maintenance chat",
        "Feed high-severity events to Slack/Teams/SIEM",
    ],
)


# ── Config ──────────────────────────────────────────────────────────


@dataclass
class AppConfig:
    nats_url: str
    nats_token: str | None = None
    subject_pattern: str = ALERT_SUBJECT_PATTERN

    min_severity: str = "high"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notify_webhook_url: str = ""
    repeat_cooldown_seconds: float = 300.0
    max_per_minute: int = 20

    # App contract (spec §03).
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"config file {path} did not parse to a dict")
    nats_url = raw.get("nats_url")
    if not nats_url:
        raise ValueError(
            "config: nats_url is required — this app consumes the "
            "platform's alert.fired.v1 events from the bus")
    return AppConfig(
        nats_url=str(nats_url),
        nats_token=raw.get("nats_token") or None,
        subject_pattern=str(
            raw.get("subject_pattern") or ALERT_SUBJECT_PATTERN),
        min_severity=str(raw.get("min_severity") or "high"),
        telegram_bot_token=str(raw.get("telegram_bot_token") or ""),
        telegram_chat_id=str(raw.get("telegram_chat_id") or ""),
        notify_webhook_url=str(raw.get("notify_webhook_url") or ""),
        repeat_cooldown_seconds=float(raw.get("repeat_cooldown_seconds", 300.0)),
        max_per_minute=int(raw.get("max_per_minute", 20)),
        contract_port=(
            int(raw["contract_port"]) if raw.get("contract_port") is not None else None
        ),
        contract_bind_host=raw.get("contract_bind_host"),
        contract_host=raw.get("contract_host"),
        opennvr_url=raw.get("opennvr_url"),
        opennvr_token=raw.get("opennvr_token"),
    )


# ── The app ─────────────────────────────────────────────────────────


class AlertNotifier(AlertSubscriber):
    """alert.fired.v1 → Telegram / webhook, severity-gated, flood-safe."""

    manifest = MANIFEST

    def setup(self) -> None:
        cfg = self.cfg
        self._min_rank = severity_rank(cfg.min_severity)
        self._telegram = (cfg.telegram_bot_token.strip(),
                          cfg.telegram_chat_id.strip())
        self._webhook = cfg.notify_webhook_url.strip()
        self._cooldown = max(0.0, float(cfg.repeat_cooldown_seconds))
        self._ceiling = max(1, int(cfg.max_per_minute))
        self._last_sent: dict[tuple[str, str], float] = {}
        self._minute_window: deque[float] = deque()
        self._forwarded = 0
        self._suppressed = 0
        self._failures = 0
        self._recent: deque[dict[str, Any]] = deque(maxlen=25)

    # ── The sink ───────────────────────────────────────────────────

    def on_alert(self, alert: dict[str, Any], subject: str) -> None:
        # The domain envelope wraps the §11.5 alert as its payload;
        # tolerate BOTH shapes (a plain alert dict on the legacy
        # plumbing subject, the envelope on the domain subject).
        body = alert.get("payload") if isinstance(alert.get("payload"), dict) else alert
        severity = body.get("severity")
        title = str(body.get("title") or "Alert")
        camera_id = str(body.get("camera_id") or alert.get("camera_id") or "")

        if severity_rank(severity) < self._min_rank:
            self._suppressed += 1
            return

        now = time.monotonic()
        key = (camera_id, title)
        last = self._last_sent.get(key)
        if self._cooldown > 0 and last is not None and (now - last) < self._cooldown:
            self._suppressed += 1
            return
        # Global ceiling: drop timestamps older than 60s, then check.
        while self._minute_window and now - self._minute_window[0] > 60.0:
            self._minute_window.popleft()
        if len(self._minute_window) >= self._ceiling:
            self._suppressed += 1
            self._note(f"CEILING hit — suppressed [{severity}] {title}", "high")
            return

        message = self._format(body, camera_id)
        delivered = self._deliver(message, body)
        if delivered:
            # Cooldown + ceiling track DELIVERED pushes only — a failed
            # delivery stays retriable by the next firing.
            self._last_sent[key] = now
            self._minute_window.append(now)
            if len(self._last_sent) > 4096:
                for stale, _ts in sorted(self._last_sent.items(),
                                         key=lambda kv: kv[1])[:2048]:
                    self._last_sent.pop(stale, None)
            self._forwarded += 1
            self._note(f"pushed [{severity}] {title}", str(severity))
        else:
            self._failures += 1
            self._note(f"DELIVERY FAILED [{severity}] {title}", "high")

    # ── Delivery ───────────────────────────────────────────────────

    @staticmethod
    def _format(body: dict[str, Any], camera_id: str) -> str:
        severity = str(body.get("severity") or "?").upper()
        title = str(body.get("title") or "Alert")
        description = str(body.get("description") or "").strip()
        lines = [f"[{severity}] {title}"]
        if description:
            lines.append(description)
        meta = []
        if camera_id:
            meta.append(f"camera {camera_id}")
        if body.get("fired_at"):
            meta.append(str(body["fired_at"]))
        if meta:
            lines.append(" · ".join(meta))
        return "\n".join(lines)

    def _deliver(self, message: str, body: dict[str, Any]) -> bool:
        """True iff every CONFIGURED channel accepted the push (no
        channels configured = nothing to do = not a success: the
        operator thinks they have alerting and they don't — count it
        as a failure so the catalog shows it)."""
        token, chat_id = self._telegram
        targets = 0
        ok = 0
        if token and chat_id:
            targets += 1
            ok += self._send_telegram(token, chat_id, message)
        if self._webhook:
            targets += 1
            ok += self._send_webhook(self._webhook, message, body)
        if targets == 0:
            logger.warning(
                "alert passed the gate but NO channel is configured — "
                "set telegram_bot_token+telegram_chat_id or "
                "notify_webhook_url in the app config")
            return False
        return ok == targets

    @staticmethod
    def _send_telegram(token: str, chat_id: str, message: str) -> bool:
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=DELIVERY_TIMEOUT_SECONDS,
            )
            return 200 <= resp.status_code < 300
        except Exception as exc:  # noqa: BLE001 — delivery trouble is counted, not fatal
            logger.warning("telegram delivery failed: %s", exc)
            return False

    @staticmethod
    def _send_webhook(url: str, message: str, body: dict[str, Any]) -> bool:
        try:
            resp = httpx.post(
                url,
                json={"message": message, "alert": body},
                timeout=DELIVERY_TIMEOUT_SECONDS,
            )
            return 200 <= resp.status_code < 300
        except Exception as exc:  # noqa: BLE001
            logger.warning("webhook delivery failed: %s", exc)
            return False

    def _note(self, message: str, level: str) -> None:
        self._recent.append(
            {"message": message, "time": time.time(), "level": level})

    # ── Contract surface ───────────────────────────────────────────

    def state_snapshot(self) -> dict[str, Any]:
        token, chat_id = self._telegram
        return {
            "forwarded_total": self._forwarded,
            "suppressed_total": self._suppressed,
            "failure_total": self._failures,
            "channels": {
                "telegram": bool(token and chat_id),
                "webhook": bool(self._webhook),
            },
            "min_severity": self.cfg.min_severity,
            "recent": list(self._recent),
        }

    def on_config_update(self, config: dict[str, Any]) -> None:
        """Channel + gate settings apply live (idempotent)."""
        if "min_severity" in config:
            self._min_rank = severity_rank(config.get("min_severity"))
            self.cfg.min_severity = str(config.get("min_severity") or "high")
        if "telegram_bot_token" in config or "telegram_chat_id" in config:
            self._telegram = (
                str(config.get("telegram_bot_token") or "").strip(),
                str(config.get("telegram_chat_id") or "").strip(),
            )
        if "notify_webhook_url" in config:
            self._webhook = str(config.get("notify_webhook_url") or "").strip()
        if "repeat_cooldown_seconds" in config:
            try:
                self._cooldown = max(
                    0.0, float(config.get("repeat_cooldown_seconds", 300.0)))
            except (TypeError, ValueError):
                pass
        if "max_per_minute" in config:
            try:
                self._ceiling = max(1, int(config.get("max_per_minute", 20)))
            except (TypeError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    return alert_app(AlertNotifier, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
