# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""The AI step library: what a workflow's AI step and an agent run on.

Phase 2 of the Agentic Workflows RFC (open-nvr discussion #620). This
first piece is the model clients — one interface over a local Ollama
and any OpenAI-compatible endpoint, with schema-constrained output::

    from opennvr_app_sdk.agent import OllamaChat

    async with OllamaChat("http://ollama:11434", model="qwen3:1.7b", think=False) as llm:
        result = await llm.chat(
            messages=[{"role": "user", "content": "Is this a delivery? ..."}],
            format={"type": "object",
                    "properties": {"decision": {"enum": ["delivery", "cab", "unknown"]}},
                    "required": ["decision"]},
        )
        decision = result.json()["decision"]

A Look step (RFC #620) is one call that answers with a typed decision::

    from opennvr_app_sdk.agent import DecisionSpec, Limits, run_look

    spec = DecisionSpec(options=("delivery", "cab", "visitor"))
    d = await run_look(llm, spec, prompt="Who is at the gate?",
                       inputs={"plate": plate}, images=[jpeg], limits=Limits.cpu())
    d.decision        # "delivery", "cab", "visitor" or "unsure"; never raises

The read tools' model-facing definitions (names, descriptions, argument
schemas) live here too, in :mod:`.tool_definitions`, so the camera agent
and core's workflow engine advertise the same text; each implements the
handlers itself. An Investigate step calls tools first, and may suspend
while a person answers an ``ask`` tool::

    out = await run_investigate(llm, spec, goal="...", tools=[Tool(...)])
    if isinstance(out, Suspended):      # persist out.state; later:
        out = await resume(llm, spec, out.state, answer, tools=[Tool(...)])

See the design note on #620.
"""
from __future__ import annotations

from .decision import UNSURE, UNSURE_REASONS, Decision, DecisionSpec, Rule, Usage
from .investigate import (
    Suspended,
    decide_message,
    investigate_messages,
    resume,
    run_investigate,
)
from .limits import Limits
from .llm import (
    ChatResult,
    LLMClient,
    LLMError,
    LLMHTTPError,
    LLMOutputError,
    LLMTimeoutError,
    LLMUnreachableError,
    OllamaChat,
    OpenAIChat,
    ToolCall,
)
from .look import look_messages, run_look
from .tool_definitions import READ_TOOL_NAMES, build_tool_definitions
from .tools import Tool, ToolResult, check_arguments, effective_mode
from .trace import TraceRow

__all__ = [
    "READ_TOOL_NAMES",
    "UNSURE",
    "UNSURE_REASONS",
    "ChatResult",
    "Decision",
    "DecisionSpec",
    "LLMClient",
    "LLMError",
    "LLMHTTPError",
    "LLMOutputError",
    "LLMTimeoutError",
    "LLMUnreachableError",
    "Limits",
    "OllamaChat",
    "OpenAIChat",
    "Rule",
    "Suspended",
    "Tool",
    "ToolCall",
    "ToolResult",
    "TraceRow",
    "Usage",
    "build_tool_definitions",
    "check_arguments",
    "decide_message",
    "effective_mode",
    "investigate_messages",
    "look_messages",
    "resume",
    "run_investigate",
    "run_look",
]
