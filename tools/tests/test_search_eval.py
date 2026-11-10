"""The search eval's scoring: ranks, recall, negatives, and the summary —
against hand-made answers, no server."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "search_eval", Path(__file__).resolve().parents[1] / "search_eval.py")
se = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(se)


def _answer(ids, **extra):
    body = {"hits": [{"id": i} for i in ids], "total": len(ids)}
    body.update(extra)
    return body


def test_a_positive_case_scores_rank_recall_and_what_was_wrong_in_the_top_five():
    r = se.score_case({"query": "q", "relevant": [10, 11, 12]}, _answer([5, 11, 7, 10, 8, 9, 12]))
    assert r["first_rank"] == 2 and r["rr"] == 0.5
    assert r["hit5"] is True and r["passed"] is True
    assert r["p5"] == 2 / 5
    assert r["recall10"] == 1.0 and r["recall_all"] == 1.0
    assert r["wrong_top5"] == [5, 7, 8]


def test_a_miss_is_a_zero_not_an_error():
    r = se.score_case({"query": "q", "relevant": [1]}, _answer([2, 3]))
    assert r["first_rank"] is None and r["rr"] == 0.0 and r["recall10"] == 0.0
    assert r["hit5"] is False and r["passed"] is False
    r = se.score_case({"query": "q", "relevant": [1]}, _answer([]))
    assert r["rr"] == 0.0 and r["p5"] == 0.0


def test_a_negative_passes_on_nothing_relaxed_or_below_the_floor_only():
    neg = {"query": "a dog", "relevant": []}
    assert se.score_case(neg, _answer([]))["passed"] is True
    assert se.score_case(neg, _answer([4, 5], relaxed={"dropped": "a dog"}))["passed"] is True
    assert se.score_case(neg, _answer([4], semantic={"used": True, "reason": "below-floor"}))["passed"] is True
    r = se.score_case(neg, _answer([4, 5, 6], semantic={"used": True, "arms": {"vector": 3}}))
    assert r["passed"] is False and r["top"] == [4, 5, 6]


def test_the_semantic_block_is_carried_for_the_report():
    sem = {"used": True, "arms": {"vector": 10, "text": 3, "caption": 4}, "added_by_caption": 2,
           "best": 0.13, "floor": 0.08, "caption": {"best": 0.81, "floor": 0.75}}
    r = se.score_case({"query": "q", "relevant": [1]}, _answer([1], semantic=sem,
                                                                 interpretation={"corrections": [["persn", "person"]]}))
    assert r["semantic"]["arms"] == sem["arms"] and r["caption_best"] == 0.81
    assert r["corrections"] == [["persn", "person"]]


def test_the_summary_averages_positives_by_kind_and_counts_negatives():
    results = [
        se.score_case({"query": "a", "relevant": [1, 2], "kind": "caption"}, _answer([1, 9, 2])),
        se.score_case({"query": "b", "relevant": [3], "kind": "typo"}, _answer([8, 7, 6, 5, 4, 3])),
        se.score_case({"query": "c", "relevant": [], "kind": "negative"}, _answer([])),
        se.score_case({"query": "d", "relevant": [], "kind": "negative"}, _answer([1, 2])),
    ]
    s = se.summarise(results)
    assert s["cases"] == 4 and s["positive"] == 2 and s["negative"] == 2
    assert s["mrr"] == round((1.0 + 1 / 6) / 2, 3)
    assert s["hit@5"] == 0.5 and s["recall@10"] == 1.0
    assert s["negatives_passed"] == 1
    assert s["by_kind"]["caption"]["n"] == 1 and s["by_kind"]["typo"]["mrr"] == round(1 / 6, 3)
