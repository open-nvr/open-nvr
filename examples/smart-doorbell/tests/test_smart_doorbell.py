# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Orchestrator tests for SmartDoorbell — dedup, severity routing,
snapshot attachment for unknown faces, and the config loader.

Pipeline + KAI-C HTTP clients are exercised separately; here we
stub the pipeline.
"""
from __future__ import annotations

import base64
import dataclasses
from pathlib import Path
from typing import Iterable
from unittest.mock import MagicMock

import pytest

from face_recognition_pipeline import FaceRead
from smart_doorbell import (
    AppConfig,
    CameraConfig,
    SmartDoorbell,
    load_config,
)


# ── Helpers ────────────────────────────────────────────────────────


def _app_config(**overrides) -> AppConfig:
    base = AppConfig(
        kaic_url="http://localhost:8100",
        kaic_api_key="test-key",
        cameras=[CameraConfig(camera_id="front-door", frame_url="http://example.invalid/snap.jpg")],
        poll_interval_seconds=0.0,
        request_timeout_seconds=1.0,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _known_read(person_id: str = "alice", name: str = "Alice Smith",
                category: str = "family") -> FaceRead:
    return FaceRead(
        face_detected=True,
        recognized=True,
        person_id=person_id,
        name=name,
        category=category,
        similarity=0.91,
        face_bbox=(100, 80, 240, 240),
        threshold=0.5,
        correlation_id="cid-1",
    )


def _unknown_read() -> FaceRead:
    return FaceRead(
        face_detected=True,
        recognized=False,
        face_bbox=(80, 60, 220, 220),
        threshold=0.5,
        correlation_id="cid-1",
    )


def _no_face_read() -> FaceRead:
    return FaceRead(
        face_detected=False,
        recognized=False,
        correlation_id="cid-1",
    )


def _build_doorbell(reads_per_call: Iterable[FaceRead | None], config: AppConfig | None = None):
    cfg = config or _app_config()
    pipeline = MagicMock()
    pipeline.process_frame.side_effect = list(reads_per_call)
    dispatcher = MagicMock()
    doorbell = SmartDoorbell(cfg, pipeline, dispatcher)

    # Stub each camera's FrameSource so tests don't hit the network.
    class _StubFrameSource:
        def fetch(self) -> bytes:
            return b"\xff\xd8jpeg"

    for cam_id in list(doorbell._frame_sources):
        doorbell._frame_sources[cam_id] = _StubFrameSource()

    return doorbell, pipeline, dispatcher


# ── Config loader ──────────────────────────────────────────────────


def test_load_config_requires_kaic_url_and_api_key(tmp_path: Path):
    cfg = tmp_path / "c.yml"
    cfg.write_text("cameras:\n  - {camera_id: a, frame_url: http://x/a}\n")
    with pytest.raises(SystemExit, match="kaic_url"):
        load_config(cfg)


def test_load_config_allows_zero_cameras_for_enroll_subcommand(tmp_path: Path):
    """Enroll / list-faces don't need cameras configured. The daemon
    rejects later; the parser accepts."""
    cfg = tmp_path / "c.yml"
    cfg.write_text("kaic_url: http://x\nkaic_api_key: y\n")
    parsed = load_config(cfg)
    assert parsed.cameras == []


def test_load_config_carries_through_recognition_threshold(tmp_path: Path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        "kaic_url: http://x\nkaic_api_key: y\n"
        "cameras:\n  - {camera_id: a, frame_url: http://x/a}\n"
        "recognition_threshold: 0.7\n"
    )
    parsed = load_config(cfg)
    assert parsed.recognition_threshold == pytest.approx(0.7)


# ── Severity routing ──────────────────────────────────────────────


def test_known_family_fires_low_severity():
    doorbell, _pipeline, dispatcher = _build_doorbell([_known_read(category="family")])
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert alert.severity == "low"
    assert "Alice Smith" in alert.title


def test_known_non_family_fires_info_severity():
    """A registered face under a workplace category (staff, contractor,
    visitor) is a notice, not a greeting — info level."""
    doorbell, _pipeline, dispatcher = _build_doorbell([_known_read(category="staff")])
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert alert.severity == "info"
    assert alert.evidence["kind"] == "known_visitor"


def test_watchlist_match_alarms_high():
    """The one recognised face that must alarm louder than a stranger."""
    doorbell, _pipeline, dispatcher = _build_doorbell([_known_read(category="watchlist")])
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert alert.severity == "high"
    assert alert.evidence["kind"] == "watchlist_visitor"
    assert "Watchlist" in alert.title


def test_expired_pass_alarms_high_and_names_the_date():
    read = _known_read(category="contractor")
    read = dataclasses.replace(read, raw={"metadata": {"valid_until": "2020-01-31"}})
    doorbell, _pipeline, dispatcher = _build_doorbell([read])
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert alert.severity == "high"
    assert alert.evidence["kind"] == "expired_pass"
    assert "2020-01-31" in alert.description


def test_unexpired_pass_is_a_normal_known_visitor():
    read = _known_read(category="contractor")
    read = dataclasses.replace(read, raw={"metadata": {"valid_until": "2999-12-31"}})
    doorbell, _pipeline, dispatcher = _build_doorbell([read])
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert alert.severity == "info"
    assert alert.evidence["kind"] == "known_visitor"


def test_unknown_face_fires_high_severity():
    doorbell, _pipeline, dispatcher = _build_doorbell([_unknown_read()])
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert alert.severity == "high"
    assert "Unknown visitor" in alert.title


# ── No-face / pipeline-failure handling ───────────────────────────


def test_no_face_detected_does_not_dispatch():
    doorbell, _pipeline, dispatcher = _build_doorbell([_no_face_read()])
    doorbell.step()
    dispatcher.dispatch.assert_not_called()


def test_pipeline_returning_none_does_not_dispatch():
    """``process_frame`` returning None (call failed) means drop
    the frame quietly — same shape as no-face."""
    doorbell, _pipeline, dispatcher = _build_doorbell([None])
    doorbell.step()
    dispatcher.dispatch.assert_not_called()


# ── Dedup ──────────────────────────────────────────────────────────


def test_dedup_keyed_on_person_id_for_known():
    cfg = _app_config(dedup_window_seconds=60.0)
    doorbell, _pipeline, dispatcher = _build_doorbell(
        [_known_read(person_id="alice"), _known_read(person_id="alice")], config=cfg,
    )
    doorbell.step()
    doorbell.step()
    assert dispatcher.dispatch.call_count == 1


def test_dedup_distinguishes_different_known_persons():
    cfg = _app_config(dedup_window_seconds=60.0)
    doorbell, _pipeline, dispatcher = _build_doorbell(
        [_known_read(person_id="alice"), _known_read(person_id="bob", name="Bob")],
        config=cfg,
    )
    doorbell.step()
    doorbell.step()
    assert dispatcher.dispatch.call_count == 2


def test_dedup_keyed_on_unknown_bucket_per_camera():
    """Two unknown faces on the SAME camera within the window dedup
    to one alert — we have no person_id to distinguish them."""
    cfg = _app_config(dedup_window_seconds=60.0)
    doorbell, _pipeline, dispatcher = _build_doorbell(
        [_unknown_read(), _unknown_read()], config=cfg,
    )
    doorbell.step()
    doorbell.step()
    assert dispatcher.dispatch.call_count == 1


def test_dedup_window_zero_fires_every_time():
    cfg = _app_config(dedup_window_seconds=0.0)
    doorbell, _pipeline, dispatcher = _build_doorbell(
        [_known_read(), _known_read()], config=cfg,
    )
    doorbell.step()
    doorbell.step()
    assert dispatcher.dispatch.call_count == 2


# ── Snapshot attachment ───────────────────────────────────────────


def test_unknown_face_alert_carries_snapshot_when_enabled():
    cfg = _app_config(attach_snapshot_for_unknowns=True)
    doorbell, _pipeline, dispatcher = _build_doorbell([_unknown_read()], config=cfg)
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert "snapshot_b64" in alert.evidence
    assert alert.evidence["snapshot_mime"] == "image/jpeg"
    # Verify the base64 decodes to the stub frame bytes.
    assert base64.b64decode(alert.evidence["snapshot_b64"]) == b"\xff\xd8jpeg"


def test_unknown_face_alert_does_not_carry_snapshot_when_disabled():
    cfg = _app_config(attach_snapshot_for_unknowns=False)
    doorbell, _pipeline, dispatcher = _build_doorbell([_unknown_read()], config=cfg)
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert "snapshot_b64" not in alert.evidence


def test_known_face_never_carries_snapshot():
    """Known-face alerts intentionally ride small so the alert bus
    stays low-bandwidth in the common case."""
    cfg = _app_config(attach_snapshot_for_unknowns=True)
    doorbell, _pipeline, dispatcher = _build_doorbell([_known_read()], config=cfg)
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert "snapshot_b64" not in alert.evidence


def test_oversized_snapshot_dropped_from_envelope(caplog):
    """A snapshot above ``snapshot_max_bytes`` is dropped from the
    envelope (the alert still fires) and a WARN log line is emitted.
    Keeps post-base64 envelope under NATS's 1 MB default max_payload."""
    # Stub frame in _build_doorbell is 6 bytes; setting the cap to 3
    # forces a drop without needing to allocate a giant blob.
    cfg = _app_config(attach_snapshot_for_unknowns=True, snapshot_max_bytes=3)
    doorbell, _pipeline, dispatcher = _build_doorbell([_unknown_read()], config=cfg)
    with caplog.at_level("WARNING", logger="smart-doorbell"):
        doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert "snapshot_b64" not in alert.evidence
    assert any(
        "exceeds snapshot_max_bytes" in rec.getMessage() for rec in caplog.records
    )


