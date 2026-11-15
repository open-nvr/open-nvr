#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Derive the coding-assistant files from docs/BUILDING_EFFICIENT_APPS.md.

The guide is the one place the app rules are written. An app scaffolded
by ``opennvr-app new`` carries them twice more, for the tools that read
them: ``AGENTS.md`` (any coding assistant) and a Claude Code skill
(``.claude/skills/opennvr-app/SKILL.md``). Both are generated here, so
there is one text to keep current::

    python scripts/gen_app_agent_guide.py            # rewrite both files
    python scripts/gen_app_agent_guide.py --check    # exit 1 if they differ

``sdk/opennvr-app-sdk/tests/test_agent_guide.py`` runs the check, so a
rule edited in the guide and not regenerated fails CI.

What is taken from the guide, per ``## Rule N — title`` section: the
``**Rule.**`` paragraph, the first paragraph of ``**How.**``, the first
code block (or, without one, the first table) and the ``**Check.**``
line when it names a validator check. The ``## Checklist`` section is taken
whole. Everything else in the two files is the fixed text below.

Stdlib only: it runs before any environment exists.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / "docs" / "BUILDING_EFFICIENT_APPS.md"
TEMPLATE = REPO / "sdk" / "opennvr-app-sdk" / "opennvr_app_sdk" / "templates" / "app"
AGENTS = TEMPLATE / "AGENTS.md"
#: ``_claude`` is renamed to ``.claude`` by the scaffold, like ``_github``.
SKILL = TEMPLATE / "_claude" / "skills" / "opennvr-app" / "SKILL.md"

_RULE_RE = re.compile(r"^## Rule (\d+) — (.+)$", re.M)
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


@dataclass
class Rule:
    number: int
    title: str
    rule: str
    how: str
    code: str
    check: str


def _paragraph(section: str, label: str) -> str:
    """The paragraph that starts with ``**Label.**``, as one line."""
    m = re.search(rf"^\*\*{label}\.\*\*[ \n](?!\n|```|\|)(.+?)(?=\n\n|\Z)", section, re.M | re.S)
    if not m:
        return ""
    text = " ".join(line.strip() for line in m.group(1).splitlines())
    return _LINK_RE.sub(r"\1", text).strip()


def parse(guide: str) -> tuple[list[Rule], str]:
    """The rules and the checklist of the guide."""
    marks = list(_RULE_RE.finditer(guide))
    if not marks:
        raise ValueError("the guide has no '## Rule N — title' sections")
    rules: list[Rule] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(guide)
        section = guide[m.end():end]
        cut = re.search(r"^(?:## |---$)", section, re.M)     # stop at the next heading or rule line
        if cut:
            section = section[:cut.start()]
        rule = _paragraph(section, "Rule")
        if not rule:
            raise ValueError(f"Rule {m.group(1)} has no '**Rule.**' paragraph")
        # The section's first code block, else its first table.
        code = (re.search(r"^```[a-z]*\n.*?^```$", section, re.M | re.S)
                or re.search(r"^\|.+\|\n\|[-| ]+\|\n(?:\|.+\|\n?)+", section, re.M))
        check = _paragraph(section, "Check")
        rules.append(Rule(int(m.group(1)), m.group(2).strip(), rule, _paragraph(section, "How"),
                          _LINK_RE.sub(r"\1", code.group(0)).rstrip() if code else "",
                          check if "`" in check else ""))
    numbers = [r.number for r in rules]
    if numbers != list(range(1, len(rules) + 1)):
        raise ValueError(f"rule numbers are not 1..{len(rules)} in order: {numbers}")
    m = re.search(r"^## Checklist\n(.+?)(?=^## )", guide, re.M | re.S)
    if not m:
        raise ValueError("the guide has no '## Checklist' section")
    checklist = "\n".join(line for line in m.group(1).strip().splitlines())
    return rules, checklist


