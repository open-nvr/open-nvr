"""Each app picks its own cameras (services/skill_assignments.py).

An app works on the cameras picked for it in its own configuration, and
on nothing else. A pick is an ordinary claim — ``consumer="app:<id>"``,
``skill`` = the app id with underscores — so:

* an app with nothing picked sees nothing and computes nothing;
* several apps may pick one camera;
* the camera page's Assignments tune platform compute only, and refuse a
  row that names an app;
* ANPR's picks still switch plate OCR on (its pick skill IS the plate
  skill), and uninstalling an app releases its picks.

Run with:
    cd server && pytest tests/test_app_picks.py -v
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

SITE_KEY = secrets.token_urlsafe(48)
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ["INTERNAL_API_KEY"] = SITE_KEY
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
from core.config import settings  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, CameraPermission, InstalledApp, Role, SkillAssignment, User  # noqa: E402
from routers import apps as apps_router  # noqa: E402
from services import skill_assignments as sa  # noqa: E402

PLATE_SKILL = "license_plate_recognition"


def _manifest(app_id, provides=()):
    return {"id": app_id, "name": app_id.replace("-", " ").title(), "version": "1.0.0",
            "category": "test", "summary": "", "requires_tasks": [],
            "subscribes": "opennvr.inference.>", "params": [], "emits": [],
            "provides": list(provides)}


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_key", SITE_KEY, raising=False)
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    role = Role(name="r")
    s.add(role)
    s.flush()
    admin = User(username="admin", email="a@x", hashed_password="x",
                 role_id=role.id, is_superuser=True, is_active=True)
    viewer = User(username="viewer", email="v@x", hashed_password="x",
                  role_id=role.id, is_superuser=False, is_active=True)
    s.add_all([admin, viewer])
    s.flush()
    gate = Camera(name="Gate", ip_address="10.0.0.1", owner_id=admin.id,
                  is_active=True, rtsp_url="rtsp://10.0.0.1/s")
    yard = Camera(name="Yard", ip_address="10.0.0.2", owner_id=admin.id,
                  is_active=True, rtsp_url="rtsp://10.0.0.2/s")
    lobby = Camera(name="Lobby", ip_address="10.0.0.3", owner_id=admin.id,
                   is_active=True, rtsp_url="rtsp://10.0.0.3/s")
    s.add_all([gate, yard, lobby])
    s.flush()
    # The viewer may see gate and yard, and manage only the gate.
    s.add_all([
        CameraPermission(user_id=viewer.id, camera_id=gate.id, can_view=True, can_manage=True),
        CameraPermission(user_id=viewer.id, camera_id=yard.id, can_view=True, can_manage=False),
    ])
    for app_id, provides in (("guard-scan-compliance", ["guard_scan"]),
                             ("occupancy-counting", ["occupancy"]),
                             ("license-plate-recognition", ["vehicles"])):
        s.add(InstalledApp(id=app_id, name=_manifest(app_id)["name"], version="1.0.0",
                           url=f"http://{app_id}:9200", enabled=True,
                           manifest_json=_manifest(app_id, provides), config_json={}))
    s.commit()
    ids = {"gate": gate.id, "yard": yard.id, "lobby": lobby.id}
    s.close()

    app = FastAPI()
    app.include_router(apps_router.router)

    def _db():
        sess = Session()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_db] = _db
    who = {"user": admin}
    app.dependency_overrides[auth_mod.get_current_active_user] = lambda: who["user"]
    app.dependency_overrides[apps_router.get_read_principal] = lambda: who["user"]
    app.dependency_overrides[auth_mod.get_current_superuser] = lambda: admin
    with TestClient(app) as tc:
        yield {"tc": tc, "ids": ids, "Session": Session, "who": who,
               "admin": admin, "viewer": viewer}


def _pick(Session, app_id, camera_id):
    s = Session()
    sa.declare(s, skill=sa.app_pick_skill(app_id), camera_id=camera_id,
               consumer=sa.app_consumer(app_id))
    s.commit()
    s.close()


# ── the model ──────────────────────────────────────────────────────


def test_a_fresh_app_has_picked_nothing(world):
    s = world["Session"]()
    assert sa.picked_camera_ids(s, "guard-scan-compliance") == set()


def test_a_pick_is_an_app_claim_named_after_the_app(world):
    assert sa.app_consumer("guard-scan-compliance") == "app:guard-scan-compliance"
    assert sa.app_pick_skill("guard-scan-compliance") == "guard_scan_compliance"
    # ANPR's pick skill is the plate skill itself — that is how its picks
    # keep plate OCR switched on without touching the OCR gates.
    assert sa.app_pick_skill("license-plate-recognition") == PLATE_SKILL


def test_two_apps_may_pick_the_same_camera(world):
    ids, Session = world["ids"], world["Session"]
    _pick(Session, "guard-scan-compliance", ids["gate"])
    _pick(Session, "occupancy-counting", ids["gate"])
    s = Session()
    assert sa.picked_camera_ids(s, "guard-scan-compliance") == {ids["gate"]}
    assert sa.picked_camera_ids(s, "occupancy-counting") == {ids["gate"]}
    assert sa.apps_using_camera(s, ids["gate"]) == [
        "guard-scan-compliance", "occupancy-counting"]


def test_an_anpr_pick_turns_plate_ocr_on_for_that_camera(world):
    ids, Session = world["ids"], world["Session"]
    _pick(Session, "license-plate-recognition", ids["yard"])
    s = Session()
    yard = s.get(Camera, ids["yard"])
    assert sa.camera_adopted(yard, PLATE_SKILL) is True
    assert sa.camera_adopted(s.get(Camera, ids["gate"]), PLATE_SKILL) is False


def test_releasing_an_apps_picks_leaves_other_apps_alone(world):
    ids, Session = world["ids"], world["Session"]
    _pick(Session, "license-plate-recognition", ids["yard"])
    _pick(Session, "occupancy-counting", ids["yard"])
    s = Session()
    assert sa.release_app_picks(s, "license-plate-recognition") == 1
    s.commit()
    yard = s.get(Camera, ids["yard"])
    # Plate OCR stops with the app gone; occupancy's pick survives.
    assert sa.camera_adopted(yard, PLATE_SKILL) is False
    assert sa.picked_camera_ids(s, "occupancy-counting") == {ids["yard"]}


def test_a_platform_plate_row_keeps_ocr_on_after_anpr_goes(world):
    """Plate OCR is core compute. A platform row on the camera page turns
    it on independently of the app, and outlives the app's picks."""
    ids, Session = world["ids"], world["Session"]
    _pick(Session, "license-plate-recognition", ids["yard"])
    s = Session()
    sa.set_operator_assignments(s, s.get(Camera, ids["yard"]), [{"skill": PLATE_SKILL}])
    sa.release_app_picks(s, "license-plate-recognition")
    s.commit()
    assert sa.camera_adopted(s.get(Camera, ids["yard"]), PLATE_SKILL) is True


