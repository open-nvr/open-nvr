# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The requested lane: describe what a question needs, now (#583)."""
from __future__ import annotations

import asyncio
import os
import secrets
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite:///./_enrichment_requests_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base  # noqa: E402
from models import Camera, EventText, Role, TimelineEvent, User, VisitDescriptor  # noqa: E402
from services import enrichment_requests as er  # noqa: E402

WALL = datetime.now(UTC).replace(microsecond=0)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, future=True)()
    s.add(Role(id=1, name="admin", description="t")); s.commit()
    s.add(User(id=1, username="o", email="o@x.test", hashed_password="x",
               is_active=True, role_id=1)); s.commit()
    # Assigned both skills: the lane honours the same per-camera gate the
    # ingest path does, so an unassigned camera is never "missing" anything.
    s.add(Camera(id=1, name="gate", ip_address="10.0.0.1", rtsp_url="rtsp://x/1", owner_id=1,
                 assignments=[{"skill": "image_captioning"}, {"skill": "vqa"}]))
    s.add(Camera(id=2, name="yard", ip_address="10.0.0.2", rtsp_url="rtsp://x/2", owner_id=1))
    s.commit()
    yield s
    s.close(); engine.dispose()


@pytest.fixture(autouse=True)
def _fresh():
    er._reset_for_tests()
    yield
    er._reset_for_tests()


def _visit(db, *, label="car", minutes_ago=5, caption=None, claims=(), evidence="e.jpg",
           camera_id=1, looked=(), now=None):
    t = (now or WALL) - timedelta(minutes=minutes_ago)
    row = TimelineEvent(camera_id=camera_id, source="tier0", event_type="track", label=label,
                        started_at=t, ended_at=t + timedelta(seconds=20), evidence_path=evidence,
                        payload={"enriched_by": list(looked)} if looked else None)
    db.add(row); db.commit(); db.refresh(row)
    if caption:
        db.add(EventText(event_id=row.id, caption=caption, source="test")); db.commit()
    for kind, value in claims:
        db.add(VisitDescriptor(event_id=row.id, kind=kind, value=value,
                               source_task="vqa", source_adapter="t")); db.commit()
    return row


FILTERS = dict(from_=None, to=None, plate=None, source=None, scope=None,
               zone_id=None, has_plate=False)


# ── what is missing ───────────────────────────────────────────────────

def test_missing_counts_what_the_question_needs_and_nothing_else(db):
    a = _visit(db, caption="a blue car")                       # described
    b = _visit(db)                                             # nothing
    c = _visit(db, claims=[("colour", "red")])                 # colour but no caption
    _visit(db, label="dog")                                    # not captionable
    out = er.missing_for(db, filters=FILTERS, labels=["car"], camera_ids=None,
                         want_caption=True, want_kinds={"colour"})
    assert out["caption"] == 2 and set(out["ids"]) >= {b.id, c.id}
    assert out["kinds"] == {"colour": 2}                       # a and b lack colour
    assert a.id in out["ids"], "a has a caption but no colour claim"
    assert out["total"] == 3, "three visits need SOMETHING"
    assert out["needs"][a.id] == {"caption": False, "descriptors": True}
    assert out["needs"][b.id] == {"caption": True, "descriptors": True}
    assert out["needs"][c.id] == {"caption": True, "descriptors": False}
    assert out["ids"] == sorted(out["ids"], reverse=True), "newest first"


def test_missing_is_capped_but_the_count_is_honest(db):
    for _ in range(6):
        _visit(db)
    out = er.missing_for(db, filters=FILTERS, labels=["car"], camera_ids=None,
                         want_caption=True, want_kinds=set(), cap=4)
    assert out["caption"] == 6 and len(out["ids"]) == 4


def test_missing_is_nothing_when_nothing_is_needed(db):
    _visit(db)
    out = er.missing_for(db, filters=FILTERS, labels=["car"], camera_ids=None,
                         want_caption=False, want_kinds=set())
    assert out["ids"] == [] and out["total"] == 0 and out["needs"] == {}


# ── the ticket ────────────────────────────────────────────────────────

def test_request_dedupes_what_is_already_queued_and_estimates():
    async def go():
        t1 = er.request([3, 2, 1], caption=True, descriptors=True)
        assert t1["requested"] == 3 and t1["already_queued"] == 0
        assert t1["eta_s"] == 3 * 3 * er.DEFAULT_CALL_S, "3 visits x 3 calls x default"
        t2 = er.request([2, 1, 9], caption=True, descriptors=False)
        assert t2["requested"] == 1 and t2["already_queued"] == 2, (
            "a UI searching on every keystroke must not multiply the work")
        assert er.queue_depth() == 4
        t3 = er.request([], caption=True, descriptors=True)
        assert t3["requested"] == 0 and t3["request_id"] is None
    _run(go())