def test_snapshot_cap_zero_disables_limit():
    """``snapshot_max_bytes=0`` means 'no cap' — useful for operators
    on NATS configured for >1 MB payloads."""
    cfg = _app_config(attach_snapshot_for_unknowns=True, snapshot_max_bytes=0)
    doorbell, _pipeline, dispatcher = _build_doorbell([_unknown_read()], config=cfg)
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert "snapshot_b64" in alert.evidence


# ── Evidence payload ──────────────────────────────────────────────


def test_alert_evidence_carries_recognition_metadata():
    doorbell, _pipeline, dispatcher = _build_doorbell([_known_read()])
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]
    e = alert.evidence
    assert e["recognized"] is True
    assert e["person_id"] == "alice"
    assert e["name"] == "Alice Smith"
    assert e["category"] == "family"
    assert e["similarity"] == pytest.approx(0.91, rel=1e-3)
    assert e["face_bbox"] == [100, 80, 240, 240]
    assert e["threshold"] == pytest.approx(0.5)


# ── Face-enrollment actions (catalog UI → adapter /faces/*) ─────────────


def _doorbell_with_faces_stub(monkeypatch, *, calls):
    """A doorbell whose _FaceAdminClient is replaced by a recorder."""
    doorbell, _p, _d = _build_doorbell([])

    class _FakeAdmin:
        def register(self, **kw):
            calls.append(("register", kw))
            return {"person_id": kw["person_id"], "status": "ok"}

        def list_faces(self, category=None):
            calls.append(("list", category))
            return {"faces": [
                {"person_id": "alex-rivera", "name": "Alex Rivera", "category": "known"},
                {"person_id": "sam-lee", "name": "Sam Lee", "category": "family"},
            ]}

        def delete_face(self, person_id):
            calls.append(("delete", person_id))
            return {"deleted": person_id}

    monkeypatch.setattr(doorbell, "_face_admin", lambda **kw: _FakeAdmin())
    return doorbell


