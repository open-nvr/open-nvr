# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""What Look and Investigate share: one step's clock, its trace and
usage, a traced model call, and the decision call with agreement draws."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from .decision import UNSURE, Decision, DecisionSpec, Usage
from .limits import Limits
from .llm import ChatResult, LLMClient, LLMError, LLMOutputError
from .trace import TraceRow

#: Agreement draws after the first are sampled hotter, so they can differ.
DRAW_TEMPERATURE = 0.8


class StepRun:
    """One AI step in progress. ``elapsed`` carries the seconds a step had
    already spent before it was suspended, so a resumed step keeps its
    budget and the time spent waiting for a person is not counted."""

    def __init__(self, client: LLMClient, *, limits: Limits, model_name: str,
                 clock: Callable[[], float], elapsed: float = 0.0,
                 usage: Usage | None = None, trace: list[TraceRow] | None = None) -> None:
        self.client = client
        self.limits = limits
        self.model_name = model_name
        self.clock = clock
        self.t0 = clock() - elapsed
        self.usage = usage or Usage()
        self.trace = trace if trace is not None else []

    def elapsed(self) -> float:
        return self.clock() - self.t0

    def remaining(self) -> float:
        return self.limits.seconds - self.elapsed()

    async def chat(self, **kwargs: Any) -> tuple[ChatResult, TraceRow]:
        """A model call inside the step's remaining time, traced. Raises
        ``LLMError`` (traced as failed) or ``TimeoutError``."""
        started, c0 = time.time(), self.clock()
        try:
            res = await asyncio.wait_for(self.client.chat(**kwargs),
                                         timeout=max(self.remaining(), 0.001))
        except LLMError as err:
            self.usage.model_calls += 1
            self.trace.append(TraceRow(kind="llm", name=self.model_name, status="failed",
                                       started_at=started, ms=_ms(self.clock() - c0),
                                       result=str(err)))
            raise
        self.usage.model_calls += 1
        self.usage.tokens_in += res.prompt_tokens or 0
        self.usage.tokens_out += res.completion_tokens or 0
        row = TraceRow(kind="llm", name=self.model_name, status="ok", started_at=started,
                       ms=_ms(self.clock() - c0), result=res.content,
                       tokens_in=res.prompt_tokens, tokens_out=res.completion_tokens)
        self.trace.append(row)
        return res, row

    def ran_out(self) -> None:
        self.trace.append(TraceRow(kind="llm", name=self.model_name, status="failed",
                                   started_at=time.time(), ms=0,
                                   result="the step ran out of time"))

    async def decide(self, spec: DecisionSpec, messages: list[dict[str, Any]], *,
                     samples: int = 1, max_tokens: int = 256) -> Decision:
        """The decision call: no tools, the output constrained to ``spec``.
        Raises ``LLMError`` / ``TimeoutError`` only from the first draw;
        later draws that fail count as disagreeing."""
        schema = spec.schema()

        async def draw(temperature: float) -> tuple[str, dict[str, str], str, float | None] | None:
            res, row = await self.chat(messages=messages, format=schema,
                                       temperature=temperature, max_tokens=max_tokens)
            try:
                answer = spec.read(res.json())
            except LLMOutputError:
                answer = None
            if answer is None:
                row.status = "failed"
            return answer

        first = await draw(0.2)
        if first is None:
            return Decision(UNSURE, unsure_reason="invalid_output")
        decision, fields, reason, self_reported = first
        if spec.rules and not decision:      # the facts matched no rule
            return Decision(UNSURE, fields, reason, self_reported=self_reported,
                            unsure_reason="no_rule")
        votes = [decision]
        for _ in range(max(0, samples - 1)):
            if self.remaining() <= 0:
                break
            try:
                again = await draw(DRAW_TEMPERATURE)
            except (LLMError, TimeoutError):
                again = None
            votes.append(again[0] if again else "")
        confidence = votes.count(decision) / len(votes)
        out = Decision(decision, fields, reason, confidence=confidence,
                       confidence_source=f"agreement of {len(votes)}",
                       self_reported=self_reported,
                       rule=spec.rule_for(fields) if spec.rules else None)
        if decision != UNSURE and confidence < spec.floor:
            out.proposed, out.decision, out.fields = decision, UNSURE, {}
            out.unsure_reason = "below_floor"
        return out

    def finish(self, decision: Decision) -> Decision:
        self.usage.seconds = round(self.elapsed(), 3)
        decision.trace, decision.usage = self.trace, self.usage
        return decision

    def failed(self, err: BaseException) -> Decision:
        """The never-raise rule: what a failure becomes."""
        if isinstance(err, TimeoutError):
            self.ran_out()
            return self.finish(Decision(UNSURE, unsure_reason="over_budget"))
        if isinstance(err, LLMError):
            return self.finish(Decision(UNSURE, unsure_reason="llm_down"))
        self.trace.append(TraceRow(kind="llm", name=self.model_name, status="failed",
                                   started_at=time.time(), ms=0, result=repr(err)))
        return self.finish(Decision(UNSURE, unsure_reason="error"))


def _ms(seconds: float) -> int:
    return int(seconds * 1000)
