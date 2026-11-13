"""A model change behind the embedding adapter re-embeds the store.

``event_embeddings.model`` records the producer as ``<adapter>:<model
version>``; a vector from another producer is replaced by the enricher,
and the backfill walks history again when any such vector exists. The
case this guards: CLIP (512 wide) → SigLIP 2 (768 wide) behind the same
``clip`` adapter name, which search would otherwise skip row by row (a
dimension mismatch is a non-match) for as long as the old vectors sat
there.

The second half of the file is the CAPTION vector: written by the
captioner the moment a caption lands, through the same text tower the
query uses, never for an empty caption, never in a way that can take
the caption back — and replaced by the same model-change walk, because
a stale caption vector is as wrong as a stale frame vector.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import os
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("INTERNAL_API_KEY", "site_" + secrets.token_hex(16))
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_reembed_test.db")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base  # noqa: E402
from models import Camera, EventEmbedding, Role, TimelineEvent, User  # noqa: E402
from services import embedding_store  # noqa: E402

T0 = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)
OLD = "clip:open_clip/ViT-B-32/laion2b_s34b_b79k"
NEW = "clip:open_clip/ViT-B-16-SigLIP2-256/webli"
#: What the clip adapter's /capabilities actually reports: the
#: architecture as the model name, the pretrained tag as the version.
NEW_CAPS = {"model": {"name": "open_clip/ViT-B-16-SigLIP2-256", "version": "webli"}}


@pytest.fixture()
def session_local(monkeypatch):
    engine = create_engine("sqlite://", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    import core.database as core_db
    monkeypatch.setattr(core_db, "SessionLocal", factory)
    s = factory()
    s.add(Role(id=1, name="admin", description="t")); s.commit()
    s.add(User(id=1, username="o", email="o@x.t", hashed_password="x", is_active=True, role_id=1)); s.commit()
    s.add(Camera(id=1, name="Yard", ip_address="10.0.0.1", rtsp_url="rtsp://x/1", owner_id=1)); s.commit()
    yield factory
    s.close()


def _visit(db, *, vec=None, model=OLD) -> int:
    row = TimelineEvent(camera_id=1, label="truck", event_type="visit", source="tier0",
                        started_at=T0, ended_at=T0 + timedelta(seconds=20),
                        evidence_path="x/y.jpg")
    db.add(row); db.commit()
    if vec is not None:
        embedding_store.put_embedding(db, event_id=row.id, vector=vec, model=model)
    return row.id


def _wire(monkeypatch, producer: str, vector: list[float], calls: list):
    import services.embed_enrichment as emb
    from core.config import settings
    monkeypatch.setattr(settings, "events_embed_enrichment", True, raising=False)

    async def _producer():
        return "clip", producer

    async def _infer(adapter, payload, *, what, **kw):
        calls.append(what)
        return list(vector)

    monkeypatch.setattr(emb, "_resolve_embed_producer", _producer)
    monkeypatch.setattr(emb, "_infer", _infer)
    return emb


def test_producer_identity_is_adapter_and_model_version():
    from services.embed_enrichment import producer_identity
    assert producer_identity("clip", NEW_CAPS) == NEW
    assert producer_identity("clip", {"model": {"version": "v2"}}) == "clip:v2"
    assert producer_identity("clip", {}) == "clip"
    assert producer_identity("clip", {"model": {"version": "  "}}) == "clip"


def test_a_vector_from_another_producer_is_replaced_and_the_same_one_is_kept(session_local, monkeypatch):
    calls: list = []
    emb = _wire(monkeypatch, NEW, [0.0, 1.0, 0.0, 0.0, 0.0, 0.0], calls)
    db = session_local()
    eid = _visit(db, vec=[1.0, 0.0, 0.0, 0.0], model=OLD)   # 4 wide, the old model
    db.close()

    asyncio.run(emb.enrich_event_embedding(eid, evidence_jpeg=b"jpg"))
    db = session_local()
    row = db.get(EventEmbedding, (eid, "image"))
    assert row.model == NEW and row.dim == 6 and calls == [f"event {eid}"]
    db.close()

    # Same producer again: already embedded, nothing called.
    asyncio.run(emb.enrich_event_embedding(eid, evidence_jpeg=b"jpg"))
    assert calls == [f"event {eid}"]


def test_stale_rows_counts_other_producers_and_unlabelled(session_local):
    db = session_local()
    _visit(db, vec=[1.0, 0.0], model=OLD)
    _visit(db, vec=[0.0, 1.0], model=NEW)
    _visit(db, vec=[1.0, 1.0], model=None)
    assert embedding_store.stale_rows(db, NEW) == 2
    assert embedding_store.stale_rows(db, OLD) == 2
    db.close()


def test_reembed_due_only_when_the_store_holds_another_producer(session_local, monkeypatch):
    import services.enrichment_backfill as bf
    _wire(monkeypatch, NEW, [0.0, 1.0], [])
    db = session_local()
    _visit(db, vec=[0.0, 1.0], model=NEW)
    db.close()
    assert asyncio.run(bf.reembed_due()) == (False, NEW)
    db = session_local()
    _visit(db, vec=[1.0, 0.0], model=OLD)
    db.close()
    assert asyncio.run(bf.reembed_due()) == (True, NEW)


def test_reembed_due_is_quiet_with_embedding_off_or_no_adapter(session_local, monkeypatch):
    import services.enrichment_backfill as bf
    import services.embed_enrichment as emb
    from core.config import settings
    monkeypatch.setattr(settings, "events_embed_enrichment", False, raising=False)
    assert asyncio.run(bf.reembed_due()) == (False, None)
    monkeypatch.setattr(settings, "events_embed_enrichment", True, raising=False)

    async def _none():
        return None
    monkeypatch.setattr(emb, "_resolve_embed_producer", _none)
    assert asyncio.run(bf.reembed_due()) == (False, None)


def test_reset_for_reembed_starts_the_walk_over(session_local):
    import services.enrichment_backfill as bf
    db = session_local()
    bf._write_state(db, {"done": True, "cursor": 7, "top": 99, "examined": 42})
    state = bf.reset_for_reembed(db, NEW)
    assert state == {"reembed_for": NEW}
    assert bf.read_state(db) == {"reembed_for": NEW}
    db.close()


# ── the caption vector ───────────────────────────────────────────────


def _wire_captioner(monkeypatch, caption, calls: list):
    """A captioner that answers ``caption`` for every frame, with the
    caption flag on. Returns the module."""
    import services.caption_enrichment as cap
    from core.config import settings
    monkeypatch.setattr(settings, "events_caption_enrichment", True, raising=False)

    async def _adapter():
        return "captioner"

    async def _caption(jpeg, adapter, camera_handle, event_id=None, *, priority="live"):
        calls.append(event_id)
        return caption

    monkeypatch.setattr(cap, "_resolve_caption_adapter", _adapter)
    monkeypatch.setattr(cap, "_caption_jpeg", _caption)
    return cap


def _wire_text_embedder(monkeypatch, producer: str, vector: list[float],
                        payloads: list, *, enabled: bool = True):
    """Like ``_wire`` but records the PAYLOAD, so a test can see which
    words went to the text tower."""
    import services.embed_enrichment as emb
    from core.config import settings
    monkeypatch.setattr(settings, "events_embed_enrichment", enabled, raising=False)

    async def _producer():
        return "clip", producer

    async def _infer(adapter, payload, *, what, **kw):
        payloads.append(payload)
        return list(vector)

    monkeypatch.setattr(emb, "_resolve_embed_producer", _producer)
    monkeypatch.setattr(emb, "_infer", _infer)
    return emb


def test_caption_text_is_the_caption_plus_attributes_and_empty_without_a_caption():
    from services.embed_enrichment import caption_text
    assert caption_text("a man at the gate", "blue kurta") == "a man at the gate blue kurta"
    assert caption_text("  a man\n at the gate ", None) == "a man at the gate"
    # Attributes alone are a filter, not a description: nothing to embed.
    assert caption_text("", "blue kurta") == ""
    assert caption_text(None, None) == ""


def test_a_caption_that_lands_is_embedded_as_the_visits_caption_vector(session_local, monkeypatch):
    """The producer. The captioner writes the words, then the same text
    tower the query goes through turns them into the visit's ``caption``
    vector — and only that: the frame is the image enricher's job."""
    from models import EventText
    payloads: list = []
    _wire_text_embedder(monkeypatch, NEW, [0.0, 1.0, 0.0], payloads)
    cap = _wire_captioner(monkeypatch, "a person wearing a light blue tunic", [])
    db = session_local()
    eid = _visit(db)
    # A caption-less row with attributes, as the descriptor enricher
    # leaves one (no caption source): the captioner fills it in.
    db.add(EventText(event_id=eid, attributes="blue tunic")); db.commit()
    db.close()

    assert asyncio.run(cap.enrich_event_caption(eid, evidence_jpeg=b"jpg")) == "done"

    db = session_local()
    row = db.get(EventEmbedding, (eid, "caption"))
    assert row is not None and row.model == NEW and row.dim == 3
    assert db.get(EventEmbedding, (eid, "image")) is None, "the captioner embedded a frame"
    assert db.get(EventText, eid).caption == "a person wearing a light blue tunic"
    db.close()
    assert payloads == [{"task": "embed",
                         "text": "a person wearing a light blue tunic blue tunic"}], (
        "the text tower did not get caption + attributes")


