# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""`AlertSubscriber` — react to alerts other apps fire.

Demonstrates: `AlertSubscriber`, `AlertSubscriber.on_alert`,
`alert_app`, `alert_subject`, `DEFAULT_ALERT_SUBJECT_PREFIX`.

This archetype consumes the `opennvr.alerts.*` tree rather than
inference: relays to Home Assistant or a SIEM, notifiers, escalation
policies. It fires no alerts of its own by default — it forwards.

Subject shape (see `alerts.py`):

    opennvr.alerts.{source.kind}.{source.name}.{camera_id}
    opennvr.alerts.>                        every alert
    opennvr.alerts.app.>                    every app-emitted alert
    opennvr.alerts.*.*.cam-front-door       every alert about one camera
"""
from dataclasses import dataclass
from typing import Any

import httpx

from opennvr_app_sdk import (
    AlertSubscriber, AppManifest, BaseAppConfig, Param, alert_app, load_app_config,
)

MANIFEST = AppManifest(
    id="home-assistant-relay",
    name="Home Assistant Relay",
    version="1.0.0",
    category="integration",
    summary="Forwards OpenNVR alerts to Home Assistant as events.",
    subscribes="opennvr.alerts.>",
    params=[
        Param("hass_url", str, required=True, description="Home Assistant base URL."),
        Param("hass_token", str, required=True, description="Long-lived access token."),
        Param("min_severity", str, default="medium",
              suggestions=["low", "medium", "high", "critical"]),
    ],
)

_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass
class AppConfig(BaseAppConfig):
    hass_url: str = ""
    hass_token: str = ""
    min_severity: str = "medium"


class Relay(AlertSubscriber):
    manifest = MANIFEST

    def setup(self) -> None:
        self.floor = _RANK.get(self.cfg.min_severity, 1)
        self.client = httpx.Client(timeout=5.0, trust_env=False)
        self.forwarded = 0

    def on_alert(self, alert: dict[str, Any], subject: str) -> None:
        """Called once per decoded alert. `alert` is the §11.5 envelope
        as a dict; `subject` is the NATS subject it arrived on, which
        tells you who fired it without parsing the body."""
        if _RANK.get(alert.get("severity", "medium"), 1) < self.floor:
            return
        try:
            self.client.post(
                f"{self.cfg.hass_url.rstrip('/')}/api/events/opennvr_alert",
                headers={"Authorization": f"Bearer {self.cfg.hass_token}"},
                json={"title": alert.get("title"),
                      "camera_id": alert.get("camera_id"),
                      "severity": alert.get("severity"),
                      "source": subject},
            )
            self.forwarded += 1
        except httpx.HTTPError:
            # A dead relay target must never take the subscriber down.
            import logging
            logging.getLogger(MANIFEST.id).warning("relay failed for %s", subject)

    def state_snapshot(self) -> dict[str, Any]:
        """What `GET /state` returns — the catalog renders it."""
        return {"forwarded": self.forwarded, "min_severity": self.cfg.min_severity}


def main(argv: list[str] | None = None) -> int:
    return alert_app(
        Relay, load_config=lambda p: load_app_config(p, AppConfig)).run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
