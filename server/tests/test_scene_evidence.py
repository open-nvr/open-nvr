# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Scene evidence — the whole frame behind the best crop of a visit.

``evidence_path`` is framed for the SUBJECT: the detection box plus a
quarter-box margin. That framing is deliberate and good at its job, and
it is exactly why it cannot answer the other question an operator asks —
what lane, whose gate, next to what. So Tier-0 posts a second JPEG per
visit and the Vehicles dialog stages that, with the plate crop pinned
over it.

The promises pinned here:

* the scene is persisted beside the crop, not instead of it;
* a scene NEVER costs a visit its row — oversized, corrupt, or arriving
  without a crop at all, the image is dropped and the history is kept.
  This is the deliberate asymmetry with ``evidence_jpeg_b64``, which
  422s: correct for the primary photo, catastrophic for a garnish;
* the scene is never fed to plate OCR — it is a wide shot, and a plate
  in it is a few pixels tall;
* ``has_scene_evidence`` means "there is ANOTHER image worth showing",
  so it is false when content-addressing collapses scene and crop to
  one file, exactly as ``has_plate_evidence`` is;
* an old pipeline that sends no scene still ingests.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import types as _types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_scene_test.db")
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

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.database as cdb  # noqa: E402
import models  # noqa: E402
import services.plate_enrichment as pe  # noqa: E402

UTC = timezone.utc
T = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
# base64 of b"hello". Never decoded into a real image by these paths —
# save_evidence_jpeg is stubbed per test.
B64 = "aGVsbG8="


@pytest.fixture(autouse=True)
def _stable_core_env():
    """Survive a sibling suite leaving routable ``MEDIAMTX_*`` values behind:
    the routers here are imported lazily, so the first import happens mid-run
    and re-validates Settings, which trips V-015. Same pin test_camera_settings
    uses, for the same reason."""
    safe = {
        "MEDIAMTX_BASE_URL": "http://127.0.0.1:8889",
        "MEDIAMTX_ADMIN_API": "http://127.0.0.1:9997/v3",
        "MEDIAMTX_HLS_URL": "http://127.0.0.1:8888",
        "MEDIAMTX_RTSP_URL": "rtsp://127.0.0.1:8554",
        "MEDIAMTX_RTSPS_URL": "rtsps://127.0.0.1:8322",
        "MEDIAMTX_PLAYBACK_URL": "http://127.0.0.1:9996",
    }
    saved = {k: os.environ.get(k) for k in safe}
    os.environ.update(safe)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture(autouse=True)
def _clean_sightings():
    """The dedup sightings map is process-global on purpose, which makes it
    cross-test state by accident."""
    with pe._sightings_lock:
        pe._recent_sightings.clear()
    yield
    with pe._sightings_lock:
        pe._recent_sightings.clear()


@pytest.fixture
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
    cam = models.Camera(name="c", ip_address="10.0.0.5", owner_id=user.id)
    s.add(cam)
    s.commit()
    out = (SessionLocal, cam.id, user.id)
    s.close()
    yield out


def _saver(monkeypatch, mapping):
    """Stub the content-addressed store: bytes in, fake relative path out."""
    from services import evidence_store as _ev

    def fake(b: bytes) -> str:
        if b not in mapping:
            raise ValueError("unexpected bytes")
        return mapping[b]

    monkeypatch.setattr(_ev, "save_evidence_jpeg", fake)


def _ingest(SessionLocal, payload):
    from fastapi import BackgroundTasks

    from routers import internal_camera_agent as ica

    background = BackgroundTasks()
    s = SessionLocal()
    try:
        out = asyncio.run(ica.ingest_track_event(payload, background, None, s))
        row = s.get(models.TimelineEvent, out["id"]) if out.get("id") else None
        # Read the columns while the session is still open.
        snap = None if row is None else (row.evidence_path,
                                         row.scene_evidence_path)
        return out, snap, background
    finally:
        s.close()


# ── ingest ──────────────────────────────────────────────────────────