_HEAD = """\
<!-- GENERATED from docs/BUILDING_EFFICIENT_APPS.md in the OpenNVR repository
     by scripts/gen_app_agent_guide.py. The rules are edited there, not here.
     Notes about THIS app belong below the last section. -->
"""

_INTRO = """\
__APP_NAME__ is an app for OpenNVR, a video recorder that runs detection
and the costly models once and publishes the results. An app subscribes
to those results. It does not decode video, run a detector or call a
model for something the platform has already computed. The ten rules
below are how; the reasons, the measurements and a reference app for
each are in the guide:
__DOCS__BUILDING_EFFICIENT_APPS.md

## Commands

```bash
uv sync                               # the SDK and pytest
uv run pytest -q                      # the app's tests; keep them green
uv run opennvr-app dev                # run the rule against a simulated camera, no stack needed
uv run opennvr-app validate --strict  # manifest, config, listing and the rules below
```

Run `validate --strict` before saying a change is done. A finding names
the file, the line and the rule. Fix it. A real exception is waived on
the flagged line or the one above, and the reason is required:

```python
cap = cv2.VideoCapture(path)   # opennvr: allow[own-decode] reads a file the operator uploaded, not a camera
```

Do not add a waiver to make a finding go away. Ask the person you are
working for first; a waiver is their decision and a reviewer reads it.

## The rules
"""

_TAIL = """\

## Where to look

- The app's rule and manifest: `__APP_MODULE__.py`. Its knobs: `config.example.yml`.
- The platform client (`OpenNVR`): __DOCS__APP_PLATFORM.md
- Every exported SDK name: __DOCS__SDK_REFERENCE.md
- The Tier-0 event shape and its traps: __DOCS__tier0-consumption.md
- Every event an app may subscribe to or publish: __DOCS__EVENT_CONTRACTS.md
- One runnable file per SDK class: the `cookbook/` directory of the
  `opennvr-app-sdk` source.

When the SDK has no call for something the app needs from the platform,
say so and stop. Do not reach core, KAI-C, an adapter or a camera with a
hand-written client: that is the thing these rules exist to prevent.
"""

_SKILL_FRONT = """\
---
name: opennvr-app
description: Rules for writing or changing this OpenNVR app. Use before editing the app's code, config or manifest, when adding a model call, a camera frame, an alert, an event subscription or stored state, and before saying a change is done.
---

"""


def _rules_block(rules: list[Rule]) -> str:
    out: list[str] = []
    for r in rules:
        out.append(f"### {r.number}. {r.title}\n")
        out.append(r.rule + "\n")
        if r.how:
            out.append(r.how + "\n")
        if r.code:
            out.append(r.code + "\n")
        if r.check:
            out.append(f"Checked by: {r.check}\n")
    return "\n".join(out)


def render_agents(guide: str) -> str:
    rules, checklist = parse(guide)
    return (_HEAD + "\n# Working on __APP_NAME__\n\n" + _INTRO + "\n" + _rules_block(rules)
            + "\n## Before you finish\n\n" + checklist + "\n" + _TAIL)


def render_skill(guide: str) -> str:
    rules, checklist = parse(guide)
    return (_SKILL_FRONT + _HEAD + "\n# Building this OpenNVR app\n\n" + _INTRO + "\n"
            + _rules_block(rules) + "\n## Before you finish\n\n" + checklist + "\n" + _TAIL)


def outputs(guide: str | None = None) -> dict[Path, str]:
    text = GUIDE.read_text(encoding="utf-8") if guide is None else guide
    return {AGENTS: render_agents(text), SKILL: render_skill(text)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="write nothing; exit 1 when a generated file differs from the guide")
    args = ap.parse_args(argv)
    stale = []
    for path, text in outputs().items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        stale.append(path)
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    for path in stale:
        print(("stale: " if args.check else "wrote: ") + str(path.relative_to(REPO)))
    if args.check and stale:
        print("run: python scripts/gen_app_agent_guide.py", file=sys.stderr)
        return 1
    if not stale:
        print("up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
