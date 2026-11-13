# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""The ``ai:`` step of a workflow file: what it may say, and running it.

RFC #620's AI step, as drafted there (section 5 of the RFC, plus rule
tables, #679). In the workflow file::

    - ai:
        mode: investigate
        goal: Decide whether the person in the yard is a threat.
        instructions: Guards on patrol are not intruders.
        tools: [describe_camera, recognize_faces, recent_events]
        decision: [ignore, watch, alert]
        fields: { who: [staff, contractor, intruder, unknown] }
        rules:
          - { when: { who: intruder }, then: alert }
          - { when: { who: [staff, contractor] }, then: ignore }
          - { then: watch }
        sure: 0.75
        limits: { tool_calls: 6, seconds: 45 }
        ignore: [ log: "{reason}" ]
        watch:  [ notify: { who: role:guard, message: "{reason}" } ]
        alert:  [ alert: { severity: medium, title: "Intruder on {camera}" } ]
        unsure: [ ask_person: { who: role:guard, message: "Threat on {camera}?" } ]

A Look step has ``prompt`` and ``input`` (``picture``, ``frames`` or
``text``) instead of ``goal``, ``instructions`` and ``tools``.

:func:`check_ai_step` lists what is wrong with one, and
:meth:`AIStep.from_dict` refuses it with that list; the engine, the
builder and an outside tool all call them, so they refuse the same steps
for the same reasons. :func:`run_ai_step` runs one over the SDK's
:func:`~opennvr_app_sdk.agent.run_look` or
:func:`~opennvr_app_sdk.agent.run_investigate` and returns a
:class:`StepResult`: the outcome, the path to take, and the rows for
``workflow_run_steps`` and ``workflow_tool_calls``. Like the SDK steps
it never raises; every failure is ``unsure`` with the reason.

Defaults chosen on #620 while the open questions there stand:

* ``rules`` are required in Investigate, optional in Look;
* the hardware class (``"gpu"`` or ``"cpu"``) is the engine's to pass,
  CPU when it does not; it sets ``limits`` the step leaves out and the
  number of agreement draws (3 on GPU, 1 on CPU, where the floor then
  cannot fire);
* a severity cap is the ``alert`` action's, not the step's;
* ``fields`` is one key, the values later steps read and, with rules,
  the facts the rules read.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..agent import (
    UNSURE,
    Decision,
    DecisionSpec,
    Limits,
    LLMClient,
    Rule,
    Suspended,
    Tool,
    ToolResult,
    TraceRow,
    resume,
    run_investigate,
    run_look,
)

Mode = Literal["look", "investigate"]
LookInput = Literal["picture", "frames", "text"]

#: Most tools an Investigate step may list (RFC #620).
MAX_TOOLS = 5
#: Effects a skill may have and still be an AI step's tool.
TOOL_EFFECTS = ("read", "ask_person")

_COMMON = frozenset({"mode", "decision", "fields", "rules", "sure", "limits"})
_KEYS: dict[str, frozenset[str]] = {
    "look": _COMMON | {"prompt", "input"},
    "investigate": _COMMON | {"goal", "instructions", "tools"},
}
_ALL_KEYS = _KEYS["look"] | _KEYS["investigate"]
_INPUTS = ("picture", "frames", "text")
_A = {"look": "a Look", "investigate": "an Investigate"}
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_.]*\}")