# ── the camera page tunes compute, and no longer points apps ───────


@pytest.mark.parametrize("skill", ["guard_scan", "guard_scan_compliance",
                                   "guard-scan-compliance", "occupancy"])
def test_the_camera_page_refuses_a_row_naming_an_app(world, skill):
    ids, Session = world["ids"], world["Session"]
    s = Session()
    with pytest.raises(ValueError) as exc:
        sa.set_operator_assignments(s, s.get(Camera, ids["gate"]), [{"skill": skill}])
    assert "own configuration" in str(exc.value)


def test_the_camera_page_still_accepts_platform_tasks(world):
    ids, Session = world["ids"], world["Session"]
    s = Session()
    gate = s.get(Camera, ids["gate"])
    sa.set_operator_assignments(s, gate, [
        {"skill": "object_detection", "labels": ["person", "truck"]},
        # ANPR's id spelling, but also the platform plate task: allowed.
        {"skill": PLATE_SKILL},
    ])
    s.commit()
    assert sa.camera_skills(s.get(Camera, ids["gate"])) == {"object_detection", PLATE_SKILL}


def test_the_camera_update_route_turns_the_refusal_into_a_422():
    """Source lockstep: the service raises ValueError; the route must not
    let that become a 500."""
    src = (REPO_ROOT / "server" / "routers" / "cameras.py").read_text(encoding="utf-8")
    block = src[src.index("set_operator_assignments(db, camera, operator_assignments)") - 200:]
    assert "except ValueError" in block[:600]
    assert "status_code=422" in block[:600]


# ── the picker endpoint ────────────────────────────────────────────