def test_enroll_face_decodes_image_and_registers(monkeypatch):
    import base64 as _b64

    calls: list = []
    doorbell = _doorbell_with_faces_stub(monkeypatch, calls=calls)
    img = _b64.b64encode(b"\xff\xd8realish-jpeg-bytes").decode()

    out = doorbell.on_action("enroll_face", {"name": "Alex Rivera", "image": img})

    assert out["enrolled"]["person_id"] == "alex-rivera"       # slug of the name
    assert out["enrolled"]["category"] == "family"             # default
    (verb, kw) = calls[0]
    assert verb == "register"
    assert kw["name"] == "Alex Rivera"
    assert kw["image_bytes"] == b"\xff\xd8realish-jpeg-bytes"  # decoded


def test_enroll_face_strips_data_url_prefix(monkeypatch):
    import base64 as _b64

    calls: list = []
    doorbell = _doorbell_with_faces_stub(monkeypatch, calls=calls)
    raw = _b64.b64encode(b"jpegbytes").decode()
    out = doorbell.on_action("enroll_face", {
        "name": "Sam", "image": f"data:image/jpeg;base64,{raw}",
    })
    assert out["enrolled"]["person_id"] == "sam"
    assert calls[0][1]["image_bytes"] == b"jpegbytes"


def test_enroll_face_validation_errors(monkeypatch):
    import pytest as _pytest

    doorbell = _doorbell_with_faces_stub(monkeypatch, calls=[])
    with _pytest.raises(ValueError, match="'name' is required"):
        doorbell.on_action("enroll_face", {"image": "x"})
    with _pytest.raises(ValueError, match="'image' is required"):
        doorbell.on_action("enroll_face", {"name": "A"})
    with _pytest.raises(ValueError, match="not valid base64"):
        doorbell.on_action("enroll_face", {"name": "A", "image": "!!!not base64!!!"})


