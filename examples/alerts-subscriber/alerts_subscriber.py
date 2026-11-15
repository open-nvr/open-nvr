# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Alerts-subscriber example app — now on the ``opennvr-app-sdk``.

The canonical subscriber-side template for OpenNVR's alert fan-out
(NATS alert fan-out). Connects to NATS, subscribes to a configurable
``opennvr.alerts.*`` subject pattern, prints each §11.5 Alert envelope
to stdout, and optionally forwards via webhook.

This is the alert-side companion to ``inference-listener``: that one
subscribes to ``opennvr.inference.*``; this one subscribes to
``opennvr.alerts.*``. Same bus, different subject family — and a
different SDK archetype: this app is the reference
:class:`~opennvr_app_sdk.AlertSubscriber` (App SDK spec §02, the
"pass-through" shape). The SDK base owns the NATS connect / subscribe
/ drain loop, per-message JSON decoding + handler exception isolation,
the §03 contract endpoints, and the CLI / signal lifecycle behind
``alert_app(AlertConsumer).run()``. What's left here is the sink —
``handle_alert`` — plus this app's config parsing and its MANIFEST.

What this is for
----------------

* **Operator-UI alerts inbox**: subscribe to ``opennvr.alerts.>`` and
  upsert into the database / push to the websocket the UI is listening
  on.
* **SIEM bridge**: subscribe to ``opennvr.alerts.>``, forward JSON to
  Splunk / Elastic / Datadog ingestion. Source field tells you which
  app emitted the alert.
* **Slack / PagerDuty bridge**: subscribe to one severity
  (``opennvr.alerts.*.*.cam-X`` filtered server-side, or filter on
  ``severity`` in code) and forward to a chat/incident tool.
* **Audit forwarder**: subscribe + write to durable storage so a
  broker outage doesn't lose history. (B2 follow-up adds JetStream
  durable consumers; for v1 this app is fire-and-forget.)

Why this is a separate example
------------------------------

Community contributors will write app-specific consumers (their own
PagerDuty rules, their own Splunk schemas). This subscriber is the
template: copy this folder, override ``handle_alert(subject, alert)``,
swap the AlertConsumer class for your own.

Run:
    python alerts_subscriber.py --config config.yml          # daemon
    python alerts_subscriber.py --config config.yml --once   # one alert then exit (testing)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from opennvr_app_sdk import (
    AlertSubscriber,
    AppManifest,
    Param,
    alert_app,
)
from opennvr_app_sdk.config import load_yaml

logger = logging.getLogger("alerts-subscriber")


MANIFEST = AppManifest(
    id="alerts-subscriber",
    name="Alerts Subscriber",
    version="1.0.0",
    category="integration",
    summary=(
        "Subscribes to the opennvr.alerts.* fan-out; prints every §11.5 "
        "envelope and optionally forwards it to a webhook."
    ),
    requires_tasks=[],  # rides the alert bus; no adapter prerequisites
    subscribes="opennvr.alerts.>",
    params=[
        Param("subject_pattern", str, default="opennvr.alerts.>"),
        Param("webhook_url", str, default=None,
              description="Optional URL each alert's raw JSON is POSTed to."),
        Param("webhook_timeout_seconds", float, default=5.0),
    ],
    emits=[],  # pass-through: consumes alerts, emits none
    # Reads no camera data, so the catalog offers no camera picker
    # (BUILDING_EFFICIENT_APPS.md, rule 6).
    camera_picker=False,
)


# ── Config ─────────────────────────────────────────────────────────


@dataclass
class AppConfig:
    nats_url: str
    nats_token: str | None
    subject_pattern: str
    webhook_url: str | None = None
    webhook_timeout_seconds: float = 5.0
    once: bool = False


def load_config(path: str) -> AppConfig:
    """Parse a YAML config file into a typed AppConfig.

    Raises ``ValueError`` on malformed input — caller's job to surface
    a useful operator message and exit non-zero.
    """
    raw = load_yaml(path)
    nats_url = str(raw.get("nats_url") or "").strip()
    if not nats_url:
        raise ValueError("config: 'nats_url' is required")
    # Distinguish "absent" (use default wildcard) from "present but
    # empty" (operator misconfig — refuse to start, same shape as
    # the inference-listener example).
    if "subject_pattern" in raw:
        subject = str(raw.get("subject_pattern") or "").strip()
        if not subject:
            raise ValueError("config: 'subject_pattern' must not be empty")
    else:
        subject = "opennvr.alerts.>"
    return AppConfig(
        nats_url=nats_url,
        nats_token=str(raw["nats_token"]) if raw.get("nats_token") else None,
        subject_pattern=subject,
        webhook_url=str(raw["webhook_url"]) if raw.get("webhook_url") else None,
        webhook_timeout_seconds=float(raw.get("webhook_timeout_seconds", 5.0)),
    )


