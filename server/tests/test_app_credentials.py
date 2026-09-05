# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Per-app credentials (services/app_keys.py).

Every SDK app used to boot with the deployment's INTERNAL_API_KEY and
could therefore read every camera, every app's config and live state.
Now ``POST /apps/register`` mints the app its own ``oak_…`` key, returned
once; with it the app reads only its own registry rows and only the
cameras the operator assigned to it, and a superuser can rotate or
revoke it without touching the site key.

Run with:
    cd server && pytest tests/test_app_credentials.py -v
"""
from __future__ import annotations

import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

import os
import secrets
import sys
import types as _types
from types import SimpleNamespace
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
from models import AppAlert as AppAlertRow  # noqa: E402
from models import Camera, InstalledApp, Role, TimelineEvent, User  # noqa: E402
from routers import apps as apps_router  # noqa: E402
from routers import internal_camera_agent as internal_router  # noqa: E402
from services import app_keys  # noqa: E402


def _manifest(app_id="loitering-detection", provides=("loitering",)):
    return {"id": app_id, "name": app_id.title(), "version": "1.0.0",
            "category": "perimeter", "summary": "", "requires_tasks": [],
            "subscribes": "opennvr.inference.>", "params": [], "emits": [],
            "provides": list(provides)}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_key", SITE_KEY, raising=False)
    monkeypatch.setattr(settings, "inference_use_mediamtx_tap", False, raising=False)
    monkeypatch.setattr(auth_mod, "auth_logger", _L(), raising=False)

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
                 role_id=role.id, is_superuser=True, is_active=True)
    s.add(admin)
    s.flush()
    gate = Camera(name="Gate", ip_address="10.0.0.1", owner_id=admin.id, is_active=True,
                  rtsp_url="rtsp://10.0.0.1/s", assignments=[{"skill": "loitering"}])
    yard = Camera(name="Yard", ip_address="10.0.0.2", owner_id=admin.id, is_active=True,
                  rtsp_url="rtsp://10.0.0.2/s",
                  assignments=[{"skill": "license_plate_recognition"}])
    lobby = Camera(name="Lobby", ip_address="10.0.0.3", owner_id=admin.id, is_active=True,
                   rtsp_url="rtsp://10.0.0.3/s")
    s.add_all([gate, yard, lobby])
    s.flush()
    from datetime import datetime, timezone
    for cam in (gate, yard):
        s.add(TimelineEvent(camera_id=cam.id, source="tier0", event_type="track",
                            label="person", started_at=datetime.now(timezone.utc)))
    s.commit()
    ids = {"gate": gate.id, "yard": yard.id, "lobby": lobby.id}
    s.close()

    app = FastAPI()
    app.include_router(apps_router.router)
    app.include_router(internal_router.router)

    def _db():
        sess = SessionLocal()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[auth_mod.get_current_superuser] = lambda: admin
    with TestClient(app) as tc:
        yield tc, ids, SessionLocal


def _site():
    return {"X-Internal-Api-Key": SITE_KEY}


def _app(key):
    return {"X-Internal-Api-Key": key}


def _register(tc, headers, **kw):
    body = {"url": "http://loitering:9200", "manifest": _manifest(), **kw}
    return tc.post("/apps/register", json=body, headers=headers)


# ── issuing ─────────────────────────────────────────────────────────────


def test_first_registration_mints_a_key_once(env):
    tc, _, SessionLocal = env
    r = _register(tc, _site(), sdk_version="0.2.0")
    assert r.status_code == 200, r.text
    body = r.json()
    key = body["api_key"]
    assert key.startswith("oak_loitering-detection_") and body["has_api_key"] is True
    assert body["registry"]["api_version"] and body["registry"]["min_sdk_version"]
    # Stored hashed, never in the clear.
    s = SessionLocal()
    row = s.get(InstalledApp, "loitering-detection")
    assert row.api_key_hash == app_keys.hash_key(key) and key not in str(row.__dict__)
    s.close()
    # Re-registering with the SITE key and no wants_key: the key stands,
    # and is NOT returned again.
    r = _register(tc, _site())
    assert r.status_code == 200 and "api_key" not in r.json()
    # Re-registering with its OWN key: accepted, nothing minted.
    r = _register(tc, _app(key))
    assert r.status_code == 200 and "api_key" not in r.json()
    assert r.json()["has_api_key"] is True
    s = SessionLocal()
    assert s.get(InstalledApp, "loitering-detection").api_key_hash == app_keys.hash_key(key)
    s.close()


def test_wants_key_reissues_and_invalidates_the_old_one(env):
    tc, *_ = env
    old = _register(tc, _site()).json()["api_key"]
    new = _register(tc, _site(), wants_key=True).json()["api_key"]
    assert new != old
    assert tc.get("/apps/loitering-detection/config", headers=_app(new)).status_code == 200
    assert tc.get("/apps/loitering-detection/config", headers=_app(old)).status_code == 401


def test_an_app_key_cannot_register_or_read_another_app(env):
    tc, *_ = env
    key = _register(tc, _site()).json()["api_key"]
    other = {"url": "http://lpr:9200", "manifest": _manifest("license-plate-recognition")}
    assert tc.post("/apps/register", json=other, headers=_site()).status_code == 200
    r = tc.post("/apps/register", json=other, headers=_app(key))
    assert r.status_code == 403
    assert tc.get("/apps/license-plate-recognition/config", headers=_app(key)).status_code == 403
    assert tc.get("/apps/license-plate-recognition/status", headers=_app(key)).status_code == 403
    # Its own: fine (status probes the app URL; unreachable degrades, not 4xx).
    assert tc.get("/apps/loitering-detection/config", headers=_app(key)).status_code == 200
    assert tc.get("/apps/loitering-detection/status", headers=_app(key)).status_code == 200


def test_garbage_and_revoked_keys_are_401(env):
    tc, *_ = env
    key = _register(tc, _site()).json()["api_key"]
    assert tc.get("/apps/loitering-detection/config",
                  headers=_app("oak_loitering-detection_" + "0" * 32)).status_code == 401
    assert tc.get("/apps/loitering-detection/config",
                  headers=_app("oak_nonsense")).status_code == 401
    assert tc.delete("/apps/loitering-detection/key").json()["has_api_key"] is False
    assert tc.get("/apps/loitering-detection/config", headers=_app(key)).status_code == 401
    # The site key still works for the platform.
    assert tc.get("/apps/loitering-detection/config", headers=_site()).status_code == 200


def test_rotate_returns_a_fresh_key(env):
    tc, *_ = env
    old = _register(tc, _site()).json()["api_key"]
    new = tc.post("/apps/loitering-detection/key/rotate").json()["api_key"]
    assert new != old
    assert tc.get("/apps/loitering-detection/config", headers=_app(old)).status_code == 401
    assert tc.get("/apps/loitering-detection/config", headers=_app(new)).status_code == 200


# ── the internal door, scoped to the app's roster ──────────────────────


def test_internal_cameras_and_events_follow_the_apps_assignments(env):
    tc, ids, _ = env
    key = _register(tc, _site()).json()["api_key"]
    # Gate is assigned "loitering" (this app's `provides`); yard and lobby are not.
    cams = tc.get("/internal/camera-agent/cameras", headers=_app(key)).json()["cameras"]
    assert [int(c["open_nvr_camera_id"]) for c in cams] == [ids["gate"]]
    # The site key sees the fleet.
    cams = tc.get("/internal/camera-agent/cameras", headers=_site()).json()["cameras"]
    assert sorted(int(c["open_nvr_camera_id"]) for c in cams) == sorted(ids.values())
    # Events likewise.
    ev = tc.get("/internal/camera-agent/events", headers=_app(key)).json()["events"]
    assert {e["camera_id"] for e in ev} == {ids["gate"]}
    ev = tc.get("/internal/camera-agent/events", headers=_site()).json()["events"]
    assert {e["camera_id"] for e in ev} == {ids["gate"], ids["yard"]}


def test_unassigned_app_sees_the_fleet_additive_rule(env):
    """No camera names this app → no restriction declared → everything
    (docs/CAMERA_ASSIGNMENTS.md, same as the SDK's cameras_for_skill)."""
    tc, ids, _ = env
    body = {"url": "http://occ:9200", "manifest": _manifest("occupancy-counting", ("occupancy",))}
    key = tc.post("/apps/register", json=body, headers=_site()).json()["api_key"]
    cams = tc.get("/internal/camera-agent/cameras", headers=_app(key)).json()["cameras"]
    assert sorted(int(c["open_nvr_camera_id"]) for c in cams) == sorted(ids.values())


def test_pipeline_write_routes_refuse_app_keys(env):
    tc, ids, _ = env
    key = _register(tc, _site()).json()["api_key"]
    r = tc.post("/internal/camera-agent/events", headers=_app(key), json={
        "camera_id": ids["gate"], "label": "person",
        "started_at": "2026-09-05T10:00:00+00:00"})
    assert r.status_code == 403
    assert tc.get("/internal/camera-agent/detect-config", headers=_app(key)).status_code == 403


def test_app_roster_resolution_unit():
    """The skill names an app answers to: `provides` + its id both ways."""
    row = _types.SimpleNamespace(id="license-plate-recognition",
                                 manifest_json={"provides": ["license_plate_recognition"]})
    assert app_keys.app_skills(row) == {"license_plate_recognition",
                                        "license-plate-recognition"}
    plain, digest = app_keys.mint_key("x-app")
    assert plain.startswith("oak_x-app_") and digest == app_keys.hash_key(plain)
    assert app_keys.looks_like_app_key(plain) and not app_keys.looks_like_app_key(SITE_KEY)


# ── user identity forwarded to the app (X-OpenNVR-User) ────────────────


def test_ui_and_action_proxies_forward_a_signed_user_context(env, monkeypatch):
    """Core signs the caller's identity + camera scope with the app's
    key hash; the SDK verifies with sha256(app key). Both halves here."""
    import sys

    tc, ids, SessionLocal = env
    key = _register(tc, _site()).json()["api_key"]
    s = SessionLocal()
    row = s.get(InstalledApp, "loitering-detection")
    row.manifest_json = {**row.manifest_json, "has_ui": True,
                         "actions": [{"name": "reset", "label": "Reset", "params": []}]}
    row.enabled = True
    s.commit()
    s.close()

    seen: dict[str, dict] = {}

    class _Resp:
        status_code = 200
        content = b"<p>ok</p>"

        def json(self):
            return {"ok": True}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, **kw):
            seen["ui"] = dict(headers or {})
            return _Resp()

        async def post(self, url, json=None, headers=None, **kw):
            seen["action"] = dict(headers or {})
            return _Resp()

    monkeypatch.setattr(apps_router.httpx, "AsyncClient", _Client)
    # A plain (non-superuser) operator granted the gate camera only.
    s = SessionLocal()
    from models import CameraPermission
    guard = User(username="guard", email="g@x", hashed_password="x",
                 role_id=s.query(Role).first().id, is_active=True)
    s.add(guard)
    s.flush()
    s.add(CameraPermission(user_id=guard.id, camera_id=ids["gate"], can_view=True))
    s.commit()
    s.refresh(guard)
    s.expunge(guard)
    s.close()
    tc.app.dependency_overrides[auth_mod.get_current_active_user] = lambda: guard

    assert tc.get("/apps/loitering-detection/ui").status_code == 200
    assert tc.post("/apps/loitering-detection/actions/reset", json={}).status_code == 200

    # Verify exactly as the SDK does (stdlib HS256 over sha256(app key)).
    sdk_path = str(REPO_ROOT / "sdk" / "opennvr-app-sdk")
    if sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)
    from opennvr_app_sdk.usercontext import signing_secret, verify_user_context

    ui = verify_user_context(seen["ui"]["X-OpenNVR-User"], signing_secret(key),
                             audience="loitering-detection")
    assert ui is not None and ui.username == "guard" and ui.purpose == "ui"
    assert ui.cameras == frozenset({ids["gate"]}) and ui.manage == frozenset()
    act = verify_user_context(seen["action"]["X-OpenNVR-User"], signing_secret(key),
                              audience="loitering-detection")
    assert act is not None and act.purpose == "action" and act.user_id == guard.id
    # Signed for THIS app: another app's secret does not verify it.
    assert verify_user_context(seen["ui"]["X-OpenNVR-User"],
                               signing_secret("oak_other_" + "0" * 32)) is None


