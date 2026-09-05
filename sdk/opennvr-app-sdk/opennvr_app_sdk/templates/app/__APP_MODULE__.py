# Copyright (c) 2026 __APP_NAME__ authors
# SPDX-License-Identifier: __LICENSE__

"""
__APP_NAME__ — an OpenNVR Detector app (scaffolded by ``opennvr-app new``;
the walkthrough is __DOCS__FIRST_DETECTOR.md).

A Detector SUBSCRIBES to the platform's inference broadcast
(``opennvr.inference.*``) and consumes detection results another app is
already driving — adapter GPU is paid once and N subscribers fan out.
The SDK's :class:`~opennvr_app_sdk.Detector` owns the NATS loop,
per-message isolation, alert dispatch, the contract server, registry
self-registration, live config, the CLI and signal handling.

What's left for YOU is THE RULE — ``on_detections`` below.

Run::

    python __APP_MODULE__.py --config config.yml
    python __APP_MODULE__.py --config config.yml --once   # one event then exit
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from opennvr_app_sdk import (
    Alert, AlertType, AppManifest, BaseAppConfig, Detector, Param, app,
    load_app_config,
)

logger = logging.getLogger("__APP_ID__")


# ── Manifest ───────────────────────────────────────────────────────
#
# Your app's declarative identity. The catalog renders a config form
# from ``params``, greys the app out unless an installed adapter
# advertises every ``requires_tasks`` name, and shows ``emits``. An
# App Store index entry mirrors these fields.
MANIFEST = AppManifest(
    id="__APP_ID__",
    name="__APP_NAME__",
    version="0.1.0",
    category="analytics",  # perimeter | analytics | vehicle | doorstep | forensics | integration
    summary="Fires an alert when __APP_NAME__ sees a watched label.",
    requires_tasks=["__TASK__"],
    subscribes="opennvr.inference.>",
    params=[
        Param("watch_labels", list, default=["person"],
              description="Detection labels that count toward the rule."),
    ],
    emits=[AlertType("__APP_ID__", severity="medium")],
    # Selling it? pricing="paid", price_note="...", entitlement="license_key"
    # and override verify_license — see APP_SURFACES.md §5b.
)


# ── Config ─────────────────────────────────────────────────────────
#
# BaseAppConfig carries everything the SDK reads (NATS, alert fan-out,
# contract server, registry). Add your own fields; load_app_config
# fills both from one YAML file and validates the base keys.


@dataclass
class AppConfig(BaseAppConfig):
    watch_labels: list[str] = field(default_factory=lambda: ["person"])

    def __post_init__(self) -> None:
        self.watch_labels = [str(s).lower() for s in self.watch_labels]
        if not self.watch_labels:
            raise ValueError("'watch_labels' must list at least one label")


def load_config(path: str) -> AppConfig:
    cfg = load_app_config(path, AppConfig)
    if cfg.subject_pattern is None:
        cfg.subject_pattern = "opennvr.inference.>"
    return cfg


# ── The rule ───────────────────────────────────────────────────────


class __APP_CLASS__(Detector):
    manifest = MANIFEST

    def setup(self) -> None:
        """Optional — allocate state here (``self.keyed_state(ttl=...)``
        for dwell timers and cooldowns; ``OpenNVR()`` for the platform)."""

    def on_detections(
        self,
        camera_id: str,
        detections: list[dict[str, Any]],
        event: dict[str, Any],
    ) -> list[Alert]:
        """THE RULE. Called once per inference event with a ``camera_id``
        and a ``result.detections`` list; return the alerts to fire.

        Starter: alert on any sighting of a watched label. Replace with
        your predicate — a zone (``Zone``), a dwell timer
        (``keyed_state``), a confidence gate, a time window."""
        matches = [d for d in detections if isinstance(d, dict)
                   and str(d.get("label", "")).lower() in self.cfg.watch_labels]
        if not matches:
            return []
        label = str(matches[0].get("label", "")).lower()
        return [Alert(
            title=f"{label.capitalize()} seen on {camera_id}",
            description=f"__APP_NAME__ observed a {label} on camera {camera_id}.",
            camera_id=camera_id,
            severity="medium",
            correlation_id=str(event.get("correlation_id") or ""),
            evidence={"label": label, "adapter": event.get("adapter"),
                      "confidence": matches[0].get("confidence")},
            tags=["__APP_ID__", label],
        )]


def main(argv: list[str] | None = None) -> int:
    return app(__APP_CLASS__, load_config=load_config).run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
