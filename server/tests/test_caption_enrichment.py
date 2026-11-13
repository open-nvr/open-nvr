# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Captions reach the canonical store — and cost nothing when unasked.

The bug this closes (found 2026-09-22): ``event_text`` had a table, a GIN
index, an ingest endpoint and a search service matching against it, and
NO producer. Nothing in the repo wrote a row except tests. The captions
the detect-pipeline already generates went out on the bus, where the
optional footage-search example caught them into its own private SQLite —
so core's search had nothing to match "red" against, and the words lived
in an app's store instead of the platform's.

These guard the closing of that gap AND the thing that must not regress
while closing it: a site that has not assigned the captioning skill pays
no inference and sees no change at all.
"""

from __future__ import annotations

import asyncio

import pytest

from services.caption_enrichment import (
    CAPTION_SKILL,
    CAPTIONABLE_LABELS,
    _resolve_caption_adapter,
    wants_caption,
)


# ── the gate: no assignment, no caption, no cost ──────────────────


def test_wants_caption_needs_the_claim_not_just_a_describable_label():
    """Exactly ``wants_plate``'s lesson, one enricher over: without the
    assignment gate every person on every camera buys an inference."""
    evidence = "cam1/2026/09/22/frame.jpg"
    assert wants_caption("person", evidence, True, {CAPTION_SKILL}) is True
    assert wants_caption("person", evidence, True, set()) is False
    assert wants_caption("person", evidence, True, {"license_plate_recognition"}) is False
    # None is "the caller could not resolve the camera", not "allow it".
    assert wants_caption("person", evidence, True, None) is False


def test_wants_caption_respects_the_other_three_gates():
    evidence = "cam1/2026/09/22/frame.jpg"
    # A label nobody captions.
    assert wants_caption("suitcase", evidence, True, {CAPTION_SKILL}) is False
    # No evidence frame to describe.
    assert wants_caption("person", None, True, {CAPTION_SKILL}) is False
    # Feature switched off deployment-wide.
    assert wants_caption("person", evidence, False, {CAPTION_SKILL}) is False


def test_captionable_labels_match_the_pipeline_routing():
    """The detect-pipeline decides what is worth captioning; core must
    not hold a second opinion. If these drift, an operator either sees
    captions on the bus that never reach the store, or pays inference on
    visits the pipeline thought not worth describing."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    dispatch = (root / "detect-pipeline/detect_pipeline/dispatch.py").read_text()
    # A readable failure, not a ValueError out of str.index: if the
    # pipeline renames this table, the person reading CI should be told
    # that rather than handed a stack trace.
    anchor = "DEFAULT_ROUTES: dict[str, list[str]] = {"
    assert anchor in dispatch, (
        "detect-pipeline's DEFAULT_ROUTES table moved or was renamed — "
        "this guard can no longer see what the pipeline captions")
    block = dispatch[dispatch.index(anchor):]
    block = block[:block.index("}")]
    routed = set(re.findall(r'"([a-z_]+)":\s*\["caption"\]', block))
    assert routed, "could not read the pipeline's caption routes"
    assert routed == CAPTIONABLE_LABELS, (
        "core's CAPTIONABLE_LABELS and the pipeline's caption routing "
        f"disagree: pipeline={sorted(routed)} core={sorted(CAPTIONABLE_LABELS)}")


# ── adapter resolution: absent, unhealthy, and deterministic ──────


def _resolve(monkeypatch, health, caps):
    async def fake_view():
        return health, caps
    import services.caption_enrichment as mod
    monkeypatch.setitem(
        __import__("sys").modules, "routers.skills",
        type("M", (), {"_kai_c_view": staticmethod(fake_view)}))
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        mod._resolve_caption_adapter())


def test_no_captioner_registered_is_a_no_op_not_an_error(monkeypatch):
    """A box with no captioner keeps label-and-time search and loses
    nothing it had. It must not raise on the ingest background task."""
    assert _resolve(monkeypatch, {}, {}) is None
    assert _resolve(monkeypatch, None, None) is None


