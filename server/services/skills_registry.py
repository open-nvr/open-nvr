# Copyright (c) 2026 OpenNVR
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""RFC-0002 Phase 1: the skills registry — an index, never a broker.

One derivation, live at request time, over the four sources decision 2
names: the KAI-C adapter registry, the App Catalog's installed
manifests, the assignment table (Phase 2 — reported honestly as
not-yet-implemented), and health. Nothing here proxies an inference,
holds a queue, or owns state: delete this module and every producer
and consumer keeps working — you only lose the *view*.

Status vocabulary (RFC-0002 lifecycle):

* adapter-provided skills: ``available`` (a healthy provider exists),
  ``degraded`` (providers exist, none currently healthy — or the
  adapter registry itself is unreachable, which is reported as itself,
  never dressed up; issue #344's lesson), ``missing-dependency`` (no
  registered provider at all).
* app-provided skills: ``dormant`` (installed, not enabled),
  ``active`` (enabled and recently seen healthy), ``degraded``
  (enabled but unreachable or stale).

``active``/``dormant`` for adapter skills needs the camera-assignment
table and arrives with Phase 2; each adapter entry carries
``assignments: None`` until then rather than a guess.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

#: An enabled app whose last_seen is older than this is degraded even if
#: its stored status column still says "ok" — the column only updates on
#: re-registration; recency is the real signal.
APP_STALE_AFTER = timedelta(minutes=10)

#: Domain events per provider. Mirrors the KAI-C normaliser map
#: (kai-c/kai_c/domain_events.py) and the App SDK alert dispatcher —
#: both contracted in docs/EVENT_CONTRACTS.md. Keep the three in step:
#: growing the normaliser map without this view makes the registry lie
#: by omission.
ADAPTER_DOMAIN_EVENTS: dict[str, list[str]] = {
    "fast_plate_ocr": ["plate.recognized.v1"],
}
APP_DOMAIN_EVENTS: list[str] = ["alert.fired.v1"]


def _healthy_adapters(adapters_health: Optional[dict]) -> Optional[set[str]]:
    """Names of adapters KAI-C reports healthy; None = registry unknown."""
    if not isinstance(adapters_health, dict):
        return None
    return {
        name for name, entry in adapters_health.items()
        if isinstance(entry, dict) and entry.get("status") == "ok"
    }


def _tasks_by_adapter(adapters_caps: Optional[dict]) -> Optional[dict[str, set[str]]]:
    """adapter name -> lowercased advertised task set; None = caps unknown."""
    if not isinstance(adapters_caps, dict):
        return None
    out: dict[str, set[str]] = {}
    for name, entry in adapters_caps.items():
        caps = (entry or {}).get("capabilities") if isinstance(entry, dict) else None
        tasks = caps.get("tasks_advertised") if isinstance(caps, dict) else None
        out[name] = {
            t.lower() for t in tasks if isinstance(t, str)
        } if isinstance(tasks, list) else set()
    return out


def _adapter_skill(entry: Any, *,
                   registered: Optional[set[str]],
                   healthy: Optional[set[str]],
                   advertised: Optional[dict[str, set[str]]]) -> dict[str, Any]:
    """One taxonomy task (server/config/tasks.yml TaskEntry) → one entry."""
    names = {entry.task.lower()} | {a.lower() for a in entry.aliases}

    if advertised is not None:
        # Real signal: adapters that actually advertise this task.
        providers = sorted(
            name for name, tasks in advertised.items() if tasks & names)
    elif registered is not None:
        # Capabilities fetch failed but the registry answered: fall back
        # to the editorial mapping intersected with what is registered.
        providers = sorted(
            set(entry.suggested_adapters) & registered)
    else:
        providers = []

    if registered is None:
        status, reason = "degraded", "adapter registry unreachable"
    elif not providers:
        status, reason = "missing-dependency", (
            "no registered adapter provides this task")
    elif healthy and set(providers) & healthy:
        status, reason = "available", None
    else:
        status, reason = "degraded", "no healthy provider"

    publishes: list[str] = []
    for p in providers:
        publishes.extend(ADAPTER_DOMAIN_EVENTS.get(p, []))

    return {
        "id": entry.task,
        "name": entry.label,
        "provider": {"kind": "adapter", "providers": providers},
        "status": status,
        "reason": reason,
        "publishes": sorted(set(publishes)),
        "config": {},                     # adapters carry no operator params here
        "assignments": None,              # Phase 2: the camera-assignment table
        "agent_skill": entry.agent_skill,
        "suggested_adapters": list(entry.suggested_adapters),
        "suggested_apps": list(entry.suggested_apps),
    }


def _app_status(row: Any, now: datetime) -> tuple[str, Optional[str]]:
    if not row.enabled:
        return "dormant", "installed but not enabled"
    if row.status == "unreachable":
        return "degraded", "app unreachable at last contact"
    last_seen = row.last_seen
    if last_seen is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if last_seen is None or now - last_seen > APP_STALE_AFTER:
        return "degraded", "no recent contact from app"
    return "active", None


def _app_skill(row: Any, now: datetime) -> dict[str, Any]:
    status, reason = _app_status(row, now)
    manifest = row.manifest_json if isinstance(row.manifest_json, dict) else {}
    params = manifest.get("params") or []
    return {
        "id": f"app:{row.id}",
        "name": row.name,
        "provider": {"kind": "app", "providers": [row.id]},
        "status": status,
        "reason": reason,
        "publishes": list(APP_DOMAIN_EVENTS),
        "config": {
            "params": [
                p.get("name") for p in params
                if isinstance(p, dict) and p.get("name")
            ],
        },
        "assignments": None,              # Phase 2
        "agent_skill": None,
        "suggested_adapters": [],
        "suggested_apps": [],
    }


def derive_skills(
    *,
    tasks_registry: Iterable[Any],
    adapters_health: Optional[dict],
    adapters_caps: Optional[dict],
    apps_rows: Iterable[Any],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The whole view, from already-fetched inputs. Pure: no I/O, no
    state — the router owns fetching (and caching) the inputs.

    ``adapters_health`` / ``adapters_caps`` are the raw KAI-C
    ``/adapters/health`` adapters map and ``/capabilities`` adapters
    map, or None when that fetch failed — the distinction is preserved
    into per-skill status and the ``sources`` block instead of being
    smoothed over.
    """
    now = now or datetime.now(timezone.utc)
    registered = (
        set(adapters_health.keys()) if isinstance(adapters_health, dict)
        else None
    )
    healthy = _healthy_adapters(adapters_health)
    advertised = _tasks_by_adapter(adapters_caps)

    skills = [
        _adapter_skill(
            e, registered=registered, healthy=healthy, advertised=advertised)
        for e in tasks_registry
    ]
    skills.extend(_app_skill(row, now) for row in apps_rows)

    return {
        "skills": skills,
        "sources": {
            "adapter_registry": "ok" if registered is not None else "unreachable",
            "adapter_capabilities": "ok" if advertised is not None else "unavailable",
            "app_catalog": "ok",
            "assignments": {"implemented": False, "phase": 2},
        },
        "generated_at": now.replace(microsecond=0).isoformat(),
    }
