# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Per-camera health stats (HA-105).

Pinned here:

* the reducer now reports per-camera mean inference time and skipped frames,
  SUMMING across the extra label (model / reason) instead of keeping one;
* bitrate comes from differencing MediaMTX's bytesReceived between reads:
  null on the first read, after a counter reset, or across a long gap;
* every absent source (pipeline down, MediaMTX admin off, paused camera)
  yields nulls, never an error;
* days retained counts from the oldest indexed segment;
* the endpoint refuses unknown/deleted cameras (404) and cameras the caller
  cannot view (403).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("SECRET_KEY", "s" * 64)
os.environ.setdefault("INTERNAL_API_KEY", "x" * 48)
os.environ.setdefault("MEDIAMTX_SECRET", "m" * 48)

_LOGGERS = ("main_logger", "auth_logger", "camera_logger", "recording_logger",
            "rtsp_logger", "api_logger", "mediamtx_logger", "config_logger",
            "storage_logger", "stream_logger", "ai_logger", "system_logger",
            "security_logger")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **kw: None


@pytest.fixture(autouse=True)
def _complete_logging_stub():
    """Sibling modules install a minimal core.logging_config stub, and the
    suite's conftest swaps modules per test file, so the stub in place at RUN
    time may lack loggers our lazy imports (core.auth, mediamtx admin) need.
    Backfill whatever module is current, at run time, without replacing it."""
    lc = sys.modules.get("core.logging_config")
    if lc is not None:
        for name in _LOGGERS:
            if getattr(lc, name, None) is None:
                try:
                    setattr(lc, name, _L())
                except (AttributeError, TypeError):
                    pass
    yield


import models  # noqa: E402
from services import camera_stats  # noqa: E402
from services.tier0_metrics import parse_prometheus_text, reduce_metrics  # noqa: E402

METRICS = """
tier0_processing_fps{camera="cam1"} 4.5
tier0_target_fps{camera="cam1"} 5
tier0_worker_up{camera="cam1"} 1
tier0_tracks_active{camera="cam1"} 2
tier0_detector_latency_seconds_sum{camera="cam1",model="yolo-a"} 3.0
tier0_detector_latency_seconds_count{camera="cam1",model="yolo-a"} 100
tier0_detector_latency_seconds_sum{camera="cam1",model="yolo-b"} 1.0
tier0_detector_latency_seconds_count{camera="cam1",model="yolo-b"} 100
tier0_detector_skipped_total{camera="cam1",reason="no_motion"} 40
tier0_detector_skipped_total{camera="cam1",reason="calibrating"} 2
tier0_worker_up{camera="cam2"} 1
"""


def test_reducer_sums_across_model_and_reason():
    rows = {r["camera"]: r for r in reduce_metrics(parse_prometheus_text(METRICS))["cameras"]}
    # (3.0 + 1.0) s over 200 runs = 20 ms, not the last series' 10 ms
    assert rows["cam1"]["inference_ms"] == 20.0
    assert rows["cam1"]["skipped_total"] == 42
    # a camera with no detector runs yet: unknown, not zero
    assert rows["cam2"]["inference_ms"] is None
    assert rows["cam2"]["skipped_total"] == 0


def test_bitrate_is_a_difference_between_reads():
    camera_stats._last_bytes.clear()
    assert camera_stats._bitrate_kbps("p", 1_000_000, 100.0) is None     # first read
    assert camera_stats._bitrate_kbps("p", 1_125_000, 110.0) == 100.0    # 125 kB in 10 s
    assert camera_stats._bitrate_kbps("p", 50, 120.0) is None             # counter reset
    assert camera_stats._bitrate_kbps("p", 1_000, 1000.0) is None         # stale gap
    assert camera_stats._bitrate_kbps("p", None, 1010.0) is None


@pytest.fixture()
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    role = models.Role(name="admin")
    s.add(role)
    s.commit()
    s.add(models.User(id=1, username="admin", email="a@x", hashed_password="h",
                      is_active=True, is_superuser=True, role_id=role.id))
    s.commit()
    yield s
    s.close()
    eng.dispose()


def _camera(db, cid=1, active=True):
    cam = models.Camera(id=cid, name=f"c{cid}", ip_address=f"192.0.2.{cid}",
                        rtsp_url=f"rtsp://192.0.2.{cid}/s", owner_id=1,
                        is_active=active)
    db.add(cam)
    db.commit()
    return cam


def _patch_sources(monkeypatch, *, path_info=None, metrics=None):
    import services.mediamtx_admin_service as mm
    import services.tier0_metrics as t0

    async def _path(path):
        return path_info or {"status": "no_admin_api"}

    async def _metrics():
        return metrics if metrics is not None else {"available": False}

    monkeypatch.setattr(mm.MediaMtxAdminService, "get_active_path_info",
                        staticmethod(_path))
    monkeypatch.setattr(t0, "get_tier0_metrics", _metrics)
    monkeypatch.setattr(camera_stats, "_recording_state",
                        lambda db, cam, now: "recording")


def test_stats_join_all_sources(db, monkeypatch):
    cam = _camera(db)
    now = datetime.now(UTC)
    db.add(models.Recording(camera_id=1, start_time=now - timedelta(days=3),
                            end_time=now - timedelta(days=3) + timedelta(seconds=60),
                            filename="a.mp4", file_path="/r/a.mp4"))
    db.commit()
    camera_stats._last_bytes.clear()
    metrics = reduce_metrics(parse_prometheus_text(METRICS))
    _patch_sources(monkeypatch, metrics=metrics, path_info={
        "status": "ok", "details": {"ready": True, "bytesReceived": 5000}})

    s = asyncio.run(camera_stats.get_camera_stats(db, cam))
    assert s["stream_ready"] is True and s["bitrate_kbps"] is None  # first read
    assert s["detect_fps"] == 4.5 and s["target_fps"] == 5.0
    assert s["inference_ms"] == 20.0 and s["skipped_total"] == 42
    assert s["tracks_active"] == 2 and s["detect_up"] is True
    assert s["recording_state"] == "recording"
    assert 2.9 < s["days_retained"] < 3.1


def test_absent_sources_are_nulls_not_errors(db, monkeypatch):
    cam = _camera(db, cid=2, active=False)   # paused: no MediaMTX path
    camera_stats._last_bytes.clear()
    _patch_sources(monkeypatch)
    s = asyncio.run(camera_stats.get_camera_stats(db, cam))
    assert s["is_active"] is False
    for key in ("stream_ready", "bitrate_kbps", "detect_fps", "inference_ms",
                "skipped_total", "days_retained"):
        assert s[key] is None, key


def test_endpoint_404_and_403(db, monkeypatch):
    cams = importlib.import_module("routers.cameras")
    import services.camera_scope as scope

    _camera(db, cid=3)
    deleted = _camera(db, cid=4)
    deleted.deleted_at = datetime.now(UTC)
    db.commit()
    user = SimpleNamespace(id=9, is_superuser=False)

    with pytest.raises(Exception) as missing:
        asyncio.run(cams.get_camera_stats(camera_id=99, db=db, current_user=user))
    assert getattr(missing.value, "status_code", None) == 404
    with pytest.raises(Exception) as gone:
        asyncio.run(cams.get_camera_stats(camera_id=4, db=db, current_user=user))
    assert getattr(gone.value, "status_code", None) == 404

    monkeypatch.setattr(scope, "can_view_camera", lambda db, u, cid: False)
    with pytest.raises(Exception) as forbidden:
        asyncio.run(cams.get_camera_stats(camera_id=3, db=db, current_user=user))
    assert getattr(forbidden.value, "status_code", None) == 403