def test_resolves_an_adapter_that_advertises_the_task(monkeypatch):
    caps = {"blip": {"capabilities": {"tasks_advertised": ["scene_caption"]}}}
    assert _resolve(monkeypatch, {"blip": {"status": "ok"}}, caps) == "blip"
    # The canonical spelling from tasks.yml works too — an adapter may
    # advertise either, and betting on one silently disables the other.
    caps = {"x": {"capabilities": {"tasks_advertised": ["image_captioning"]}}}
    assert _resolve(monkeypatch, {"x": {"status": "ok"}}, caps) == "x"


def test_an_unhealthy_captioner_is_skipped_but_unknown_health_is_tried(monkeypatch):
    caps = {
        "blip": {"capabilities": {"tasks_advertised": ["scene_caption"]}},
        "moondream": {"capabilities": {"tasks_advertised": ["scene_caption"]}},
    }
    # blip is down; the other one answers.
    health = {"blip": {"status": "bad"}, "moondream": {"status": "ok"}}
    assert _resolve(monkeypatch, health, caps) == "moondream"
    # Health unknown entirely: try anyway rather than refuse to caption
    # because a probe was unavailable. Sorted, so the pick is stable.
    assert _resolve(monkeypatch, None, caps) == "blip"


def test_an_adapter_advertising_something_else_is_not_picked(monkeypatch):
    caps = {"ocr": {"capabilities": {"tasks_advertised": ["license_plate_recognition"]}}}
    assert _resolve(monkeypatch, {"ocr": {"status": "ok"}}, caps) is None


# ── the search contract this exists to serve ──────────────────────


def test_filling_event_text_can_only_add_matches():
    """The property that makes this safe to ship on a live box: the
    search service joins event_text with an OUTER join, so rows gaining
    text can add matches and refine ranking but can never remove a
    result that returns today."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "services/search_service.py").read_text()
    assert "outerjoin(EventText" in src, (
        "search_service no longer OUTER joins event_text — filling that "
        "table would now REMOVE results for visits nobody captioned")


@pytest.mark.parametrize("label", sorted(CAPTIONABLE_LABELS))
def test_every_captionable_label_is_one_tier0_actually_emits(label):
    """A label we caption but Tier-0 never emits is dead config; the
    COCO classes below are the ones the detector reports."""
    coco_ish = {"person", "bicycle", "car", "motorcycle", "bus", "truck",
                "train", "boat"}
    assert label in coco_ish


def test_the_ingest_path_gates_the_caption_the_same_way_it_gates_ocr():
    """The gate is only worth having if the ingest path actually uses it.

    A source-level guard, deliberately: the behavioural cost of a
    regression here is an inference per visit on every camera of every
    deployment, which no unit test of ``wants_caption`` alone would
    catch — the function can stay perfect while the caller stops asking
    it."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "routers/internal_camera_agent.py").read_text()
    assert "enrich_event_caption" in src, (
        "nothing queues the caption task — event_text goes back to "
        "having no producer, which is the bug this closed")
    assert "wants_caption(" in src, (
        "the caption task is queued without the wants_caption gate")
    # Queued as a BACKGROUND task, never awaited on the ingest path: a
    # visit must not wait on a captioner to be recorded.
    assert "background.add_task(enrich_event_caption" in src, (
        "the caption must be a background task — awaiting it puts an "
        "adapter call on the ingest request path")
    # And gated by the same per-camera assignment the OCR sweep uses.
    assert "camera_skills(camera)" in src


# ── A timeout is not "unreachable" (#583) ────────────────────────────
#
# The field report: 478 warnings an hour reading "ollamavlm unreachable ()"
# while the adapter answered /health and moondream held 290% of a core.
# The empty brackets were httpx's ReadTimeout with no message. An operator
# checked the network; the box had lost two cores to a captioner that
# could not keep up, and the log said nothing of the kind.

def _run_caption_with(monkeypatch, raising):
    import asyncio

    import httpx

    from services import caption_enrichment as mod

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            raise raising

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        mod._caption_jpeg(b"\xff\xd8\xffjpeg", "ollamavlm", "cam1", event_id=1))


