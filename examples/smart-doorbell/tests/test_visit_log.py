# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The door's memory — what survives a restart, and what is allowed to.

The feed and the stranger wall were deques, so a redeploy wiped every
stranger the doorbell had ever seen and "who came to my door three days
ago" had no answer. These guard the three properties that make the
answer trustworthy: that it survives a restart, that it stays bounded,
and that losing the store never stops the door ringing.
"""
from __future__ import annotations

import inspect
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from face_recognition_pipeline import FaceRead
from opennvr_app_sdk.client import TimelineAPI

_REAL_VISIT_AT = inspect.signature(TimelineAPI.visit_at)
_REAL_ADD_CLAIMS = inspect.signature(TimelineAPI.add_claims)
_REAL_FIND = inspect.signature(TimelineAPI.find)

from smart_doorbell import AppConfig, CameraConfig, SmartDoorbell
from visit_log import MAX_ENTRIES, MAX_THUMBNAILS, STATE_KEY, VisitLog


# ── a stand-in for core's per-app key/value store ────────────────────


class _Store:
    """The durable half of the SDK client, in a dict."""

    def __init__(self, seed: dict[str, Any] | None = None) -> None:
        self.values: dict[str, Any] = dict(seed or {})
        self.writes = 0

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.writes += 1
        self.values[key] = value


class _Timeline:
    """core's RFC-0003 surface, reduced to what the doorbell asks of it.

    Bound against the real client's signatures, so a rename in the SDK
    breaks these tests rather than quietly letting them accept a call
    no client can make — the fake that accepted anything is how the
    camera-agent shipped a call to a method that did not exist.
    """

    def __init__(self, bound=None, visits=()):
        #: What `visit_at` answers. None = core unreachable.
        self.bound = bound
        self.written: list[dict] = []
        #: What `find` answers for the restored feed.
        self.visits = list(visits)

    def visit_at(self, camera, at, **kw):
        _REAL_VISIT_AT.bind(self, camera, at, **kw)
        return self.bound

    def add_claims(self, event_id, claims, **kw):
        _REAL_ADD_CLAIMS.bind(self, event_id, claims, **kw)
        self.written.append({"event_id": event_id, "claims": list(claims), **kw})
        return {"ok": True, "written": len(claims)}

    def find(self, text="", **kw):
        _REAL_FIND.bind(self, text, **kw)
        return {"results": self.visits, "total": len(self.visits)}


class _Nvr:
    def __init__(self, store: _Store | None = None, timeline=None) -> None:
        self.state = store or _Store()
        self.saved: list[bytes] = []
        self.evidence: dict[str, bytes] = {}
        self.timeline = timeline if timeline is not None else _Timeline()

    def save_evidence(self, jpeg: bytes) -> str:
        self.saved.append(jpeg)
        path = f"evidence/{len(self.saved)}.jpg"
        #: What the platform still holds, by path. Dropping an entry
        #: stands in for the retention sweep.
        self.evidence[path] = jpeg
        return path

    def read_evidence(self, path: str) -> bytes | None:
        return self.evidence.get(path)


class _BrokenNvr:
    """Core is there and refusing, which is the interesting failure."""

    class _S:
        def get(self, key, default=None):
            raise RuntimeError("core unreachable")

        def set(self, key, value):
            raise RuntimeError("core unreachable")

    def __init__(self) -> None:
        self.state = self._S()

    def save_evidence(self, jpeg: bytes):
        raise RuntimeError("core unreachable")

    def read_evidence(self, path: str):
        raise RuntimeError("core unreachable")


def _visit(n: int, *, at: float | None = None, recognized: bool = False,
           thumb: str | None = "data:image/jpeg;base64,AAAA") -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": f"s{n}", "at": at if at is not None else time.time(),
        "camera": "front-door", "recognized": recognized,
    }
    if thumb and not recognized:
        entry["thumb"] = thumb
    return entry


# ── it survives a restart ────────────────────────────────────────────


def test_a_visit_written_by_one_process_is_read_by_the_next():
    """The whole point. A deque answers "who is at the door now"; only
    this answers "who came on Tuesday"."""
    store = _Store()
    VisitLog(_Nvr(store)).record(_visit(1))

    after_restart = VisitLog(_Nvr(store))
    assert [e["id"] for e in after_restart.entries] == ["s1"]


def test_the_crop_goes_to_platform_evidence_not_into_the_value():
    """A base64 crop per visit would put megabytes into a value that is
    rewritten on every visit."""
    nvr = _Nvr()
    log = VisitLog(nvr)
    stored = log.record(_visit(1), crop=b"\xff\xd8full-size-crop")

    assert nvr.saved == [b"\xff\xd8full-size-crop"]
    assert stored["evidence_path"] == "evidence/1.jpg"
    assert b"full-size-crop" not in repr(nvr.state.values[STATE_KEY]).encode()


def test_recent_is_newest_first_and_strangers_are_only_the_unrecognised():
    log = VisitLog(_Nvr())
    now = time.time()
    log.record(_visit(1, at=now - 300))
    log.record(_visit(2, at=now - 200, recognized=True))
    log.record(_visit(3, at=now - 100))

    assert [e["id"] for e in log.recent()] == ["s3", "s2", "s1"]
    assert [e["id"] for e in log.strangers()] == ["s3", "s1"]


# ── it stays bounded ─────────────────────────────────────────────────


def test_visits_older_than_the_window_are_forgotten():
    log = VisitLog(_Nvr(), max_days=7)
    old = time.time() - 30 * 86400
    log.record(_visit(1, at=old))
    log.record(_visit(2))
    assert [e["id"] for e in log.entries] == ["s2"]


def test_the_count_cap_applies_whatever_the_age():
    log = VisitLog(_Nvr(), max_entries=3)
    for i in range(6):
        log.record(_visit(i))
    assert [e["id"] for e in log.entries] == ["s3", "s4", "s5"]


def test_a_configured_cap_cannot_exceed_the_hard_one():
    """A config saying "keep 100000" must not turn every visit into a
    megabyte round-trip."""
    log = VisitLog(_Nvr(), max_entries=10_000_000, max_days=10_000)
    assert log._max_entries == MAX_ENTRIES
    assert log._max_days <= 365


def test_only_the_newest_strangers_keep_their_picture():
    """The line is bytes; the thumbnail is kilobytes. An older stranger
    keeps the record of the visit and loses the photo."""
    log = VisitLog(_Nvr(), max_entries=MAX_THUMBNAILS + 5)
    for i in range(MAX_THUMBNAILS + 5):
        log.record(_visit(i))

    entries = log.entries
    with_thumbs = [e for e in entries if e.get("thumb")]
    assert len(with_thumbs) == MAX_THUMBNAILS
    # And the ones that lost it say so, so the wall can explain itself.
    aged = [e for e in entries if not e.get("thumb")]
    assert aged and all(e.get("thumb_dropped") for e in aged)
    assert len(entries) == MAX_THUMBNAILS + 5, "the VISITS are all still there"


def test_an_undated_entry_is_not_kept_forever():
    """An entry with no usable timestamp cannot be aged out, so one bad
    write would pin a row for the life of the site."""
    store = _Store({STATE_KEY: [{"id": "junk"}, _visit(1)]})
    assert [e["id"] for e in VisitLog(_Nvr(store)).entries] == ["s1"]


def test_a_garbage_value_does_not_crash_the_door():
    store = _Store({STATE_KEY: "not a list"})
    assert VisitLog(_Nvr(store)).entries == []


# ── losing the store is not losing the doorbell ──────────────────────


def test_an_unreachable_store_reads_empty_and_does_not_raise():
    log = VisitLog(_BrokenNvr())
    assert log.entries == []


def test_a_write_that_fails_still_updates_this_process():
    """Only the durability is lost. The dashboard must stay right until
    the next successful write restores it."""
    log = VisitLog(_BrokenNvr())
    log.record(_visit(1), crop=b"\xff\xd8x")
    assert [e["id"] for e in log.entries] == ["s1"]


def test_the_load_is_attempted_once_not_per_poll():
    """Retrying on every dashboard poll turns an unreachable core into a
    request storm."""
    calls = {"n": 0}

    class _Counting(_BrokenNvr):
        class _S(_BrokenNvr._S):
            def get(self, key, default=None):
                calls["n"] += 1
                raise RuntimeError("core unreachable")

        def __init__(self):
            self.state = self._S()

    log = VisitLog(_Counting())
    for _ in range(5):
        _ = log.entries
    assert calls["n"] == 1


def test_forgetting_a_stranger_removes_them_for_good():
    store = _Store()
    log = VisitLog(_Nvr(store))
    log.record(_visit(1))
    log.record(_visit(2))

    assert log.forget("s1") is True
    assert log.forget("s1") is False
    assert [e["id"] for e in VisitLog(_Nvr(store)).entries] == ["s2"]


# ── the app end ──────────────────────────────────────────────────────


def _config(**overrides) -> AppConfig:
    base = AppConfig(
        kaic_url="http://localhost:8100",
        kaic_api_key="test-key",
        cameras=[CameraConfig(camera_id="front-door",
                              frame_url="http://example.invalid/snap.jpg")],
        poll_interval_seconds=0.0,
        request_timeout_seconds=1.0,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _doorbell(reads, store: _Store | None = None, timeline=None, **cfg):
    pipeline = MagicMock()
    pipeline.process_frame.side_effect = list(reads)
    dispatcher = MagicMock()
    app = SmartDoorbell(_config(**cfg), pipeline, dispatcher)
    app.nvr = _Nvr(store or _Store(), timeline=timeline)

    class _Stub:
        def fetch(self) -> bytes:
            return b"\xff\xd8jpeg"

    for cam_id in list(app._frame_sources):
        app._frame_sources[cam_id] = _Stub()
    return app, dispatcher


def _unknown() -> FaceRead:
    return FaceRead(face_detected=True, recognized=False, person_id=None,
                    name=None, category=None, similarity=0.2,
                    correlation_id="c", face_bbox=None)


def _known() -> FaceRead:
    return FaceRead(face_detected=True, recognized=True, person_id="alice",
                    name="Alice Smith", category="family", similarity=0.9,
                    correlation_id="c", face_bbox=None)


def test_a_visit_lands_in_the_durable_history(monkeypatch):
    store = _Store()
    app, _ = _doorbell([_unknown()], store)
    app.on_frame("front-door", b"\xff\xd8jpeg")

    kept = store.values[STATE_KEY]
    assert len(kept) == 1
    assert kept[0]["camera"] == "front-door"
    assert kept[0]["recognized"] is False


def test_a_recognised_face_is_written_to_the_platform_not_a_local_log():
    """The point of RFC-0003, and of this app's port.

    The name used to live in this app's own key/value store, where the
    operator's timeline could not see it and no other app could either.
    It goes to the visit now, bound through core, with the binding
    recorded — and the local store keeps no name at all.
    """
    tl = _Timeline(bound={"event_id": 8140, "binding": "window",
                          "reason": "one visit was in progress"})
    store = _Store()
    app, _ = _doorbell([_known()], store, timeline=tl)
    app.on_frame("front-door", b"\xff\xd8jpeg")

    assert len(tl.written) == 1
    claim = tl.written[0]["claims"][0]
    assert claim["kind"] == "face_id"
    assert claim["value"] == "Alice Smith"
    assert tl.written[0]["event_id"] == 8140
    assert tl.written[0]["binding"] == "window"

    # And nothing identifying stayed behind.
    assert "Alice Smith" not in str(store.values), (
        "the name is still in the app's own store")


def test_an_unrecognised_visitor_claims_no_identity():
    """"Somebody unknown was here" is already a visit in the store.
    Writing `face_id: unknown` would turn the ABSENCE of an identity
    into an assertion about one, and every reader counting faces would
    count it."""
    tl = _Timeline(bound={"event_id": 8140, "binding": "window"})
    app, _ = _doorbell([_unknown()], _Store(), timeline=tl)
    app.on_frame("front-door", b"\xff\xd8jpeg")

    assert tl.written == []


def test_an_ambiguous_instant_writes_no_name():
    """Two people at the door. The frame does not say whose face it is,
    core refuses to choose, and the doorbell must not choose either."""
    tl = _Timeline(bound={"event_id": None, "binding": None,
                          "reason": "ambiguous", "candidates": [1, 2]})
    app, _ = _doorbell([_known()], _Store(), timeline=tl)
    app.on_frame("front-door", b"\xff\xd8jpeg")

    assert tl.written == []
    assert app.state_snapshot()["identity_binding"]["ambiguous"] == 1


def test_an_unreachable_core_loses_the_name_loudly_not_the_ring():
    """The doorbell still rang — the alert goes out before any of this.
    What must not happen is the name being silently dropped, because
    "not recorded" is not a quieter version of "there was no name"."""
    tl = _Timeline(bound=None)      # None = core unreachable
    app, dispatcher = _doorbell([_known()], _Store(), timeline=tl)
    app.on_frame("front-door", b"\xff\xd8jpeg")

    assert dispatcher.dispatch.called, "the doorbell stopped ringing"
    assert tl.written == []
    assert app.state_snapshot()["identity_binding"]["unreachable"] == 1


def test_a_guessed_binding_is_recorded_as_a_guess():
    """`nearest` is allowed and labelled. A reader that must not act on
    a guess filters on it; one that never sees the field cannot."""
    tl = _Timeline(bound={"event_id": 8140, "binding": "nearest",
                          "reason": "nearest within 2.0s"})
    app, _ = _doorbell([_known()], _Store(), timeline=tl)
    app.on_frame("front-door", b"\xff\xd8jpeg")

    assert tl.written[0]["binding"] == "nearest"
    assert app.state_snapshot()["identity_binding"]["nearest"] == 1


def test_the_dashboard_comes_back_with_yesterdays_visitors():
    """The feed is read from the PLATFORM now, so a redeploy shows what
    the operator's timeline shows — not a second history that only this
    app could see."""
    tl = _Timeline(
        bound={"event_id": 8140, "binding": "window"},
        visits=[{
            "id": 8140, "camera_id": 3, "camera_name": "front-door",
            "started_at": "2026-09-22T19:04:00+00:00", "label": "person",
            "claims": [{"kind": "face_id", "value": "Alice Smith"}],
        }])
    store = _Store()
    app, _ = _doorbell([_unknown()], store, timeline=tl)

    snap = app.state_snapshot()
    assert snap["recent"] == [], "history is read lazily, not in the ctor"

    app.on_frame("front-door", b"\xff\xd8jpeg")
    snap = app.state_snapshot()
    assert len(snap["recent"]) == 2, "the restored visit and the new one"
    assert any("Alice Smith recognised" in r["message"] for r in snap["recent"])
    assert any("Unknown visitor" in r["message"] for r in snap["recent"])


def test_a_visit_with_no_face_claim_reads_as_unknown():
    """Absent, not blank. A visit nobody recognised has no claim at
    all, and the feed must say so rather than rendering an empty name."""
    tl = _Timeline(visits=[{
        "id": 1, "camera_id": 3, "camera_name": "front-door",
        "started_at": "2026-09-22T19:04:00+00:00", "claims": [],
    }])
    app, _ = _doorbell([], _Store(), timeline=tl)
    app._restore_history()

    assert app.state_snapshot()["recent"][0]["name"] is None
    assert "Unknown visitor" in app.state_snapshot()["recent"][0]["message"]


def test_a_restored_tile_with_no_picture_says_so():
    """The visit still happened. Saying "snapshot aged out" beats a
    broken image, because the caption is the answer to the question."""
    old = _visit(1, at=time.time() - 60)
    old.pop("thumb", None)
    old["thumb_dropped"] = True
    store = _Store({STATE_KEY: [old]})
    app, _ = _doorbell([_unknown()], store)
    app._restore_history()

    tile = next(t for t in app._stranger_gallery if t["id"] == "s1")
    assert tile["aged_out"] is True
    assert not tile["image"]
    assert "snapshot aged out" in app.ui_html()


def test_the_door_still_rings_when_the_history_store_is_down():
    """The alert goes out before the write, and the write cannot raise
    through on_frame."""
    pipeline = MagicMock()
    pipeline.process_frame.side_effect = [_unknown()]
    dispatcher = MagicMock()
    app = SmartDoorbell(_config(), pipeline, dispatcher)
    app.nvr = _BrokenNvr()

    class _Stub:
        def fetch(self) -> bytes:
            return b"\xff\xd8jpeg"

    for cam_id in list(app._frame_sources):
        app._frame_sources[cam_id] = _Stub()

    app.on_frame("front-door", b"\xff\xd8jpeg")
    assert dispatcher.dispatch.call_count == 1


def test_enrolling_a_stranger_takes_them_out_of_history_too(monkeypatch):
    """Or the tile reappears on the next restart for somebody who is now
    enrolled."""
    store = _Store()
    app, _ = _doorbell([_unknown()], store)
    app.on_frame("front-door", b"\xff\xd8jpeg")
    sid = store.values[STATE_KEY][0]["id"]
    app._stranger_crops[sid] = b"\xff\xd8crop"
    monkeypatch.setattr(app, "_enroll", lambda **kw: {"ok": True})

    app.on_action("enroll_stranger", {"stranger_id": sid, "name": "Bob"})
    assert store.values[STATE_KEY] == []


# ── enrolling a face from last Tuesday ───────────────────────────────


def test_the_full_crop_comes_back_from_the_platform():
    """Not the wall thumbnail — the crop the enroller needs. The
    thumbnail is ~190px and exists so the tile can be drawn."""
    nvr = _Nvr()
    log = VisitLog(nvr)
    log.record(_visit(1), crop=b"\xff\xd8the-full-320px-crop")

    assert log.crop("s1") == b"\xff\xd8the-full-320px-crop"


def test_a_crop_the_platform_no_longer_holds_is_none():
    nvr = _Nvr()
    log = VisitLog(nvr)
    log.record(_visit(1), crop=b"\xff\xd8x")
    nvr.evidence.clear()              # the retention sweep got there

    assert log.crop("s1") is None


def test_a_visit_that_never_had_a_crop_asks_the_platform_nothing():
    """A visit with no evidence_path has no picture anywhere, so the
    round trip is one we already know the answer to."""
    nvr = _Nvr()
    asked: list[str] = []
    nvr.read_evidence = lambda p: asked.append(p)  # type: ignore[assignment]
    log = VisitLog(nvr)
    log.record(_visit(1))

    assert log.crop("s1") is None
    assert log.crop("nosuchvisit") is None
    assert asked == [], "it asked for a picture it knew was not there"


def test_an_unreachable_platform_is_none_not_an_exception():
    log = VisitLog(_BrokenNvr())
    assert log.crop("s1") is None


def test_a_stranger_from_before_the_restart_can_still_be_enrolled(monkeypatch):
    """The point. "The doorbell was restarted" is not something an
    operator should have to care about when they click Enrol on a face
    from Tuesday."""
    store = _Store()
    first, _ = _doorbell([_unknown()], store)
    first.on_frame("front-door", b"\xff\xd8jpeg")
    sid = store.values[STATE_KEY][0]["id"]
    saved = first.nvr.evidence          # the platform's copy survives

    second, _ = _doorbell([_unknown()], store)
    second.nvr.evidence.update(saved)
    second._restore_history()
    assert sid not in second._stranger_crops, "nothing in memory yet"

    enrolled: dict = {}
    monkeypatch.setattr(second, "_enroll",
                        lambda **kw: enrolled.update(kw) or {"ok": True})
    second.on_action("enroll_stranger", {"stranger_id": sid, "name": "Bob"})

    assert enrolled["image_bytes"], "enrolled from the full crop"
    assert store.values[STATE_KEY] == [], "and left the history"


def test_the_wall_never_enrols_from_the_thumbnail(monkeypatch):
    """A ~190px face would teach the adapter something worse than the
    operator believes they handed over. With no fetchable crop the
    action refuses instead."""
    store = _Store()
    app, _ = _doorbell([_unknown()], store)
    app.on_frame("front-door", b"\xff\xd8jpeg")
    sid = store.values[STATE_KEY][0]["id"]

    second, _ = _doorbell([_unknown()], store)   # no evidence carried over
    second._restore_history()
    monkeypatch.setattr(second, "_enroll", lambda **kw: {"ok": True})

    with pytest.raises(KeyError):
        second.on_action("enroll_stranger", {"stranger_id": sid, "name": "Bob"})


def test_the_wall_distinguishes_kept_from_aged_out():
    """Two different absences. Saying "aged out" for a face that is
    still there tells an operator it is gone when they could enrol
    from it."""
    kept = _visit(1, at=time.time() - 60)
    kept.pop("thumb", None)
    kept["evidence_path"] = "evidence/1.jpg"
    gone = _visit(2, at=time.time() - 30)
    gone.pop("thumb", None)
    store = _Store({STATE_KEY: [kept, gone]})

    app, _ = _doorbell([_unknown()], store)
    app._restore_history()

    tiles = {t["id"]: t for t in app._stranger_gallery}
    assert tiles["s1"]["photo_kept"] is True and tiles["s1"]["aged_out"] is False
    assert tiles["s2"]["aged_out"] is True

    html = app.ui_html()
    assert "photo kept" in html
    assert "snapshot aged out" in html


@pytest.mark.parametrize("field", ["history_days", "history_max"])
def test_the_retention_settings_are_exposed_as_params(field):
    from smart_doorbell import MANIFEST

    assert any(p.name == field for p in MANIFEST.params), (
        f"{field} cannot be changed from the catalog")