def test_scene_is_persisted_beside_the_crop(db, monkeypatch):
    import base64

    from routers import internal_camera_agent as ica

    SessionLocal, cam_id, _uid = db
    _saver(monkeypatch, {b"hello": "ab/crop.jpg", b"scene!": "cd/scene.jpg"})
    payload = ica.TrackEventIn(
        camera_id=cam_id, label="car", track_id="t1", started_at=T,
        evidence_jpeg_b64=B64,
        scene_jpeg_b64=base64.b64encode(b"scene!").decode(),
    )
    out, snap, _bg = _ingest(SessionLocal, payload)
    assert snap == ("ab/crop.jpg", "cd/scene.jpg")
    assert out["scene_evidence_path"] == "cd/scene.jpg"


def test_a_scene_without_a_crop_does_not_500(db, monkeypatch):
    """Regression: the evidence branch does `import base64` INSIDE its own
    `if`, binding a function-local. A scene-only payload that reused the
    bare name would raise UnboundLocalError — a 500, and a lost visit."""
    from routers import internal_camera_agent as ica

    SessionLocal, cam_id, _uid = db
    _saver(monkeypatch, {b"hello": "cd/scene.jpg"})
    payload = ica.TrackEventIn(
        camera_id=cam_id, label="car", track_id="t2", started_at=T,
        evidence_jpeg_b64=None, scene_jpeg_b64=B64,
    )
    _out, snap, _bg = _ingest(SessionLocal, payload)
    assert snap == (None, "cd/scene.jpg")