def test_an_empty_caption_is_never_embedded(session_local, monkeypatch):
    """"Looked and found nothing" is recorded on the visit, not in the
    vector store: a vector of nothing would match nothing and make
    "which visits still need a caption vector" wrong."""
    payloads: list = []
    emb = _wire_text_embedder(monkeypatch, NEW, [0.0, 1.0, 0.0], payloads)
    cap = _wire_captioner(monkeypatch, "", [])
    db = session_local(); eid = _visit(db); db.close()

    assert asyncio.run(cap.enrich_event_caption(eid, evidence_jpeg=b"jpg")) == "done"
    # And the producer asked directly, over a visit with no words:
    assert asyncio.run(emb.enrich_event_caption_embedding(eid)) is None

    db = session_local()
    assert db.get(EventEmbedding, (eid, "caption")) is None
    db.close()
    assert payloads == [], "the text tower was called for an empty caption"


def test_caption_vectors_are_behind_the_embed_flag(session_local, monkeypatch):
    from models import EventText
    payloads: list = []
    _wire_text_embedder(monkeypatch, NEW, [0.0, 1.0, 0.0], payloads, enabled=False)
    cap = _wire_captioner(monkeypatch, "a red truck", [])
    db = session_local(); eid = _visit(db); db.close()

    assert asyncio.run(cap.enrich_event_caption(eid, evidence_jpeg=b"jpg")) == "done"

    db = session_local()
    assert db.get(EventText, eid).caption == "a red truck", "the caption itself was lost"
    assert db.get(EventEmbedding, (eid, "caption")) is None
    db.close()
    assert payloads == []