def test_the_estimate_uses_the_slowest_recent_adapter():
    from services import enrichment_gate as eg
    gate = eg.gate_for("moondream-vlm")
    _run(gate.admit()); gate.release("ok", elapsed_s=12.0)
    assert er.per_call_estimate() == 12.0


def test_kinds_for_attrs_resolves_bare_words_through_the_vocabulary():
    assert er.kinds_for_attrs([("colour", "blue")]) == {"colour"}
    assert "colour" in er.kinds_for_attrs([(None, "blue")])
    assert "vehicle_type" in er.kinds_for_attrs([(None, "van")])


# ── the worker ────────────────────────────────────────────────────────

@pytest.fixture()
def enrichers(monkeypatch):
    """Record which enricher was asked about which visit, with what
    priority, and let a test script drops."""
    seen = {"caption": [], "descriptors": [], "drop": set()}

    async def _cap(event_id, *a, priority="live", **k):
        seen["caption"].append((int(event_id), priority))
        if int(event_id) in seen["drop"]:
            seen["drop"].discard(int(event_id)); return "dropped"
        return "done"

    async def _desc(event_id, *a, priority="live", **k):
        seen["descriptors"].append((int(event_id), priority))
        return "done"

    import services.caption_enrichment as cap_mod
    import services.descriptor_enrichment as desc_mod
    monkeypatch.setattr(cap_mod, "enrich_event_caption", _cap)
    monkeypatch.setattr(desc_mod, "enrich_event_descriptors", _desc)
    monkeypatch.setattr(er, "_DROP_RETRY_S", 0.0)
    monkeypatch.setattr(er, "_PAUSED_POLL_S", 0.01)
    return seen


@pytest.fixture()
def bus(monkeypatch):
    out = []

    async def _pub(**kw):
        out.append(kw)

    import services.event_bus_service as b
    monkeypatch.setattr(b, "publish_system_alert", _pub)
    return out


def test_the_worker_describes_in_order_with_the_requested_priority(enrichers, bus):
    async def go():
        er.request([5, 4], caption=True, descriptors=True)
        req = er._q().get_nowait()
        await er.process_one_request(req)
    _run(go())
    assert enrichers["caption"] == [(5, "requested"), (4, "requested")]
    assert enrichers["descriptors"] == [(5, "requested"), (4, "requested")]
    assert er.queue_depth() == 0, "released from the in-flight set"
    assert bus and bus[0]["alert_type"] == er.DONE_ALERT_TYPE
    assert bus[0]["payload"]["described"] == 2 and bus[0]["payload"]["dropped"] == 0


def test_a_visit_the_gate_refuses_is_retried_then_counted(enrichers, bus):
    enrichers["drop"].add(4)                    # dropped once, then answers
    async def go():
        er.request([4], caption=True, descriptors=False)
        await er.process_one_request(er._q().get_nowait())
    _run(go())
    assert [e for e, _ in enrichers["caption"]] == [4, 4]
    assert bus[0]["payload"]["dropped"] == 0
    # ...and one refused every time is given up on, and said so
    er._reset_for_tests()
    enrichers["drop"].update({7})

    async def _always_drop(event_id, *a, **k):
        return "dropped"

    import services.caption_enrichment as cap_mod
    import pytest as _pt
    mp = _pt.MonkeyPatch(); mp.setattr(cap_mod, "enrich_event_caption", _always_drop)
    try:
        async def go2():
            er.request([7], caption=True, descriptors=False)
            await er.process_one_request(er._q().get_nowait())
        _run(go2())
    finally:
        mp.undo()
    assert bus[-1]["payload"]["dropped"] == 1


def test_the_worker_holds_while_the_governor_is_paused(enrichers, bus, monkeypatch):
    from services import enrichment_governor as gov
    paused = {"on": True}
    monkeypatch.setattr(gov, "host_cpu_high", lambda: paused["on"])

    async def go():
        er.request([1], caption=True, descriptors=False)
        task = asyncio.ensure_future(er.process_one_request(er._q().get_nowait()))
        await asyncio.sleep(0.05)
        assert enrichers["caption"] == [], "nothing sent while PAUSED"
        paused["on"] = False
        gov.governor()._last_eval = -1e9         # the pause is long over
        await asyncio.wait_for(task, 2)
    _run(go())
    assert enrichers["caption"] == [(1, "requested")]


# ── the search-side block ─────────────────────────────────────────────