def test_a_timeout_is_logged_as_a_timeout_with_the_limit(monkeypatch, caplog):
    import logging

    import httpx

    with caplog.at_level(logging.WARNING, logger="caption_enrichment"):
        assert _run_caption_with(monkeypatch, httpx.ReadTimeout("")) is None
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "timed out after" in line, line
    assert "limit 90s" in line, line
    assert "unreachable" not in line, "a timeout must not read as a network fault"


def test_a_connect_timeout_is_unreachable_not_slow(monkeypatch, caplog):
    """httpx.ConnectTimeout is a TimeoutException too — a host that never
    answered the SYN. Filing it under "the captioner is slower than the
    visit rate" would send the operator to look at model speed while
    KAI-C is down: the inverse of the misdiagnosis #583 fixed."""
    import logging

    import httpx

    with caplog.at_level(logging.WARNING, logger="caption_enrichment"):
        assert _run_caption_with(monkeypatch, httpx.ConnectTimeout("")) is None
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "unreachable" in line and "ConnectTimeout" in line, line
    assert "slower than the visit rate" not in line


def test_a_refused_connection_is_still_unreachable(monkeypatch, caplog):
    import logging

    import httpx

    with caplog.at_level(logging.WARNING, logger="caption_enrichment"):
        assert _run_caption_with(monkeypatch, httpx.ConnectError("refused")) is None
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "unreachable" in line, line
    assert "ConnectError" in line, "name the exception — '()' told nobody anything"
    assert "timed out" not in line


# ── "looked and found nothing" is recorded, and the requested lane ────

