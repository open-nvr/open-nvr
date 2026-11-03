# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""Workflow files (RFC #620): the public contract, ``workflow: v1``.

:func:`validate` checks a whole file, as the engine, the builder and
outside tools must all check it, and says where each problem is::

    from opennvr_app_sdk.workflows import Registry, validate

    report = validate(yaml_text, registry=Registry(skills=..., cameras=...))
    report.ok, report.errors      # [Problem(path="then[0].ai.rules", message=...)]
    report.needs                  # the skills, apps, camera roles... it uses

The ``ai:`` step is checked and run by :mod:`.ai_step`, to an outcome
with the rows that explain it::

    from opennvr_app_sdk.workflows import AIStep, run_ai_step

    step = AIStep.from_dict(doc["then"][0]["ai"], skills=site_skills)
    result = await run_ai_step(step, llm, inputs=event, images=[jpeg])
    result.outcome        # one of the step's outcomes, or "unsure"
    result.actions        # that outcome's path, as written
    result.row()          # the workflow_run_steps row
    result.trace_rows()   # the workflow_tool_calls rows

The file's format is in :mod:`.validate`; running the other steps is the
engine's.
"""
from __future__ import annotations

from .ai_step import (
    MAX_TOOLS,
    TOOL_EFFECTS,
    AIStep,
    AIStepError,
    StepResult,
    check_ai_step,
    resume_ai_step,
    run_ai_step,
)
from .validate import (
    CONTRACT,
    AppInfo,
    Problem,
    Registry,
    Report,
    parse_duration,
    validate,
)

__all__ = [
    "CONTRACT",
    "MAX_TOOLS",
    "TOOL_EFFECTS",
    "AIStep",
    "AIStepError",
    "AppInfo",
    "Problem",
    "Registry",
    "Report",
    "StepResult",
    "check_ai_step",
    "parse_duration",
    "resume_ai_step",
    "run_ai_step",
    "validate",
]
