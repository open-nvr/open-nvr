# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the event sink + HTTP camera provider."""
from __future__ import annotations

import io
import json

from detect_pipeline.bus import EventSink, subject_for
from detect_pipeline.pipeline import FrameResult
from detect_pipeline.providers import HttpCameraProvider
from detect_pipeline.tracking import Track


def _frame(seq=7, ts=1.5):
    class _F:
        pass
    f = _F(); f.seq = seq; f.ts = ts
    return f


def _track(tid=1, label="person", box=(10, 20, 40, 80), score=0.83):
    return Track(id=tid, label=label, box=box, score=score, confirmed=True)


# ── EventSink ───────────────────────────────────────────────────────

def test_subject_uses_inference_convention():
    assert subject_for("cam-front") == "opennvr.inference.tier0.cam-front.completed"


def test_sink_publishes_tracks_as_json():
    sent = []
    sink = EventSink(lambda subj, data: sent.append((subj, data)))
    result = FrameResult(tracks=[_track()], calibrating=False)
    sink.publish("cam-front", result, _frame())
    assert len(sent) == 1
    subj, data = sent[0]
    assert subj == "opennvr.inference.tier0.cam-front.completed"
    payload = json.loads(data)
    assert payload["camera_id"] == "cam-front"
    assert payload["adapter"] == "tier0"
    assert payload["tracks"][0]["id"] == 1 and payload["tracks"][0]["label"] == "person"
    assert payload["tracks"][0]["box"] == [10, 20, 40, 80]
    assert payload["tracks"][0]["best"] is False           # no crop retained here


def test_payload_best_flag_true_when_crop_retained():
    sent = []
    sink = EventSink(lambda subj, data: sent.append(data))
    t = _track()
    t.best_crop = object()                                 # a retained best crop
    sink.publish("cam-front", FrameResult(tracks=[t]), _frame())
    assert json.loads(sent[0])["tracks"][0]["best"] is True


def test_sink_skips_empty_by_default_but_can_publish_them():
    sent = []
    empty = FrameResult(tracks=[], calibrating=True)
    EventSink(lambda s, d: sent.append(1)).publish("c", empty, _frame())
    assert sent == []                                   # empty suppressed
    EventSink(lambda s, d: sent.append(1), publish_empty=True).publish("c", empty, _frame())
    assert sent == [1]                                  # opt-in publishes empties


def test_sink_publish_returns_whether_it_actually_published():
    # the return value is what lets the worker count *real* events, not no-op
    # frames (else tier0_events_published_total over-counts on quiet cameras).
    sink = EventSink(lambda s, d: None)
    assert sink.publish("c", FrameResult(tracks=[]), _frame()) is False
    assert sink.publish("c", FrameResult(tracks=[_track()]), _frame()) is True
    assert EventSink(lambda s, d: None, publish_empty=True).publish(
        "c", FrameResult(tracks=[]), _frame()) is True


# ── HttpCameraProvider ──────────────────────────────────────────────

class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_provider_maps_camera_agent_endpoint_to_specs():
    # shape of the existing GET /api/v1/internal/camera-agent/cameras endpoint
    body = json.dumps({"cameras": [
        {"camera_id": "cam1", "name": "Front", "frame_url": "rtsp://mediamtx:8554/cam-1?jwt=x", "source": "mediamtx"},
        {"camera_id": "cam2", "name": "Gate", "frame_url": "rtsp://u:p@10.0.0.9:554/sub", "source": "camera"},
    ]}).encode()

    captured = {}

    def opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["key"] = req.get_header("X-internal-api-key")
        return _FakeResp(body)

    prov = HttpCameraProvider("http://opennvr-core:8000/", api_key="secret", opener=opener)
    specs = prov.list_cameras()
    assert captured["url"] == "http://opennvr-core:8000/api/v1/internal/camera-agent/cameras"
    assert captured["key"] == "secret"
    assert [s.camera_id for s in specs] == ["cam1", "cam2"]
    assert specs[0].name == "Front"
    assert specs[0].substream_url == "rtsp://mediamtx:8554/cam-1?jwt=x"   # MediaMTX tap
    assert all(s.analyze for s in specs)                                 # on by default
    assert all(s.fps == 2 for s in specs)                                # default rate