def _caption_harness(monkeypatch, *, caption_result, flag: bool = True,
                     priorities=("live", "live"), existing_text: dict | None = None):
    """The real enrich_event_caption over one visit, with the adapter
    call stubbed. Returns (calls, row_payload, results, caption_on_row);
    ``existing_text`` seeds an EventText row (the descriptor enricher's
    caption-less one, say) before the runs."""
    import asyncio
    import pathlib
    import tempfile
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import core.database
    from core.config import settings
    from core.database import Base
    from models import Camera, Role, TimelineEvent, User
    from services import caption_enrichment as mod
    from services import evidence_store

    engine = create_engine("sqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(Role(id=1, name="admin", description="t")); db.commit()
    db.add(User(id=1, username="o", email="o@x.test", hashed_password="x",
                is_active=True, role_id=1)); db.commit()
    db.add(Camera(id=1, name="c", ip_address="10.0.0.1", rtsp_url="rtsp://x/1", owner_id=1))
    db.commit()
    now = datetime.now(UTC)
    db.add(TimelineEvent(id=1, camera_id=1, source="tier0", event_type="track", label="car",
                         started_at=now, ended_at=now + timedelta(seconds=5),
                         evidence_path="e.jpg"))
    db.commit()
    if existing_text is not None:
        from models import EventText
        db.add(EventText(event_id=1, **existing_text)); db.commit()

    class _Shared:
        def __init__(self, s):
            self._s = s

        def __getattr__(self, n):
            return getattr(self._s, n)

        def close(self):
            pass

    monkeypatch.setattr(core.database, "SessionLocal", lambda: _Shared(db))
    monkeypatch.setattr(settings, "events_caption_enrichment", flag, raising=False)
    tmp = pathlib.Path(tempfile.mkdtemp()) / "e.jpg"
    tmp.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(evidence_store, "resolve_evidence", lambda _p: tmp)

    async def _adapter():
        return "moondream-vlm"

    calls = []

    async def _cap(jpeg, adapter, handle, event_id=None, *, priority="live"):
        calls.append(priority)
        return caption_result

    monkeypatch.setattr(mod, "_resolve_caption_adapter", _adapter)
    monkeypatch.setattr(mod, "_caption_jpeg", _cap)
    loop = asyncio.new_event_loop()
    try:
        results = [loop.run_until_complete(mod.enrich_event_caption(1, priority=p))
                   for p in priorities]
        db.expire_all()
        payload = dict(db.get(TimelineEvent, 1).payload or {})
        from models import EventText
        text = db.get(EventText, 1)
        caption_on_row = text.caption if text is not None else None
    finally:
        loop.close(); db.close(); engine.dispose()
    return calls, payload, results, caption_on_row


def test_an_empty_answer_is_recorded_so_nobody_asks_forever(monkeypatch):
    """The call was made and the captioner had nothing to say. Without a
    mark the requested lane and the catch-up would offer this visit
    again on every pass — 'looked and found nothing' must be a row."""
    calls, payload, results, _ = _caption_harness(monkeypatch, caption_result="")
    assert calls == ["live"], "the second run short-circuited on the mark"
    assert "image_captioning" in payload.get("enriched_by", [])
    assert results == ["done", None], "looked once (a result); short-circuited once"


def test_a_failed_call_is_not_an_attempt(monkeypatch):
    """A timeout, a refused connection, a 503: the adapter never answered.
    Marking that would hide the visit from every later pass over a
    transient failure — it is asked again."""
    calls, payload, results, _ = _caption_harness(monkeypatch, caption_result=None)
    assert calls == ["live", "live"]
    assert "image_captioning" not in payload.get("enriched_by", [])


def test_the_adapter_answering_with_nothing_usable_is_an_empty_answer(monkeypatch):
    """_caption_jpeg: a 200 with no caption in it is "", a failure is None."""
    import asyncio

    from services import caption_enrichment as cap
    from services import enrichment_gate as eg

    async def answered(*a, **k):
        return {"result": {"caption": "   "}}

    async def failed(*a, **k):
        return None

    monkeypatch.setattr(eg, "infer_through_gate", answered)
    assert asyncio.run(cap._caption_jpeg(b"j", "m", "cam1")) == ""
    monkeypatch.setattr(eg, "infer_through_gate", failed)
    assert asyncio.run(cap._caption_jpeg(b"j", "m", "cam1")) is None

    async def malformed(*a, **k):
        return {"error": "model loading"}          # a 200 with no result in it

    monkeypatch.setattr(eg, "infer_through_gate", malformed)
    assert asyncio.run(cap._caption_jpeg(b"j", "m", "cam1")) is None, (
        "not an answer: a minute of these must not brand visits 'looked at'")


def test_a_visit_with_claims_but_no_caption_still_gets_one(monkeypatch):
    """The descriptor enricher creates a caption-less EventText for its
    claims. That row is not 'already described' — before this, every
    such visit was re-queued by the lane forever and captioned never."""
    calls, _, results, caption = _caption_harness(
        monkeypatch, caption_result="a red car", existing_text={"attributes": "red"})
    assert calls == ["live"] and results == ["done", None]
    assert caption == "a red car"


def test_someone_elses_caption_is_never_overwritten(monkeypatch):
    calls, _, results, caption = _caption_harness(
        monkeypatch, caption_result="a red car",
        existing_text={"caption": "delivery van at the gate", "source": "an-app"})
    assert calls == [] and results == [None, None]
    assert caption == "delivery van at the gate"


def test_a_dropped_call_is_not_recorded_as_attempted(monkeypatch):
    from services.enrichment_gate import DROPPED
    calls, payload, results, _ = _caption_harness(monkeypatch, caption_result=DROPPED)
    assert calls == ["live", "live"], "nobody looked; it is asked again"
    assert "image_captioning" not in payload.get("enriched_by", [])
    assert results == ["dropped", "dropped"]


def test_the_requested_lane_ignores_the_site_wide_switch(monkeypatch):
    """EVENTS_CAPTION_ENRICHMENT=false says 'do not describe every visit
    unasked'. A visit somebody ASKED about is not that."""
    calls, _, results, _ = _caption_harness(monkeypatch, caption_result="a car", flag=False,
                                         priorities=("live", "requested"))
    assert calls == ["requested"], "live returned at the switch; requested went out"
    assert results == [None, "done"]