def test_an_embedder_failure_never_costs_the_caption(session_local, monkeypatch):
    """The caption is committed before its vector is attempted, and the
    attempt swallows everything: a dead adapter leaves the words exactly
    where the captioner put them and the task still reports done."""
    import services.embed_enrichment as emb
    from models import EventText
    _wire_text_embedder(monkeypatch, NEW, [0.0, 1.0, 0.0], [])

    async def _boom(adapter, payload, *, what, **kw):
        raise RuntimeError("adapter on fire")
    monkeypatch.setattr(emb, "_infer", _boom)
    cap = _wire_captioner(monkeypatch, "a red truck", [])
    db = session_local(); eid = _visit(db); db.close()

    assert asyncio.run(cap.enrich_event_caption(eid, evidence_jpeg=b"jpg")) == "done"

    db = session_local()
    assert db.get(EventText, eid).caption == "a red truck"
    assert db.get(EventEmbedding, (eid, "caption")) is None
    db.close()


def test_a_dropped_caption_embedding_is_reported_so_the_backfill_holds(session_local, monkeypatch):
    import services.embed_enrichment as emb
    from models import EventText
    from services.enrichment_gate import DROPPED
    _wire_text_embedder(monkeypatch, NEW, [0.0, 1.0, 0.0], [])

    async def _dropped(adapter, payload, *, what, **kw):
        return DROPPED
    monkeypatch.setattr(emb, "_infer", _dropped)
    db = session_local(); eid = _visit(db)
    db.add(EventText(event_id=eid, caption="a red truck", source="t")); db.commit(); db.close()

    assert asyncio.run(emb.enrich_event_caption_embedding(eid)) == "dropped"