def test_an_oversized_scene_drops_the_image_not_the_visit(db, monkeypatch):
    """The asymmetry with evidence_jpeg_b64 is deliberate and load-bearing:
    that one 422s, which raises BEFORE the row is written. Doing the same
    for a garnish would delete a visit from history over a big JPEG."""
    from routers import internal_camera_agent as ica
    from services.evidence_store import MAX_EVIDENCE_BYTES

    SessionLocal, cam_id, _uid = db
    _saver(monkeypatch, {b"hello": "ab/crop.jpg"})
    payload = ica.TrackEventIn(
        camera_id=cam_id, label="car", track_id="t3", started_at=T,
        evidence_jpeg_b64=B64,
        scene_jpeg_b64="A" * ((MAX_EVIDENCE_BYTES * 4) // 3 + 100),
    )
    _out, snap, _bg = _ingest(SessionLocal, payload)
    assert snap[0] == "ab/crop.jpg", "the visit lost its evidence"
    assert snap[1] is None, "an oversized scene must be dropped"


def test_a_corrupt_scene_drops_the_image_not_the_visit(db, monkeypatch):
    from routers import internal_camera_agent as ica

    SessionLocal, cam_id, _uid = db
    _saver(monkeypatch, {b"hello": "ab/crop.jpg"})
    payload = ica.TrackEventIn(
        camera_id=cam_id, label="car", track_id="t4", started_at=T,
        evidence_jpeg_b64=B64, scene_jpeg_b64="!!!not base64!!!",
    )
    _out, snap, _bg = _ingest(SessionLocal, payload)
    assert snap == ("ab/crop.jpg", None)


def test_the_scene_is_never_offered_to_plate_enrichment(db, monkeypatch):
    """A wide shot is the worst possible OCR input — the plate in it is a
    few pixels tall. A scene alone must not make a visit look enrichable."""
    from routers import internal_camera_agent as ica

    SessionLocal, cam_id, _uid = db
    _saver(monkeypatch, {b"hello": "cd/scene.jpg"})
    payload = ica.TrackEventIn(
        camera_id=cam_id, label="car", track_id="t5", started_at=T,
        evidence_jpeg_b64=None, scene_jpeg_b64=B64,
    )
    _out, snap, background = _ingest(SessionLocal, payload)
    assert snap[1] == "cd/scene.jpg"
    assert background.tasks == [], "the scene queued an OCR sweep"


def test_an_old_pipeline_payload_still_ingests(db, monkeypatch):
    """The field is optional on the wire in both directions: a pipeline that
    predates it omits the key, and pydantic ignores extras the other way."""
    from routers import internal_camera_agent as ica

    SessionLocal, cam_id, _uid = db
    _saver(monkeypatch, {b"hello": "ab/crop.jpg"})
    payload = ica.TrackEventIn(
        camera_id=cam_id, label="car", track_id="t6", started_at=T,
        evidence_jpeg_b64=B64,
    )
    _out, snap, _bg = _ingest(SessionLocal, payload)
    assert snap == ("ab/crop.jpg", None)


# ── serializer ──────────────────────────────────────────────────────


def _row(**kw):
    return models.TimelineEvent(
        id=1, camera_id=1, source="tier0", event_type="track", label="car",
        started_at=T, **kw,
    )


def test_serializer_offers_a_distinct_scene():
    from routers.timeline_events import _serialize

    out = _serialize(_row(evidence_path="ab/crop.jpg",
                          scene_evidence_path="cd/scene.jpg"))
    assert out["has_scene_evidence"] is True
    assert out["scene_evidence_url"] == "/api/v1/events/1/scene-evidence"


def test_serializer_hides_a_scene_identical_to_the_crop():
    """A frame-filling detection crops to the frame itself, and the
    content-addressed store then hands back the SAME path for both. One
    picture shown twice, labelled two different things, is worse than one
    picture shown once — so the flag reports nothing more to see."""
    from routers.timeline_events import _serialize

    out = _serialize(_row(evidence_path="ab/same.jpg",
                          scene_evidence_path="ab/same.jpg"))
    assert out["has_scene_evidence"] is False


def test_serializer_on_a_row_that_predates_the_column():
    from routers.timeline_events import _serialize

    out = _serialize(_row(evidence_path="ab/crop.jpg"))
    assert out["has_scene_evidence"] is False
    assert out["scene_evidence_url"] is None


# ── the route ───────────────────────────────────────────────────────


def _get_scene(session, event_id, user):
    from fastapi import HTTPException

    from routers.timeline_events import get_event_scene_evidence

    try:
        return asyncio.run(get_event_scene_evidence(event_id, user, session))
    except HTTPException as e:
        return e.status_code


def _event(s, cam_id, **kw):
    row = models.TimelineEvent(
        camera_id=cam_id, source="tier0", event_type="track", label="car",
        started_at=T, **kw,
    )
    s.add(row)
    s.commit()
    return row


def test_route_404s_when_no_scene_was_stored(db):
    from types import SimpleNamespace

    SessionLocal, cam_id, uid = db
    s = SessionLocal()
    try:
        row = _event(s, cam_id, evidence_path="ab/crop.jpg")
        user = SimpleNamespace(id=uid, is_superuser=False)
        assert _get_scene(s, row.id, user) == 404
    finally:
        s.close()


def test_route_404s_not_403_on_someone_elses_camera(db):
    """404, not 403: a 403 confirms the event exists on a camera the caller
    cannot see, which is itself the leak."""
    from types import SimpleNamespace

    SessionLocal, cam_id, uid = db
    s = SessionLocal()
    try:
        row = _event(s, cam_id, evidence_path="ab/crop.jpg",
                     scene_evidence_path="cd/scene.jpg")
        stranger = SimpleNamespace(id=uid + 999, is_superuser=False)
        assert _get_scene(s, row.id, stranger) == 404
    finally:
        s.close()


def test_route_404s_when_the_file_has_been_swept(db, monkeypatch, tmp_path):
    """Retention ages evidence out by mtime while the row lives on. Same
    behaviour /evidence already has: the row stays, the image 404s."""
    from types import SimpleNamespace

    from services import evidence_store as _ev

    monkeypatch.setattr(
        _ev, "settings",
        _types.SimpleNamespace(recordings_base_path=str(tmp_path)),
    )
    SessionLocal, cam_id, uid = db
    s = SessionLocal()
    try:
        row = _event(s, cam_id, scene_evidence_path="cd/gone.jpg")
        user = SimpleNamespace(id=uid, is_superuser=False)
        assert _get_scene(s, row.id, user) == 404
    finally:
        s.close()


# ── the read frame (plate_frame_path) ───────────────────────────────
#
# The scene and the vehicle crop are both the visit BEST-THUMBNAIL
# moment, and a visit is not reliably one vehicle: track association
# merges a departing car with the one arriving behind it, so those two
# images can show a different car from the one the plate came off
# (observed live 2026-09-03 — a black Audi captioned K884RS). The frame
# the plate was cut out of is the only image that cannot lie about that.


def test_store_plate_images_keeps_the_frame_as_well_as_the_crop(monkeypatch):
    from services import evidence_store as _ev
    from services import plate_enrichment as _pe

    saved = []

    def fake_save(b):
        saved.append(b)
        return f"ab/{len(saved)}.jpg"

    monkeypatch.setattr(_ev, "save_evidence_jpeg", fake_save)
    monkeypatch.setattr(_pe, "crop_to_plate_box", lambda j, b: b"PLATEPIXELS")

    crop, frame = _pe.store_plate_images(b"WHOLE-ATTEMPT", (1.0, 1.0, 9.0, 9.0))
    assert (crop, frame) == ("ab/1.jpg", "ab/2.jpg")
    assert saved == [b"PLATEPIXELS", b"WHOLE-ATTEMPT"]


def test_a_frame_is_kept_even_when_the_plate_box_is_unusable(monkeypatch):
    """#385 refuses to store a crop it could not narrow to the plate. The
    frame has no such requirement — it is the attempt itself, and it
    still answers WHICH car better than the visit best frame does."""
    from services import evidence_store as _ev
    from services import plate_enrichment as _pe

    monkeypatch.setattr(_ev, "save_evidence_jpeg", lambda b: "cd/frame.jpg")
    monkeypatch.setattr(_pe, "crop_to_plate_box", lambda j, b: None)

    crop, frame = _pe.store_plate_images(b"WHOLE-ATTEMPT", None)
    assert crop is None and frame == "cd/frame.jpg"


def test_storing_the_frame_never_raises(monkeypatch):
    from services import evidence_store as _ev
    from services import plate_enrichment as _pe

    def boom(_b):
        raise ValueError("disk said no")

    monkeypatch.setattr(_ev, "save_evidence_jpeg", boom)
    assert _pe.store_plate_frame(b"WHOLE-ATTEMPT") is None
    assert _pe.store_plate_frame(None) is None


def test_stamp_records_the_frame_beside_the_crop():
    from services.plate_enrichment import stamp_plate_evidence

    row = _row()
    stamp_plate_evidence(row, "ab/crop.jpg", frame_path="cd/frame.jpg")
    assert row.plate_evidence_path == "ab/crop.jpg"
    assert row.plate_frame_path == "cd/frame.jpg"


def test_serializer_offers_a_distinct_read_frame():
    from routers.timeline_events import _serialize

    out = _serialize(_row(evidence_path="ab/veh.jpg",
                          plate_frame_path="cd/frame.jpg"))
    assert out["has_plate_frame"] is True
    assert out["plate_frame_url"] == "/api/v1/events/1/plate-frame"


def test_serializer_keeps_a_read_frame_identical_to_the_vehicle_crop():
    """When the winning look IS the crop Tier-0 picked as the visit's
    best frame, both paths content-address to one file. That file is
    still the frame the plate was read from — the flag must say so, or
    the UI (which no longer shows the vehicle frame as a stand-in)
    reports "no read frame" for a row that has one. Seen live: rows
    27726 and 27783 on the reporting install."""
    from routers.timeline_events import _serialize

    out = _serialize(_row(evidence_path="ab/same.jpg",
                          plate_frame_path="ab/same.jpg"))
    assert out["has_plate_frame"] is True
    assert out["plate_frame_url"] == "/api/v1/events/1/plate-frame"


def test_plate_frame_route_404s_on_null_and_on_a_foreign_camera(db):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from routers.timeline_events import get_event_plate_frame

    def get(session, event_id, user):
        try:
            return asyncio.run(get_event_plate_frame(event_id, user, session))
        except HTTPException as exc:
            return exc.status_code

    SessionLocal, cam_id, uid = db
    s = SessionLocal()
    try:
        bare = _event(s, cam_id, evidence_path="ab/veh.jpg")
        owner = SimpleNamespace(id=uid, is_superuser=False)
        assert get(s, bare.id, owner) == 404

        withframe = _event(s, cam_id, evidence_path="ab/veh.jpg",
                           plate_frame_path="cd/frame.jpg")
        stranger = SimpleNamespace(id=uid + 999, is_superuser=False)
        assert get(s, withframe.id, stranger) == 404
    finally:
        s.close()


# ── connection release ──────────────────────────────────────────────
#
# FastAPI closes a yield-dependency session only AFTER the response body
# has shipped, and nginx serves this path with proxy_buffering off — so a
# JPEG transfer paced by a slow client used to pin one pooled connection,
# `idle in transaction`, for its whole duration. With one image per table
# row, a single page view could take the entire pool of 30; two
# connections were still pinned 20 MINUTES after their tab had gone.


def test_the_image_route_releases_its_connection_before_streaming(db):
    from types import SimpleNamespace

    from fastapi.responses import FileResponse

    from routers.timeline_events import get_event_scene_evidence

    SessionLocal, cam_id, uid = db
    s = SessionLocal()
    try:
        event_id = _event(s, cam_id, scene_evidence_path="cd/scene.jpg").id
        # Point resolve_evidence at a real file so we reach the return. The
        # route imports it INSIDE the function, so the patch has to land on
        # the source module, not on routers.timeline_events.
        import tempfile
        from pathlib import Path

        import services.evidence_store as _ev

        tmp = Path(tempfile.mkdtemp()) / "scene.jpg"
        tmp.write_bytes(b"JPEGBYTES")
        _orig = _ev.resolve_evidence
        _ev.resolve_evidence = lambda _p: tmp
        try:
            user = SimpleNamespace(id=uid, is_superuser=False)
            resp = asyncio.run(get_event_scene_evidence(event_id, user, s))
        finally:
            _ev.resolve_evidence = _orig
        assert isinstance(resp, FileResponse)
        assert not s.in_transaction(), (
            "the route still holds its transaction open while the body "
            "streams — this is what pinned the pool")
    finally:
        s.close()


def test_release_leaves_the_session_usable(db):
    """get_db calls close() again in its finally, and the tests hand the same
    session back in — release() must be a no-op on both, not a poison pill."""
    from core.database import release

    SessionLocal, cam_id, _uid = db
    s = SessionLocal()
    try:
        # Read the id BEFORE releasing: close() expunges every instance, so
        # a post-release row.id is a DetachedInstanceError. That contract is
        # exactly why the ingest route captures event_id up front.
        row_id = _event(s, cam_id, evidence_path="ab/crop.jpg").id
        release(s)
        release(s)                                   # idempotent
        assert s.get(models.TimelineEvent, row_id) is not None
    finally:
        s.close()


def test_ingest_releases_before_its_background_task_runs(db, monkeypatch):
    """Starlette runs BackgroundTasks inside the request exit stack, so an
    un-released ingest pins its connection for the whole OCR sweep. At ~1
    visit/sec that alone exhausted the pool."""
    from routers import internal_camera_agent as ica

    SessionLocal, cam_id, _uid = db
    _saver(monkeypatch, {b"hello": "ab/crop.jpg"})
    payload = ica.TrackEventIn(
        camera_id=cam_id, label="car", track_id="rel-1", started_at=T,
        evidence_jpeg_b64=B64,
    )
    from fastapi import BackgroundTasks

    background = BackgroundTasks()
    s = SessionLocal()
    try:
        out = asyncio.run(ica.ingest_track_event(payload, background, None, s))
        assert out["id"] is not None
        assert not s.in_transaction(), "ingest still holds its transaction"
    finally:
        s.close()
