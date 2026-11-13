# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""opennvr_app_sdk.workflows: the ai: step, from the YAML to its rows."""
from __future__ import annotations

import asyncio
import json

import pytest
import yaml

from opennvr_app_sdk.agent import (
    UNSURE,
    ChatResult,
    LLMUnreachableError,
    Tool,
    ToolCall,
)
from opennvr_app_sdk.workflows import (
    AIStep,
    AIStepError,
    check_ai_step,
    resume_ai_step,
    run_ai_step,
)

GATE = """
then:
  - ai:
      mode: look
      prompt: Who is at the gate?
      decision: [open_for_delivery, ask_resident, ignore]
      fields: { company: [dhl, ups, other, none] }
      rules:
        - { when: { company: [dhl, ups] }, then: open_for_delivery }
        - { when: { company: other }, then: ask_resident }
      sure: 0.75
      open_for_delivery: [ notify: { who: role:guard, message: "{company} at the gate" } ]
      ask_resident: [ ask_person: { who: role:resident, message: "{reason}" } ]
      ignore: []
      unsure: [ log: "{reason}" ]
"""

NIGHT = """
mode: investigate
goal: Decide whether the person in the yard is a threat.
instructions: Guards on patrol are not intruders.
tools: [describe_camera, recognize_faces]
decision: [ignore, watch, alert]
fields: { who: [staff, contractor, intruder, unknown] }
rules:
  - { when: { who: intruder }, then: alert }
  - { when: { who: [staff, contractor] }, then: ignore }
  - { then: watch }
limits: { tool_calls: 3 }
ignore: [ log: "{reason}" ]
watch: [ notify: { who: role:guard, message: "{reason}" } ]
alert: [ alert: { severity: medium, title: "Intruder on {camera}" } ]
unsure: [ ask_person: { who: role:guard, message: "Threat on {camera}?" } ]
"""

SKILLS = {"describe_camera": "read", "recognize_faces": "read", "open_gate": "actuate"}


def gate() -> dict:
    return yaml.safe_load(GATE)["then"][0]["ai"]


def night(**changes) -> dict:
    raw = yaml.safe_load(NIGHT)
    for k, v in changes.items():
        if v is None:
            raw.pop(k, None)
        else:
            raw[k] = v
    return raw


