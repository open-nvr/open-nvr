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
