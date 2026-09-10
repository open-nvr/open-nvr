# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Roster and identity — which cameras, and who am I.

Demonstrates: `discover_cameras`, `cameras_for_skill`,
`filter_cameras_for_skill`, `full_frame_polygon`, `AppCredentials`,
`auth_headers`, `ContractMixin.credentials`,
`ContractMixin.register_with_opennvr`, `ContractMixin.on_config_update`.

An app never holds the site key. At registration core issues it a
credential of its own, scoped to the cameras an operator assigned it —
so "which cameras do I watch?" and "who am I?" are the same question,
and neither has a hard-coded answer.
"""
from typing import Any

from opennvr_app_sdk import (
    AppCredentials, auth_headers, cameras_for_skill, discover_cameras,
    filter_cameras_for_skill, full_frame_polygon,
)


def my_cameras(opennvr_url: str) -> list[dict[str, Any]]:
    """The roster, as core scopes it for this app's key. Never raises —
    an app that refuses to start because discovery blipped is worse
    than an app that starts with an empty roster and picks it up on the
    next config poll."""
    return discover_cameras(opennvr_url)


def cameras_doing_lpr(opennvr_url: str) -> list[str]:
    """Operators assign capabilities per camera ('camera 1 does LPR,
    2–3 count people'). Respect that rather than watching everything:
    an EMPTY list means the operator has not pointed this skill at
    anything yet, which is an instruction, not an error."""
    return cameras_for_skill(opennvr_url, "license-plate-recognition")


def split_by_skill(cameras: list[dict[str, Any]]) -> dict[str, list[str]]:
    """The same filter applied to a roster you already have."""
    return {
        skill: filter_cameras_for_skill(cameras, skill) or []
        for skill in ("license-plate-recognition", "occupancy-counting")
    }


def default_zone_for(camera_id: str) -> list[list[int]]:
    """A camera with no zone drawn watches the whole frame. Use this
    rather than special-casing 'no zone' everywhere in the rule."""
    return full_frame_polygon()


def who_am_i(explicit_key: str | None = None) -> dict[str, str]:
    """The credential resolution order, once: the app's OWN key issued
    at registration, else an explicit value, else the
    OPENNVR_INTERNAL_API_KEY the installer sets. `auth_headers` turns
    whichever it found into the headers core accepts — both the
    internal-key header and a bearer, so the same dict works against
    every route."""
    creds = AppCredentials(explicit_key)
    return auth_headers(creds.token())


class RosterAware:
    """Inside an archetype, the roster is live: core re-delivers config
    on a poll, so an app that reads it in `on_config_update` follows the
    operator without a restart."""

    def on_config_update(self, config: dict[str, Any]) -> None:
        self.watching = list(config.get("cameras") or [])
        self.zones = config.get("zones") or {}
        # Idempotent on purpose — the first call usually restates what
        # boot config already applied.