def test_list_faces_returns_table_rows(monkeypatch):
    calls: list = []
    doorbell = _doorbell_with_faces_stub(monkeypatch, calls=calls)
    out = doorbell.on_action("list_faces", {})
    assert [r["person_id"] for r in out["results"]] == ["alex-rivera", "sam-lee"]
    assert out["results"][0]["name"] == "Alex Rivera"


def test_delete_face(monkeypatch):
    import pytest as _pytest

    calls: list = []
    doorbell = _doorbell_with_faces_stub(monkeypatch, calls=calls)
    assert doorbell.on_action("delete_face", {"person_id": "alex-rivera"})["deleted"] == "alex-rivera"
    assert calls[0] == ("delete", "alex-rivera")
    with _pytest.raises(ValueError, match="'person_id' is required"):
        doorbell.on_action("delete_face", {})


def test_unknown_action_raises_keyerror(monkeypatch):
    import pytest as _pytest

    doorbell = _doorbell_with_faces_stub(monkeypatch, calls=[])
    with _pytest.raises(KeyError):
        doorbell.on_action("nope", {})


# ── Dashboard surfaces: /state views, /ui, live config ─────────────


def _real_jpeg(w: int = 640, h: int = 480) -> bytes:
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 60, 30)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def test_state_carries_the_views_the_manifest_declares(monkeypatch):
    """Every state_schema path resolves in state_snapshot — a declared view
    whose path is missing renders as an em-dash forever and nobody notices."""
    doorbell, _, _ = _build_doorbell([_known_read(), _unknown_read()])
    monkeypatch.setattr(doorbell, "_face_admin",
                        lambda **kw: MagicMock(list_faces=lambda: {"faces": [{}, {}, {}]}))
    doorbell.step(); doorbell.step()
    state = doorbell.state_snapshot()
    from smart_doorbell import MANIFEST
    for view in MANIFEST.state_schema:
        node = state
        for part in view.path.split("."):
            assert part in node, f"view {view.name!r}: path {view.path!r} missing from /state"
            node = node[part]
    assert state["enrolled_faces"] == 3
    assert state["visits"] == {"known": 1, "unknown": 1}
    assert [r["camera_id"] for r in state["camera_health"]] == ["front-door"]
    assert state["camera_health"][0]["status"] == "ok"
    assert state["recent"][-1]["camera"] == "front-door"
    assert state["recent"][0]["name"] == "Alice Smith"
    assert state["recent"][0]["category"] == "family"


def test_stranger_gallery_holds_a_thumbnail_not_the_frame():
    """A 640x480 frame becomes a ~190px data: URI; /state is polled every
    few seconds, so the wall must stay cheap."""
    doorbell, _, _ = _build_doorbell([_unknown_read()])
    frame = _real_jpeg()

    class _Src:
        def fetch(self) -> bytes:
            return frame
    doorbell._frame_sources["front-door"] = _Src()
    doorbell.step()
    gallery = doorbell.state_snapshot()["stranger_gallery"]
    assert len(gallery) == 1
    uri = gallery[0]["image"]
    assert uri.startswith("data:image/jpeg;base64,")
    assert len(uri) < len(frame)  # shrunk, not merely re-encoded
    assert gallery[0]["label"] == "front-door"