def test_no_app_key_means_no_user_context(env, monkeypatch):
    tc, ids, SessionLocal = env
    _register(tc, _site())
    s = SessionLocal()
    row = s.get(InstalledApp, "loitering-detection")
    row.manifest_json = {**row.manifest_json, "has_ui": True}
    app_keys.revoke_key(row)
    s.commit()
    s.close()
    seen = {}

    class _Resp:
        status_code = 200
        content = b"<p>ok</p>"

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, **kw):
            seen["ui"] = dict(headers or {})
            return _Resp()

    monkeypatch.setattr(apps_router.httpx, "AsyncClient", _Client)
    admin = SessionLocal().query(User).filter_by(username="admin").one()
    tc.app.dependency_overrides[auth_mod.get_current_active_user] = lambda: admin
    assert tc.get("/apps/loitering-detection/ui").status_code == 200
    assert "X-OpenNVR-User" not in seen["ui"]


# ── the app platform door (routers/app_platform.py) ────────────────────


@pytest.fixture
def platform(env, monkeypatch):
    """The credentials env plus the platform router and fakes for the
    two services it fronts (KAI-C capture, MediaMTX segment index)."""
    from routers import app_platform as plat_router

    tc, ids, SessionLocal = env
    tc.app.include_router(plat_router.router)

    class _Kai:
        async def capture_frame_bytes(self, rtsp_url, camera_id):
            return b"\xff\xd8snap" if camera_id == ids["gate"] else None

    import services.kai_c_service as kcs
    monkeypatch.setattr(kcs, "get_kai_c_service", lambda: _Kai())

    async def _segments(path, start=None, end=None, timeout=10.0):
        return [{"start": "2026-09-05T10:00:00Z", "duration": 60.0, "path": path}]

    from services import mediamtx_client
    monkeypatch.setattr(mediamtx_client, "list_segments", _segments)
    monkeypatch.setattr(settings, "mediamtx_playback_url", "http://mediamtx:9996", raising=False)
    key = _register(tc, _site()).json()["api_key"]
    return tc, ids, SessionLocal, key


