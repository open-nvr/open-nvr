# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""RFC-0002 Phase 1: the skills registry derivation.

``derive_skills`` is pure — inputs in, view out — so the whole status
matrix is testable without KAI-C, a DB, or FastAPI. What these tests
pin is the honesty rules: status from real signals (a healthy provider
actually advertising the task), an unreachable source reported as
itself rather than smoothed into a guess (issue #344's lesson), and
the not-yet-implemented assignment source declared, not faked.
"""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# routers.ai_models (imported by the taxonomy round-trip test) pulls in
# core.config, whose Settings() needs these — same bootstrap as the
# other suites.
os.environ.setdefault("DATABASE_URL", "sqlite:///./_skills_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from services.skills_registry import (  # noqa: E402
    APP_STALE_AFTER,
    derive_skills,
)

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class Task:
    """Duck-typed stand-in for routers.ai_models.TaskEntry."""
    task: str
    label: str = ""
    agent_skill: str | None = None
    aliases: list[str] = field(default_factory=list)
    suggested_adapters: list[str] = field(default_factory=list)
    suggested_apps: list[str] = field(default_factory=list)


def _app_row(app_id="license-plate-recognition", *, enabled=True, status="ok",
             last_seen=NOW - timedelta(seconds=30), manifest=None):
    return SimpleNamespace(
        id=app_id, name=app_id, enabled=enabled, status=status,
        last_seen=last_seen,
        manifest_json=manifest if manifest is not None else {
            "params": [{"name": "poll_interval_seconds"}, {"name": "watchlist"}],
        },
    )


def _skill(view, skill_id):
    matches = [s for s in view["skills"] if s["id"] == skill_id]
    assert matches, f"{skill_id} not in view: {[s['id'] for s in view['skills']]}"
    return matches[0]


HEALTH = {
    "yolov8": {"status": "ok", "url": "http://yolov8:9002"},
    "fast_plate_ocr": {"status": "error", "url": "http://lpr:9004",
                       "message": "connect timeout"},
}
CAPS = {
    "yolov8": {"capabilities": {"tasks_advertised": ["object_detection"]}},
    "fast_plate_ocr": {"capabilities": {
        "tasks_advertised": ["license_plate_recognition"]}},
}


def test_available_needs_a_healthy_advertising_provider():
    view = derive_skills(
        tasks_registry=[Task("object_detection")],
        adapters_health=HEALTH, adapters_caps=CAPS, apps_rows=[], now=NOW)
    s = _skill(view, "object_detection")
    assert s["status"] == "available" and s["reason"] is None
    assert s["provider"] == {"kind": "adapter", "providers": ["yolov8"]}


def test_unhealthy_provider_is_degraded_not_available():
    # fast_plate_ocr is registered and advertises the task — but its
    # health probe failed. Process-up is not the signal (issue #344).
    view = derive_skills(
        tasks_registry=[Task("license_plate_recognition")],
        adapters_health=HEALTH, adapters_caps=CAPS, apps_rows=[], now=NOW)
    s = _skill(view, "license_plate_recognition")
    assert s["status"] == "degraded"
    assert s["reason"] == "no healthy provider"
    # And the normaliser mirror still names what a provider would publish.
    assert s["publishes"] == ["plate.recognized.v1"]


def test_no_provider_at_all_is_missing_dependency():
    view = derive_skills(
        tasks_registry=[Task("face_recognition",
                             suggested_adapters=["insightface"],
                             suggested_apps=["smart-doorbell"])],
        adapters_health=HEALTH, adapters_caps=CAPS, apps_rows=[], now=NOW)
    s = _skill(view, "face_recognition")
    assert s["status"] == "missing-dependency"
    # The on-ramp fields ride the registry (the agent's private
    # suggested_apps derivation becomes a registry field — RFC Phase 1).
    assert s["suggested_adapters"] == ["insightface"]
    assert s["suggested_apps"] == ["smart-doorbell"]


def test_alias_advertisement_counts_as_providing():
    caps = {"yolov8": {"capabilities": {"tasks_advertised": ["Object-Det"]}}}
    view = derive_skills(
        tasks_registry=[Task("object_detection", aliases=["object-det"])],
        adapters_health={"yolov8": {"status": "ok"}},
        adapters_caps=caps, apps_rows=[], now=NOW)
    assert _skill(view, "object_detection")["status"] == "available"


def test_caps_fetch_down_falls_back_to_editorial_mapping():
    view = derive_skills(
        tasks_registry=[Task("object_detection",
                             suggested_adapters=["yolov8"])],
        adapters_health={"yolov8": {"status": "ok"}},
        adapters_caps=None, apps_rows=[], now=NOW)
    s = _skill(view, "object_detection")
    assert s["status"] == "available"
    assert s["provider"]["providers"] == ["yolov8"]
    assert view["sources"]["adapter_capabilities"] == "unavailable"


def test_registry_unreachable_is_reported_not_guessed():
    view = derive_skills(
        tasks_registry=[Task("object_detection")],
        adapters_health=None, adapters_caps=None, apps_rows=[], now=NOW)
    s = _skill(view, "object_detection")
    assert s["status"] == "degraded"
    assert s["reason"] == "adapter registry unreachable"
    assert view["sources"]["adapter_registry"] == "unreachable"


def test_app_lifecycle_dormant_active_degraded():
    rows = [
        _app_row("a-dormant", enabled=False),
        _app_row("a-active"),
        _app_row("a-unreachable", status="unreachable"),
        _app_row("a-stale",
                 last_seen=NOW - APP_STALE_AFTER - timedelta(seconds=1)),
        _app_row("a-never-seen", last_seen=None),
    ]
    view = derive_skills(tasks_registry=[], adapters_health={},
                         adapters_caps={}, apps_rows=rows, now=NOW)
    assert _skill(view, "app:a-dormant")["status"] == "dormant"
    assert _skill(view, "app:a-active")["status"] == "active"
    assert _skill(view, "app:a-unreachable")["status"] == "degraded"
    assert _skill(view, "app:a-stale")["status"] == "degraded"
    assert _skill(view, "app:a-never-seen")["status"] == "degraded"


def test_app_entry_shape():
    view = derive_skills(tasks_registry=[], adapters_health={},
                         adapters_caps={}, apps_rows=[_app_row()], now=NOW)
    s = _skill(view, "app:license-plate-recognition")
    assert s["provider"] == {"kind": "app",
                             "providers": ["license-plate-recognition"]}
    assert s["publishes"] == ["alert.fired.v1"]
    assert s["config"]["params"] == ["poll_interval_seconds", "watchlist"]


def test_naive_last_seen_is_treated_as_utc():
    # SQLite (and some drivers) hand back naive datetimes; a naive-vs-
    # aware comparison must not crash the whole view.
    row = _app_row("a-naive",
                   last_seen=(NOW - timedelta(seconds=30)).replace(tzinfo=None))
    view = derive_skills(tasks_registry=[], adapters_health={},
                         adapters_caps={}, apps_rows=[row], now=NOW)
    assert _skill(view, "app:a-naive")["status"] == "active"


def test_assignments_source_is_declared_not_faked():
    view = derive_skills(tasks_registry=[], adapters_health={},
                         adapters_caps={}, apps_rows=[], now=NOW)
    assert view["sources"]["assignments"] == {"implemented": False, "phase": 2}


def test_real_tasks_yml_derives_without_error():
    # The wired inputs: the actual curated taxonomy file must flow
    # through the derivation (guards a tasks.yml field rename breaking
    # the endpoint at runtime rather than in CI).
    from routers.ai_models import _load_tasks_registry
    view = derive_skills(
        tasks_registry=_load_tasks_registry(),
        adapters_health=HEALTH, adapters_caps=CAPS, apps_rows=[], now=NOW)
    assert len(view["skills"]) >= 5
    assert all(s["status"] in {
        "available", "degraded", "missing-dependency"} for s in view["skills"])