def test_a_model_change_re_embeds_caption_vectors_too(session_local, monkeypatch):
    """The walk that replaces stale frame vectors must replace stale
    caption vectors: a caption vector from the old model compared with
    a query from the new one is a confident ordering of noise."""
    from models import EventText
    payloads: list = []
    emb = _wire_text_embedder(monkeypatch, NEW, [0.0, 1.0, 0.0, 0.0, 0.0, 0.0], payloads)
    db = session_local()
    eid = _visit(db, vec=[0.0, 1.0, 0.0, 0.0, 0.0, 0.0], model=NEW)   # frame: current
    db.add(EventText(event_id=eid, caption="a lorry at the loading bay", source="t"))
    db.commit()
    embedding_store.put_embedding(db, event_id=eid, vector=[1.0, 0.0, 0.0, 0.0],
                                  model=OLD, kind="caption")              # caption: stale
    assert embedding_store.stale_rows(db, NEW) == 1, "a stale caption vector was not counted"
    db.close()

    import services.enrichment_backfill as bf
    assert asyncio.run(bf.reembed_due()) == (True, NEW), (
        "a store whose only stale vector is a caption did not trigger the re-embed")

    assert asyncio.run(emb.enrich_event_caption_embedding(eid)) == "done"
    db = session_local()
    row = db.get(EventEmbedding, (eid, "caption"))
    assert row.model == NEW and row.dim == 6
    assert embedding_store.stale_rows(db, NEW) == 0
    assert db.query(EventEmbedding).filter_by(event_id=eid).count() == 2, (
        "re-embedding the caption touched the frame vector")
    db.close()

    # Same producer again: nothing to do, nothing called.
    assert asyncio.run(emb.enrich_event_caption_embedding(eid)) is None
    assert len(payloads) == 1


# ── the caption arm's own model (text_embed) ────────────────────────

TEXT_PRODUCER = "text-embed:BAAI/bge-small-en-v1.5/cls-onnx"


def _wire_two_models(monkeypatch, *, text_producer, frame_producer=NEW,
                     frame_vector=(0.0, 1.0, 0.0), text_vector=(1.0, 0.0),
                     payloads: list):
    """A frame embedder AND a sentence encoder, each answering its own
    vector; ``text_producer=None`` means no text_embed adapter is
    registered and the caption path must fall back to the frame one."""
    import services.embed_enrichment as emb
    from core.config import settings
    monkeypatch.setattr(settings, "events_embed_enrichment", True, raising=False)

    async def _frame():
        return "clip", frame_producer

    async def _text():
        return None if text_producer is None else ("text-embed", text_producer)

    async def _infer(adapter, payload, *, what, **kw):
        payloads.append((adapter, payload))
        return list(text_vector if adapter == "text-embed" else frame_vector)

    monkeypatch.setattr(emb, "_resolve_embed_producer", _frame)
    monkeypatch.setattr(emb, "_resolve_text_embed_producer", _text)
    monkeypatch.setattr(emb, "_infer", _infer)
    return emb