def test_the_picker_lists_every_camera_and_what_is_picked(world):
    tc, ids, Session = world["tc"], world["ids"], world["Session"]
    _pick(Session, "guard-scan-compliance", ids["yard"])
    body = tc.get("/apps/guard-scan-compliance/cameras").json()
    assert body["consumer"] == "app:guard-scan-compliance"
    assert body["skill"] == "guard_scan_compliance"
    assert body["camera_picker"] is True
    by_id = {c["id"]: c for c in body["cameras"]}
    assert set(by_id) == set(ids.values())
    assert by_id[ids["yard"]]["picked"] is True
    assert by_id[ids["gate"]]["picked"] is False
    assert by_id[ids["gate"]]["handle"] == f"cam{ids['gate']}"
    assert all(c["can_manage"] for c in body["cameras"])


def test_the_picker_says_which_other_apps_use_a_camera(world):
    tc, ids, Session = world["tc"], world["ids"], world["Session"]
    _pick(Session, "guard-scan-compliance", ids["gate"])
    _pick(Session, "occupancy-counting", ids["gate"])
    body = tc.get("/apps/guard-scan-compliance/cameras").json()
    by_id = {c["id"]: c for c in body["cameras"]}
    # Other apps only — never the app whose picker this is.
    assert by_id[ids["gate"]]["used_by"] == [_manifest("occupancy-counting")["name"]]
    assert by_id[ids["yard"]]["used_by"] == []
    assert "live_online" in by_id[ids["gate"]]


def test_a_non_admin_sees_only_their_cameras_and_manages_fewer(world):
    tc, ids = world["tc"], world["ids"]
    world["who"]["user"] = world["viewer"]
    body = tc.get("/apps/guard-scan-compliance/cameras").json()
    by_id = {c["id"]: c for c in body["cameras"]}
    assert set(by_id) == {ids["gate"], ids["yard"]}
    assert by_id[ids["gate"]]["can_manage"] is True
    assert by_id[ids["yard"]]["can_manage"] is False


def test_the_picker_404s_for_an_unknown_app(world):
    assert world["tc"].get("/apps/nope/cameras").status_code == 404


def test_the_config_poll_carries_the_picks(world):
    """A pick changes no config key, so it must ride the poll for a running
    app to notice it within one interval."""
    tc, ids, Session = world["tc"], world["ids"], world["Session"]
    assert tc.get("/apps/guard-scan-compliance/config").json()["cameras"] == []
    _pick(Session, "guard-scan-compliance", ids["gate"])
    _pick(Session, "guard-scan-compliance", ids["lobby"])
    assert tc.get("/apps/guard-scan-compliance/config").json()["cameras"] == sorted(
        [ids["gate"], ids["lobby"]])


def test_the_app_list_counts_each_apps_picks(world):
    tc, ids, Session = world["tc"], world["ids"], world["Session"]
    _pick(Session, "occupancy-counting", ids["gate"])
    _pick(Session, "occupancy-counting", ids["yard"])
    apps = {a["id"]: a for a in tc.get("/apps").json()}
    assert apps["occupancy-counting"]["picked_cameras"] == 2
    assert apps["guard-scan-compliance"]["picked_cameras"] == 0
    assert apps["guard-scan-compliance"]["camera_picker"] is True


def test_an_app_can_declare_it_takes_no_picks(world):
    tc, Session = world["tc"], world["Session"]
    s = Session()
    row = s.get(InstalledApp, "occupancy-counting")
    row.manifest_json = {**row.manifest_json, "camera_picker": False}
    s.commit()
    apps = {a["id"]: a for a in tc.get("/apps").json()}
    assert apps["occupancy-counting"]["camera_picker"] is False


def test_reregistering_refreshes_pick_labels_from_the_manifest(world):
    """The boot-time re-register is where an upgraded manifest lands, so
    that is where picks made before the upgrade learn its tier0_labels."""
    tc, ids, Session = world["tc"], world["ids"], world["Session"]
    _pick(Session, "occupancy-counting", ids["gate"])
    manifest = {**_manifest("occupancy-counting", ["occupancy"]),
                "tier0_labels": ["backpack", "suitcase"]}
    r = tc.post("/apps/register", json={"url": "http://occupancy-counting:9200",
                                        "manifest": manifest},
                headers={"X-Internal-Api-Key": SITE_KEY})
    assert r.status_code == 200, r.text
    s = Session()
    cam = s.query(Camera).get(ids["gate"])
    assert cam.assignments == [
        {"skill": "occupancy_counting", "labels": ["backpack", "suitcase"]}]
    s.close()


