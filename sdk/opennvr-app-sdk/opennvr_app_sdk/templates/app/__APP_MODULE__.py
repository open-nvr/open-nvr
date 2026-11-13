# Copyright (c) 2026 __APP_NAME__ authors
# SPDX-License-Identifier: __LICENSE__

"""
__APP_NAME__ — an OpenNVR app (scaffolded by ``opennvr-app new``; the
walkthrough is __DOCS__FIRST_DETECTOR.md).

The app subscribes to the platform's detection stream and fires alerts
on what it cares about — zero adapter GPU cost, it rides detection the
platform is already doing.

Everything below the rule is the SDK's: the NATS loop, per-message
isolation, alert dispatch, the contract server, registry
self-registration, live config, the CLI and signal handling. What's
left for YOU is THE RULE — the decorated function.

Run::

    opennvr-app dev                                    # a simulated camera
    python __APP_MODULE__.py --config config.yml
    python __APP_MODULE__.py --config config.yml --once   # one event then exit
"""
from __future__ import annotations

from opennvr_app_sdk import App

# ── The app ────────────────────────────────────────────────────────
#
# Your app's declarative identity. The catalog renders a card from it,
# builds a config form from the params, greys the app out unless an
# installed adapter advertises every ``requires_tasks`` name, and shows
# what it emits. An App Store index entry mirrors these fields.
app = App(
    "__APP_ID__",
    name="__APP_NAME__",
    version="0.1.0",
    # perimeter | analytics | vehicle | doorstep | forensics | integration
    category="analytics",
    summary="Fires an alert when __APP_NAME__ sees a watched object.",
    requires_tasks=["__TASK__"],
    # Selling it? pricing="paid", price_note="...", and add an
    # @app.on_license() handler — see APP_SURFACES.md §5b.
)

# Operator-settable knobs. Each becomes a field in config.yml, a form
# field in the catalog, and an attribute on ``event.config``. Refer to
# one from a rule filter with "$name" so the operator can tune it
# without a code change.
app.param("min_confidence", float, default=0.5,
          description="Ignore detections the model is less sure of than this.")
app.param("dwell_s", float, default=0.0,
          description="Seconds present before the alert fires. 0 = immediately.")

# A dashboard, declared: the catalog renders these over whatever
# ``app.store`` and @app.state() hold. No frontend required.
app.metric("alerted", label="Alerts fired")
app.log("recent", label="Recent sightings", limit=20)


@app.on_setup()
def prepare(config) -> None:
    """Runs once with the parsed config, before any event."""
    app.store["alerted"] = 0
    app.store["recent"] = []


# ── The rule ───────────────────────────────────────────────────────
#
# Called once per detection that passes the filters, with everything
# about that detection in one object: event.camera, event.label,
# event.confidence, event.zone, event.dwell_s, event.track_id,
# event.count("car"), event.config, event.nvr (the platform).
#
# Filters worth knowing (all optional):
#   zone="driveway"          only inside a zone the operator draws
#   dwell="$dwell_s"         only after N seconds inside the filters, once
#   cooldown=60              at most one alert a minute for the same object
#   camera="cam-1"           only on one camera
#   min_confidence="$min_confidence"
#
# Starter: alert on any sighting of a person. Replace it with yours.


@app.on_detection("person",
                  min_confidence="$min_confidence",
                  dwell="$dwell_s",
                  severity="medium",
                  # The alert kind, for the catalog listing. Declared
                  # rather than derived from the function name, so
                  # renaming the function is a safe refactor.
                  emits="__APP_ID__")
def person_seen(event) -> None:
    """THE RULE."""
    event.alert(
        f"{event.label.capitalize()} seen on {event.camera}",
        f"__APP_NAME__ observed a {event.label} on camera {event.camera}.",
    )
    app.store["alerted"] += 1
    app.store["recent"] = ([f"{event.label} on {event.camera}"]
                           + app.store["recent"])[:20]


def main(argv: list[str] | None = None) -> int:
    return app.run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
