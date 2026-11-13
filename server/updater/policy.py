# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Automatic updates: is it time to install the update the host found?

Pure, so every rule is tested on its own (tests/test_updater_policy.py).
The host runner asks once a minute (``python -m updater due``); the policy
is what the app saved (data/update-requests/policy.env).

An automatic update needs all of: mode ``auto``; a stable release; nothing
the operator must do first (manual steps) and nothing that cannot be rolled
back automatically (state migrations); a step no bigger than the policy
allows (patch, or minor); the site-local maintenance window; and, unless the
release is urgent, this site's random delay of up to 72 hours since the
update was first seen, so a bad release does not reach every site at once.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from updater.manifest import Manifest, upgrade_kind

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_DELAY_H = 72


@dataclass(frozen=True)
class Policy:
    mode: str = "off"
    auto_scope: str = "patch"
    window_days: tuple[str, ...] = ("sun",)
    window_start: str = "02:00"
    window_end: str = "04:00"

    @classmethod
    def from_env(cls, values: dict[str, str]) -> Policy:
        days = tuple(d for d in values.get("WINDOW_DAYS", "sun").split(",") if d in DAYS) or ("sun",)
        return cls(mode=values.get("MODE", "off"), auto_scope=values.get("AUTO_SCOPE", "patch"),
                   window_days=days, window_start=values.get("WINDOW_START", "02:00"),
                   window_end=values.get("WINDOW_END", "04:00"))


def in_window(policy: Policy, local: datetime) -> bool:
    """Is *local* (site time) inside the window? A window that ends before it
    starts runs past midnight, and belongs to the day it starts on."""
    def minutes(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    start, end = minutes(policy.window_start), minutes(policy.window_end)
    now = local.hour * 60 + local.minute
    today = DAYS[local.weekday()]
    yesterday = DAYS[(local.weekday() - 1) % 7]
    if start < end:
        return today in policy.window_days and start <= now < end
    # Over midnight: the evening part today, or the morning part of a window
    # that began yesterday.
    return (today in policy.window_days and now >= start) or (yesterday in policy.window_days and now < end)


def site_delay(seed: str, version: str) -> timedelta:
    """This site's wait for this release: stable for the pair, spread over 0-72 h."""
    digest = hashlib.sha256(f"{seed}:{version}".encode()).digest()
    return timedelta(minutes=int.from_bytes(digest[:4], "big") % (MAX_DELAY_H * 60))


def due(policy: Policy, candidate: Manifest, installed_version: str, *, local_now: datetime,
        utc_now: datetime, first_seen: datetime | None, seed: str) -> tuple[bool, str]:
    """(install now?, why or why not)."""
    if policy.mode != "auto":
        return False, f"updates are {policy.mode}, not automatic"
    if candidate.channel != "stable":
        return False, "only releases are installed automatically"
    if candidate.manual_steps:
        return False, "the release needs steps on the host first"
    if candidate.state_migrations:
        return False, "the release cannot be rolled back automatically"
    kind = upgrade_kind(installed_version, candidate.version)
    allowed = {"patch": {"patch"}, "minor": {"patch", "minor"}}.get(policy.auto_scope, {"patch"})
    if kind not in allowed:
        return False, f"a {kind} update is installed by hand (policy: {policy.auto_scope} updates only)"
    if not in_window(policy, local_now):
        return False, "outside the maintenance window"
    if not candidate.urgent:
        start = first_seen or utc_now
        wait = site_delay(seed, candidate.version)
        if utc_now < start + wait:
            return False, f"waiting until {(start + wait).strftime('%Y-%m-%d %H:%M')} UTC (this site's delay)"
    return True, "due"
