# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""One row per model call and per tool call, so a run explains itself.

Shaped for core's ``workflow_tool_calls`` table (RFC #620): the engine
writes :meth:`TraceRow.as_row` as it is. A tool call is recorded with its
arguments as the model asked for them, before any handler normalised
them, so a wrong argument is visible as the model's.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

_RESULT_CHARS = 2000

Status = Literal["ok", "failed", "refused", "suspended"]


@dataclass
class TraceRow:
    kind: Literal["llm", "tool"]
    name: str                         # the model, or the tool
    status: Status
    started_at: float                 # epoch seconds
    ms: int
    args_asked: dict[str, Any] | str | None = None
    result: str = ""                  # capped; the error message when it failed
    mode: str | None = None           # a tool's effective mode
    tokens_in: int | None = None
    tokens_out: int | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if len(self.result) > _RESULT_CHARS:
            self.result = self.result[:_RESULT_CHARS] + "…"

    def as_row(self) -> dict[str, Any]:
        return asdict(self)