class AIStepError(ValueError):
    """An ``ai:`` step that cannot run; ``problems`` says every reason."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class AIStep:
    """One checked ``ai:`` step. Build it with :meth:`from_dict`.

    ``paths`` maps each outcome, ``unsure`` included, to its list of
    actions, kept as written: the actions are the engine's."""

    mode: Mode
    spec: DecisionSpec
    paths: dict[str, list[Any]]
    prompt: str = ""
    input: LookInput = "picture"
    goal: str = ""
    instructions: str = ""
    tools: tuple[str, ...] = ()
    tool_calls: int | None = None
    seconds: float | None = None

    @classmethod
    def from_dict(cls, raw: Any, *, skills: Mapping[str, str] | Iterable[str] | None = None,
                  where: str = "ai") -> AIStep:
        """The step under an ``ai:`` key, checked; :class:`AIStepError`
        when :func:`check_ai_step` finds anything."""
        problems = check_ai_step(raw, skills=skills, where=where)
        if problems:
            raise AIStepError(problems)
        limits = raw.get("limits") or {}
        return cls(
            mode=raw["mode"], spec=_spec(raw), paths={o: list(raw[o]) for o in _outcomes(raw)},
            prompt=str(raw.get("prompt") or "").strip(), input=raw.get("input", "picture"),
            goal=str(raw.get("goal") or "").strip(),
            instructions=str(raw.get("instructions") or "").strip(),
            tools=tuple(raw.get("tools") or ()),
            tool_calls=limits.get("tool_calls"), seconds=limits.get("seconds"))

    def limits(self, hardware: str = "cpu") -> Limits:
        """The step's own limits, the hardware class's preset for any it
        leaves out."""
        preset = Limits.for_hardware(hardware)
        return Limits(tool_calls=preset.tool_calls if self.tool_calls is None else self.tool_calls,
                      seconds=preset.seconds if self.seconds is None else float(self.seconds))


def check_ai_step(raw: Any, *, skills: Mapping[str, str] | Iterable[str] | None = None,
                  where: str = "ai") -> list[str]:
    """Everything wrong with one ``ai:`` step, each as ``"<where>.<key>:
    why"``; empty when it can run.

    ``skills`` is the site's registry, a skill's name to its effect
    (``read``, ``ask_person``, or anything else for a skill that moves
    things), or just the names, all read. Tools resolve by skill name at
    import, so a missing one is named. Without ``skills`` the names are
    not checked here; the run then refuses any the engine cannot supply.
    """
    if not isinstance(raw, Mapping):
        return [f"{where}: an AI step is a mapping of keys, got {type(raw).__name__}"]
    out: list[str] = []

    def bad(key: str, why: str) -> None:
        out.append(f"{where}.{key}: {why}" if key else f"{where}: {why}")

    mode = raw.get("mode")
    if mode not in _KEYS:
        bad("mode", f"must be look or investigate, got {mode!r}")
        return out

    decision = raw.get("decision")
    if not (isinstance(decision, list) and decision
            and all(isinstance(o, str) and o.strip() for o in decision)):
        bad("decision", "must be a list of outcome names" + _bool_hint(decision))
        return out
    decision_ok = UNSURE not in decision and len(set(decision)) == len(decision)
    if UNSURE in decision:
        bad("decision", "unsure is always an outcome; do not list it")
    if len(set(decision)) != len(decision):
        bad("decision", "outcomes must be distinct")
    for o in decision:
        if o in _ALL_KEYS:
            bad("decision", f"{o!r} is one of the step's own keys and cannot name an outcome")
    outcomes = [*decision, UNSURE]

    for key in raw:
        if key in _KEYS[mode] or key in outcomes:
            continue
        if key in _ALL_KEYS:
            bad(key, f"is not a key of {_A[mode]} step")
        else:
            bad(key, f"unknown key; an outcome's path is keyed by one of {outcomes}")

    for o in outcomes:
        if o not in raw:
            bad(o, "every outcome needs a path, unsure too (an empty list does nothing)")
        elif not isinstance(raw[o], list):
            bad(o, "a path is a list of actions")

    fields = raw.get("fields", {})
    if not isinstance(fields, Mapping) or not all(
            isinstance(v, list) and v and all(isinstance(x, str) for x in v)
            for v in fields.values()):
        bad("fields", "must map each field to a list of the values it may take"
                      + _bool_hint(list(fields.values()) if isinstance(fields, Mapping) else []))
        fields = None
    rules = raw.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(r, Mapping) for r in rules):
        bad("rules", "must be a list of {when: {field: value}, then: outcome}")
        rules = None
    elif mode == "investigate" and not rules:
        bad("rules", "an Investigate step decides by its rules; the model reports the fields")
    sure = raw.get("sure", 0.75)
    if isinstance(sure, bool) or not isinstance(sure, (int, float)) or not 0 <= sure <= 1:
        bad("sure", f"must be a number from 0 to 1, got {sure!r}")
    elif fields is not None and rules is not None and decision_ok:
        try:
            _spec(raw)
        except (ValueError, TypeError) as err:  # the SDK's own refusal, so both say the same
            bad("rules" if rules and "rule" in str(err) else "fields", str(err))

    limits = raw.get("limits", {})
    allowed = {"seconds"} if mode == "look" else {"tool_calls", "seconds"}
    if not isinstance(limits, Mapping):
        bad("limits", f"must be a mapping of {sorted(allowed)}")
    else:
        for k, v in limits.items():
            if k not in allowed:
                bad(f"limits.{k}", f"is not a limit of {_A[mode]} step")
            elif isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0 or (
                    k == "tool_calls" and not isinstance(v, int)):
                bad(f"limits.{k}", f"must be a positive {'whole ' if k == 'tool_calls' else ''}"
                                   f"number, got {v!r}")

    text_keys = ("prompt",) if mode == "look" else ("goal", "instructions")
    for k in text_keys:
        v = raw.get(k)
        if v is None:
            if k != "instructions":
                bad(k, f"{_A[mode]} step needs a {k}")
            continue
        if not isinstance(v, str) or (k != "instructions" and not v.strip()):
            bad(k, "must be text")
        elif _PLACEHOLDER.search(v):
            bad(k, f"{_PLACEHOLDER.search(v).group()} is not filled in here: the event's "
                   "values reach the model as data, never in its instructions (RFC #620, §7)")

    if mode == "look":
        if raw.get("input", "picture") not in _INPUTS:
            bad("input", f"must be one of {list(_INPUTS)}, got {raw.get('input')!r}")
    else:
        out.extend(_check_tools(raw.get("tools"), skills, where))
    return out


