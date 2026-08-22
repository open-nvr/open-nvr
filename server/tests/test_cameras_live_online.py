# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""`live_online` on GET /cameras/ — the field the UI renders its stream badge from.

Connectivity is tracked in memory by CameraStatusService (MediaMTX
runOnReady/runOnNotReady hooks + a reconciler). Before this field existed the
web UI had to probe /cameras/{id}/mediamtx-status once per camera to find out
whether a stream was live, which cost three MediaMTX round trips per row.

The contract pinned here:

* live_online mirrors the tracker's committed state for active cameras;
* it is None (UNKNOWN) — never False — when the tracker has not seen a camera,
  because False renders as "Disconnected" and would paint the whole fleet red
  for the first 30s after every restart;
* paused cameras are always None: the reconciler never walks them;
* it never widens what a non-superuser can see;
* attaching it costs no MediaMTX call, so the list endpoint keeps working when
  the media server is unreachable.

Run with:

    cd server && pytest tests/test_cameras_live_online.py -v
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_cameras_live_online_test.db")
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
# Force-assign (not setdefault): an earlier-collected test may have installed a
# narrower stub without a __getattr__ fallback.
sys.modules["core.logging_config"] = _lm

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.auth as core_auth  # noqa: E402
import services.camera_status_service as css  # noqa: E402
from core.auth import create_access_token, get_password_hash  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Camera, Role, User  # noqa: E402
from routers import cameras as cameras_router  # noqa: E402
from services.camera_status_service import CameraStatusService  # noqa: E402
from services.mediamtx_admin_service import MediaMtxAdminService  # noqa: E402

