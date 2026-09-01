# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""RFC-0002 Phase 2: the assignment table — union semantics + projection.

The behaviours pinned here ARE the phase's acceptance criteria:

* a skill runs on the UNION of its consumers' cameras; releasing one
  consumer's claim shrinks the union, and releasing the last one makes
  the skill dormant in the registry (gap 7);
* ``Camera.assignments`` — what Tier-0 reconcile, the SDK's
  ``cameras_for_skill`` and the internal endpoint read — is the
  table's projection, byte-compatible with what the editor wrote
  historically, so assigning in ONE place changes what every consumer
  does without any of them changing a line;
* the operator editor keeps its full-replace contract but only over
  its OWN claims — an app's claim survives an operator edit;
* label narrowing merges additively, and an unrestricted claim wins.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_sa_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

import types as _types  # noqa: E402


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm = _types.ModuleType("core.logging_config")
_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.database as cdb  # noqa: E402
import models  # noqa: E402
from services import skill_assignments as svc  # noqa: E402
from services.skills_registry import derive_skills  # noqa: E402
from tests.test_skills_registry import Task  # noqa: E402

# Imported at collection time on purpose: other suites pop core.* from
# sys.modules with docker-hostname env set, so a lazy in-test import of
# the router chain would re-run Settings() into its trust-zone validator.
from routers.skills import router as _skills_router  # noqa: E402

LPR = "license_plate_recognition"


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.database", cdb)
    monkeypatch.setitem(sys.modules, "models", models)
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng)
    monkeypatch.setattr(cdb, "SessionLocal", SessionLocal)
    s = SessionLocal()
    role = models.Role(name="admin")
    s.add(role)
    s.commit()
    user = models.User(username="u", email="u@x", hashed_password="x",
                       role_id=role.id)
    s.add(user)
    s.commit()
    cams = []
    for name in ("gate", "yard"):
        cam = models.Camera(name=name, ip_address="10.0.0.5", owner_id=user.id)
        s.add(cam)
        s.commit()
        cams.append(cam.id)
    yield s, cams
    s.close()


def _assignments_of(s, cam_id):
    return s.query(models.Camera).get(cam_id).assignments


def test_union_two_consumers_release_one_then_last(db):
    s, (gate, yard) = db
    svc.declare(s, skill=LPR, camera_id=gate, consumer="operator")
    svc.declare(s, skill=LPR, camera_id=gate,
                consumer="app:license-plate-recognition")
    s.commit()
    assert svc.assignments_by_skill(s) == {LPR: [gate]}
    assert _assignments_of(s, gate) == [{"skill": LPR}]

    # Releasing ONE consumer must not shrink the union — the other
    # still holds a claim, and it must be the RIGHT other: a release
    # names a (skill, camera, consumer) triple, never "some row".
    assert svc.release(s, skill=LPR, camera_id=gate, consumer="operator")
    s.commit()
    assert svc.assignments_by_skill(s) == {LPR: [gate]}
    assert _assignments_of(s, gate) == [{"skill": LPR}]
    remaining = svc.skill_view(s, LPR)["cameras"][0]["consumers"]
    assert remaining == [
        {"consumer": "app:license-plate-recognition", "params": None}]

    # Releasing the LAST claim empties the union and the projection —
    # gap 7's "unassigning makes the skill dormant".
    assert svc.release(s, skill=LPR, camera_id=gate,
                       consumer="app:license-plate-recognition")
    s.commit()
    assert svc.assignments_by_skill(s) == {}
    assert _assignments_of(s, gate) is None


def test_dormant_in_the_registry_when_union_empties(db):
    s, (gate, _) = db
    health = {"lpr": {"status": "ok"}}
    caps = {"lpr": {"capabilities": {"tasks_advertised": [LPR]}}}

    def status():
        view = derive_skills(
            tasks_registry=[Task(LPR)], adapters_health=health,
            adapters_caps=caps, apps_rows=[],
            assignments=svc.assignments_by_skill(s))
        return next(x for x in view["skills"] if x["id"] == LPR)

    assert status()["status"] == "dormant"          # healthy, unassigned
    svc.declare(s, skill=LPR, camera_id=gate, consumer="agent")
    s.commit()
    entry = status()
    assert entry["status"] == "active"
    assert entry["assignments"] == {"cameras": [gate]}
    svc.release(s, skill=LPR, camera_id=gate, consumer="agent")
    s.commit()
    assert status()["status"] == "dormant"
    # And without the assignment source, the pre-Phase-2 word stands.
    view = derive_skills(tasks_registry=[Task(LPR)], adapters_health=health,
                         adapters_caps=caps, apps_rows=[])
    assert view["skills"][0]["status"] == "available"
    assert view["sources"]["assignments"] == {"implemented": False, "phase": 2}