def _check_tools(tools: Any, skills: Mapping[str, str] | Iterable[str] | None,
                 where: str) -> list[str]:
    if not (isinstance(tools, list) and tools and all(isinstance(t, str) for t in tools)):
        return [f"{where}.tools: an Investigate step lists the skills it may call, by name"]
    out = []
    if len(tools) > MAX_TOOLS:
        out.append(f"{where}.tools: at most {MAX_TOOLS}, got {len(tools)}")
    if len(set(tools)) != len(tools):
        out.append(f"{where}.tools: listed twice: {sorted({t for t in tools if tools.count(t) > 1})}")
    if skills is not None:
        effects = dict(skills) if isinstance(skills, Mapping) else {s: "read" for s in skills}
        for t in tools:
            if t not in effects:
                out.append(f"{where}.tools: {t!r} is not a skill on this site")
            elif effects[t] not in TOOL_EFFECTS:
                out.append(f"{where}.tools: {t!r} moves things ({effects[t]}); an AI step's "
                           "tools only read or ask a person")
    return out


def _bool_hint(values: Any) -> str:
    flat = [x for v in values for x in (v if isinstance(v, list) else [v])] \
        if isinstance(values, list) else []
    if any(isinstance(x, bool) for x in flat):
        return "; YAML reads yes/no/on/off unquoted as true/false, so quote them"
    return ""


def _outcomes(raw: Mapping[str, Any]) -> list[str]:
    return [*raw["decision"], UNSURE]


def _spec(raw: Mapping[str, Any]) -> DecisionSpec:
    return DecisionSpec(options=tuple(raw["decision"]),
                        fields={k: tuple(v) for k, v in (raw.get("fields") or {}).items()},
                        floor=float(raw.get("sure", 0.75)),
                        rules=tuple(Rule.from_dict(r) for r in raw.get("rules") or ()))


# ── running it ─────────────────────────────────────────────────────────