def test_snapshot_and_recordings_follow_the_roster(platform):
    tc, ids, _, key = platform
    r = tc.get(f"/internal/app/cameras/{ids['gate']}/snapshot", headers=_app(key))
    assert r.status_code == 200 and r.content == b"\xff\xd8snap"
    # Not in this app's roster → 404 (never 403); site key → allowed but offline → 503.
    assert tc.get(f"/internal/app/cameras/{ids['yard']}/snapshot", headers=_app(key)).status_code == 404
    assert tc.get(f"/internal/app/cameras/{ids['yard']}/snapshot", headers=_site()).status_code == 503
    body = tc.get(f"/internal/app/recordings/{ids['gate']}", headers=_app(key)).json()
    assert body["count"] == 1 and body["recordings"][0]["duration"] == 60.0
    assert tc.get(f"/internal/app/recordings/{ids['lobby']}", headers=_app(key)).status_code == 404
    url = tc.get(f"/internal/app/recordings/{ids['gate']}/url", headers=_app(key),
                 params={"start": "2026-09-05T10:00:00Z", "duration": 60}).json()["url"]
    assert url.startswith("http://mediamtx:9996/get?") and "duration=60" in url


def test_plates_and_alerts_are_scoped_to_the_app(platform):
    tc, ids, SessionLocal, key = platform
    s = SessionLocal()
    from datetime import datetime, timezone
    for cam, plate in ((ids["gate"], "GATE111"), (ids["yard"], "YARD222")):
        s.add(TimelineEvent(camera_id=cam, source="tier0", event_type="track",
                            label="car", plate_text=plate,
                            started_at=datetime.now(timezone.utc)))
    s.add_all([
        AppAlertRow(alert_id="a-mine", fired_at=datetime.now(timezone.utc), severity="high",
                    title="mine", source_kind="app", source_name="loitering-detection"),
        AppAlertRow(alert_id="a-theirs", fired_at=datetime.now(timezone.utc), severity="high",
                    title="theirs", source_kind="app", source_name="other-app"),
    ])
    s.commit()
    s.close()
    stats = tc.get("/internal/app/plates/stats", headers=_app(key)).json()
    assert stats["total_reads"] == 1          # the gate's read only
    assert tc.get("/internal/app/plates/stats", headers=_site()).json()["total_reads"] == 2
    assert tc.get("/internal/app/plates/summary", headers=_app(key),
                  params={"plate": "YARD222"}).json()["total_reads"] == 0
    mine = tc.get("/internal/app/alerts", headers=_app(key)).json()["alerts"]
    assert [a["title"] for a in mine] == ["mine"]
    both = tc.get("/internal/app/alerts", headers=_site()).json()["alerts"]
    assert {a["title"] for a in both} == {"mine", "theirs"}