def test_caption_vectors_go_through_the_sentence_encoder_when_one_is_registered(session_local, monkeypatch):
    """With a text_embed adapter the caption vector is ITS vector, under
    ITS producer, asked as a document — the frame embedder's text tower
    is never asked for a caption."""
    from models import EventText
    payloads: list = []
    emb = _wire_two_models(monkeypatch, text_producer=TEXT_PRODUCER, payloads=payloads)
    db = session_local()
    eid = _visit(db)
    db.add(EventText(event_id=eid, caption="a lorry at the loading bay", source="t")); db.commit()
    db.close()

    assert asyncio.run(emb.enrich_event_caption_embedding(eid)) == "done"

    db = session_local()
    row = db.get(EventEmbedding, (eid, "caption"))
    assert row.model == TEXT_PRODUCER and row.dim == 2
    db.close()
    assert payloads == [("text-embed", {"task": "text_embed", "kind": "document",
                                        "text": "a lorry at the loading bay"})]
    # Same producer again: nothing to do.
    assert asyncio.run(emb.enrich_event_caption_embedding(eid)) is None
    assert len(payloads) == 1


def test_without_a_sentence_encoder_the_caption_path_is_the_frame_embedders_text_tower(session_local, monkeypatch):
    from models import EventText
    payloads: list = []
    emb = _wire_two_models(monkeypatch, text_producer=None, payloads=payloads)
    db = session_local()
    eid = _visit(db)
    db.add(EventText(event_id=eid, caption="a lorry", source="t")); db.commit()
    db.close()
    assert asyncio.run(emb.enrich_event_caption_embedding(eid)) == "done"
    assert payloads == [("clip", {"task": "embed", "text": "a lorry"})], (
        "the fallback must send the embed adapters the bare shape they always had")
    db = session_local()
    assert db.get(EventEmbedding, (eid, "caption")).model == NEW
    db.close()


def test_the_caption_query_reuses_the_frame_vector_only_when_it_is_the_same_adapter(monkeypatch):
    payloads: list = []
    emb = _wire_two_models(monkeypatch, text_producer=TEXT_PRODUCER, payloads=payloads)
    vec, adapter = asyncio.run(emb.embed_caption_query("red lorry", frame=([0.0, 1.0, 0.0], "clip")))
    assert (vec, adapter) == ([1.0, 0.0], "text-embed")
    assert payloads == [("text-embed", {"task": "text_embed", "kind": "query", "text": "red lorry"})]

    payloads.clear()
    emb = _wire_two_models(monkeypatch, text_producer=None, payloads=payloads)
    vec, adapter = asyncio.run(emb.embed_caption_query("red lorry", frame=([0.0, 1.0, 0.0], "clip")))
    assert (vec, adapter) == ([0.0, 1.0, 0.0], "clip") and payloads == [], (
        "one adapter serving both arms must not be asked the same words twice")
    vec, adapter = asyncio.run(emb.embed_caption_query("red lorry"))
    assert adapter == "clip" and payloads == [("clip", {"task": "embed", "text": "red lorry"})]
    assert asyncio.run(emb.embed_caption_query("   ")) == (None, None)


def test_a_caption_space_beside_a_frame_space_is_not_a_mixed_store(session_local):
    """384-d captions next to 768-d frames is the intended arrangement:
    each kind is judged against itself, and the capability says which
    size and producer each kind has."""
    db = session_local()
    eid = _visit(db, vec=[0.0, 1.0, 0.0, 0.0], model=NEW)
    embedding_store.put_embedding(db, event_id=eid, vector=[1.0, 0.0], model=TEXT_PRODUCER, kind="caption")
    cap = embedding_store.capability(db)
    assert cap.available and cap.dim == 4 and cap.caption_dim == 2
    assert cap.models == (NEW,) and cap.caption_models == (TEXT_PRODUCER,)
    assert not cap.mixed_models and not cap.mixed_caption_models
    assert "not comparable" not in cap.note and "Mixed" not in cap.note

    # Two producers WITHIN the caption kind is the mixed case.
    eid2 = _visit(db)
    embedding_store.put_embedding(db, event_id=eid2, vector=[0.0, 1.0], model=NEW, kind="caption")
    cap = embedding_store.capability(db)
    assert cap.mixed_caption_models and not cap.mixed_models
    assert "Mixed producers (caption:" in cap.note
    # Two SIZES within one kind is the incoherent one.
    embedding_store.put_embedding(db, event_id=eid2, vector=[0.0, 1.0, 0.0], model=NEW, kind="caption")
    cap = embedding_store.capability(db)
    assert cap.caption_dim is None and "2 different caption vector sizes" in cap.note
    db.close()


