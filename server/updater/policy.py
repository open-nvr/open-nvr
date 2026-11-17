# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Automatic updates: is it time to install the update the host found?

Pure, so every rule is tested on its own (tests/test_updater_policy.py).
The host runner asks once a minute (``python -m updater due``); the policy
is what the app saved (data/update-requests/policy.env). Until something is
saved the policy is the default: ``auto``, fixes and new features (never a
major version), every night between 02:00 and 04:00 site time.

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

from updater.manifest import Manifest, parse_version, upgrade_kind

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_DELAY_H = 72
#: Apps spread over a shorter delay than releases: an app update touches one
#: container with its own backup, and a release can wait three days.
APP_MAX_DELAY_H = 12
MODES = ("off", "notify", "manual", "auto")
_SCOPES = {"patch": {"patch"}, "minor": {"patch", "minor"}}


@dataclass(frozen=True)
class Policy:
    mode: str = "auto"
    auto_scope: str = "minor"
    window_days: tuple[str, ...] = DAYS
    window_start: str = "02:00"
    window_end: str = "04:00"
    #: App updates (updater/apps.py). Empty: the same as the release mode
    #: and scope above. ``app_modes`` holds per-app modes, (id, mode) pairs.
    apps_mode: str = ""
    apps_auto_scope: str = ""
    app_modes: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_env(cls, values: dict[str, str]) -> Policy:
        days = tuple(d for d in values.get("WINDOW_DAYS", "").split(",") if d in DAYS) or DAYS
        per_app = tuple(sorted(
            (key[4:-5].lower().replace("_", "-"), value)
            for key, value in values.items()
            if key.startswith("APP_") and key.endswith("_MODE") and len(key) > 9 and value in MODES
        ))
        return cls(mode=values.get("MODE", "auto"), auto_scope=values.get("AUTO_SCOPE", "minor"),
                   window_days=days, window_start=values.get("WINDOW_START", "02:00"),
                   window_end=values.get("WINDOW_END", "04:00"),
                   apps_mode=values.get("APPS_MODE", "") if values.get("APPS_MODE", "") in MODES else "",
                   apps_auto_scope=values.get("APPS_AUTO_SCOPE", "") if values.get("APPS_AUTO_SCOPE") in _SCOPES else "",
                   app_modes=per_app)

    def app_mode(self, app_id: str) -> str:
        """This app's mode: its own, else the apps mode, else the release mode."""
        return dict(self.app_modes).get(app_id) or self.apps_mode or self.mode

    def app_scope(self) -> str:
        return self.apps_auto_scope or self.auto_scope


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
    allowed = _SCOPES.get(policy.auto_scope, {"patch"})
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


# ── apps ─────────────────────────────────────────────────────────────


def app_delay(seed: str, app_id: str, version: str) -> timedelta:
    """This site's wait for this app release: stable for the triple, 0-12 h."""
    digest = hashlib.sha256(f"{seed}:app:{app_id}:{version}".encode()).digest()
    return timedelta(minutes=int.from_bytes(digest[:4], "big") % (APP_MAX_DELAY_H * 60))


def choose_offer(policy: Policy, report, *, now: bool = False):
    """(the offer to install, "") or (None, why not). ``now`` is the
    operator's Update now: the newest release, whatever the step. A site on a
    withdrawn version is moved to the newest release too, so it gets off it."""
    offers = sorted(report.offers, key=lambda o: parse_version(o.version), reverse=True)
    if not offers:
        return None, report.reason or "no newer release"
    if now or report.installed_revoked:
        return offers[0], ""
    allowed = _SCOPES.get(policy.app_scope(), {"patch"})
    for offer in offers:
        if offer.kind in allowed:
            return offer, ""
    return None, (f"{offers[0].version} is a {offers[0].kind} update, installed by hand "
                  f"(policy: {policy.app_scope()} updates only)")


def due_app(policy: Policy, app_id: str, offer, *, verified: tuple[bool, str] | None,
            local_now: datetime, utc_now: datetime, first_seen: datetime | None, seed: str,
            now: bool = False) -> tuple[bool, str]:
    """(install this app release now?, why or why not). ``now``: the
    operator asked (Update now), so no window and no delay, and a release
    that can't be rolled back automatically is theirs to take. The signature
    is never waived."""
    mode = policy.app_mode(app_id)
    if mode == "off":
        return False, "app updates are off"
    if verified is None:
        return False, "its signature has not been checked yet"
    if not verified[0]:
        return False, f"its signature did not check out: {verified[1]}"
    if now:
        return (True, "asked for") if mode in ("manual", "auto") else (False, f"app updates are {mode}")
    if mode != "auto":
        return False, f"app updates are {mode}, not automatic"
    if offer.rollback == "manual":
        return False, "this release can't be rolled back automatically; install it by hand"
    if not in_window(policy, local_now):
        return False, "outside the maintenance window"
    start = first_seen or utc_now
    wait = app_delay(seed, app_id, offer.version)
    if utc_now < start + wait:
        return False, f"waiting until {(start + wait).strftime('%Y-%m-%d %H:%M')} UTC (this site's delay)"
    return True, "due"