def test_known_visitor_never_lands_on_the_stranger_wall():
    doorbell, _, _ = _build_doorbell([_known_read()])
    doorbell.step()
    assert doorbell.state_snapshot()["stranger_gallery"] == []


def test_enrolled_count_is_cached_and_survives_an_unreachable_adapter(monkeypatch):
    doorbell, _, _ = _build_doorbell([])
    calls = []

    def _admin(**kw):
        calls.append(1)
        raise ConnectionError("adapter down")
    monkeypatch.setattr(doorbell, "_face_admin", _admin)
    assert doorbell.state_snapshot()["enrolled_faces"] is None
    assert doorbell.state_snapshot()["enrolled_faces"] is None
    assert len(calls) == 1, "a failed lookup must not be retried on every poll"


def test_camera_health_reports_a_dead_camera():
    doorbell, _, _ = _build_doorbell([])

    class _Dead:
        def fetch(self) -> bytes:
            raise RuntimeError("connection refused")
    doorbell._frame_sources["front-door"] = _Dead()
    doorbell.step()
    row = doorbell.state_snapshot()["camera_health"][0]
    assert row["status"] == "error"
    assert "connection refused" in row["error"]


def test_ui_html_is_static_and_escapes_operator_data(monkeypatch):
    doorbell, _, _ = _build_doorbell([_known_read(name="<b>Mallory</b>")])
    monkeypatch.setattr(doorbell, "_face_admin",
                        lambda **kw: MagicMock(list_faces=lambda: {"faces": []}))
    doorbell.step()
    page = doorbell.ui_html()
    assert "<script" not in page.lower()
    assert "&lt;b&gt;Mallory&lt;/b&gt;" in page
    assert "<b>Mallory</b>" not in page
    assert "front-door" in page


def test_config_form_edits_apply_live():
    from face_recognition_pipeline import FaceRecognitionPipelineConfig
    doorbell, pipeline, _ = _build_doorbell([])
    pipeline.config = FaceRecognitionPipelineConfig(recognition_threshold=0.5)
    doorbell.on_config_update({"recognition_threshold": 0.8, "dedup_window_seconds": 5})
    assert pipeline.config.recognition_threshold == 0.8
    assert doorbell.config.recognition_threshold == 0.8
    assert doorbell.config.dedup_window_seconds == 5.0
    # Idempotent: re-delivering the same values changes nothing.
    doorbell.on_config_update({"recognition_threshold": 0.8})
    assert pipeline.config.recognition_threshold == 0.8


def test_enroll_rejects_a_category_outside_the_vocabulary(monkeypatch):
    import base64 as _b64
    doorbell = _doorbell_with_faces_stub(monkeypatch, calls=[])
    img = _b64.b64encode(b"\xff\xd8jpeg").decode()
    with pytest.raises(ValueError, match="category"):
        doorbell.on_action("enroll_face", {"name": "A", "image": img, "category": "boss"})
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        doorbell.on_action("enroll_face", {"name": "A", "image": img, "valid_until": "31/12/2026"})


def test_enroll_stores_notes_expiry_and_a_thumbnail_in_metadata(monkeypatch):
    import base64 as _b64
    calls: list = []
    doorbell = _doorbell_with_faces_stub(monkeypatch, calls=calls)
    img = _b64.b64encode(_real_jpeg(320, 320)).decode()
    out = doorbell.on_action("enroll_face", {
        "name": "Priya Nair", "image": img, "category": "contractor",
        "notes": "Lift maintenance, Otis", "valid_until": "2026-12-31",
    })
    verb, kw = calls[0]
    assert verb == "register"
    assert kw["category"] == "contractor"
    assert kw["metadata"]["notes"] == "Lift maintenance, Otis"
    assert kw["metadata"]["valid_until"] == "2026-12-31"
    assert kw["metadata"]["thumbnail"].startswith("data:image/jpeg;base64,")
    assert out["enrolled"]["valid_until"] == "2026-12-31"


