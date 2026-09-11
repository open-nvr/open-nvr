#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Validate server/config/adapters_index.yml.

A malformed entry degrades to a missing card at runtime rather than a
500 — which is the right behaviour for a catalog, and the wrong thing to
discover in production. This is what makes it fail in CI instead.

Beyond the shape, it checks the things a listing can get wrong in a way
nobody notices until an operator installs it:

* an adapter advertising a task no app has a convention for (a typo in
  `object_detecton` is undiscoverable, because nothing asks for it);
* a task that no app requires AND no adapter serves, in either
  direction — a capability gap or a dead listing;
* a null fingerprint, which silently exempts the adapter from KAI-C's
  drift detection;
* an unreviewed TODO left in by `opennvr-adapter listing`.

    python scripts/validate_adapters_index.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = REPO_ROOT / "server" / "config" / "adapters_index.yml"
APPS_INDEX = REPO_ROOT / "server" / "config" / "apps_index.yml"
TASKS = REPO_ROOT / "server" / "config" / "tasks.yml"

REQUIRED = ("id", "name", "summary", "version", "image", "tasks_advertised")
# Kept in step with ``routers.adapters_catalog.KNOWN_TIERS`` by
# ``test_known_tiers_match_the_validator`` — this script stays free of
# the server's dependencies so CI can run it on its own.
KNOWN_TIERS = {"first_party", "community"}


def known_tasks() -> set[str]:
    """Every task the platform has a convention for, plus their aliases."""
    try:
        raw = yaml.safe_load(TASKS.read_text()) or []
    except (OSError, yaml.YAMLError):
        return set()
    rows = raw if isinstance(raw, list) else raw.get("tasks", [])
    names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("task"):
            names.add(str(row["task"]))
        for alias in row.get("aliases") or []:
            names.add(str(alias))
    return names


def tasks_apps_require() -> set[str]:
    try:
        raw = yaml.safe_load(APPS_INDEX.read_text()) or []
    except (OSError, yaml.YAMLError):
        return set()
    return {str(task) for entry in raw if isinstance(entry, dict)
            for task in (entry.get("requires_tasks") or [])}


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    try:
        entries = yaml.safe_load(INDEX.read_text()) or []
    except (OSError, yaml.YAMLError) as exc:
        print(f"error: {INDEX.name}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(entries, list):
        print(f"error: {INDEX.name}: expected a list of entries", file=sys.stderr)
        return 1

    seen_ids: set[str] = set()
    advertised: set[str] = set()
    catalogue_tasks = known_tasks()

    for index, entry in enumerate(entries):
        where = f"entry {index}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: not a mapping")
            continue
        where = f"{entry.get('id', where)!r}"

        for field in REQUIRED:
            if not entry.get(field):
                errors.append(f"{where}: {field} is required")

        adapter_id = str(entry.get("id") or "")
        if adapter_id in seen_ids:
            errors.append(f"{where}: duplicate id")
        seen_ids.add(adapter_id)

        tier = entry.get("tier", "community")
        if tier not in KNOWN_TIERS:
            errors.append(f"{where}: tier {tier!r} must be one of {sorted(KNOWN_TIERS)}")

        tasks = entry.get("tasks_advertised") or []
        if not tasks:
            errors.append(
                f"{where}: advertises no task — KAI-C never routes work to it, "
                f"so the listing is undiscoverable")
        for task in tasks:
            advertised.add(str(task))
            if catalogue_tasks and str(task) not in catalogue_tasks:
                errors.append(
                    f"{where}: task {task!r} is not a known convention "
                    f"(server/config/tasks.yml) — a typo here is invisible, "
                    f"because no app asks for it")

        for field, value in _walk(entry):
            if isinstance(value, str) and value.strip() == "TODO":
                errors.append(
                    f"{where}: {field} is still TODO — `opennvr-adapter listing` "
                    f"leaves those for the author to fill in")

        model = entry.get("model") or {}
        if isinstance(model, dict) and not model.get("fingerprinted", False):
            warnings.append(
                f"{where}: the adapter computes no model fingerprint, so KAI-C "
                f"skips drift detection — a model swapped underneath a "
                f"deployment goes unnoticed")

        permissions = entry.get("permissions") or {}
        if isinstance(permissions, dict) and permissions.get("network_egress"):
            declared = [str(h) for h in permissions["network_egress"]]
            summary = str(entry.get("summary", ""))
            # EVERY host has to be disclosed, not just the first one: an
            # adapter listing api.vendor.com plus a telemetry endpoint
            # was passing on the strength of the one it mentioned.
            unmentioned = [h for h in declared if h not in summary]
            if unmentioned:
                warnings.append(
                    f"{where}: declares egress to {', '.join(unmentioned)} "
                    f"without saying so in the summary; the operator is the "
                    f"one being asked to allow it")

    for required_task in sorted(tasks_apps_require() - advertised):
        warnings.append(
            f"task {required_task!r} is required by a listed app but no listed "
            f"adapter advertises it — that app greys out on a fresh install")

    for line in warnings:
        print(f"warning: {line}")
    for line in errors:
        print(f"error: {line}", file=sys.stderr)

    if errors:
        print(f"\n{len(errors)} error(s) in {INDEX.name}", file=sys.stderr)
        return 1
    print(f"\nOK — {len(entries)} adapter(s), {len(advertised)} task(s), "
          f"{len(warnings)} warning(s)")
    return 0


def _walk(value, prefix: str = ""):
    """Every (dotted-path, value) leaf, so a TODO cannot hide in a
    nested block."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        for position, child in enumerate(value):
            yield from _walk(child, f"{prefix}[{position}]")
    else:
        yield prefix, value


if __name__ == "__main__":
    raise SystemExit(main())
