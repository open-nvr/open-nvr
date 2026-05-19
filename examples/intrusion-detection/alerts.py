# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Alert emission for the intrusion-detection example.

Two delivery channels in v1:

* **stdout** — always fires. Operator-visible log line, machine-grep-able.
* **webhook** — optional HTTP POST. Slack, Discord, Teams, PagerDuty —
  any service that accepts an incoming-webhook JSON payload works.

Future channels (NATS subjects, OpenNVR alerts-API native endpoint,
SMS/email via OpenNVR's notification settings) land alongside the
NATS event bus (B1) and the operator-UI alerts inbox (A2.5b).

The Alert shape on the wire matches §11.5 of the contract design so
downstream consumers (UI inbox, audit log, SIEM) parse it identically
to KAI-C-emitted alerts. The contract calls this the "app-emitted
alert" shape.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


# ── Alert payload (matches §11.5 wire shape) ───────────────────────


@dataclass
class AlertSource:
    """The ``source`` block of the §11.5 alert envelope."""

    kind: str = "app"  # one of: kai-c / adapter / app
    name: str = "intrusion-detection"
    version: str = "1.0.0"


@dataclass
class Alert:
    """A single fired alert.

    Maps 1:1 to the §11.5 alert wire shape: ``alert_id``, ``fired_at``,
    ``title``, ``description``, ``severity``, ``source``, ``camera_id``,
    ``correlation_id``, ``evidence``, ``tags``.

    Severity levels are operator-visible — ``low`` / ``medium`` /
    ``high`` / ``critical`` per the design doc.
    """

    title: str
    description: str
    camera_id: str
    severity: str = "high"  # low / medium / high / critical
    source: AlertSource = field(default_factory=AlertSource)
    correlation_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    alert_id: str = field(default_factory=lambda: f"alrt_{uuid.uuid4().hex[:12]}")
    fired_at: str = field(default_factory=lambda: _utcnow_iso())

    def to_wire(self) -> dict[str, Any]:
        """Serialize to the §11.5 JSON shape."""
        return {
            "alert_id": self.alert_id,
            "fired_at": self.fired_at,
            "title": self.title,
            "description": self.description,
            "severity": self.severity,
            "source": asdict(self.source),
            "camera_id": self.camera_id,
            "correlation_id": self.correlation_id,
            "evidence": dict(self.evidence),
            "tags": list(self.tags),
        }


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp without microseconds."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ── Channels ───────────────────────────────────────────────────────


class AlertChannel(Protocol):
    """Anything that can ``send(alert)`` is a channel. Stdout + webhook
    are the v1 implementations; future channels (NATS, OpenNVR alerts
    API) plug in here without touching the detector loop."""

    def send(self, alert: Alert) -> bool:
        ...  # pragma: no cover — Protocol


class StdoutChannel:
    """Always-on channel: human-readable line to stdout."""

    name = "stdout"

    def send(self, alert: Alert) -> bool:
        # Single line, machine-grep-friendly. Severity in CAPS so a
        # tail -f operator spots high/critical quickly.
        line = (
            f"ALERT [{alert.severity.upper()}] "
            f"{alert.fired_at} "
            f"camera={alert.camera_id} "
            f"title={alert.title!r} "
            f"correlation_id={alert.correlation_id or '-'} "
            f"alert_id={alert.alert_id}"
        )
        print(line, flush=True)
        return True


class WebhookChannel:
    """POST the alert's JSON wire shape to an operator-configured URL.

    Failures are LOGGED but never raise — a dead webhook should not
    prevent stdout alerts from firing or crash the detector loop. This
    matches the §11.2 "audit forwarding failures are themselves
    audited" pattern (though we don't yet have an audit sink here —
    that lands when the example talks to KAI-C's audit log directly).
    """

    name = "webhook"

    def __init__(self, url: str, *, timeout_seconds: float = 5.0) -> None:
        self._url = url
        self._timeout = timeout_seconds

    def send(self, alert: Alert) -> bool:
        body = alert.to_wire()
        try:
            response = httpx.post(
                self._url,
                json=body,
                timeout=self._timeout,
                # trust_env=False so an operator-side HTTP_PROXY env
                # doesn't redirect alert delivery through a proxy that
                # might not honor the schema.
                trust_env=False,
            )
            if response.status_code >= 400:
                logger.warning(
                    "webhook %s returned %d: %s",
                    self._url,
                    response.status_code,
                    response.text[:200],
                )
                return False
            return True
        except Exception as exc:
            logger.warning("webhook %s failed: %s", self._url, exc)
            return False


# ── Dispatcher ─────────────────────────────────────────────────────


class AlertDispatcher:
    """Holds an ordered list of channels and fires an alert through
    all of them, isolating each channel's failures.

    ``fire()`` returns a per-channel report so the caller can audit
    delivery outcomes; the detector loop ignores this in v1 but the
    OpenNVR alerts-API integration (A2.5b) will record it.
    """

    def __init__(self, channels: list[AlertChannel]) -> None:
        if not channels:
            raise ValueError("AlertDispatcher requires at least one channel.")
        self._channels = channels

    def fire(self, alert: Alert) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for channel in self._channels:
            channel_name = getattr(channel, "name", channel.__class__.__name__)
            try:
                ok = channel.send(alert)
            except Exception as exc:
                logger.exception("channel %s raised: %s", channel_name, exc)
                ok = False
            results[channel_name] = ok
        return results


def build_dispatcher(*, webhook_url: str | None) -> AlertDispatcher:
    """Convenience factory used by ``intrusion_detection.py`` config
    loading. stdout is always included; webhook is opt-in via config."""
    channels: list[AlertChannel] = [StdoutChannel()]
    if webhook_url:
        channels.append(WebhookChannel(webhook_url))
    return AlertDispatcher(channels)