def test_enroll_from_the_strangers_wall_uses_the_door_crop(monkeypatch):
    calls: list = []
    doorbell, _, _ = _build_doorbell([_unknown_read()])
    frame = _real_jpeg()

    class _Src:
        def fetch(self) -> bytes:
            return frame
    doorbell._frame_sources["front-door"] = _Src()
    doorbell.step()
    sid = doorbell.state_snapshot()["stranger_gallery"][0]["id"]

    class _FakeAdmin:
        def register(self, **kw):
            calls.append(kw); return {"ok": True}
    monkeypatch.setattr(doorbell, "_face_admin", lambda **kw: _FakeAdmin())

    img = doorbell.on_action("stranger_image", {"stranger_id": sid})
    assert img["mime"] == "image/jpeg" and len(img["image"]) > 100

    out = doorbell.on_action("enroll_stranger", {"stranger_id": sid, "name": "Courier Dev", "category": "visitor"})
    assert out["enrolled"]["person_id"] == "courier-dev"
    assert calls[0]["image_bytes"]  # the crop, not the operator's upload
    assert len(calls[0]["image_bytes"]) < len(frame)

    with pytest.raises(KeyError):
        doorbell.on_action("enroll_stranger", {"stranger_id": "nope", "name": "X"})


def test_list_faces_merges_metadata_and_last_seen(monkeypatch):
    doorbell, _, _ = _build_doorbell([_known_read(person_id="sam-lee")])
    doorbell.step()

    class _FakeAdmin:
        def list_faces(self, category=None):
            return {"faces": [{"person_id": "sam-lee", "name": "Sam Lee", "category": "family",
                               "metadata": {"notes": "Flat 4B", "valid_until": "2020-01-01"}}]}
    monkeypatch.setattr(doorbell, "_face_admin", lambda **kw: _FakeAdmin())
    out = doorbell.on_action("list_faces", {})
    row = out["results"][0]
    assert row["notes"] == "Flat 4B"
    assert row["expired"] is True
    assert row["last_seen"] is not None
    assert "watchlist" in out["categories"]


def test_update_face_sends_only_the_changed_fields(monkeypatch):
    calls: list = []
    doorbell, _, _ = _build_doorbell([])

    class _FakeAdmin:
        def update(self, person_id, **changes):
            calls.append((person_id, changes)); return {"ok": True}
    monkeypatch.setattr(doorbell, "_face_admin", lambda **kw: _FakeAdmin())
    doorbell.on_action("update_face", {"person_id": "sam-lee", "category": "staff", "notes": "Night shift"})
    pid, changes = calls[0]
    assert pid == "sam-lee"
    assert changes == {"category": "staff", "metadata": {"notes": "Night shift"}}
    with pytest.raises(ValueError, match="nothing to change"):
        doorbell.on_action("update_face", {"person_id": "sam-lee"})


def test_expiry_is_looked_up_from_the_directory_when_the_match_lacks_it(monkeypatch):
    """The adapter's recognition reply has no metadata; the app must still
    know a contractor's pass ran out."""
    doorbell, _pipeline, dispatcher = _build_doorbell([_known_read(person_id="otis-1", category="contractor")])

    class _FakeAdmin:
        def list_faces(self, category=None):
            return {"faces": [{"person_id": "otis-1", "name": "Otis", "category": "contractor",
                               "metadata": {"valid_until": "2020-01-31"}}]}
    monkeypatch.setattr(doorbell, "_face_admin", lambda **kw: _FakeAdmin())
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]
    assert alert.evidence["kind"] == "expired_pass"


def test_directory_refresh_uses_a_short_timeout_not_the_enrolment_budget(monkeypatch):
    """/state and the run loop both refresh the directory; a silent
    adapter must cost them seconds, not the 30 s a photo upload may take."""
    doorbell, _, _ = _build_doorbell([])
    seen: list = []
    real = doorbell._face_admin

    def spy(*, timeout_seconds=None):
        seen.append(timeout_seconds)
        return real(timeout_seconds=timeout_seconds)
    monkeypatch.setattr(doorbell, "_face_admin", spy)
    monkeypatch.setattr("smart_doorbell._FaceAdminClient.list_faces", lambda self: {"faces": []})
    doorbell.state_snapshot()
    assert seen == [doorbell._DIRECTORY_TIMEOUT_S]
    assert doorbell._DIRECTORY_TIMEOUT_S < doorbell.config.request_timeout_seconds or doorbell.config.request_timeout_seconds <= 3


