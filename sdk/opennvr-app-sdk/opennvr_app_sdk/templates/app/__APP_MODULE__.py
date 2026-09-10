# Copyright (c) 2026 __APP_NAME__ authors
# SPDX-License-Identifier: __LICENSE__

"""
__APP_NAME__ — an OpenNVR app (scaffolded by ``opennvr-app new``; the
walkthrough is __DOCS__FIRST_DETECTOR.md).

The app subscribes to the platform's inference broadcast
(``opennvr.inference.*``) and consumes detection results another app is
already driving — adapter GPU is paid once and N apps fan out from one
inference stream.

Everything below the rule is the SDK's: the NATS loop, per-message
isolation, alert dispatch, the contract server, registry
self-registration, live config, the CLI and signal handling. What's
left for YOU is THE RULE — the decorated function.

Run::

    python __APP_MODULE__.py --config config.yml
    python __APP_MODULE__.py --config config.yml --once   # one event then exit
"""
from __future__ import annotations

from opennvr_app_sdk import App

# ── The app ────────────────────────────────────────────────────────
#
# This is your app's declarative identity. The catalog renders a card
# from it, builds a config form from the params, greys the app out
# unless an installed adapter advertises every ``requires_tasks`` name,
# and lists what it emits. An App Store index entry mirrors these
# fields.
app = App(
    "__APP_ID__",
    name="__APP_NAME__",
    version="0.1.0",
    # perimeter | analytics | vehicle | doorstep | forensics | integration
    category="analytics",
    summary="Fires an alert when __APP_NAME__ sees a watched object.",
    requires_tasks=["__TASK__"],
    # Selling it? pricing="paid", price_note="...", entitlement="license_key"
    # — see APP_SURFACES.md §5b.
)

# Operator-settable knobs. Each becomes a field in config.yml, a form
# field in the catalog, and an attribute on ``event.config``.
app.param(
    "min_confidence", float, default=0.5,
    description="Ignore detections the model is less sure of than this.",
)

# The alert kinds this app can fire, so the catalog can document and
# route them. Omit this and one is derived per rule, named after the
# function; declare it when the listing needs a stable name.
app.emits("__APP_ID__", severity="medium")


# ── The rule ───────────────────────────────────────────────────────
#
# Called once per detection that passes the filters, with everything
# about that detection in one object: ``event.camera``, ``event.label``,
# ``event.confidence``, ``event.zone``, ``event.dwell_s``,
# ``event.track_id``, ``event.count("car")``, ``event.config``.
#
# Filters worth knowing (all optional):
#   zone="driveway"    only inside a zone the operator drew
#   dwell=30           only after 30s of continuous presence, once
#   cooldown=60        at most one alert a minute for the same object
#   camera="cam-1"     only on one camera
#
# Starter: alert on any sighting of a person. Replace it with yours.


@app.on_detection("person", severity="medium")
def person_seen(event):
    """THE RULE."""
    if event.confidence < event.config.min_confidence:
        return
    event.alert(
        f"{event.label.capitalize()} seen on {event.camera}",
        f"__APP_NAME__ observed a {event.label} on camera {event.camera}.",
    )


def main(argv: list[str] | None = None) -> int:
    return app.run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