def test_uninstall_releases_the_apps_picks():
    """Source lockstep: uninstall must release picks, or plate OCR and
    compute keep running for an app that is gone."""
    src = (REPO_ROOT / "server" / "routers" / "apps.py").read_text(encoding="utf-8")
    body = src[src.index("async def uninstall_app("):]
    body = body[:body.index("return _serialize_intent(row)")]
    assert "release_app_picks(db, app_id)" in body


def test_a_deleted_camera_drops_out_of_the_picks(world):
    ids, Session = world["ids"], world["Session"]
    _pick(Session, "guard-scan-compliance", ids["gate"])
    s = Session()
    s.get(Camera, ids["gate"]).deleted_at = _dt.datetime.now(_dt.timezone.utc)
    s.commit()
    assert sa.picked_camera_ids(s, "guard-scan-compliance") == set()
    assert s.query(SkillAssignment).count() == 1  # the row itself is the delete path's job


# ── enable/disable and the union (#539) ─────────────────────────────
#
# A disabled app is not a consumer. Its picks STAY — the operator's
# camera selection has to survive a Disable and come back exactly on
# Enable — but they contribute nothing to the union while the app is
# off, so nothing downstream keeps computing for an app that is off.


def test_disabling_an_app_drops_its_picks_from_the_union(world):
    """The bug: Disable flipped the flag and left the projection alone,
    so a disabled ANPR app went on buying plate OCR on every vehicle."""
    tc, ids, Session = world["tc"], world["ids"], world["Session"]
    _pick(Session, "license-plate-recognition", ids["yard"])
    s = Session()
    assert sa.camera_adopted(s.get(Camera, ids["yard"]), PLATE_SKILL) is True
    s.close()

    assert tc.post("/apps/license-plate-recognition/disable").status_code == 200

    s = Session()
    yard = s.get(Camera, ids["yard"])
    assert sa.camera_adopted(yard, PLATE_SKILL) is False
    assert yard.assignments is None
    # The pick itself survives — Disable is not Uninstall.
    assert sa.picked_camera_ids(s, "license-plate-recognition") == {ids["yard"]}
    s.close()


def test_enabling_restores_the_selection_exactly(world):
    tc, ids, Session = world["tc"], world["ids"], world["Session"]
    _pick(Session, "license-plate-recognition", ids["yard"])
    _pick(Session, "license-plate-recognition", ids["gate"])
    tc.post("/apps/license-plate-recognition/disable")
    assert tc.post("/apps/license-plate-recognition/enable").status_code == 200

    s = Session()
    for cam_id in (ids["yard"], ids["gate"]):
        assert sa.camera_adopted(s.get(Camera, cam_id), PLATE_SKILL) is True
    assert sa.camera_adopted(s.get(Camera, ids["lobby"]), PLATE_SKILL) is False
    s.close()


def test_a_disabled_app_does_not_shrink_another_consumers_claim(world):
    """Union semantics: one consumer going quiet must not take a skill
    away from the others who asked for it."""
    ids, Session = world["ids"], world["Session"]
    tc = world["tc"]
    _pick(Session, "license-plate-recognition", ids["yard"])
    s = Session()
    # The camera page's own platform row, on the same skill.
    sa.declare(s, skill=PLATE_SKILL, camera_id=ids["yard"],
               consumer=sa.OPERATOR_CONSUMER)
    s.commit()
    s.close()

    tc.post("/apps/license-plate-recognition/disable")

    s = Session()
    # The operator asked for plate OCR here; the app's state is not theirs.
    assert sa.camera_adopted(s.get(Camera, ids["yard"]), PLATE_SKILL) is True
    s.close()


def test_a_disabled_apps_tier0_labels_stop_widening_the_camera(world):
    """A pick's labels widen Tier-0's tracked set. A disabled app must
    not keep that widening — it is compute the app asked for."""
    tc, ids, Session = world["tc"], world["ids"], world["Session"]
    s = Session()
    app = s.get(InstalledApp, "occupancy-counting")
    app.manifest_json = {**_manifest("occupancy-counting", ["occupancy"]),
                         "tier0_labels": ["backpack"]}
    s.commit()
    s.close()
    _pick(Session, "occupancy-counting", ids["gate"])

    s = Session()
    assert s.get(Camera, ids["gate"]).assignments == [
        {"skill": "occupancy_counting", "labels": ["backpack"]}]
    s.close()

    tc.post("/apps/occupancy-counting/disable")

    s = Session()
    assert s.get(Camera, ids["gate"]).assignments is None
    s.close()