def test_stranger_thumbnail_is_the_face_not_the_porch():
    """The wall tile is built from the face crop, so a wide porch camera
    still shows a face at 120 px."""
    doorbell, _, _ = _build_doorbell([_unknown_read()])
    frame = _real_jpeg(1280, 720)

    class _Src:
        def fetch(self) -> bytes:
            return frame
    doorbell._frame_sources["front-door"] = _Src()
    doorbell.step()
    from PIL import Image
    import base64 as _b64, io as _io
    tile = doorbell.state_snapshot()["stranger_gallery"][0]["image"].split(",", 1)[1]
    with Image.open(_io.BytesIO(_b64.b64decode(tile))) as im:
        w, h = im.size
    # _unknown_read's bbox is 140x160 with half-face margins → roughly square,
    # nothing like the 16:9 frame.
    assert 0.7 < w / h < 1.4


# ── Multi-photo enrolment ───────────────────────────────────────────


def test_append_adds_a_photo_to_an_existing_person_without_touching_their_details(monkeypatch):
    import base64 as _b64
    calls: list = []
    doorbell = _doorbell_with_faces_stub(monkeypatch, calls=calls)
    img = _b64.b64encode(_real_jpeg(320, 320)).decode()
    out = doorbell.on_action("enroll_face", {"person_id": "alice-rivera", "image": img, "append": True})
    verb, kw = calls[0]
    assert kw["append"] is True and kw["person_id"] == "alice-rivera"
    assert kw["name"] == "" and kw["category"] == ""          # unchanged on the adapter side
    assert kw["metadata"] == {}                                 # no blank notes, no new avatar
    assert out["enrolled"]["appended"] is True
    # Notes given on an append DO update; a bad date is still refused.
    doorbell.on_action("enroll_face", {"person_id": "alice-rivera", "image": img, "append": True, "notes": "Flat 4B"})
    assert calls[1][1]["metadata"] == {"notes": "Flat 4B"}
    with pytest.raises(ValueError, match="person_id"):
        doorbell.on_action("enroll_face", {"image": img, "append": True})


def test_stranger_assigned_to_a_person_becomes_their_sample_and_leaves_the_wall(monkeypatch):
    calls: list = []
    doorbell, _, _ = _build_doorbell([_unknown_read(), _unknown_read()])
    frame = _real_jpeg()

    class _Src:
        def fetch(self) -> bytes:
            return frame
    doorbell._frame_sources["front-door"] = _Src()
    doorbell.config.dedup_window_seconds = 0
    doorbell.step(); doorbell.step()
    wall = doorbell.state_snapshot()["stranger_gallery"]
    assert len(wall) == 2
    sid = wall[0]["id"]

    class _FakeAdmin:
        def register(self, **kw):
            calls.append(kw); return {"ok": True, "face": {"name": "Alice Rivera", "category": "family", "samples": 3, "metadata": {}}}
    monkeypatch.setattr(doorbell, "_face_admin", lambda **kw: _FakeAdmin())

    out = doorbell.on_action("enroll_stranger", {"stranger_id": sid, "person_id": "alice-rivera"})
    assert calls[0]["append"] is True and calls[0]["person_id"] == "alice-rivera"
    assert out["enrolled"]["name"] == "Alice Rivera" and out["enrolled"]["samples"] == 3
    remaining = doorbell.state_snapshot()["stranger_gallery"]
    assert [g["id"] for g in remaining] == [wall[1]["id"]]       # the assigned capture is gone
    with pytest.raises(KeyError):
        doorbell.on_action("stranger_image", {"stranger_id": sid})


def test_list_faces_reports_sample_counts(monkeypatch):
    doorbell, _, _ = _build_doorbell([])

    class _FakeAdmin:
        def list_faces(self, category=None):
            return {"faces": [{"person_id": "a", "name": "A", "category": "family", "samples": 4},
                              {"person_id": "b", "name": "B", "category": "staff"}]}   # older adapter
    monkeypatch.setattr(doorbell, "_face_admin", lambda **kw: _FakeAdmin())
    rows = doorbell.on_action("list_faces", {})["results"]
    assert [r["samples"] for r in rows] == [4, 1]


# ── Connected: cameras picked in the catalog ───────────────────────


