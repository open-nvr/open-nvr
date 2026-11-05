# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The attendance app's tick, against a fake core, KAI-C and bus."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from attendance_app import MANIFEST, AppConfig, AttendanceApp

T0 = datetime(2026, 11, 2, 3, 30, tzinfo=UTC)


class FakeHttp:
    def __init__(self, active):
        self.active = active

    def get_json(self, path, **_):
        assert path == "/api/v1/internal/app/attendance/active"
        return self.active


class FakeAI:
    def __init__(self):
        self.calls = []
        self.result = {"faces": [], "detected_count": 0}
        self.fail = False

    def infer(self, adapter, jpeg, *, task, camera_id=None, params=None, correlation_id=None):
        self.calls.append((adapter, task, camera_id, params))
        if self.fail:
            raise RuntimeError("adapter down")
        return {"result": self.result}


class FakeNvr:
    def __init__(self, active):
        self._http = FakeHttp(active)
        self.ai = FakeAI()
        self.frames = {1: b"jpeg-1", 2: b"jpeg-2"}
        self.evidence = []

    def snapshot(self, camera):
        return self.frames.get(int(camera))

    def save_evidence(self, jpeg):
        self.evidence.append(jpeg)
        return f"ev/{len(self.evidence)}.jpg"


class FakeBus:
    def __init__(self):
        self.sent = []

    def publish(self, schema, *, camera_id, payload, correlation_id=None):
        self.sent.append((schema, camera_id, payload))
        return True


def _session(sid=1, cams=(1,), people=("asha",)):
    return {"session_id": sid, "camera_ids": list(cams), "person_ids": list(people),
            "start_at": T0.isoformat(), "end_at": (T0 + timedelta(minutes=50)).isoformat(),
            "attempt_interval_s": 150, "bucket_minutes": 5, "match_threshold": 0.5}


def _app(active, now):
    nvr, bus = FakeNvr(active), FakeBus()
    clock = {"now": now}
    app = AttendanceApp(AppConfig(), nvr=nvr, events=bus, clock=lambda: clock["now"])
    return app, nvr, bus, clock


def test_manifest_lights_the_page_and_needs_face_identify():
    assert MANIFEST.provides == ["attendance"]
    assert MANIFEST.requires_tasks == ["face_identify"]


def test_a_look_publishes_one_attempt_with_its_members_and_evidence():
    app, nvr, bus, _ = _app({"sessions": [_session()]}, T0 + timedelta(minutes=1))
    nvr.ai.result = {"detected_count": 30, "faces": [
        {"recognized": True, "person_id": "asha", "similarity": 0.8, "bbox": [1, 2, 3, 4]},
        {"recognized": True, "person_id": "visitor", "similarity": 0.9}]}
    app.handle_tick()

    assert nvr.ai.calls == [("insightface_attendance", "face_identify", "cam1",
                             {"threshold": 0.5, "min_face_px": 20})]
    [(schema, cam, payload)] = bus.sent
    assert schema == "attendance.attempt.v1" and cam == "cam1"
    assert payload["ok"] is True and payload["faces_detected"] == 30
    assert [m["person_id"] for m in payload["matches"]] == ["asha"]
    assert payload["evidence_path"] == "ev/1.jpg" and nvr.evidence == [b"jpeg-1"]


def test_the_interval_is_kept_between_ticks():
    app, _nvr, bus, clock = _app({"sessions": [_session()]}, T0 + timedelta(minutes=1))
    app.handle_tick()
    clock["now"] += timedelta(seconds=60)
    app.handle_tick()
    assert len(bus.sent) == 1
    clock["now"] += timedelta(seconds=90)
    app.handle_tick()
    assert len(bus.sent) == 2


def test_no_frame_or_a_failed_adapter_is_still_reported_as_a_failed_look():
    app, nvr, bus, _ = _app({"sessions": [_session(cams=(1, 2))]}, T0 + timedelta(minutes=1))
    nvr.frames.pop(2)
    nvr.ai.fail = True
    app.handle_tick()
    oks = {cam: p["ok"] for _s, cam, p in bus.sent}
    assert oks == {"cam1": False, "cam2": False}
    assert app.state_snapshot()["looks_failed"] == 2
    assert app.not_ready_reason().startswith("face adapter")