def test_pending_for_asks_only_what_the_box_offers(db, monkeypatch):
    async def _offers():
        return True, {"colour"}
    monkeypatch.setattr(er, "box_offers", _offers)
    _visit(db); _visit(db, caption="a car")
    block = _run(er.pending_for(db, filters=FILTERS, labels=["car"], camera_ids=None,
                                want_caption=True, want_kinds={"colour", "clothing_top"}))
    assert block["needs"] == {"caption": True, "kinds": ["colour"]}
    assert block["missing"] == 2 and block["requested"] == 2
    assert block["request_id"] and block["eta_s"] > 0
    # asked again: nothing new, nothing multiplied
    again = _run(er.pending_for(db, filters=FILTERS, labels=["car"], camera_ids=None,
                                want_caption=True, want_kinds={"colour"}))
    assert again["requested"] == 0 and again["already_queued"] == 2


def test_pending_for_is_none_when_the_box_offers_nothing(db, monkeypatch):
    async def _offers():
        return False, set()
    monkeypatch.setattr(er, "box_offers", _offers)
    _visit(db)
    assert _run(er.pending_for(db, filters=FILTERS, labels=["car"], camera_ids=None,
                               want_caption=True, want_kinds={"colour"})) is None


# ── missing_for decides exactly as the enrichers will ─────────────────

def test_a_visit_the_enricher_already_looked_at_is_not_missing_that(db):
    cap_looked = _visit(db, looked=["image_captioning"])       # captioner had nothing to say
    vqa_looked = _visit(db, looked=["vqa"])                    # VQA ran, found nothing countable
    fresh = _visit(db)
    out = er.missing_for(db, filters=FILTERS, labels=["car"], camera_ids=None,
                         want_caption=True, want_kinds={"colour"})
    # 'looked and found nothing' is not 'never looked': each looked visit
    # is missing only the OTHER thing.
    assert out["needs"][cap_looked.id] == {"caption": False, "descriptors": True}
    assert out["needs"][vqa_looked.id] == {"caption": True, "descriptors": False}
    assert out["needs"][fresh.id] == {"caption": True, "descriptors": True}
    assert out["caption"] == 2 and out["kinds"] == {"colour": 2}
    both = _visit(db, looked=["image_captioning", "vqa"])
    out = er.missing_for(db, filters=FILTERS, labels=["car"], camera_ids=None,
                         want_caption=True, want_kinds={"colour"})
    assert both.id not in out["needs"], "looked at for both: nothing to ask"


def test_an_unassigned_camera_is_never_missing_anything(db):
    _visit(db, camera_id=2)
    out = er.missing_for(db, filters=FILTERS, labels=["car"], camera_ids=None,
                         want_caption=True, want_kinds={"colour"})
    assert out["ids"] == [] and out["total"] == 0, (
        "no assignment, no caption, no cost — the lane keeps the ingest gate")


def test_only_the_kinds_asked_of_that_class_count(db):
    _visit(db, label="car")
    out = er.missing_for(db, filters=FILTERS, labels=["car"], camera_ids=None,
                         want_caption=False, want_kinds={"clothing_top"})
    assert out["ids"] == [], "a lorry is not asked what colour its top is"


def test_people_are_missing_descriptors_only_with_the_opt_in(db, monkeypatch):
    from core.config import settings
    _visit(db, label="person")
    monkeypatch.setattr(settings, "events_descriptor_people", False, raising=False)
    off = er.missing_for(db, filters=FILTERS, labels=["person"], camera_ids=None,
                         want_caption=False, want_kinds={"clothing_top"})
    assert off["ids"] == [], "the enricher would refuse; the lane must not promise"
    monkeypatch.setattr(settings, "events_descriptor_people", True, raising=False)
    on = er.missing_for(db, filters=FILTERS, labels=["person"], camera_ids=None,
                        want_caption=False, want_kinds={"clothing_top"})
    assert len(on["ids"]) == 1


# ── the ticket knows WHAT was asked, not only which visit ─────────────

def test_a_later_question_needing_another_step_is_not_swallowed():
    async def go():
        t1 = er.request([7], needs={7: {"caption": True, "descriptors": False}})
        assert t1["requested"] == 1
        t2 = er.request([7], needs={7: {"caption": True, "descriptors": True}})
        assert t2["requested"] == 1 and t2["already_queued"] == 0, (
            "the colour claim was not queued yet — a caption-only request must not hide it")
        t3 = er.request([7], needs={7: {"caption": True, "descriptors": True}})
        assert t3["requested"] == 0 and t3["already_queued"] == 1
        assert er._inflight[7] == {"caption", "descriptors"}
    _run(go())