def test_detect_fps_env_sets_default_rate(monkeypatch):
    # DETECT_FPS is the pipeline's main CPU dial (detection runs on every
    # analyzed frame): env sets the default, an explicit per-camera fps wins,
    # and garbage/out-of-range values degrade safely instead of crashing the
    # reconcile loop.
    from detect_pipeline.providers import _to_spec

    cam = {"camera_id": "c1", "frame_url": "rtsp://x/main"}

    monkeypatch.setenv("DETECT_FPS", "3")   # ≠ the built-in default (2), so this proves env wins
    assert _to_spec(cam).fps == 3
    # explicit per-camera fps beats the env default
    assert _to_spec({**cam, "fps": 7}).fps == 7
    # clamped to [1, 30]
    monkeypatch.setenv("DETECT_FPS", "0")
    assert _to_spec(cam).fps == 1
    monkeypatch.setenv("DETECT_FPS", "99")
    assert _to_spec(cam).fps == 30
    # non-integer falls back to the default (2), never raises
    monkeypatch.setenv("DETECT_FPS", "fast")
    assert _to_spec(cam).fps == 2
    monkeypatch.delenv("DETECT_FPS")
    assert _to_spec(cam).fps == 2


def test_provider_returns_empty_on_failure():
    def boom(req, timeout=None):
        raise OSError("connection refused")

    prov = HttpCameraProvider("http://core:8000", opener=boom)
    assert prov.list_cameras() == []                    # never raises; retries next tick


# ── Assignments → per-camera labels + opt-in skip (slice 3) ─────────


def _cam(cid="cam4", assignments=None):
    c = {"camera_id": cid, "name": cid, "frame_url": f"rtsp://m/{cid}"}
    if assignments is not None:
        c["assignments"] = assignments
    return c


def test_object_detection_assignment_labels_become_per_camera(monkeypatch):
    from detect_pipeline.providers import _to_spec

    spec = _to_spec(_cam(assignments=[
        {"skill": "object_detection", "labels": ["Person", "TRUCK", ""]},
        {"skill": "occupancy_counting"},
    ]))
    assert spec.labels == frozenset({"person", "truck"})
    assert spec.analyze is True


def test_no_assignments_means_global_labels_and_analyze(monkeypatch):
    from detect_pipeline.providers import _to_spec

    assert _to_spec(_cam()).labels is None
    assert _to_spec(_cam()).analyze is True
    # An object_detection assignment WITHOUT labels declares intent but no
    # narrowing — global DETECT_LABELS still applies.
    spec = _to_spec(_cam(assignments=[{"skill": "object_detection"}]))
    assert spec.labels is None and spec.analyze is True


def test_skip_unassigned_is_opt_in(monkeypatch):
    from detect_pipeline.providers import _to_spec

    lpr_only = _cam(assignments=[{"skill": "license_plate_recognition"}])
    # Default: an LPR-only camera is STILL analyzed — Tier-0 feeds many
    # subscribers (footage-search, the agent's events) beyond detection apps.
    monkeypatch.delenv("DETECT_SKIP_UNASSIGNED", raising=False)
    assert _to_spec(lpr_only).analyze is True

    # Opt-in: the operator declares the CPU saving is worth it.
    monkeypatch.setenv("DETECT_SKIP_UNASSIGNED", "true")
    assert _to_spec(lpr_only).analyze is False
    # A detection-shaped assignment keeps the camera analyzed even then...
    assert _to_spec(_cam(assignments=[
        {"skill": "license_plate_recognition"}, {"skill": "occupancy_counting"},
    ])).analyze is True
    # ...and NO assignments at all is never skipped (no restriction declared).
    assert _to_spec(_cam()).analyze is True
    assert _to_spec(_cam(assignments=[])).analyze is True