def test_stale_rows_counts_per_kind_and_reembed_is_due_for_either(session_local, monkeypatch):
    """Frames stale against the frame producer, captions against the
    sentence encoder: a caption row the old text tower wrote is stale
    once a text_embed adapter is registered, and the walk runs."""
    import services.enrichment_backfill as bf
    payloads: list = []
    _wire_two_models(monkeypatch, text_producer=TEXT_PRODUCER, payloads=payloads)
    db = session_local()
    eid = _visit(db, vec=[0.0, 1.0, 0.0], model=NEW)                    # frame: current
    embedding_store.put_embedding(db, event_id=eid, vector=[0.0, 1.0, 0.0], model=NEW,
                                  kind="caption")                        # caption: the old tower's
    assert embedding_store.stale_rows(db, NEW, kind="image") == 0
    assert embedding_store.stale_rows(db, TEXT_PRODUCER, kind="caption") == 1
    assert embedding_store.stale_rows(db, NEW) == 0, (
        "the all-kinds count cannot see it: the caption row carries the frame "
        "producer, which is exactly what the per-kind count is for")
    db.close()
    assert asyncio.run(bf.reembed_due()) == (True, NEW)

    db = session_local()
    embedding_store.put_embedding(db, event_id=eid, vector=[1.0, 0.0], model=TEXT_PRODUCER,
                                  kind="caption")
    db.close()
    assert asyncio.run(bf.reembed_due()) == (False, NEW)


# ── an adapter nobody has probed yet is tried, not skipped ──────────────


def _kai_c(monkeypatch, caps: dict, health: dict | None):
    import routers.skills as skills

    async def _view():
        return health, caps

    monkeypatch.setattr(skills, "_kai_c_view", _view)


def test_an_adapter_with_unpolled_health_is_tried_and_a_failing_one_is_skipped(monkeypatch):
    """After a core restart every adapter re-registers and KAI-C says
    "unknown" for each until its next poll. Skipping them meant the
    embedders were off for those minutes and search ran words-only
    without saying so. Only a REPORTED failure skips an adapter."""
    import services.embed_enrichment as emb
    caps = {"clip": {"capabilities": {"tasks_advertised": ["embed"], "model": {"name": "m", "version": "v"}}},
            "text-embed": {"capabilities": {"tasks_advertised": ["text_embed"], "model": {"name": "bge", "version": "cls"}}}}
    _kai_c(monkeypatch, caps, {"clip": {"status": "unknown", "reason": "health not polled yet"},
                               "text-embed": {"status": "unknown"}})
    assert asyncio.run(emb._resolve_embed_producer()) == ("clip", "clip:m/v")
    assert asyncio.run(emb._resolve_text_embed_producer()) == ("text-embed", "text-embed:bge/cls")
    _kai_c(monkeypatch, caps, {"clip": {"status": "error"}, "text-embed": {"status": "ok"}})
    assert asyncio.run(emb._resolve_embed_producer()) is None
    assert asyncio.run(emb._resolve_text_embed_producer()) == ("text-embed", "text-embed:bge/cls")
    _kai_c(monkeypatch, caps, None)                       # no health block at all: try
    assert asyncio.run(emb._resolve_embed_producer()) == ("clip", "clip:m/v")
    _kai_c(monkeypatch, caps, {})                         # a health block that names nobody: try
    assert asyncio.run(emb._resolve_embed_producer()) == ("clip", "clip:m/v")