@dataclass
class StepResult:
    """One run of an AI step.

    ``outcome`` is one of the step's outcomes, or None while
    ``suspended`` (an ``ask`` tool waits for a person: persist
    :meth:`row`, which carries the state, and call
    :func:`resume_ai_step` with the answer). ``actions`` is the path to
    take. :meth:`row` is the ``workflow_run_steps`` row and
    :meth:`trace_rows` the ``workflow_tool_calls`` rows."""

    mode: Mode
    model: str
    outcome: str | None
    actions: list[Any]
    decision: Decision | None = None
    suspended: Suspended | None = None
    missing_tools: list[TraceRow] = field(default_factory=list)

    def values(self) -> dict[str, Any]:
        """What later steps' placeholders may read: ``{outcome}``,
        ``{reason}``, ``{confidence}`` and each field by name."""
        d = self.decision
        if d is None:
            return {}
        return {**d.fields, "outcome": d.decision, "reason": d.reason,
                "confidence": d.confidence}

    def row(self) -> dict[str, Any]:
        d = self.decision or Decision(UNSURE)
        usage = asdict(d.usage)
        state = None
        if self.suspended is not None:
            state = self.suspended.state
            usage = dict(state.get("usage") or usage)
        return {
            "mode": self.mode, "outcome": self.outcome,
            "fields": dict(d.fields) if self.decision else {},
            "reason": d.reason, "confidence": d.confidence,
            "confidence_source": d.confidence_source, "self_reported": d.self_reported,
            "unsure_reason": d.unsure_reason if self.decision else None,
            "proposed": d.proposed, "rule": d.rule, "evidence": list(d.evidence),
            "model": self.model, "usage": usage, "state": state,
        }

    def trace_rows(self) -> list[dict[str, Any]]:
        """The whole trace so far, suspended or not: write it over the
        step's earlier rows, not after them."""
        rows = [r.as_row() for r in self.missing_tools]
        if self.suspended is not None:
            return rows + [dict(r) for r in self.suspended.state.get("trace") or []]
        return rows + [r.as_row() for r in (self.decision.trace if self.decision else [])]


async def run_ai_step(step: AIStep, client: LLMClient, *,
                      inputs: Mapping[str, Any] | None = None,
                      images: Sequence[bytes | str] = (),
                      tools: Mapping[str, Tool] | Sequence[Tool] = (),
                      policy: Mapping[str, str] | None = None, site_facts: str = "",
                      hardware: str = "cpu", model_name: str = "llm",
                      clock: Callable[[], float] = time.monotonic) -> StepResult:
    """Run one AI step on the event's ``inputs``.

    Look sends ``images`` as its ``input`` says: the first for
    ``picture``, all for ``frames``, none for ``text``. Investigate gets
    ``tools``, the site's implementations by skill name; one the step
    lists that is not among them is not offered, and says so in the
    trace. ``policy`` is the site's mode per tool and only tightens."""
    limits = step.limits(hardware)
    samples = 3 if hardware.strip().lower() == "gpu" else 1
    if step.mode == "look":
        shown = {"picture": list(images[:1]), "frames": list(images), "text": []}[step.input]
        d = await run_look(client, step.spec, prompt=step.prompt, inputs=dict(inputs or {}),
                           images=shown, limits=limits, samples=samples,
                           model_name=model_name, clock=clock)
        return _result(step, model_name, d)
    offered, missing = _resolve(step, tools)
    out = await run_investigate(client, step.spec, goal=step.goal, tools=offered,
                                instructions=step.instructions, site_facts=site_facts,
                                inputs=dict(inputs or {}), policy=dict(policy or {}),
                                limits=limits, samples=samples, model_name=model_name,
                                clock=clock)
    return _result(step, model_name, out, missing)


async def resume_ai_step(step: AIStep, client: LLMClient, state: Mapping[str, Any],
                         answer: ToolResult | str, *,
                         tools: Mapping[str, Tool] | Sequence[Tool] = (),
                         clock: Callable[[], float] = time.monotonic) -> StepResult:
    """Carry on a suspended step with the person's answer. ``state`` is
    the stored row's ``state``."""
    offered, missing = _resolve(step, tools)
    out = await resume(client, step.spec, dict(state), answer, tools=offered, clock=clock)
    return _result(step, str(state.get("model_name") or "llm"), out, missing)


def _resolve(step: AIStep, tools: Mapping[str, Tool] | Sequence[Tool]
             ) -> tuple[list[Tool], list[TraceRow]]:
    by_name = dict(tools) if isinstance(tools, Mapping) else {t.name: t for t in tools}
    offered = [by_name[n] for n in step.tools if n in by_name]
    missing = [TraceRow(kind="tool", name=n, status="refused", started_at=time.time(), ms=0,
                        result="not available on this site", mode="unavailable")
               for n in step.tools if n not in by_name]
    return offered, missing


def _result(step: AIStep, model: str, out: Decision | Suspended,
            missing: list[TraceRow] | None = None) -> StepResult:
    if isinstance(out, Suspended):
        return StepResult(step.mode, model, None, [], suspended=out, missing_tools=missing or [])
    return StepResult(step.mode, model, out.decision, list(step.paths.get(out.decision, [])),
                      decision=out, missing_tools=missing or [])
