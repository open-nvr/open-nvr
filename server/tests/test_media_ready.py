# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HA-113: media_ready.

* the clip window is padded, capped to the END of long visits, and ready
  READY_LAG_S after it closes (HA-007: the open segment plays);
* a new visit schedules exactly one media_ready naming its images and the
  clip; a duplicate ingest schedules none;
* an app alert's push names its (already stored) images and schedules the
  clip when the alert has a camera;
* media_ready is delivered once the clip is playable, and a token needs
  recordings.view to receive it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from services import media_ready as mr
from tests.test_api_tokens import env  # noqa: F401 - shared fixture

T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def test_clip_window_is_padded_and_ready_after_the_lag():
    w = mr.clip_window(T0, T0 + timedelta(seconds=20))
    assert w["start"] == (T0 - timedelta(seconds=mr.CLIP_PRE_S)).isoformat()
    assert w["duration_s"] == 20 + mr.CLIP_PRE_S + mr.CLIP_POST_S
    assert w["ready_at"] == T0 + timedelta(seconds=20 + mr.CLIP_POST_S + mr.READY_LAG_S)


def test_long_visits_keep_their_end():
    end = T0 + timedelta(minutes=30)
    w = mr.clip_window(T0, end)
    assert w["duration_s"] == mr.MAX_CLIP_S
    assert w["start"] == (end + timedelta(seconds=mr.CLIP_POST_S)
                          - timedelta(seconds=mr.MAX_CLIP_S)).isoformat()


def test_visit_payload_names_its_images_and_clip(monkeypatch):
    got = []
    monkeypatch.setattr(mr, "_schedule", lambda cam, payload, at: got.append((cam, payload, at)))
    row = SimpleNamespace(id=9, camera_id=2, label="car", started_at=T0,
                          ended_at=T0 + timedelta(seconds=4), zone_ids=[1],
                          evidence_path="a.jpg", scene_evidence_path="b.jpg",
                          plate_evidence_path=None, plate_frame_path=None)
    mr.schedule_for_visit(row)
    cam, payload, at = got[0]
    assert cam == 2 and payload["source"] == "event" and payload["id"] == 9
    assert payload["images"] == ["evidence", "scene"] and payload["zone_ids"] == [1]
    assert "ready_at" not in payload["clip"]
    assert at == T0 + timedelta(seconds=4 + mr.CLIP_POST_S + mr.READY_LAG_S)


def test_published_once_the_clip_plays(monkeypatch):
    from services import event_bus_service as ebs

    sent = []

    async def fake(event):
        sent.append(event)

    bus = ebs.EventBus()
    monkeypatch.setattr(ebs, "_event_bus_instance", bus)
    monkeypatch.setattr(bus, "publish", fake)
    asyncio.run(mr._publish_when_ready(3, {"source": "event", "id": 1},
                                       datetime.now(UTC) - timedelta(seconds=1)))
    assert sent == [{"event_type": "media_ready", "camera_id": 3, "task": "event",
                     "payload": {"source": "event", "id": 1}}]


def test_alerts_without_a_camera_schedule_nothing(monkeypatch):
    got = []
    monkeypatch.setattr(mr, "_schedule", lambda *a: got.append(a))
    mr.schedule_for_alert({"id": 1}, None, T0)
    assert got == []
    mr.schedule_for_alert({"id": 1, "alert_id": "x", "image_names": ["face"]}, 4, T0)
    assert got[0][0] == 4 and got[0][1]["images"] == ["face"]


def test_only_new_visits_schedule_media_ready(env, monkeypatch):  # noqa: F811
    from fastapi import BackgroundTasks

    from routers import internal_camera_agent as ica

    got = []
    monkeypatch.setattr(mr, "schedule_for_visit", lambda row: got.append(row.id))
    payload = ica.TrackEventIn(camera_id=1, label="person", track_id="t9",
                               started_at=T0, ended_at=T0 + timedelta(seconds=3))
    s = env.Session()
    try:
        first = asyncio.run(ica.ingest_track_event(payload, BackgroundTasks(), None, s))
        again = asyncio.run(ica.ingest_track_event(payload, BackgroundTasks(), None, s))
    finally:
        s.close()
    assert again.get("duplicate") is True
    assert got == [first["id"]]


def test_alert_push_names_images_and_schedules_the_clip(env, monkeypatch):  # noqa: F811
    from services import alerts_inbox, event_bus_service as ebs

    sent, scheduled = [], []

    async def fake(event):
        sent.append(event)

    bus = ebs.EventBus()
    monkeypatch.setattr(ebs, "_event_bus_instance", bus)
    monkeypatch.setattr(bus, "publish", fake)
    monkeypatch.setattr(mr, "schedule_for_alert",
                        lambda stored, cam, at: scheduled.append((stored["id"], cam, at)))
    monkeypatch.setattr(alerts_inbox, "_split_images",
                        lambda ev: ({}, {"face": "ab/f.jpg", "body": "ab/b.jpg"}))
    envelope = {"alert_id": "al-1", "title": "Person at gate", "severity": "info",
                "camera_id": "cam2", "evidence": {"x": 1}}
    msg = SimpleNamespace(data=json.dumps(envelope).encode(), subject="opennvr.alerts.x")
    asyncio.run(alerts_inbox._handle_message(msg))
    push = sent[0]["payload"]
    assert push["images"] == ["body", "face"] and push["severity"] == "low"
    assert scheduled and scheduled[0][1] == 2


def test_token_needs_recordings_view_for_media_ready():
    from services.api_tokens import TOKEN_EVENT_SCOPES

    assert TOKEN_EVENT_SCOPES["media_ready"] == "recordings.view"

