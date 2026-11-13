# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""Rule tables: the model reports facts, the step's rules decide."""
from __future__ import annotations

import asyncio
import json

import pytest

from opennvr_app_sdk.agent import (
    UNSURE,
    ChatResult,
    DecisionSpec,
    Rule,
    Tool,
    ToolCall,
    decide_message,
    look_messages,
    run_investigate,
    run_look,
)

NIGHT = DecisionSpec(
    options=("ignore", "watch", "alert"),
    fields={"who": ("staff", "visitor", "intruder", "animal", "unknown")},
    rules=(Rule(when={"who": "intruder"}, then="alert"),
           Rule(when={"who": ["staff", "animal"]}, then="ignore"),
           Rule(when={"who": ["visitor", "unknown"]}, then="watch")))


class Script:
    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = []

    async def chat(self, *, messages, tools=None, format=None, temperature=0.2, max_tokens=256):
        self.calls.append({"messages": messages, "format": format, "tools": tools})
        t = self.turns.pop(0)
        if isinstance(t, list):
            calls = [ToolCall(name=n, arguments=a, id=f"c{i}") for i, (n, a) in enumerate(t)]
            return ChatResult(content="", tool_calls=calls,
                              message={"role": "assistant", "content": ""})
        content = json.dumps(t)
        return ChatResult(content=content, message={"role": "assistant", "content": content})


def facts(who, **kw):
    return {"reason": "seen on camera", "who": who, "confidence": 0.8, **kw}


# ── the table ──────────────────────────────────────────────────────────


def test_first_match_wins_and_no_match_is_none():
    spec = DecisionSpec(options=("a", "b"), fields={"x": ("1", "2", "3")},
                        rules=(Rule(when={"x": "1"}, then="a"),
                               Rule(when={"x": ["1", "2"]}, then="b")))
    assert spec.outcome({"x": "1"}) == "a"
    assert spec.outcome({"x": "2"}) == "b"
    assert spec.outcome({"x": "3"}) is None


def test_an_empty_when_is_the_default():
    spec = DecisionSpec(options=("a", "b"), fields={"x": ("1", "2")},
                        rules=(Rule(when={"x": "1"}, then="a"), Rule(then="b")))
    assert spec.outcome({"x": "2"}) == "b"


def test_rules_from_yaml_shaped_dicts():
    spec = DecisionSpec(options=("alert",), fields={"who": ("intruder", "staff")},
                        rules=({"when": {"who": "intruder"}, "then": "alert"},))
    assert spec.rules == (Rule(when={"who": ("intruder",)}, then="alert"),)
    with pytest.raises(ValueError, match="a rule is"):
        DecisionSpec(options=("alert",), fields={"who": ("x",)},
                     rules=({"if": {"who": "x"}, "then": "alert"},))


@pytest.mark.parametrize("rule,msg", [
    (Rule(when={"who": "intruder"}, then="call_police"), "not an option"),
    (Rule(when={"mood": "angry"}, then="alert"), "not one of the fields"),
    (Rule(when={"who": "ghost"}, then="alert"), "not a value who can take"),
])
def test_a_table_that_cannot_work_is_refused_when_it_is_written(rule, msg):
    with pytest.raises(ValueError, match=msg):
        DecisionSpec(options=("ignore", "alert"), fields={"who": ("staff", "intruder")},
                     rules=(rule,))


def test_with_rules_the_model_is_never_asked_for_the_decision():
    schema = NIGHT.schema()
    assert "decision" not in schema["properties"]
    assert list(schema["properties"]) == ["reason", "who", "confidence"]
    assert "decision" not in decide_message(NIGHT)["content"]
    assert "decision" not in look_messages(NIGHT, prompt="Who?")[1]["content"]


def test_without_rules_the_measured_wording_is_unchanged():
    """#674: this text is measured; rules must not move it."""
    plain = DecisionSpec(options=("ignore", "alert"), fields={"who": ("staff", "intruder")})
    assert decide_message(plain)["content"] == (
        "Now decide. decision: one of ['ignore', 'alert', 'unsure']. "
        "who: one of ['staff', 'intruder']. reason: one short sentence, citing what "
        "the tools showed. confidence: your confidence from 0 to 1. Answer with JSON only.")
    assert "decision" in plain.schema()["properties"]


# ── the step ───────────────────────────────────────────────────────────


def test_the_rules_decide_even_when_the_model_would_have_said_otherwise():
    # A model that writes a "decision" anyway is ignored: the facts decide.
    d = asyncio.run(run_look(Script({**facts("intruder"), "decision": "ignore"}), NIGHT,
                             prompt="Who is in the yard?"))
    assert (d.decision, d.fields) == ("alert", {"who": "intruder"})


def test_facts_no_rule_matches_are_unsure_no_rule_and_keep_the_facts():
    spec = DecisionSpec(options=("alert",), fields={"who": ("intruder", "staff")},
                        rules=(Rule(when={"who": "intruder"}, then="alert"),))
    d = asyncio.run(run_look(Script(facts("staff")), spec, prompt="Who?"))
    assert (d.decision, d.unsure_reason, d.fields) == (UNSURE, "no_rule", {"who": "staff"})


def test_a_missing_fact_is_invalid_output():
    d = asyncio.run(run_look(Script({"reason": "?", "confidence": 1}), NIGHT, prompt="Who?"))
    assert (d.decision, d.unsure_reason) == (UNSURE, "invalid_output")


def test_investigate_with_rules():
    async def look(args):
        return "yard: a guard in a vest"
    tool = Tool(name="describe_camera", description="look", handler=look,
                parameters={"type": "object", "properties": {"camera_id": {"type": "string"}}})
    # Look, stop calling tools ("done"), then report the facts.
    llm = Script([("describe_camera", {"camera_id": "yard"})], "done", facts("staff"))
    d = asyncio.run(run_investigate(llm, NIGHT, goal="Decide.", tools=[tool]))
    assert (d.decision, d.fields) == ("ignore", {"who": "staff"})
    assert "decision" not in llm.calls[-1]["format"]["properties"]


def test_agreement_compares_outcomes_not_wording():
    # staff and animal are different facts with the same outcome: they agree.
    d = asyncio.run(run_look(Script(facts("staff"), facts("animal"), facts("intruder")),
                             NIGHT, prompt="Who?", samples=3))
    assert d.confidence == pytest.approx(2 / 3)


def test_the_decision_names_the_rule_that_fired():
    assert NIGHT.rule_for({"who": "animal"}) == 2
    assert NIGHT.rule_for({"who": "nobody"}) is None
    d = asyncio.run(run_look(Script(facts("animal")), NIGHT, prompt="Who?"))
    assert (d.decision, d.rule) == ("ignore", 2)


def test_the_rule_is_kept_when_the_floor_makes_it_unsure():
    llm = Script(facts("intruder"), facts("staff"), facts("staff"))
    d = asyncio.run(run_look(llm, NIGHT, prompt="Who?", samples=3))
    assert (d.decision, d.unsure_reason, d.proposed, d.rule) == (UNSURE, "below_floor", "alert", 1)


def test_without_rules_or_without_a_match_there_is_no_rule():
    plain = DecisionSpec(options=("ignore", "alert"))
    d = asyncio.run(run_look(Script({"reason": "r", "decision": "alert", "confidence": 1}),
                             plain, prompt="Who?"))
    assert d.rule is None
    spec = DecisionSpec(options=("alert",), fields={"who": ("intruder", "staff")},
                        rules=(Rule(when={"who": "intruder"}, then="alert"),))
    assert asyncio.run(run_look(Script(facts("staff")), spec, prompt="Who?")).rule is None