PASSWORD = "Str0ng!passw0rd"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(core_auth, "auth_logger", _L(), raising=False)
    monkeypatch.setattr(cameras_router, "camera_logger", _L(), raising=False)

    # Fresh, non-singleton tracker. cameras.py imports the *accessor*, which
    # reads this module global, so swapping it here swaps what the endpoint sees.
    tracker = CameraStatusService()
    monkeypatch.setattr(css, "_service", tracker)

    # The list path must never reach MediaMTX. Blow up loudly if it tries.
    async def _explode(*a, **k):
        raise AssertionError("GET /cameras/ must not call MediaMTX")

    monkeypatch.setattr(
        MediaMtxAdminService, "list_active_paths", staticmethod(_explode)
    )
    monkeypatch.setattr(
        MediaMtxAdminService, "get_active_path", staticmethod(_explode)
    )
    monkeypatch.setattr(MediaMtxAdminService, "path_status", staticmethod(_explode))

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    session_factory = sessionmaker(bind=eng)

    app = FastAPI()
    app.include_router(cameras_router.router, prefix="/api/v1")

    def _get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db

    db = session_factory()
    role = Role(name="admin", description="test role")
    db.add(role)
    db.flush()

    def make_user(username: str, superuser: bool) -> User:
        u = User(
            username=username,
            email=f"{username}@example.com",
            hashed_password=get_password_hash(PASSWORD),
            is_active=True,
            is_superuser=superuser,
            password_set=True,
            role_id=role.id,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        return u

    owner = make_user("owner", False)
    admin = make_user("admin", True)
    stranger = make_user("stranger", False)

    counter = {"n": 0}

    def make_camera(**kw) -> Camera:
        counter["n"] += 1
        n = counter["n"]
        defaults = dict(
            name=f"Cam {n}",
            ip_address=f"10.0.0.{n}",
            port=554,
            owner_id=owner.id,
            is_active=True,
            status="provisioned",
        )
        defaults.update(kw)
        cam = Camera(**defaults)
        db.add(cam)
        db.commit()
        db.refresh(cam)
        return cam

    client = TestClient(app)
    try:
        yield _types.SimpleNamespace(
            client=client,
            db=db,
            tracker=tracker,
            owner=owner,
            admin=admin,
            stranger=stranger,
            make_camera=make_camera,
        )
    finally:
        db.close()


def _auth(username: str = "owner") -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': username})}"}


def _rows(env, user: str = "owner", **params) -> dict:
    resp = env.client.get("/api/v1/cameras/", params=params, headers=_auth(user))
    assert resp.status_code == 200, resp.text
    return {c["id"]: c for c in resp.json()["cameras"]}


def test_live_online_mirrors_the_tracker(env):
    up = env.make_camera()
    down = env.make_camera()
    env.tracker._status[up.id] = True
    env.tracker._status[down.id] = False

    rows = _rows(env, "admin")
    assert rows[up.id]["live_online"] is True
    assert rows[down.id]["live_online"] is False


def test_unseeded_tracker_reports_unknown_not_offline(env):
    """The restart-window guard.

    _status is empty for the first RECONCILE_INITIAL_DELAY_SECONDS. If that
    surfaced as False the UI would show every camera "Disconnected" after each
    restart, which is exactly the false alarm this field must not create.
    """
    cams = [env.make_camera() for _ in range(3)]

    rows = _rows(env, "admin")
    for cam in cams:
        assert rows[cam.id]["live_online"] is None


def test_paused_camera_is_unknown_even_if_tracker_says_online(env):
    """A paused camera has no MediaMTX path, so a stale True must not leak out.

    The reconciler only walks is_active cameras, so an entry left over from
    before the pause would otherwise render the camera as live.
    """
    paused = env.make_camera(is_active=False)
    env.tracker._status[paused.id] = True

    rows = _rows(env, "admin", active_only=False)
    assert rows[paused.id]["live_online"] is None


def test_live_online_does_not_widen_visibility(env):
    """A non-superuser still sees only their own rows, live_online or not."""
    mine = env.make_camera()
    theirs = env.make_camera(owner_id=env.stranger.id)
    env.tracker._status[mine.id] = True
    env.tracker._status[theirs.id] = True

    rows = _rows(env, "owner")
    assert mine.id in rows
    assert theirs.id not in rows
    assert rows[mine.id]["live_online"] is True


def test_list_serves_without_mediamtx(env):
    """The whole point: no MediaMTX round trip on the list path.

    Every MediaMtxAdminService entry point is monkeypatched to raise, so this
    passing proves the endpoint reads the in-memory tracker instead.
    """
    cam = env.make_camera()
    env.tracker._status[cam.id] = True

    rows = _rows(env, "admin")
    assert rows[cam.id]["live_online"] is True


# ---------------------------------------------------------------------------
# recording_state vs live_online — the two badges must never contradict.
# ---------------------------------------------------------------------------


def _backdate_creation(env, cam, hours: int = 2):
    """Push the camera out of its creation grace period.

    That branch short-circuits ahead of everything below, so any test about
    steady-state behaviour has to leave it first.
    """
    from datetime import UTC, datetime, timedelta

    cam.created_at = datetime.now(UTC) - timedelta(hours=hours)
    env.db.commit()


def _recording_on(env, cam):
    """Turn recording on for `cam`.

    `recording_enabled` is read from the camera's config row, not the camera,
    and without one every state below collapses to "off".
    """
    from models import CameraConfig

    env.db.add(CameraConfig(camera_id=cam.id, recording_enabled=True))
    env.db.commit()


def _recent_recording(env, cam, seconds_ago: int):
    """Index one segment `seconds_ago` in the past for `cam`."""
    from datetime import UTC, datetime, timedelta

    from models import Recording

    env.db.add(Recording(
        camera_id=cam.id,
        filename=f"{cam.id}-seg.mp4",
        file_path=f"/rec/{cam.id}-{seconds_ago}.mp4",
        start_time=datetime.now(UTC) - timedelta(seconds=seconds_ago),
    ))
    env.db.commit()


def test_offline_camera_never_reports_recording(env):
    """A dead source cannot be recording, however fresh the last segment is.

    Segment age lags: it keeps reading "recording" for the whole stall
    threshold after a drop. That is what produced a "Recording" badge sitting
    next to a "Disconnected" stream on the cameras page.
    """
    cam = env.make_camera()
    _recording_on(env, cam)
    _recent_recording(env, cam, seconds_ago=30)
    env.tracker._status[cam.id] = False

    rows = _rows(env, "admin")
    assert rows[cam.id]["live_online"] is False
    assert rows[cam.id]["recording_state"] == "not_recording"


def test_online_camera_with_fresh_segment_still_reports_recording(env):
    """The happy path must be untouched by the guard above."""
    cam = env.make_camera()
    _recording_on(env, cam)
    _recent_recording(env, cam, seconds_ago=30)
    env.tracker._status[cam.id] = True

    rows = _rows(env, "admin")
    assert rows[cam.id]["recording_state"] == "recording"


def test_unknown_liveness_falls_back_to_segment_age(env):
    """During the restart window the tracker has no opinion.

    Segment age is all we have then, so a camera that was recording a moment
    ago must not be downgraded just because state has not re-seeded.
    """
    cam = env.make_camera()
    _recording_on(env, cam)
    _recent_recording(env, cam, seconds_ago=30)
    # tracker deliberately left empty -> live_online None

    rows = _rows(env, "admin")
    assert rows[cam.id]["live_online"] is None
    assert rows[cam.id]["recording_state"] == "recording"


def test_offline_camera_past_threshold_is_still_stalled(env):
    """`not_recording` must not swallow `stalled`.

    Once the watchdog would alarm, the badge has to say stalled — that
    agreement is the whole reason this derivation uses the watchdog's own
    thresholds.
    """
    from datetime import UTC, datetime, timedelta

    from services.recording_watchdog import STALL_THRESHOLD_SECONDS

    cam = env.make_camera()
    # Backdated out of the grace period, which short-circuits ahead of the
    # stall check — and a camera younger than its own segments is not a state
    # that can occur in the field.
    cam.created_at = datetime.now(UTC) - timedelta(hours=2)
    env.db.commit()
    _recording_on(env, cam)
    _recent_recording(env, cam, seconds_ago=int(STALL_THRESHOLD_SECONDS) + 60)
    env.tracker._status[cam.id] = False

    rows = _rows(env, "admin")
    assert rows[cam.id]["recording_state"] == "stalled"


def test_brand_new_offline_camera_does_not_claim_recording(env):
    """The grace period exists so a just-added camera isn't libelled as dead.

    It must not become a licence to claim Recording for a camera we already
    know never connected — which is exactly what a freshly added, unreachable
    camera looked like.
    """
    cam = env.make_camera()  # created_at == now, well inside GRACE_PERIOD
    _recording_on(env, cam)
    env.tracker._status[cam.id] = False

    rows = _rows(env, "admin")
    assert rows[cam.id]["recording_state"] == "not_recording"


# ---------------------------------------------------------------------------
# The reconnect window: stream up, newest INDEXED segment still pre-outage.
# ---------------------------------------------------------------------------


def _online_since(env, cam, seconds_ago: int):
    from datetime import UTC, datetime, timedelta

    env.tracker._status[cam.id] = True
    env.tracker._online_since[cam.id] = datetime.now(UTC) - timedelta(seconds=seconds_ago)


def test_just_reconnected_camera_is_not_reported_stalled(env):
    """Ready + "Stalled 18m" was the mirror of the Disconnected+Recording bug.

    MediaMTX indexes a segment only when it closes, so for a segment duration
    after a source returns, the newest indexed one still predates the outage.
    Judging on age alone calls a demonstrably streaming camera stalled.
    """
    cam = env.make_camera()
    _backdate_creation(env, cam)
    _recording_on(env, cam)
    _recent_recording(env, cam, seconds_ago=18 * 60)   # last pre-outage segment
    _online_since(env, cam, seconds_ago=30)            # came back 30s ago

    rows = _rows(env, "admin")
    assert rows[cam.id]["live_online"] is True
    assert rows[cam.id]["recording_state"] == "recording"


def test_long_running_stream_with_no_segments_is_still_stalled(env):
    """The floor must not become a blanket amnesty.

    Stream up well past the threshold with nothing written is a genuine
    recorder failure — the case the watchdog exists to catch.
    """
    from services.recording_watchdog import STALL_THRESHOLD_SECONDS

    cam = env.make_camera()
    _backdate_creation(env, cam)
    _recording_on(env, cam)
    _recent_recording(env, cam, seconds_ago=int(STALL_THRESHOLD_SECONDS) * 3)
    _online_since(env, cam, seconds_ago=int(STALL_THRESHOLD_SECONDS) * 2)

    rows = _rows(env, "admin")
    assert rows[cam.id]["live_online"] is True
    assert rows[cam.id]["recording_state"] == "stalled"


def test_paused_camera_is_off_not_stalled(env):
    """A paused camera's stale segments are expected, not an incident.

    The watchdog skips inactive cameras entirely, so reporting "Stalled 3h"
    here invented an alarm state nothing else in the system agreed with.
    """
    cam = env.make_camera(is_active=False)
    _backdate_creation(env, cam)
    _recording_on(env, cam)
    _recent_recording(env, cam, seconds_ago=3 * 60 * 60)

    rows = _rows(env, "admin", active_only=False)
    assert rows[cam.id]["live_online"] is None
    assert rows[cam.id]["recording_state"] == "off"


def test_never_recorded_camera_stays_no_data_when_stream_is_old(env):
    """"never" outranks "stalled": nothing was ever written, and saying so is
    more useful to an operator than a stall age computed off the stream."""
    from services.recording_watchdog import STALL_THRESHOLD_SECONDS

    cam = env.make_camera()
    _backdate_creation(env, cam)
    _recording_on(env, cam)
    _online_since(env, cam, seconds_ago=int(STALL_THRESHOLD_SECONDS) * 2)

    rows = _rows(env, "admin")
    assert rows[cam.id]["recording_state"] == "never"
