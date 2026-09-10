# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Assignment decides where a skill's inference RUNS — and who may say so.

A camera used to be advisory about what it was for: the editor said
"nothing assigned = no restriction declared", and that was honoured in
the cheap places and ignored in the expensive one. Plate OCR ran on
every vehicle on every camera, assigned or not, because ``wants_plate``
took no camera argument and so could not consult an assignment even in
principle.

Three rules are pinned here, and they are the whole change:

* **eligible** — what a picker may OFFER. An unassigned camera is open
  to every skill, so a fresh install shows a full picker.
* **adopted** — what COSTS money. Inference runs only on a camera that
  carries the skill; an unassigned camera computes nothing.
* **who may assign** — changing a claim turns a camera's compute on or
  off, so it needs the same permission as editing that camera. The two
  claim routes took any active user's JWT.

Run with:
    cd server && pytest tests/test_camera_assignment_gate.py -v
"""
from __future__ import annotations

import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.auth as auth_mod  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, CameraPermission, Role, User  # noqa: E402
from routers import skills as skills_router  # noqa: E402
from services.plate_enrichment import PLATE_SKILL, wants_plate  # noqa: E402
from services.skill_assignments import (  # noqa: E402
    camera_adopted,
    camera_eligible,
    camera_skills,
)

_SERVER = REPO_ROOT / "server"


def _cam(*skills):
    """A stand-in camera carrying exactly these skills."""
    return _types.SimpleNamespace(assignments=[{"skill": s} for s in skills])


# ── the rule itself ────────────────────────────────────────────────


def test_unassigned_camera_is_eligible_everywhere_and_adopted_nowhere():
    """The two halves must disagree about an unassigned camera — that
    disagreement IS the design. Offer it in every picker; compute on it
    for nobody until someone points a skill at it."""
    blank = _cam()
    assert camera_skills(blank) == set()
    for skill in (PLATE_SKILL, "occupancy_counting", "face_recognition"):
        assert camera_eligible(blank, skill) is True
        assert camera_adopted(blank, skill) is False
    # A camera with no assignments attribute at all (an older row, or a
    # projection that has not been recomputed) is the same case.
    assert camera_eligible(_types.SimpleNamespace(), PLATE_SKILL) is True
    assert camera_adopted(_types.SimpleNamespace(), PLATE_SKILL) is False


def test_a_claimed_camera_is_closed_to_every_other_skill():
    lpr = _cam(PLATE_SKILL)
    assert camera_adopted(lpr, PLATE_SKILL) is True
    assert camera_eligible(lpr, PLATE_SKILL) is True
    # Claimed by LPR — occupancy may not even be offered it, so an
    # operator cannot accidentally spend that camera twice.
    assert camera_eligible(lpr, "occupancy_counting") is False
    assert camera_adopted(lpr, "occupancy_counting") is False
    # Multiple claims: each is open, everything else is closed.
    both = _cam(PLATE_SKILL, "occupancy_counting")
    assert camera_eligible(both, "occupancy_counting") is True
    assert camera_eligible(both, "face_recognition") is False


def test_skill_names_compare_case_and_space_insensitively():
    messy = _types.SimpleNamespace(assignments=[
        {"skill": "  License_Plate_Recognition "}, {"skill": ""},
        {"nothing": "here"}, "junk", 42,
    ])
    assert camera_skills(messy) == {PLATE_SKILL}
    assert camera_adopted(messy, " LICENSE_PLATE_RECOGNITION ") is True


def test_junk_assignments_never_widen_what_a_camera_carries():
    """A malformed projection must read as "carries nothing" — open to
    pickers and closed to compute — never as "carries this"."""
    for junk in (None, "not-a-list", 7, {"skill": PLATE_SKILL}):
        cam = _types.SimpleNamespace(assignments=junk)
        assert camera_skills(cam) == set()
        assert camera_adopted(cam, PLATE_SKILL) is False


# ── the expensive path: no claim, no OCR ───────────────────────────


def test_wants_plate_needs_the_claim_not_just_a_vehicle():
    """The gate that used to be missing. A car in front of an
    unassigned camera is still a car — and still must not be read."""
    evidence = "cam1/2026/09/10/frame.jpg"
    assert wants_plate("car", evidence, True, {PLATE_SKILL}) is True
    assert wants_plate("car", evidence, True, set()) is False
    assert wants_plate("car", evidence, True, {"occupancy_counting"}) is False
    # None is not "unknown, allow it" — a caller that cannot say which
    # skills a camera carries gets no OCR. Fail closed, deliberately.
    assert wants_plate("car", evidence, True, None) is False
    # The pre-existing gates still hold on an assigned camera.
    assert wants_plate("person", evidence, True, {PLATE_SKILL}) is False
    assert wants_plate("car", None, True, {PLATE_SKILL}) is False
    assert wants_plate("car", evidence, False, {PLATE_SKILL}) is False


def test_both_ocr_entry_points_are_gated_in_lockstep():
    """Two doors lead to KAI-C: the ingest sweep and the early-attempt
    endpoint. Gating one and not the other buys nothing — anything that
    can POST /plates/attempt would still get free OCR on any camera."""
    src = (_SERVER / "routers" / "internal_camera_agent.py").read_text()
    assert "camera_skills(camera)" in src, (
        "ingest no longer passes the camera's skills to wants_plate — "
        "plate OCR is running on every camera again")
    assert "camera_adopted(camera, PLATE_SKILL)" in src, (
        "/plates/attempt no longer checks adoption — any producer can "
        "spend OCR on a camera nobody assigned to LPR")


# ── who may change a claim ─────────────────────────────────────────


@pytest.fixture()
def env():
    """Two cameras owned by an admin; a guard who may VIEW the gate and
    MANAGE the yard. The caller is switchable so one client speaks as
    either user."""
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    s = SessionLocal()
    role = Role(name="admin")
    s.add(role)
    s.flush()
    admin = User(username="admin", email="a@x", hashed_password="x",
                 role_id=role.id, is_superuser=True)
    guard = User(username="guard", email="g@x", hashed_password="x",
                 role_id=role.id)
    s.add_all([admin, guard])
    s.flush()
    gate = Camera(name="Gate", ip_address="10.0.0.1", owner_id=admin.id)
    yard = Camera(name="Yard", ip_address="10.0.0.2", owner_id=admin.id)
    s.add_all([gate, yard])
    s.flush()
    s.add_all([
        CameraPermission(user_id=guard.id, camera_id=gate.id,
                         can_view=True, can_manage=False),
        CameraPermission(user_id=guard.id, camera_id=yard.id,
                         can_view=True, can_manage=True),
    ])
    s.commit()
    ids = {"gate": gate.id, "yard": yard.id}
    s.close()

    app = FastAPI()
    app.include_router(skills_router.router, prefix="/api/v1")

    def _db():
        sess = SessionLocal()
        try:
            yield sess
        finally:
            sess.close()

    current = {"user": guard}
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[auth_mod.get_current_active_user] = (
        lambda: current["user"])
    with TestClient(app) as tc:
        yield tc, current, admin, guard, ids
    engine.dispose()


def _claim_url(cam_id):
    return f"/api/v1/skills/{PLATE_SKILL}/cameras/{cam_id}"


CONSUMER = "app:license-plate-recognition"


def test_a_viewer_cannot_turn_a_cameras_compute_on(env):
    tc, current, admin, guard, ids = env
    # can_view only — declaring here would start paying for OCR on a
    # camera this user may not configure.
    assert tc.put(_claim_url(ids["gate"]),
                  json={"consumer": CONSUMER}).status_code == 403
    # can_manage — their own camera, their own call.
    r = tc.put(_claim_url(ids["yard"]), json={"consumer": CONSUMER})
    assert r.status_code == 200
    assert [c["camera_id"] for c in r.json()["cameras"]] == [ids["yard"]]


def test_a_viewer_cannot_turn_someone_elses_compute_off(env):
    tc, current, admin, guard, ids = env
    current["user"] = admin
    assert tc.put(_claim_url(ids["gate"]),
                  json={"consumer": CONSUMER}).status_code == 200
    current["user"] = guard
    # Release is the same authority as declare: a viewer silencing a
    # gate camera's plate reads is the outage, not the safe direction.
    r = tc.delete(_claim_url(ids["gate"]), params={"consumer": CONSUMER})
    assert r.status_code == 403
    current["user"] = admin
    assert tc.get(f"/api/v1/skills/{PLATE_SKILL}/cameras").json()["cameras"]


def test_the_claim_view_shows_only_the_callers_cameras(env):
    """A claim names a camera, so the view is camera data and obeys the
    same grants every other camera read does."""
    tc, current, admin, guard, ids = env
    current["user"] = admin
    for cam in ids.values():
        assert tc.put(_claim_url(cam),
                      json={"consumer": "operator"}).status_code == 200
    seen = {c["camera_id"]
            for c in tc.get(f"/api/v1/skills/{PLATE_SKILL}/cameras")
            .json()["cameras"]}
    assert seen == set(ids.values())
    # The guard may VIEW both, so both stay — the filter is on view
    # grants, not on the manage grant that gates the writes above.
    current["user"] = guard
    seen = {c["camera_id"]
            for c in tc.get(f"/api/v1/skills/{PLATE_SKILL}/cameras")
            .json()["cameras"]}
    assert seen == set(ids.values())
