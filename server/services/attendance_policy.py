# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The rules attendance is taken by: the site's, and a group's overrides.

A session is cut into buckets of ``bucket_minutes``. The app looks at each
camera every ``attempt_interval_s``; a bucket in which at least one frame
was looked at counts, and a person is PRESENT when they were recognised in
at least ``present_ratio`` of the counted buckets — LATE if the first of
those sightings came after ``late_grace_minutes``. ``match_threshold`` is
the face similarity a sighting needs.

Stored in ``security_settings`` under ``attendance_policy`` (site) and on
``attendance_groups.policy`` (overrides, any subset of the keys). Unknown
keys and out-of-range values are refused, never stored: a policy that says
something the finalizer does not do is worse than no policy.
"""

from __future__ import annotations

import json
from typing import Any

SITE_POLICY_KEY = "attendance_policy"

#: The defaults, and the measured reason for the interval: a 50-face frame
#: costs ~10 s of CPU with buffalo_l, so a camera every 150 s is ~7 % of one
#: adapter and still gives every 5-minute bucket two chances.
DEFAULT_POLICY: dict[str, Any] = {
    "bucket_minutes": 5,
    "present_ratio": 0.6,
    "late_grace_minutes": 10,
    "match_threshold": 0.5,
    "attempt_interval_s": 150,
    "finalize_grace_minutes": 5,
}

#: key -> (type, low, high), inclusive. present_ratio's low bound is
#: exclusive in spirit (0 would mark everyone present); 0.05 says so.
_BOUNDS: dict[str, tuple[type, float, float]] = {
    "bucket_minutes": (int, 1, 60),
    "present_ratio": (float, 0.05, 1.0),
    "late_grace_minutes": (int, 0, 240),
    "match_threshold": (float, 0.3, 0.95),
    "attempt_interval_s": (int, 30, 1800),
    # At least 1: a look sent right at the bell must reach core before
    # the verdict is drawn, or it is linked and never counted.
    "finalize_grace_minutes": (int, 1, 120),
}


def validate_overrides(values: dict[str, Any] | None) -> dict[str, Any]:
    """The given keys, type-checked and range-checked. Raises ValueError
    naming the first thing wrong."""
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ValueError("policy must be an object")
    out: dict[str, Any] = {}
    for key, raw in values.items():
        if key not in _BOUNDS:
            raise ValueError(f"unknown policy key {key!r}")
        kind, low, high = _BOUNDS[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{key} must be a number")
        if kind is int and float(raw) != int(raw):
            raise ValueError(f"{key} must be a whole number")
        value = kind(raw)
        if not (low <= value <= high):
            raise ValueError(f"{key} must be between {low} and {high}")
        out[key] = value
    return out


def check_consistent(policy: dict[str, Any]) -> None:
    """Rules across keys, on a complete (effective) policy."""
    bucket_s = policy["bucket_minutes"] * 60
    if policy["attempt_interval_s"] * 2 > bucket_s:
        raise ValueError(
            "attempt_interval_s must be at most half of bucket_minutes, so every "
            f"bucket gets two chances (bucket {bucket_s} s, interval "
            f"{policy['attempt_interval_s']} s)")


def site_policy(db) -> dict[str, Any]:
    """The site policy: defaults overlaid with what was saved. A saved row
    that no longer validates (a key removed in a later version) is
    ignored key by key rather than failing every session."""
    from models import SecuritySetting

    row = db.query(SecuritySetting).filter(SecuritySetting.key == SITE_POLICY_KEY).first()
    policy = dict(DEFAULT_POLICY)
    if row is None:
        return policy
    try:
        saved = json.loads(row.json_value)
    except (TypeError, ValueError):
        return policy
    if isinstance(saved, dict):
        for key, value in saved.items():
            try:
                policy.update(validate_overrides({key: value}))
            except ValueError:
                continue
    return policy


def effective_policy(site: dict[str, Any], group_overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Site policy with the group's overrides on top."""
    policy = dict(site)
    if group_overrides:
        for key, value in group_overrides.items():
            try:
                policy.update(validate_overrides({key: value}))
            except ValueError:
                continue
    return policy
