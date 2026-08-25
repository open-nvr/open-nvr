# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visit lifecycle + poster — the tier0 half of the events store (RFC-0001 C1)."""

import io
import json
from dataclasses import dataclass, field, replace

import pytest

from detect_pipeline.events_poster import Visit, VisitLifecycle, VisitPoster

T0 = 1_700_000_000.0


@dataclass
class _Tr:
    id: int
    label: str = "person"
    score: float = 0.8
    stationary: bool = False
    confirmed: bool = True
    best_crop: object = field(default=None)


# ── lifecycle ───────────────────────────────────────────────────────

def test_visit_finishes_when_track_vanishes():
    lc = VisitLifecycle("3")
    assert lc.observe([_Tr(1)], T0) == []
    assert lc.observe([_Tr(1, score=0.9)], T0 + 5) == []      # still here
    done = lc.observe([], T0 + 6)                              # gone
    assert len(done) == 1
    v = done[0]
    assert (v.camera_id, v.label, v.track_id) == ("3", "person", "1")
    assert v.score == 0.9                                      # max over life
    assert (v.ended_at - v.started_at).total_seconds() == 5.0


def test_flickers_never_become_history():
    lc = VisitLifecycle("3", min_duration_s=1.0)
    lc.observe([_Tr(1, confirmed=False)], T0)
    assert lc.observe([], T0 + 10) == []                       # never confirmed
    lc.observe([_Tr(2)], T0)
    assert lc.observe([], T0 + 0.3) == []                      # too short


def test_flush_finishes_live_visits():
    lc = VisitLifecycle("3")
    lc.observe([_Tr(1), _Tr(2)], T0)
    lc.observe([_Tr(1), _Tr(2)], T0 + 3)
    assert {v.track_id for v in lc.flush()} == {"1", "2"}
    assert lc.flush() == []


def test_crop_encoded_via_bestframe(monkeypatch):
    import detect_pipeline.bestframe as bf
    monkeypatch.setattr(bf, "_encode_jpeg", lambda crop, quality=85: b"\xff\xd8jpg")
    lc = VisitLifecycle("3")
    lc.observe([_Tr(1, best_crop=object())], T0)
    lc.observe([_Tr(1, best_crop=object())], T0 + 2)
    done = lc.observe([], T0 + 3)
    assert done[0].jpeg == b"\xff\xd8jpg"


# ── poster ──────────────────────────────────────────────────────────

def _visit(jpeg=None):
    from datetime import datetime, timezone
    t = datetime.fromtimestamp(T0, tz=timezone.utc)
    return Visit("3", "person", 0.9, "7", t, t, False, jpeg)


class _Resp(io.BytesIO):
    status = 201
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_post_carries_auth_and_evidence():
    seen = {}
    def opener(req, timeout=None):
        seen["url"] = req.full_url
        seen["key"] = req.get_header("X-internal-api-key")
        seen["body"] = json.loads(req.data.decode())
        return _Resp()
    p = VisitPoster("http://core:8000", "sekret", opener=opener)
    p._post(_visit(jpeg=b"\xff\xd8x"))
    assert seen["url"].endswith("/api/v1/internal/camera-agent/events")
    assert seen["key"] == "sekret"
    assert seen["body"]["camera_id"] == 3
    assert seen["body"]["label"] == "person"
    assert "evidence_jpeg_b64" in seen["body"]


def test_full_queue_drops_not_blocks():
    p = VisitPoster("http://core:8000", None, maxsize=1)
    assert p.submit(_visit()) is True
    assert p.submit(_visit()) is False                          # dropped, no block
    assert p._dropped == 1


# ── numeric camera id (core keys events on Camera.id, not "cam1") ───

def _post_body(visit):
    seen = {}
    def opener(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return _Resp()
    VisitPoster("http://core:8000", None, opener=opener)._post(visit)
    return seen["body"]


def test_post_prefers_explicit_nvr_camera_id():
    body = _post_body(replace(_visit(), camera_id="cam9", nvr_camera_id=3))
    assert body["camera_id"] == 3


@pytest.mark.parametrize("handle", ["cam7", "cam-7", "cam_7", "7"])
def test_post_parses_cam_handle_when_no_numeric_id(handle):
    body = _post_body(replace(_visit(), camera_id=handle))     # older specs
    assert body["camera_id"] == 7


def test_post_rejects_unparseable_camera_id():
    with pytest.raises(ValueError):
        _post_body(replace(_visit(), camera_id="front-door"))


def test_lifecycle_threads_nvr_camera_id_into_visits():
    lc = VisitLifecycle("cam3", nvr_camera_id=3)
    lc.observe([_Tr(1)], T0)
    lc.observe([_Tr(1)], T0 + 5)
    done = lc.observe([], T0 + 6)
    assert (done[0].camera_id, done[0].nvr_camera_id) == ("cam3", 3)


# ── history loss is now countable (the docstring's promised metric) ──

def test_dropped_visits_are_counted_by_reason():
    from dataclasses import replace as _replace

    from detect_pipeline import metrics as m

    m.metrics.reset()

    # 1. queue full
    p = VisitPoster("http://core:8000", None, maxsize=1)
    p.submit(_visit())
    p.submit(_visit())
    assert m.metrics.value(
        "tier0_visits_dropped_total", {"camera": "3", "reason": "queue_full"}
    ) == 1

    # 2. a post that can never succeed (unresolvable camera id)
    def opener(req, timeout=None):
        raise AssertionError("must not reach the network")

    bad = VisitPoster("http://core:8000", None, opener=opener)
    bad._q.put_nowait(_replace(_visit(), camera_id="front-door"))
    try:
        bad._post(bad._q.get())
    except ValueError:
        m.record_visit_dropped("front-door", "unresolved_camera")
    assert m.metrics.value(
        "tier0_visits_dropped_total",
        {"camera": "front-door", "reason": "unresolved_camera"},
    ) == 1
    m.metrics.reset()


def test_junk_suppression_is_counted_not_silent():
    """Flickers are dropped by design — but silently dropping them made a
    misconfigured floor indistinguishable from an empty scene."""
    from detect_pipeline import metrics as m

    m.metrics.reset()
    lc = VisitLifecycle("7", nvr_camera_id=7, min_duration_s=1.0)
    lc.observe([_Tr(1, confirmed=False)], T0)
    assert lc.observe([], T0 + 10) == []                 # never confirmed
    lc.observe([_Tr(2)], T0)
    assert lc.observe([], T0 + 0.3) == []                # under the floor

    assert m.metrics.value(
        "tier0_visits_dropped_total", {"camera": "7", "reason": "unconfirmed"}
    ) == 1
    assert m.metrics.value(
        "tier0_visits_dropped_total", {"camera": "7", "reason": "too_short"}
    ) == 1
    m.metrics.reset()