def test_an_adapter_without_face_identify_is_named_not_ignored():
    app, nvr, bus, _ = _app({"sessions": [_session()]}, T0 + timedelta(minutes=1))
    nvr.ai.result = {"task": "face_detection", "faces": None}
    app.handle_tick()
    assert bus.sent[0][2]["ok"] is False
    assert "face_identify" in app.not_ready_reason()


def test_core_not_answering_changes_nothing_and_says_so():
    app, _nvr, bus, _ = _app(None, T0 + timedelta(minutes=1))
    app.handle_tick()
    assert bus.sent == [] and "did not answer" in app.not_ready_reason()


def test_switched_off_in_the_catalog_does_nothing():
    app, nvr, bus, _ = _app({"sessions": [_session()]}, T0 + timedelta(minutes=1))
    app.app_enabled = False
    app.handle_tick()
    assert bus.sent == [] and nvr.ai.calls == []


def test_the_adapter_follows_the_config():
    app, nvr, _bus, _ = _app({"sessions": [_session()]}, T0 + timedelta(minutes=1))
    app.on_config_update({"adapter": "rekognition"})
    app.handle_tick()
    assert nvr.ai.calls[0][0] == "rekognition"


def test_state_reports_what_it_watches():
    app, _nvr, _bus, _ = _app({"sessions": [_session(cams=(1, 2)), _session(2, cams=(2,))]},
                             T0 + timedelta(minutes=1))
    app.handle_tick()
    state = app.state_snapshot()
    assert state["sessions_running"] == 2 and state["cameras_watched"] == 2
    assert state["attempts_published"] == 2 and len(state["recent"]) == 2


def test_a_look_is_dated_when_its_frame_was_taken_not_when_the_tick_began():
    """Looks run one after another; on a busy tick the last camera's frame
    is taken minutes after the tick started."""
    app, nvr, bus, clock = _app({"sessions": [_session(cams=(1, 2))]}, T0 + timedelta(minutes=1))
    times = iter([T0 + timedelta(minutes=1, seconds=1), T0 + timedelta(minutes=3)])
    real_snapshot = nvr.snapshot

    def slow_snapshot(camera):
        clock["now"] = next(times)
        return real_snapshot(camera)

    nvr.snapshot = slow_snapshot
    app.handle_tick()
    stamps = [p["at"] for _s, _c, p in bus.sent]
    assert stamps == [(T0 + timedelta(minutes=1, seconds=1)).isoformat(),
                      (T0 + timedelta(minutes=3)).isoformat()]


def test_one_failed_poll_does_not_leave_the_app_not_ready():
    app, nvr, _bus, _ = _app(None, T0 - timedelta(hours=8))   # evening, no class
    app.handle_tick()
    assert "did not answer" in app.not_ready_reason()
    nvr._http.active = {"sessions": []}
    app.handle_tick()
    assert app.not_ready_reason() is None


def test_a_stop_request_ends_the_tick_between_cameras():
    app, nvr, bus, _ = _app({"sessions": [_session(cams=(1, 2))]}, T0 + timedelta(minutes=1))
    real = nvr.snapshot

    def snapshot_then_stop(camera):
        app.request_stop()                 # docker stop lands during the first look
        return real(camera)

    nvr.snapshot = snapshot_then_stop
    app.handle_tick()
    assert [cam for _s, cam, _p in bus.sent] == ["cam1"]


def test_a_tick_stops_looking_after_its_time_and_the_rest_wait():
    app, nvr, bus, _ = _app({"sessions": [_session(cams=(1, 2))]}, T0 + timedelta(minutes=1))
    app.cfg.max_tick_seconds = 0.0          # out of time after the first look
    app.handle_tick()
    assert [cam for _s, cam, _p in bus.sent] == ["cam1"]
    app.cfg.max_tick_seconds = 60.0
    app.handle_tick()                       # cam2 is still due; cam1 is not
    assert [cam for _s, cam, _p in bus.sent] == ["cam1", "cam2"]
