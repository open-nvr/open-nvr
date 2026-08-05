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


def test_provider_returns_empty_on_failure():
    def boom(req, timeout=None):
        raise OSError("connection refused")

    prov = HttpCameraProvider("http://core:8000", opener=boom)
    assert prov.list_cameras() == []                    # never raises; retries next tick