def test_disable_reprojects_every_camera_the_app_picked(world):
    tc, ids, Session = world["tc"], world["ids"], world["Session"]
    for cam in ("gate", "yard", "lobby"):
        _pick(Session, "occupancy-counting", ids[cam])
    tc.post("/apps/occupancy-counting/disable")
    s = Session()
    for cam in ("gate", "yard", "lobby"):
        assert s.get(Camera, ids[cam]).assignments is None
    s.close()


def test_declaring_a_pick_for_a_disabled_app_stays_dormant(world):
    """Picking a camera while the app is off records the choice without
    switching compute on — it lights up when the app is enabled."""
    tc, ids, Session = world["tc"], world["ids"], world["Session"]
    tc.post("/apps/license-plate-recognition/disable")
    _pick(Session, "license-plate-recognition", ids["yard"])

    s = Session()
    assert sa.camera_adopted(s.get(Camera, ids["yard"]), PLATE_SKILL) is False
    s.close()

    tc.post("/apps/license-plate-recognition/enable")
    s = Session()
    assert sa.camera_adopted(s.get(Camera, ids["yard"]), PLATE_SKILL) is True
    s.close()


def test_the_registry_agrees_with_compute_about_a_disabled_app(world):
    """`assignments_by_skill` feeds GET /skills. Compute follows the
    projection, so this map has to as well — otherwise the registry
    calls a skill active on a camera where nothing is computing."""
    tc, ids, Session = world["tc"], world["ids"], world["Session"]
    _pick(Session, "license-plate-recognition", ids["yard"])
    s = Session()
    assert sa.assignments_by_skill(s) == {PLATE_SKILL: [ids["yard"]]}
    s.close()

    tc.post("/apps/license-plate-recognition/disable")

    s = Session()
    assert sa.assignments_by_skill(s) == {}
    s.close()

    tc.post("/apps/license-plate-recognition/enable")
    s = Session()
    assert sa.assignments_by_skill(s) == {PLATE_SKILL: [ids["yard"]]}
    s.close()


def test_the_claims_view_shows_a_dormant_pick_without_counting_it(world):
    """Hiding the claim would make the pick vanish from the one view
    that exists so a release is never a surprise. It is shown, marked,
    and left out of the union."""
    tc, ids, Session = world["tc"], world["ids"], world["Session"]
    _pick(Session, "license-plate-recognition", ids["yard"])
    s = Session()
    # Enabled: the key is sparse, so nothing changes shape.
    claims = sa.skill_view(s, PLATE_SKILL)["cameras"][0]["consumers"]
    assert claims == [{"consumer": "app:license-plate-recognition",
                       "params": None}]
    s.close()

    tc.post("/apps/license-plate-recognition/disable")

    s = Session()
    view = sa.skill_view(s, PLATE_SKILL)
    assert view["union"] == []
    assert view["cameras"][0]["consumers"] == [
        {"consumer": "app:license-plate-recognition", "params": None,
         "dormant": True}]
    s.close()


def test_an_operators_claim_is_never_dormant(world):
    tc, ids, Session = world["tc"], world["ids"], world["Session"]
    _pick(Session, "license-plate-recognition", ids["yard"])
    s = Session()
    sa.declare(s, skill=PLATE_SKILL, camera_id=ids["yard"],
               consumer=sa.OPERATOR_CONSUMER)
    s.commit()
    s.close()

    tc.post("/apps/license-plate-recognition/disable")

    s = Session()
    view = sa.skill_view(s, PLATE_SKILL)
    assert view["union"] == [ids["yard"]]     # the operator still wants it
    by_consumer = {c["consumer"]: c for c in view["cameras"][0]["consumers"]}
    assert by_consumer["operator"].get("dormant") is None
    assert by_consumer["app:license-plate-recognition"]["dormant"] is True
    assert sa.assignments_by_skill(s) == {PLATE_SKILL: [ids["yard"]]}
    s.close()


def test_label_sync_projects_off_one_disabled_app_lookup(world):
    """Source lockstep: every project_camera loop passes the shared
    lookup, or an app re-registration re-queries installed_apps once per
    camera it picked."""
    src = (REPO_ROOT / "server" / "services" / "skill_assignments.py").read_text(
        encoding="utf-8")
    for fn in ("def sync_app_pick_labels(", "def release_app_picks(",
               "def reproject_app_cameras("):
        body = src[src.index(fn):]
        body = body[:body.index("\ndef ", 1)]
        assert "project_camera(db, camera)" not in body, fn