def test_operator_replace_touches_only_operator_claims(db):
    s, (gate, yard) = db
    cam = s.query(models.Camera).get(gate)
    svc.declare(s, skill=LPR, camera_id=gate, consumer="app:lpr")
    svc.set_operator_assignments(s, cam, [
        {"skill": "object_detection", "labels": ["person"]},
    ])
    s.commit()
    assert _assignments_of(s, gate) == [
        {"skill": LPR},
        {"skill": "object_detection", "labels": ["person"]},
    ]
    # The editor's full-replace clears ONLY operator claims: the app's
    # LPR claim survives an operator submitting a list without it.
    svc.set_operator_assignments(s, cam, [])
    s.commit()
    assert _assignments_of(s, gate) == [{"skill": LPR}]
    assert svc.assignments_by_skill(s) == {LPR: [gate]}


def test_labels_merge_additively_and_unrestricted_wins(db):
    s, (gate, _) = db
    svc.declare(s, skill="object_detection", camera_id=gate,
                consumer="app:occupancy", params={"labels": ["person"]})
    svc.declare(s, skill="object_detection", camera_id=gate,
                consumer="app:line-crossing", params={"labels": ["Car", "person"]})
    s.commit()
    assert _assignments_of(s, gate) == [
        {"skill": "object_detection", "labels": ["car", "person"]},
    ]
    # A claim with NO labels means "no restriction" — it must widen, not
    # narrow (another consumer's restriction can never hide detections).
    svc.declare(s, skill="object_detection", camera_id=gate,
                consumer="operator")
    s.commit()
    assert _assignments_of(s, gate) == [{"skill": "object_detection"}]


def test_declare_is_idempotent_and_updates_params(db):
    s, (gate, _) = db
    svc.declare(s, skill=LPR, camera_id=gate, consumer="agent",
                params={"labels": ["car"]})
    svc.declare(s, skill=LPR, camera_id=gate, consumer="agent",
                params={"labels": ["truck"]})
    s.commit()
    rows = s.query(models.SkillAssignment).all()
    assert len(rows) == 1
    assert rows[0].params == {"labels": ["truck"]}


def test_unknown_camera_and_bad_shapes_refuse(db):
    s, (gate, _) = db
    with pytest.raises(LookupError):
        svc.declare(s, skill=LPR, camera_id=99999, consumer="agent")
    with pytest.raises(ValueError):
        svc.declare(s, skill="", camera_id=gate, consumer="agent")
    with pytest.raises(ValueError):
        svc.declare(s, skill="x" * 101, camera_id=gate, consumer="agent")
    assert svc.release(s, skill=LPR, camera_id=gate, consumer="ghost") is False


def test_skill_view_shows_claims_per_camera(db):
    s, (gate, yard) = db
    svc.declare(s, skill=LPR, camera_id=gate, consumer="operator")
    svc.declare(s, skill=LPR, camera_id=yard, consumer="app:lpr",
                params={"labels": ["car"]})
    s.commit()
    view = svc.skill_view(s, LPR)
    assert view["union"] == sorted([gate, yard])
    by_cam = {c["camera_id"]: c["consumers"] for c in view["cameras"]}
    assert by_cam[gate] == [{"consumer": "operator", "params": None}]
    assert by_cam[yard] == [{"consumer": "app:lpr",
                             "params": {"labels": ["car"]}}]


def test_api_routes_exist_with_auth():
    paths = {r.path for r in _skills_router.routes}
    assert "/skills/{skill_id}/cameras" in paths
    assert "/skills/{skill_id}/cameras/{camera_id}" in paths


# ── Issue #372: deleted cameras must not hold skill claims ─────────
#
# Camera deletion is a soft delete. A stale claim on a binned camera
# used to keep the restriction ARMED while scoping consumers (the LPR
# app) to a camera that no longer exists — every live camera ignored,
# nothing warning anyone. "The assignment list for that skill is the
# whole truth" must mean the truth about LIVE cameras.

from datetime import UTC, datetime  # noqa: E402


def _soft_delete(s, cam_id):
    cam = s.query(models.Camera).get(cam_id)
    cam.is_active = False
    cam.deleted_at = datetime.now(UTC)
    s.commit()