def test_app_state_is_per_app_and_bounded(platform):
    tc, ids, _, key = platform
    other = {"url": "http://lpr:9200", "manifest": _manifest("license-plate-recognition")}
    other_key = tc.post("/apps/register", json=other, headers=_site()).json()["api_key"]

    assert tc.get("/internal/app/state/cooldown", headers=_app(key)).status_code == 404
    r = tc.put("/internal/app/state/cooldown", headers=_app(key), json={"cam1": 12.5})
    assert r.status_code == 200 and r.json()["value"] == {"cam1": 12.5}
    assert tc.get("/internal/app/state/cooldown", headers=_app(key)).json()["value"] == {"cam1": 12.5}
    # Another app's key does not see it; the site key must name the app.
    assert tc.get("/internal/app/state/cooldown", headers=_app(other_key)).status_code == 404
    assert tc.get("/internal/app/state/cooldown", headers=_site()).status_code == 400
    assert tc.get("/internal/app/state/cooldown", headers=_site(),
                  params={"app_id": "loitering-detection"}).json()["value"] == {"cam1": 12.5}
    items = tc.get("/internal/app/state", headers=_app(key), params={"prefix": "cool"}).json()
    assert [i["key"] for i in items["items"]] == ["cooldown"]
    # Bounds: key shape, value size.
    assert tc.put("/internal/app/state/bad/key", headers=_app(key), json=1).status_code == 404
    assert tc.put("/internal/app/state/" + "k" * 201, headers=_app(key), json=1).status_code == 400
    assert tc.put("/internal/app/state/big", headers=_app(key),
                  json="x" * (256 * 1024 + 1)).status_code == 413
    assert tc.delete("/internal/app/state/cooldown", headers=_app(key)).json()["deleted"] is True
    assert tc.delete("/internal/app/state/cooldown", headers=_app(key)).json()["deleted"] is False