def test_a_picked_camera_is_watched_through_core_and_reported_on_the_dashboard():
    doorbell, pipeline, _ = _build_doorbell([_known_read()], _app_config(cameras=[]))
    doorbell._config_poll_thread = object()   # picks arrive on the config poll

    class _Core:
        def get_frame(self, camera_id):
            return b"\xff\xd8jpeg"

    doorbell._source._core = _Core()
    doorbell.on_cameras_update(frozenset({5}))
    doorbell.step()
    assert pipeline.process_frame.call_count == 1
    state = doorbell.state_snapshot()
    assert state["cameras"] == ["cam5"]
    assert [row["camera_id"] for row in state["camera_health"]] == ["cam5"]
    assert state["camera_health"][0]["status"] == "ok"


def test_nothing_picked_watches_nothing():
    doorbell, pipeline, _ = _build_doorbell([_known_read()], _app_config(cameras=[]))
    doorbell._config_poll_thread = object()
    doorbell.on_cameras_update(frozenset())
    doorbell.step()
    assert pipeline.process_frame.call_count == 0
    assert doorbell.state_snapshot()["cameras"] == []


# ── How loud a face is, is the site's decision ───────────────────────
#
# A stranger at a family front door and a stranger at a depot gate are
# the same event and not the same emergency. The severities were hard
# coded at "high", which is neither the loudest the platform offers nor
# adjustable — so a site that wanted a stranger to actually alarm had no
# way to say so, and a site that found it shrill had no way to soften
# it. Defaults are unchanged, so an existing install hears no difference.


def test_a_stranger_can_be_made_critical():
    """The ask: unknown faces should be able to alarm at the top level."""
    cfg = _app_config(unknown_severity="critical")
    doorbell, _p, dispatcher = _build_doorbell([_unknown_read()], config=cfg)
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]

    assert alert.severity == "critical"
    assert alert.evidence["kind"] == "unknown_visitor"


def test_the_stranger_default_is_unchanged():
    """A shipped default getting louder without being asked is its own
    kind of bug — somebody's night gets interrupted by an upgrade."""
    doorbell, _p, dispatcher = _build_doorbell([_unknown_read()])
    doorbell.step()
    assert dispatcher.dispatch.call_args.args[0].severity == "high"


def test_known_severity_overrides_the_category_table():
    cfg = _app_config(known_severity="medium")
    doorbell, _p, dispatcher = _build_doorbell(
        [_known_read(category="family")], config=cfg)
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]

    assert alert.severity == "medium"
    assert alert.evidence["kind"] == "known_visitor"


def test_a_watchlist_match_is_never_quietened_by_it():
    """The knob is for ordinary visitors. A watchlist face and an
    expired pass are the two recognised people who must stay loud, so
    they are decided BEFORE it is read — otherwise softening the
    doorbell would silently soften the alarm it exists for.
    """
    cfg = _app_config(known_severity="info")
    doorbell, _p, dispatcher = _build_doorbell(
        [_known_read(category="watchlist")], config=cfg)
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]

    assert alert.severity == "high"
    assert alert.evidence["kind"] == "watchlist_visitor"


def test_an_expired_pass_is_never_quietened_by_it():
    cfg = _app_config(known_severity="info")
    read = dataclasses.replace(_known_read(category="contractor"),
                               raw={"metadata": {"valid_until": "2020-01-31"}})
    doorbell, _p, dispatcher = _build_doorbell([read], config=cfg)
    doorbell.step()
    alert = dispatcher.dispatch.call_args.args[0]

    assert alert.severity == "high"
    assert alert.evidence["kind"] == "expired_pass"


def test_a_misspelled_severity_falls_back_and_says_so(caplog):
    """Silently ignoring a typo is the wrong failure: the operator sets
    `critcal`, hears no change, and concludes the setting does nothing —
    which is true, and invisible."""
    from smart_doorbell import _severity

    with caplog.at_level("WARNING"):
        assert _severity("critcal", "high") == "high"
    assert "critcal" in caplog.text, caplog.text

    # An empty value is not a typo — it is the documented way to say
    # "use the default" — so it must not warn.
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert _severity("", "high") == "high"
        assert _severity(None, "high") == "high"
    assert caplog.text == "", "an unset value is not a mistake"

    # Case and whitespace are an operator typing, not an error.
    assert _severity("  CRITICAL ", "high") == "critical"
