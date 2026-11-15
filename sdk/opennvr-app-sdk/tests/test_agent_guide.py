# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""The coding-assistant files a scaffolded app carries are derived from
docs/BUILDING_EFFICIENT_APPS.md, and this test keeps them derived.

The rules are written once, in the guide. ``AGENTS.md`` and the Claude
Code skill in the app template are generated from it by
``scripts/gen_app_agent_guide.py``; a rule edited in one place and not
the other is the drift this fails on.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from opennvr_app_sdk import scaffold

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "gen_app_agent_guide.py"

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists() or not (REPO / "docs" / "BUILDING_EFFICIENT_APPS.md").exists(),
    reason="the guide and its generator live in the OpenNVR repository, not in an sdist")


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_app_agent_guide", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module          # dataclasses resolve the module by name
    spec.loader.exec_module(module)
    return module


def test_the_generated_files_match_the_guide(gen):
    for path, text in gen.outputs().items():
        assert path.exists(), f"{path} is missing — run: python scripts/gen_app_agent_guide.py"
        assert path.read_text(encoding="utf-8") == text, (
            f"{path.relative_to(REPO)} differs from the guide — "
            "run: python scripts/gen_app_agent_guide.py")


def test_every_rule_reaches_the_assistant(gen):
    rules, checklist = gen.parse(gen.GUIDE.read_text(encoding="utf-8"))
    assert len(rules) == 10
    agents = gen.AGENTS.read_text(encoding="utf-8")
    for rule in rules:
        assert f"### {rule.number}. {rule.title}" in agents
        assert rule.rule in agents
    assert checklist in agents and "validate --strict" in agents


def test_a_guide_without_its_structure_is_refused(gen):
    with pytest.raises(ValueError, match="no '## Rule"):
        gen.parse("# A guide\n\nprose only\n")
    with pytest.raises(ValueError, match="Rule 1 has no"):
        gen.parse("## Rule 1 — A title\n\nno rule paragraph\n\n## Checklist\n- [ ] x\n\n## End\n")
    with pytest.raises(ValueError, match="not 1"):
        gen.parse("## Rule 2 — A title\n\n**Rule.** Do it.\n\n## Checklist\n- [ ] x\n\n## End\n")


def test_a_scaffolded_app_carries_both_files(tmp_path):
    app_dir = scaffold.generate("gate-watch", "object_detection", tmp_path)
    agents = (app_dir / "AGENTS.md").read_text(encoding="utf-8")
    skill = (app_dir / ".claude" / "skills" / "opennvr-app" / "SKILL.md").read_text(encoding="utf-8")
    assert not (app_dir / "_claude").exists()
    assert "# Working on Gate Watch" in agents and "gate_watch.py" in agents
    assert skill.startswith("---\nname: opennvr-app\n") and "Gate Watch is an app for OpenNVR" in skill
    # No template token survives, and the guide link resolves somewhere real.
    for text in (agents, skill):
        assert "__APP_" not in text and "__DOCS__" not in text
        assert "https://github.com/open-nvr/open-nvr/blob/main/docs/BUILDING_EFFICIENT_APPS.md" in text
    # The scaffold itself breaks none of the rules it ships.
    config = (app_dir / "config.example.yml").read_text(encoding="utf-8")
    assert 'subject_pattern: "opennvr.inference.tier0.>"' in config and "consume_tier0: true" in config