# ── licensed apps (services/app_entitlements.py) ───────────────────────


def _licensed_manifest():
    m = _manifest("paid-app", provides=("paid",))
    m.update({"pricing": "subscription", "price_note": "$29 / camera / year",
              "entitlement": "license_key"})
    return m


@pytest.fixture
def licensed(env, monkeypatch):
    """A registered licensed app whose /entitlement/verify is a fake:
    GOOD-KEY → valid (plan pro, expires 2099), anything else → invalid,
    and the app can be made unreachable."""
    tc, ids, SessionLocal = env
    tc.post("/apps/register", headers=_site(),
            json={"url": "http://paid:9200", "manifest": _licensed_manifest()})
    calls: list[dict] = []
    state = {"reachable": True}

    class _Resp:
        def __init__(self, body, code=200):
            self._b, self.status_code, self.text = body, code, ""

        def json(self):
            return self._b

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, **kw):
            if not state["reachable"]:
                raise ConnectionError("down")
            calls.append({"url": url, "json": json, "headers": headers})
            key = (json or {}).get("license_key")
            if key == "GOOD-KEY":
                return _Resp({"valid": True, "plan": "pro", "expires_at": "2099-01-01T00:00:00Z",
                              "message": "", "limits": {"cameras": 8}})
            return _Resp({"valid": False, "message": "unknown key"})

        async def get(self, url, **kw):
            return _Resp({"status": "ok", "ready": True})

    import services.app_entitlements as ent
    monkeypatch.setattr(ent.httpx, "AsyncClient", _Client)
    return tc, SessionLocal, calls, state


