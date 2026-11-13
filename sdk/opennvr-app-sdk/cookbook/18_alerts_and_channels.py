# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Alerts — the envelope, the channels, and how to add your own.

Demonstrates: `Alert`, `AlertSource`, `alert_subject`,
`DEFAULT_ALERT_SUBJECT_PREFIX`, `AlertDispatcher`, `build_dispatcher`,
`StdoutChannel`, `WebhookChannel`, `NatsAlertChannel`, `AlertChannel`,
`set_default_source`.

Alerts are what reach a human. The envelope matches §11.5 so every
consumer — the operator inbox, a SIEM bridge, another app — parses one
shape. Delivery is a list of channels: stdout always, plus whatever the
operator configured, plus anything you write.
"""
from opennvr_app_sdk import (
    Alert, AlertDispatcher, AlertSource, StdoutChannel, WebhookChannel,
    alert_subject, build_dispatcher,
)


def a_well_formed_alert(camera_id: str, correlation_id: str) -> Alert:
    """`alert_id` and `fired_at` fill themselves in; `source` comes from
    the app's identity, which the archetype sets around every handler
    call. What is worth your attention is the rest."""
    return Alert(
        title=f"Loitering on {camera_id}",          # one line, in the inbox
        description="A person has been by the cars for 40 seconds.",
        camera_id=camera_id,
        severity="high",                            # low|medium|high|critical
        # Thread the platform's correlation id so the alert joins the
        # inference, the audit line and the evidence frame in one chain.
        correlation_id=correlation_id,
        # Anything a consumer might route or filter on. Keep it flat and
        # JSON-serializable.
        evidence={"dwell_s": 40.2, "label": "person", "confidence": 0.91},
        # Cheap, greppable routing hints.
        tags=["loitering", "night"],
    )


def where_it_lands(alert: Alert) -> str:
    """The subject is derived from the alert's own source block, so a
    subscriber can filter without parsing the body:

        opennvr.alerts.>                       every alert
        opennvr.alerts.app.>                   every app-emitted alert
        opennvr.alerts.*.*.cam-front-door      one camera
        opennvr.alerts.app.loitering.>         one app
    """
    return alert_subject(alert)
    # -> "opennvr.alerts.app.loitering-detection.cam-front-door"


def the_dispatcher_apps_get_for_free(cfg) -> AlertDispatcher:
    """What every runner builds from config: stdout always on, webhook
    and NATS opt-in. A dead webhook is logged, never raised — alert
    delivery must not take a long-lived app down."""
    return build_dispatcher(
        webhook_url=cfg.webhook_url,
        nats_alerts_url=cfg.nats_alerts_url,
        nats_alerts_token=cfg.nats_alerts_token,
    )


class PagerChannel:
    """Your own channel: anything with `name` and `send(alert) -> bool`.
    Swallow your own failures — the contract is that one dead channel
    never stops the others."""

    name = "pager"

    def __init__(self, url: str) -> None:
        self.url = url

    def send(self, alert: Alert) -> bool:
        if alert.severity not in ("high", "critical"):
            return True                       # not an error, just not paged
        try:
            import httpx
            httpx.post(self.url, json=alert.to_wire(), timeout=5.0,
                       trust_env=False)
            return True
        except Exception:
            return False


def custom_fan_out(webhook: str, pager: str) -> AlertDispatcher:
    return AlertDispatcher(channels=[
        StdoutChannel(), WebhookChannel(webhook), PagerChannel(pager),
    ])


def emitting_as_someone_else() -> Alert:
    """`source` identifies who fired the alert. The archetypes set it
    from `manifest.id` per handler call, so several apps can share one
    process without clobbering each other — pass it explicitly only when
    you are relaying on another party's behalf."""
    return Alert(title="Relayed", description="From an upstream system.",
                 camera_id="cam-1",
                 source=AlertSource(kind="app", name="siem-bridge",
                                    version="1.0.0"))