def test_described_counts_visits_that_ran_not_ones_given_up_on(enrichers, bus, monkeypatch):
    async def _always_drop(event_id, *a, **k):
        return "dropped"
    import services.descriptor_enrichment as desc_mod
    monkeypatch.setattr(desc_mod, "enrich_event_descriptors", _always_drop)

    async def go():
        er.request([1, 2], needs={1: {"caption": True, "descriptors": False},
                                  2: {"caption": False, "descriptors": True}})
        await er.process_one_request(er._q().get_nowait())
    _run(go())
    p = bus[-1]["payload"]
    assert p["visits"] == 2 and p["described"] == 1 and p["dropped"] == 1


def test_a_cancelled_request_releases_its_calls_and_visits(enrichers, bus):
    async def go():
        er.request([1, 2, 3], caption=True, descriptors=True)
        assert er._pending_calls == 9
        req = er._q().get_nowait()

        async def slow(*a, **k):
            await asyncio.sleep(10)
        import services.caption_enrichment as cap_mod
        import pytest as _pt
        mp = _pt.MonkeyPatch(); mp.setattr(cap_mod, "enrich_event_caption", slow)
        task = asyncio.ensure_future(er.process_one_request(req))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        mp.undo()
    _run(go())
    assert er._pending_calls == 0, "none of a cancelled request's calls is ahead of anyone"
    assert er.queue_depth() == 0, "nothing stays 'in flight' for a request that is gone"
    assert bus and bus[-1]["payload"]["request_id"]
    assert bus[-1]["payload"]["cancelled"] is True, "announced, but not as done"


def test_an_early_return_is_not_described(enrichers, bus, monkeypatch):
    """Row gone, no adapter, unreadable frame: the enricher returns None
    and nobody looked. The DONE announcement must not call that
    described — a listener would stop asking."""
    async def _gone(event_id, *a, **k):
        return None
    import services.caption_enrichment as cap_mod
    monkeypatch.setattr(cap_mod, "enrich_event_caption", _gone)

    async def go():
        er.request([1, 2], caption=True)
        await er.process_one_request(er._q().get_nowait())
    _run(go())
    assert bus[-1]["payload"]["described"] == 0 and bus[-1]["payload"]["dropped"] == 0


def test_a_visit_the_live_path_still_owns_is_not_missing(db):
    """Seconds old: its ingest-time caption and VQA are at the gate or on
    the wire. Queuing it here too runs the inference twice."""
    now = datetime.now(UTC)            # not WALL: that is stamped at import, minutes ago
    _visit(db, minutes_ago=1, now=now)
    _visit(db, minutes_ago=5, now=now)
    out = er.missing_for(db, filters=FILTERS, labels=None, camera_ids=None,
                         want_caption=True, want_kinds=set())
    assert out["total"] == 1


def test_missing_for_never_trips_the_ingest_unassigned_counter(db):
    """wants_caption counts an unassigned camera toward the ingest path's
    'N qualifying visits skipped' warning. A search is not an ingest."""
    from services import caption_enrichment as cap
    _visit(db, camera_id=2); _visit(db, camera_id=2)
    before = cap._unassigned_skipped
    out = er.missing_for(db, filters=FILTERS, labels=None, camera_ids=None,
                         want_caption=True, want_kinds={"colour"})
    assert out["total"] == 0 and cap._unassigned_skipped == before


def test_a_visit_whose_caption_call_failed_is_left_alone_for_the_cooldown(db, monkeypatch):
    """A timed-out call is not 'looked and found nothing' — the visit stays
    missing for later passes — but the on-demand lane must not ask the
    same slow captioner about it again on the very next question."""
    from datetime import UTC, datetime
    from core.config import settings
    from services.caption_enrichment import CAPTION_FAILED_AT

    just = _visit(db); long_ago = _visit(db); fresh = _visit(db)
    for row, ago_s in ((just, 30), (long_ago, 3600)):
        row.payload = {CAPTION_FAILED_AT: (datetime.now(UTC) - timedelta(seconds=ago_s)).isoformat()}
        db.commit()
    monkeypatch.setattr(settings, "events_caption_retry_cooldown_s", 600.0, raising=False)
    out = er.missing_for(db, filters=FILTERS, labels=["car"], camera_ids=None,
                         want_caption=True, want_kinds=set())
    assert just.id not in out["needs"], "failed 30 s ago: inside the cooldown"
    assert long_ago.id in out["needs"] and fresh.id in out["needs"]
    assert out["caption"] == 2
    # cooldown off: asked again at once
    monkeypatch.setattr(settings, "events_caption_retry_cooldown_s", 0, raising=False)
    out = er.missing_for(db, filters=FILTERS, labels=["car"], camera_ids=None,
                         want_caption=True, want_kinds=set())
    assert just.id in out["needs"]