# ── Subscriber ─────────────────────────────────────────────────────


class AlertConsumer(AlertSubscriber):
    """The reference AlertSubscriber. The SDK base owns the NATS loop
    (connect / subscribe / decode / drain) and calls
    :meth:`on_alert` per decoded envelope; ``stop()`` (or SIGINT /
    SIGTERM via the runner) cleanly drains and exits.

    Override ``handle_alert(subject, alert_dict)`` in a subclass to
    plug your own logic in — that's the extension point for
    operator-UI inbox writers, SIEM bridges, Slack bots, etc. The
    default implementation prints the alert to stdout in a one-line-
    per-alert format and, if ``webhook_url`` is set, forwards the
    raw JSON via HTTP POST.
    """

    manifest = MANIFEST

    def setup(self) -> None:
        self._received_count: int = 0
        self._forwarded_count: int = 0
        self._forward_failed_count: int = 0

    def on_alert(self, alert: dict[str, Any], subject: str) -> None:
        """SDK hook — count the envelope, delegate to this app's
        historical extension point (which spells the arguments
        ``(subject, alert)``)."""
        self._received_count += 1
        self.handle_alert(subject, alert)

    async def run(self, *, once: bool = False) -> None:
        logger.info(
            "subscribing to %r on %s (webhook=%s)",
            self.cfg.subject_pattern,
            self.cfg.nats_url,
            self.cfg.webhook_url or "none",
        )
        try:
            await super().run(once=once)
        finally:
            logger.info(
                "alerts-subscriber shutting down — received=%d "
                "webhook_forwarded=%d webhook_failed=%d",
                self._received_count,
                self._forwarded_count,
                self._forward_failed_count,
            )

    def state_snapshot(self) -> dict[str, Any]:
        """``GET /state`` — running consume / forward counters."""
        return {
            "received": self._received_count,
            "webhook_forwarded": self._forwarded_count,
            "webhook_failed": self._forward_failed_count,
        }

    # ── Extension point ───────────────────────────────────────────

    def handle_alert(self, subject: str, alert: dict[str, Any]) -> None:
        """Default handler: print a one-line summary + optional
        webhook forward.

        ``alert`` is the JSON-decoded §11.5 Alert envelope —
        ``alert_id``, ``fired_at``, ``title``, ``description``,
        ``severity``, ``source``, ``camera_id``, ``correlation_id``,
        ``evidence``, ``tags``.

        Override in a subclass for real processing (DB insert, SIEM
        forward, Slack notification, etc.). The default behavior is
        deliberately operator-grep-friendly.
        """
        line = (
            f"ALERT [{str(alert.get('severity', '?')).upper()}] "
            f"{alert.get('fired_at', '?')} "
            f"subject={subject} "
            f"camera={alert.get('camera_id', '?')} "
            f"title={alert.get('title', '?')!r} "
            f"source={(alert.get('source') or {}).get('name', '?')} "
            f"correlation_id={alert.get('correlation_id') or '-'} "
            f"alert_id={alert.get('alert_id', '?')}"
        )
        print(line, flush=True)

        if self._config.webhook_url:
            ok = self._forward_to_webhook(alert)
            if ok:
                self._forwarded_count += 1
            else:
                self._forward_failed_count += 1

    def _forward_to_webhook(self, alert: dict[str, Any]) -> bool:
        """POST the raw §11.5 JSON to ``webhook_url``. Failures are
        LOGGED but never raise — same shape as the SDK's
        WebhookChannel.

        We use a fresh ``httpx.post`` per alert (no shared session)
        because: (a) the alert rate is low compared to inference;
        (b) keeping this fully stateless makes ``handle_alert``
        trivially overridable from a subclass without worrying about
        client lifecycle.
        """
        try:
            response = httpx.post(
                self._config.webhook_url,
                json=alert,
                timeout=self._config.webhook_timeout_seconds,
                # trust_env=False to ignore operator-side HTTP_PROXY
                # — alert forwarding should go direct, not through
                # whatever proxy might be configured for general http.
                trust_env=False,
            )
            if response.status_code >= 400:
                logger.warning(
                    "webhook %s returned %d: %s",
                    self._config.webhook_url,
                    response.status_code,
                    response.text[:200],
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "webhook %s failed: %s", self._config.webhook_url, exc,
            )
            return False


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (``[project.scripts]``). The SDK
    runner owns argparse, logging, signals, and the loop lifecycle."""
    return alert_app(AlertConsumer, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