def test_licensed_app_cannot_be_enabled_without_an_accepted_key(licensed):
    tc, SessionLocal, calls, state = licensed
    r = tc.post("/apps/paid-app/enable")
    assert r.status_code == 402 and "licence key" in r.json()["detail"]
    view = tc.get("/apps", headers=_site()).json()
    rows = view["apps"] if isinstance(view, dict) else view
    row = next(a for a in rows if a["id"] == "paid-app")
    assert row["entitlement"]["mode"] == "license_key"
    assert row["entitlement"]["status"] == "none" and not row["entitlement"]["has_license_key"]

    # A rejected key: stored, verdict invalid, still cannot enable.
    r = tc.put("/apps/paid-app/license", json={"license_key": "BAD"})
    assert r.status_code == 200 and r.json()["entitlement"]["status"] == "invalid"
    assert r.json()["entitlement"]["message"] == "unknown key"
    assert tc.post("/apps/paid-app/enable").status_code == 402
    # The key went to the app over the site-key-gated verify route, and
    # is never readable back.
    assert calls[-1]["url"] == "http://paid:9200/entitlement/verify"
    assert calls[-1]["json"] == {"license_key": "BAD"}
    assert "X-Internal-Api-Key" in calls[-1]["headers"]
    assert "BAD" not in tc.get("/apps", headers=_site()).text

    # The right key: valid, and enabling works.
    r = tc.put("/apps/paid-app/license", json={"license_key": "GOOD-KEY"})
    ent = r.json()["entitlement"]
    assert ent["status"] == "valid" and ent["plan"] == "pro"
    assert ent["limits"] == {"cameras": 8} and ent["expires_at"].startswith("2099")
    assert tc.post("/apps/paid-app/enable").json()["enabled"] is True
    # Stored encrypted, not in the clear.
    s = SessionLocal()
    row = s.get(InstalledApp, "paid-app")
    assert row.license_key_encrypted and "GOOD-KEY" not in row.license_key_encrypted
    s.close()


def test_unreachable_app_keeps_its_last_verdict(licensed):
    tc, SessionLocal, calls, state = licensed
    tc.put("/apps/paid-app/license", json={"license_key": "GOOD-KEY"})
    state["reachable"] = False
    r = tc.post("/apps/paid-app/license/verify")
    ent = r.json()["entitlement"]
    assert ent["status"] == "valid" and "could not reach" in ent["message"]
    assert tc.post("/apps/paid-app/enable").status_code == 200
    # Clearing the key resets everything.
    assert tc.delete("/apps/paid-app/license").json()["entitlement"]["status"] == "none"


def test_free_apps_are_never_asked(licensed):
    tc, SessionLocal, calls, state = licensed
    key = _register(tc, _site()).json()["api_key"]          # loitering (free)
    assert tc.post("/apps/loitering-detection/enable").status_code == 200
    assert calls == []
    # The verdict rides the app's config poll.
    body = tc.get("/apps/loitering-detection/config", headers=_app(key)).json()
    assert body["entitlement"]["mode"] == "none" and body["entitlement"]["status"] == "none"


def test_index_external_listing_is_not_installable(monkeypatch, env):
    tc, *_ = env
    entry = apps_router.IndexEntry(
        id="acme-lpr", name="Acme LPR", summary="s", category="vehicles", version="2.0",
        kind="external", external_url="https://acme.example/opennvr", docs_url="https://d",
        pricing="paid", price_note="$99", entitlement="license_key", author="Acme")
    monkeypatch.setattr(apps_router, "_load_apps_index", lambda: [entry])
    monkeypatch.setattr(apps_router, "_require_install_enabled", lambda: None)
    admin = SimpleNamespace(id=1, username="admin", is_superuser=True)
    tc.app.dependency_overrides[apps_router.require_apps_install] = lambda: admin
    tc.app.dependency_overrides[auth_mod.get_current_active_user] = lambda: admin
    listing = tc.get("/apps/index", headers=_site()).json()["apps"][0]
    assert listing["kind"] == "external" and listing["install"] is None
    assert listing["verified"] is False and listing["featured"] is False   # defaults
    assert listing["pricing"] == "paid" and listing["external_url"].startswith("https://acme")
    r = tc.post("/apps/index/acme-lpr/install")
    assert r.status_code == 400 and "external listing" in r.json()["detail"]
    with pytest.raises(ValueError):
        apps_router.IndexEntry(id="x", name="x", summary="s", category="c", version="1",
                               kind="external", external_url="http://insecure", docs_url="d")
    with pytest.raises(ValueError):
        apps_router.IndexEntry(id="x", name="x", summary="s", category="c", version="1",
                               docs_url="d")          # installable without image/install