def test_deleted_camera_drops_out_of_the_union(db):
    """The QA scenario from #372: LPR assigned to a camera that later
    lands in the bin. The union must shrink to the live cameras, and
    when the LAST live claim goes the restriction must lift entirely
    (empty map = dormant/no restriction), not stay armed pointing at a
    tombstone."""
    s, (gate, yard) = db
    svc.declare(s, skill=LPR, camera_id=gate, consumer="operator")
    svc.declare(s, skill=LPR, camera_id=yard, consumer="operator")
    s.commit()
    assert svc.assignments_by_skill(s) == {LPR: [gate, yard]}

    _soft_delete(s, yard)
    assert svc.assignments_by_skill(s) == {LPR: [gate]}

    _soft_delete(s, gate)
    # No key at all — 'skill not in map' IS the dormant/unrestricted
    # signal, so consumers fall back to every live camera.
    assert svc.assignments_by_skill(s) == {}


def test_deleted_camera_hidden_from_skill_view(db):
    """The operator view must show the same union consumers act on."""
    s, (gate, yard) = db
    svc.declare(s, skill=LPR, camera_id=gate, consumer="operator")
    svc.declare(s, skill=LPR, camera_id=yard, consumer="operator")
    s.commit()
    _soft_delete(s, yard)
    view = svc.skill_view(s, LPR)
    assert view["union"] == [gate]
    assert [c["camera_id"] for c in view["cameras"]] == [gate]


def test_orphan_claim_for_missing_camera_is_ignored(db):
    """A row whose camera was hard-deleted entirely (pre-fix installs)
    must be invisible too — the join, not just the tombstone filter."""
    s, (gate, _) = db
    svc.declare(s, skill=LPR, camera_id=gate, consumer="operator")
    s.commit()
    s.add(models.SkillAssignment(skill=LPR, camera_id=99999,
                                 consumer="operator"))
    s.commit()
    assert svc.assignments_by_skill(s) == {LPR: [gate]}
    assert svc.skill_view(s, LPR)["union"] == [gate]


def test_release_camera_claims_drops_only_that_camera(db):
    s, (gate, yard) = db
    svc.declare(s, skill=LPR, camera_id=gate, consumer="operator")
    svc.declare(s, skill="object_detection", camera_id=gate,
                consumer="app:x")
    svc.declare(s, skill=LPR, camera_id=yard, consumer="operator")
    s.commit()
    assert svc.release_camera_claims(s, gate) == 2
    s.commit()
    assert s.query(models.SkillAssignment).filter_by(
        camera_id=gate).count() == 0
    assert svc.assignments_by_skill(s) == {LPR: [yard]}
    # Idempotent — a second release finds nothing.
    assert svc.release_camera_claims(s, gate) == 0


def test_both_delete_endpoints_release_claims():
    """Lockstep: the cleanup must be wired into BOTH camera delete
    paths (soft delete commits it with the tombstone; hard delete needs
    it before the camera row for the FK). A helper nothing calls is the
    bug back again."""
    src = (Path(__file__).resolve().parents[1]
           / "routers" / "cameras.py").read_text()
    assert src.count("release_camera_claims(db, camera_id)") == 2


def test_migration_sweep_matches_the_query_filter(db):
    """The one-time migration sweep (ff77bb88cc99) must remove exactly
    the rows the fixed query ignores: claims on soft-deleted cameras
    and orphans. Executes the migration's own DELETE (extracted from
    the file, so this stays lockstep with what actually ships) against
    a DB seeded with all three row kinds."""
    import re

    from sqlalchemy import text

    s, (gate, yard) = db
    svc.declare(s, skill=LPR, camera_id=gate, consumer="operator")
    svc.declare(s, skill=LPR, camera_id=yard, consumer="operator")
    s.commit()
    _soft_delete(s, yard)                                  # tombstone claim
    s.add(models.SkillAssignment(skill=LPR, camera_id=99999,
                                 consumer="operator"))     # orphan claim
    s.commit()

    mig = (Path(__file__).resolve().parents[1] / "migrations" / "versions"
           / "ff77bb88cc99_sweep_deleted_camera_skill_claims.py").read_text()
    m = re.search(r'sa\.text\(\s*"""(.*?)"""', mig, re.S)
    assert m, "migration DELETE statement not found"
    s.execute(text(m.group(1)))
    s.commit()

    left = s.query(models.SkillAssignment).all()
    assert [(r.skill, r.camera_id) for r in left] == [(LPR, gate)]
