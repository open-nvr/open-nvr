# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The plan must recognise the task strings adapters actually advertise.

The bug (found 2026-09-22): ``TASK_DESCRIPTORS`` is keyed on the
CANONICAL task names from tasks.yml, and ``build_plan`` looked those up
with the RAW string an adapter advertised. Two of the four entries could
therefore never match a shipped adapter — Moondream advertises
"visual_qa" against a table keyed "vqa", BLIP advertises "scene_caption"
against "image_captioning".

The visible effect was not a crash. The plan reported a healthy skill
that promised NO descriptor kinds, so /search/enrichment-plan returned an
empty descriptor_kinds list, a colour filter was never worth offering,
and an enricher driven by the plan would have had nothing to run.
"""

from __future__ import annotations

from services.enrichment_plan import build_plan


def _caps(*adapters):
    return {"adapters": [{"name": n, "tasks": t} for n, t in adapters]}


def test_visual_qa_resolves_to_the_vqa_descriptor_kinds():
    """The one that matters most: vqa is the only shipped task that
    produces the attribute kinds search filters on."""
    plan = build_plan(_caps(("moondream", ["visual_qa"])), {})
    entry = next(s for s in plan if s.descriptor_kinds)
    assert entry.task == "vqa"
    assert "colour" in entry.descriptor_kinds
    assert "vehicle_type" in entry.descriptor_kinds


def test_scene_caption_resolves_to_image_captioning():
    plan = build_plan(_caps(("blip", ["scene_caption"])), {})
    assert [s.task for s in plan] == ["image_captioning"]


def test_the_two_that_already_matched_still_do():
    """Regression guard on the half that was never broken."""
    plan = build_plan(_caps(
        ("fast_plate_ocr", ["license_plate_recognition"]),
        ("insightface", ["face_recognition"]),
    ), {})
    by = {s.task: s for s in plan}
    assert by["license_plate_recognition"].descriptor_kinds == ["plate"]
    assert by["face_recognition"].descriptor_kinds == ["face_id"]
    assert by["license_plate_recognition"].labels == [
        "car", "truck", "bus", "motorcycle"]


def test_one_skill_advertised_two_ways_is_one_entry_with_both_adapters():
    """Grouping on the canonical name, so a box running BLIP and
    Moondream shows one captioning skill served by two adapters rather
    than two half-described ones."""
    plan = build_plan(_caps(
        ("blip", ["scene_caption"]),
        ("moondream", ["image_captioning"]),
    ), {})
    assert len(plan) == 1
    assert plan[0].task == "image_captioning"
    assert plan[0].adapters == ["blip", "moondream"]


def test_an_unknown_task_survives_unchanged():
    """tasks.yml is a taxonomy, not a whitelist — a free-text task from a
    third-party adapter must still register under its own name."""
    plan = build_plan(_caps(("custom", ["counting_penguins"])), {})
    assert [s.task for s in plan] == ["counting_penguins"]
    assert plan[0].descriptor_kinds == []


def test_the_shipped_adapters_index_agrees_with_the_plan_table():
    """The end-to-end version: every descriptor-producing task the plan
    promises must be reachable from a string a SHIPPED adapter actually
    advertises, or that skill is invisible to enrichment on a real box.

    Uses the same YAML-direct path the fix does. Importing
    routers.ai_models here would drag core.config.settings in and raise,
    which is exactly how the first version of this fix ended up a silent
    no-op.
    """
    import pathlib

    import yaml

    from services.enrichment_plan import TASK_DESCRIPTORS, _canonical_tasks

    root = pathlib.Path(__file__).resolve().parents[1]
    raw = yaml.safe_load((root / "config/adapters_index.yml").read_text())
    adapters = raw["adapters"] if isinstance(raw, dict) and "adapters" in raw else raw
    canon = _canonical_tasks()
    assert canon, "tasks.yml alias map is empty — the fix is a no-op"

    advertised = {t for a in adapters for t in (a.get("tasks_advertised") or [])}
    reachable = {canon.get(str(t).strip().lower(), str(t)) for t in advertised}
    missing = set(TASK_DESCRIPTORS) - reachable
    assert not missing, (
        f"the plan promises descriptor kinds for {sorted(missing)}, but no "
        "shipped adapter advertises anything that canonicalises to them")


# ── The shapes KAI-C actually sends ──────────────────────────────────
#
# Trimmed from a running box's /capabilities and /adapters/health. The
# helpers above build a list of {name, tasks}; KAI-C sends a dict keyed
# by adapter name with the tasks under capabilities.tasks_advertised, and
# health entries that say status "ok" rather than healthy true. Read
# against the invented shape, the real plan was [] on every deployment.

_REAL_CAPS = {
    "kai_c": {"version": "0.1.5", "service": "kai-c"},
    "adapters": {
        "clip": {"url": "http://clip-adapter:9004",
                 "capabilities": {"adapter": {"name": "clip"},
                                  "tasks_advertised": ["embed"]}},
        "ollamavlm": {"url": "http://ollamavlm-adapter:9009",
                      "capabilities": {"adapter": {"name": "ollamavlm"},
                                       "tasks_advertised": ["visual_qa", "scene_caption"]}},
        "fast_plate_ocr": {"url": "http://fast-plate-ocr-adapter:9003",
                           "capabilities": {"tasks_advertised": ["license_plate_recognition"]}},
        # KAI-C could not reach this one: no capabilities block at all.
        "moondream": {"url": "http://moondream-adapter:9007", "error": "connect timeout"},
    },
}
_REAL_HEALTH = {
    "kai_c_status": "ok",
    "adapters": {"clip": {"status": "ok"}, "ollamavlm": {"status": "ok"},
                 "fast_plate_ocr": {"status": "error"}},
}


def test_the_real_capabilities_shape_yields_a_plan():
    plan = {s.task: s for s in build_plan(_REAL_CAPS, _REAL_HEALTH)}
    assert "vqa" in plan, sorted(plan)
    assert plan["vqa"].adapters == ["ollamavlm"]
    assert plan["vqa"].healthy is True
    assert plan["vqa"].descriptor_kinds == ["colour", "vehicle_type", "clothing_top", "carrying"]
    assert plan["image_captioning"].adapters == ["ollamavlm"]
    assert plan["embed"].adapters == ["clip"]
    # health said "error", so the skill is listed but not runnable
    assert plan["license_plate_recognition"].healthy is False
    # an unreachable adapter contributes no tasks and no crash
    assert not any("moondream" in s.adapters for s in plan.values())


def test_status_ok_counts_as_healthy_and_healthy_false_still_wins():
    caps = _caps(("a", ["visual_qa"]), ("b", ["visual_qa"]))
    plan = build_plan(caps, {"adapters": {"a": {"status": "ok"}, "b": {"healthy": False, "status": "ok"}}})
    vqa = next(s for s in plan if s.task == "vqa")
    assert vqa.healthy is True            # a is live
    plan = build_plan(_caps(("b", ["visual_qa"])), {"adapters": {"b": {"healthy": False}}})
    assert next(s for s in plan if s.task == "vqa").healthy is False