class Script:
    """A model that answers from a list: a dict as JSON, a list as tool
    calls, an exception raised."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = []

    async def chat(self, *, messages, tools=None, format=None, temperature=0.2, max_tokens=256):
        self.calls.append({"messages": messages, "tools": tools, "format": format})
        t = self.turns.pop(0)
        if isinstance(t, Exception):
            raise t
        if isinstance(t, list):
            calls = [ToolCall(name=n, arguments=a, id=f"c{i}") for i, (n, a) in enumerate(t)]
            return ChatResult(content="", tool_calls=calls,
                              message={"role": "assistant", "content": ""})
        content = t if isinstance(t, str) else json.dumps(t)
        return ChatResult(content=content, prompt_tokens=50, completion_tokens=10,
                          message={"role": "assistant", "content": content})


def tool(name, result="yard: a guard in a vest", mode="auto"):
    async def handler(args):
        return result
    return Tool(name=name, description=f"{name}", handler=handler, mode=mode,
                effect="ask_person" if mode == "ask" else "read",
                parameters={"type": "object", "properties": {"camera_id": {"type": "string"}}})


def problems(raw, **kw) -> str:
    return " | ".join(check_ai_step(raw, **kw))


# ── Look, end to end ───────────────────────────────────────────────────


def test_look_from_the_yaml_to_a_run_row_with_a_trace():
    step = AIStep.from_dict(gate())
    llm = Script({"reason": "a DHL van at the gate", "company": "dhl", "confidence": 0.9})
    result = asyncio.run(run_ai_step(step, llm, inputs={"plate": "AB12 CDE"},
                                     images=[b"jpeg1", b"jpeg2"], model_name="qwen3:1.7b"))

    assert result.outcome == "open_for_delivery"
    assert result.actions == [{"notify": {"who": "role:guard", "message": "{company} at the gate"}}]
    assert result.values() == {"company": "dhl", "outcome": "open_for_delivery",
                               "reason": "a DHL van at the gate", "confidence": 1.0}
    row = result.row()
    assert {k: row[k] for k in ("mode", "outcome", "fields", "rule", "unsure_reason", "model",
                                "state")} == {
        "mode": "look", "outcome": "open_for_delivery", "fields": {"company": "dhl"},
        "rule": 1, "unsure_reason": None, "model": "qwen3:1.7b", "state": None}
    assert row["usage"]["model_calls"] == 1 and row["usage"]["tokens_in"] == 50
    (call,) = result.trace_rows()
    assert (call["kind"], call["name"], call["status"]) == ("llm", "qwen3:1.7b", "ok")
    json.dumps([row, *result.trace_rows()])          # both store as they are

    # The plate went in as data; "picture" sends the first image only.
    user = llm.calls[0]["messages"][1]
    assert "AB12 CDE" in user["content"] and len(user["images"]) == 1
    assert "AB12 CDE" not in llm.calls[0]["messages"][0]["content"]


def test_look_input_says_which_images_go():
    for input_, n in (("frames", 2), ("text", 0)):
        step = AIStep.from_dict({**gate(), "input": input_})
        llm = Script({"reason": "r", "company": "dhl", "confidence": 1})
        asyncio.run(run_ai_step(step, llm, images=[b"a", b"b"]))
        assert len(llm.calls[0]["messages"][1].get("images", [])) == n


def test_look_without_rules_lets_the_model_choose():
    raw = {**gate(), "rules": []}
    llm = Script({"reason": "r", "company": "none", "decision": "ignore", "confidence": 1})
    result = asyncio.run(run_ai_step(AIStep.from_dict(raw), llm))
    assert (result.outcome, result.actions, result.row()["rule"]) == ("ignore", [], None)


def test_a_failure_is_the_unsure_path_never_a_raise():
    step = AIStep.from_dict(gate())
    result = asyncio.run(run_ai_step(step, Script(LLMUnreachableError("down"))))
    assert (result.outcome, result.actions) == (UNSURE, [{"log": "{reason}"}])
    assert result.row()["unsure_reason"] == "llm_down"
    assert result.trace_rows()[0]["status"] == "failed"
    # Facts no rule covers: unsure, with the facts kept for the person.
    result = asyncio.run(run_ai_step(step, Script({"reason": "r", "company": "none",
                                                   "confidence": 1})))
    assert (result.outcome, result.row()["unsure_reason"]) == (UNSURE, "no_rule")
    assert result.row()["fields"] == {"company": "none"}


def test_the_hardware_class_sets_the_limits_and_the_draws():
    step = AIStep.from_dict(night())
    assert (step.limits("gpu").tool_calls, step.limits("gpu").seconds) == (3, 45.0)
    assert (step.limits().tool_calls, step.limits().seconds) == (3, 120.0)
    answer = {"reason": "r", "company": "dhl", "confidence": 1}
    llm = Script(answer, answer, answer)
    result = asyncio.run(run_ai_step(AIStep.from_dict(gate()), llm, hardware="gpu"))
    assert len(llm.calls) == 3 and result.row()["confidence_source"] == "agreement of 3"


# ── Investigate ────────────────────────────────────────────────────────


def test_investigate_runs_the_tools_then_the_rules_decide():
    step = AIStep.from_dict(night(), skills=SKILLS)
    llm = Script([("describe_camera", {"camera_id": "yard"})], "done",
                 {"reason": "a guard in a vest", "who": "staff", "confidence": 0.9})
    result = asyncio.run(run_ai_step(step, llm, inputs={"camera": "yard"},
                                     tools=[tool("describe_camera"), tool("recognize_faces")]))
    assert (result.outcome, result.row()["rule"]) == ("ignore", 2)
    kinds = [(r["kind"], r["name"], r["status"]) for r in result.trace_rows()]
    assert kinds == [("llm", "llm", "ok"), ("tool", "describe_camera", "ok"),
                     ("llm", "llm", "ok"), ("llm", "llm", "ok")]
    assert result.row()["evidence"] == [result.trace_rows()[1]["id"]]


def test_a_listed_tool_the_site_cannot_supply_is_not_offered_and_says_so():
    step = AIStep.from_dict(night())
    llm = Script("done", {"reason": "r", "who": "unknown", "confidence": 1})
    result = asyncio.run(run_ai_step(step, llm, tools={"describe_camera": tool("describe_camera")}))
    assert [t["function"]["name"] for t in llm.calls[0]["tools"]] == ["describe_camera"]
    first = result.trace_rows()[0]
    assert (first["name"], first["status"], first["result"]) == (
        "recognize_faces", "refused", "not available on this site")
    assert result.outcome == "watch"


def test_an_ask_tool_suspends_into_the_row_and_resumes_from_it():
    step = AIStep.from_dict(night(), skills=SKILLS)
    tools = [tool("describe_camera"), tool("recognize_faces", mode="ask")]
    llm = Script([("recognize_faces", {"camera_id": "yard"})])
    waiting = asyncio.run(run_ai_step(step, llm, tools=tools, model_name="granite4:micro"))
    assert (waiting.outcome, waiting.actions) == (None, [])
    row = json.loads(json.dumps(waiting.row()))        # as the engine stores it
    assert row["state"]["pending"]["name"] == "recognize_faces"
    assert [r["status"] for r in waiting.trace_rows()][-1] == "suspended"

    llm = Script("done", {"reason": "the guard says it is them", "who": "staff",
                          "confidence": 1})
    done = asyncio.run(resume_ai_step(step, llm, row["state"], "Yes, that is Sam", tools=tools))
    assert (done.outcome, done.model, done.row()["state"]) == ("ignore", "granite4:micro", None)
    assert [r["status"] for r in done.trace_rows()][:3] == ["ok", "suspended", "ok"]


# ── what the validator refuses ─────────────────────────────────────────


def test_the_rfc_examples_pass():
    assert check_ai_step(gate()) == []
    assert check_ai_step(night(), skills=SKILLS) == []
    assert check_ai_step(night(), skills=["describe_camera", "recognize_faces"]) == []


@pytest.mark.parametrize("changes, says", [
    ({"mode": "act"}, "ai.mode: must be look or investigate"),
    ({"decision": []}, "ai.decision: must be a list of outcome names"),
    ({"decision": [True, False]}, "quote them"),
    ({"decision": ["ignore", "unsure"]}, "unsure is always an outcome"),
    ({"decision": ["ignore", "goal"]}, "'goal' is one of the step's own keys"),
    ({"watch": None}, "ai.watch: every outcome needs a path"),
    ({"unsure": None}, "ai.unsure: every outcome needs a path, unsure too"),
    ({"alert": {"alert": {}}}, "ai.alert: a path is a list of actions"),
    ({"colour": "red"}, "ai.colour: unknown key"),
    ({"prompt": "Who?"}, "ai.prompt: is not a key of an Investigate step"),
    ({"rules": None}, "ai.rules: an Investigate step decides by its rules"),
    ({"rules": [{"when": {"who": "ghost"}, "then": "alert"}]}, "is not a value who can take"),
    ({"rules": [{"when": {"mood": "calm"}, "then": "alert"}]}, "'mood' is not one of the fields"),
    ({"rules": [{"then": "arrest"}]}, "outcome 'arrest' is not an option"),
    ({"rules": [{"if": {"who": "staff"}}]}, "ai.rules: a rule is"),
    ({"fields": {"who": "staff"}}, "ai.fields: must map each field"),
    ({"sure": 2}, "ai.sure: must be a number from 0 to 1"),
    ({"limits": {"tool_calls": 2.5}}, "ai.limits.tool_calls: must be a positive whole number"),
    ({"limits": {"minutes": 2}}, "ai.limits.minutes: is not a limit"),
    ({"goal": None}, "ai.goal: an Investigate step needs a goal"),
    ({"goal": "Is {plate} on the list?"}, "{plate} is not filled in here"),
    ({"tools": []}, "ai.tools: an Investigate step lists the skills"),
    ({"tools": ["describe_camera"] * 2}, "listed twice"),
    ({"tools": ["describe_camera", "a", "b", "c", "d", "e"]}, "at most 5"),
    ({"tools": ["describe_camera", "fly_drone"]}, "'fly_drone' is not a skill on this site"),
    ({"tools": ["describe_camera", "open_gate"]}, "'open_gate' moves things (actuate)"),
])
def test_a_step_that_cannot_run_is_refused_with_the_reason(changes, says):
    assert says in problems(night(**changes), skills=SKILLS)


def test_look_has_its_own_keys():
    assert "ai.tools: is not a key of a Look step" in problems({**gate(), "tools": ["x"]})
    assert "ai.input: must be one of" in problems({**gate(), "input": "video"})
    assert "ai.limits.tool_calls: is not a limit of a Look step" in problems(
        {**gate(), "limits": {"tool_calls": 2}})
    assert "ai.prompt: a Look step needs a prompt" in problems(
        {k: v for k, v in gate().items() if k != "prompt"})


def test_every_problem_is_reported_at_once_and_from_dict_refuses():
    raw = night(sure=5, watch=None, tools=["open_gate"])
    with pytest.raises(AIStepError) as err:
        AIStep.from_dict(raw, skills=SKILLS, where="then[0].ai")
    assert len(err.value.problems) == 3
    assert all(p.startswith("then[0].ai.") for p in err.value.problems)
    assert check_ai_step("look") == ["ai: an AI step is a mapping of keys, got str"]
