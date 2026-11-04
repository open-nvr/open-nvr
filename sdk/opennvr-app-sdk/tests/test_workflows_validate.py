# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""opennvr_app_sdk.workflows.validate: the workflow file, workflow: v1."""
from __future__ import annotations

import copy

import pytest
import yaml

from opennvr_app_sdk.workflows import (
    AppInfo,
    Problem,
    Registry,
    parse_duration,
    validate,
)

#: RFC #620, section 5, with the reply keys quoted and a rule table.
NIGHT = """
workflow: v1
name: Night intrusion triage
limits: { cooldown: 2m, one_at_a_time: track, per_hour: 20, ai_per_hour: 40 }
when:
  alert: { app: intrusion-detection, cameras: [yard-east, yard-west] }
if:
  - site_mode: [armed_home, armed_away]
  - time: { from: "22:00", to: "06:00" }
then:
  - ai:
      mode: investigate
      goal: Decide whether the person in the yard is a threat.
      tools: [describe_camera, recent_events, site_lookup]
      decision: [ignore, watch, alert]
      fields: { who: [staff, contractor, unknown] }
      rules:
        - { when: { who: [staff, contractor] }, then: ignore }
        - { then: alert }
      ignore: [ log: "{reason}" ]
      watch:  [ notify: { who: role:guard, message: "{reason}" } ]
      alert:  [ alert: { severity: medium, title: "Intruder on {camera}" } ]
      unsure:
        - ask_person: { who: role:guard, message: "Threat on {camera}?", replies: ["Yes", "No"],
                        timeout: 60s }
          "Yes": [ alert: { severity: high, title: "Intruder on {camera}" } ]
always: [ log: "night triage ran" ]
"""

#: The RFC's Gate concierge: the gate opens only on a guard's reply.
GATE = """
workflow: v1
name: Gate concierge
when:
  event: { subject: plate.read.v1, cameras: [gate] }
then:
  - ai:
      mode: look
      prompt: Who is at the gate?
      decision: [delivery, cab, visitor]
      delivery:
        - ask_person: { who: role:guard, message: "Delivery at the gate. Let in?",
                        replies: ["Let in", "Refuse"], default: "Refuse" }
          "Let in": [ app: { id: gate-controller, action: open } ]
          no_answer: [ log: "nobody answered" ]
      cab: [ log: "cab" ]
      visitor: [ notify: { who: role:guard, message: "Visitor at the gate" } ]
      unsure: [ log: "{reason}" ]
"""

SITE = Registry(skills={"describe_camera": "read", "recent_events": "read", "site_lookup": "read"},
                apps={"intrusion-detection": AppInfo(),
                      "gate-controller": AppInfo(actions={"open": True, "status": False})},
                cameras=["yard-east", "yard-west", "gate"], webhooks=["ticketing"],
                lists=["staff"])


def doc(text=NIGHT):
    return yaml.safe_load(text)


def errors(source, registry=None):
    return [str(p) for p in validate(source, registry=registry).errors]


# ── files that pass ────────────────────────────────────────────────────


def test_the_rfc_examples_pass_and_say_what_they_need():
    for text in (NIGHT, GATE):
        report = validate(text, registry=SITE)
        assert report.ok, report.errors
    report = validate(NIGHT)
    assert report.needs == {"apps": ["intrusion-detection"], "cameras": ["yard-east", "yard-west"],
                            "skills": ["describe_camera", "recent_events", "site_lookup"]}
    assert report.warnings == []        # the high alert is on a person's reply


def test_an_ai_decided_high_alert_is_a_warning_not_an_error():
    d = doc()
    d["then"][0]["ai"]["alert"] = [{"alert": {"severity": "high", "title": "t"}}]
    report = validate(d)
    assert report.ok
    assert [w.path for w in report.warnings] == ["then[0].ai.alert[0].alert.severity"]


def test_a_person_s_reply_is_not_ai_decided():
    report = validate(NIGHT)
    assert not any("Yes" in w.path for w in report.warnings)


# ── the safety rule ────────────────────────────────────────────────────


def test_a_moving_action_straight_under_an_ai_outcome_is_refused():
    g = doc(GATE)
    g["then"][0]["ai"]["delivery"] = [{"app": {"id": "gate-controller", "action": "open"}}]
    assert errors(g, SITE) == [(
        "then[0].ai.delivery[0].app: this moves things, so it must follow an Ask a person reply "
        "or a plain rule (if) on the same path, not an AI outcome")]


def test_no_answer_is_nobody_s_choice():
    g = doc(GATE)
    g["then"][0]["ai"]["delivery"][0]["no_answer"] = [
        {"app": {"id": "gate-controller", "action": "open"}}]
    assert "then[0].ai.delivery[0].no_answer[0].app: this moves things" in errors(g, SITE)[0]


def test_a_plain_rule_after_the_ai_step_allows_it():
    g = doc(GATE)
    g["then"][0]["ai"]["delivery"] = [{"if": {"list": {"of": "plate", "in": "staff"}},
                                       "then": [{"app": {"id": "gate-controller",
                                                         "action": "open"}}]}]
    assert errors(g, SITE) == []


def test_the_file_s_guards_count_but_an_ai_outcome_resets_them():
    d = doc()
    d["then"].insert(0, {"site_mode": "armed_away"})       # under the if: guards: allowed
    assert errors(d) == []
    d["then"][1]["ai"]["alert"] = [{"site_mode": "armed_away"}]
    assert errors(d)[0].startswith("then[1].ai.alert[0].site_mode: this moves things")
    del d["if"]
    assert errors(d)[0].startswith("then[0].site_mode: this moves things")


def test_an_unknown_app_or_action_is_taken_to_move_things():
    g = doc(GATE)
    g["then"][0]["ai"]["cab"] = [{"app": {"id": "gate-controller", "action": "status"}}]
    assert errors(g, SITE) == []                           # status does not move
    g["then"][0]["ai"]["cab"] = [{"app": {"id": "boom-barrier", "action": "lift"}}]
    assert errors(g) and "moves things" in errors(g)[-1]   # no registry: the safe side


def test_a_critical_alert_needs_a_person_or_a_rule():
    d = doc()
    d["then"][0]["ai"]["alert"] = [{"alert": {"severity": "critical", "title": "t"}}]
    assert "a critical alert needs a person or a plain rule" in errors(d)[0]
    d["then"][0]["ai"]["unsure"][0]["Yes"] = [{"alert": {"severity": "critical", "title": "t"}}]
    assert len(errors(d)) == 1


# ── what is refused, and where ─────────────────────────────────────────


@pytest.mark.parametrize("change, path, says", [
    (lambda d: d.update(workflow="v2"), "workflow", "must be 'v1'"),
    (lambda d: d.pop("name"), "name", "needs a name"),
    (lambda d: d.update(colour="red"), "colour", "unknown key"),
    (lambda d: d.update(state="paused"), "state", "off, test or on"),
    (lambda d: d["limits"].update(cooldown="2 minutes"), "limits.cooldown", "a duration"),
    (lambda d: d["limits"].update(one_at_a_time="site"), "limits.one_at_a_time", "one of"),
    (lambda d: d["limits"].update(per_hour=0), "limits.per_hour", "at least 1"),
    (lambda d: d["limits"].update(burst=3), "limits.burst", "unknown limit"),
    (lambda d: d.update(when={}), "when", "exactly one trigger"),
    (lambda d: d.update(when={"person_in_zone": {}}), "when.person_in_zone",
     "zone presence comes through"),
    (lambda d: d["when"]["alert"].pop("app"), "when.alert.app", "the app whose alerts"),
    (lambda d: d["when"]["alert"].update(cameras=[7]), "when.alert.cameras", "by role"),
    (lambda d: d["when"]["alert"].update(cameras=["Yard East"]), "when.alert.cameras",
     "lower-case words"),
    (lambda d: d.update(when={"event": {"subject": "plates"}}), "when.event.subject",
     "contracted event"),
    (lambda d: d.update(when={"schedule": {"at": "7am"}}), "when.schedule.at", "HH:MM"),
    (lambda d: d.update(when={"schedule": {"at": "07:00", "days": ["monday"]}}),
     "when.schedule.days", "a list of"),
    (lambda d: d["if"][1].update(time={"from": "22:00"}), "if[1].time", "from and to"),
    (lambda d: d["if"].append({"weather": "rain"}), "if[2].weather", "unknown condition"),
    (lambda d: d.update(then=[]), "then", "needs steps"),
    (lambda d: d["then"].append({"jump": 1}), "then[1].jump", "unknown step"),
    (lambda d: d["then"].append({"log": "a", "notify": {}}), "then[1]", "one step at a time"),
    (lambda d: d["then"].append({"call": {}}), "then[1].call", "Phase 3"),
    (lambda d: d["then"].append({"log": "x", "then": []}), "then[1].then", "has no paths"),
    (lambda d: d["then"].append({"wait": 30}), "then[1].wait", "a duration"),
    (lambda d: d["then"].append({"alert": {"severity": "urgent", "title": "t"}}),
     "then[1].alert.severity", "one of"),
    (lambda d: d["then"].append({"notify": {"who": "role:guard"}}), "then[1].notify",
     "who and message"),
    (lambda d: d["then"].append({"if": {"camera": ["gate"]}}), "then[1].then",
     "the steps when the condition holds"),
    (lambda d: d["then"].append({"for_each": {"cameras": ["gate"]}}), "then[1].do",
     "the steps to repeat"),
    (lambda d: d["then"][0]["ai"].update(tools=["open_gate"]), "then[0].ai.tools", "moves things"),
    (lambda d: d["then"][0]["ai"].pop("watch"), "then[0].ai.watch", "every outcome needs a path"),
    (lambda d: d["then"][0]["ai"]["unsure"][0].update({"Maybe": []}),
     "then[0].ai.unsure[0].Maybe", "not one of the replies"),
    (lambda d: d["then"][0]["ai"]["unsure"][0]["ask_person"].update(default="Later"),
     "then[0].ai.unsure[0].ask_person.default", "one of the replies"),
])
def test_a_file_that_cannot_run_is_refused_at_the_key(change, path, says):
    d = copy.deepcopy(doc())
    change(d)
    reg = Registry(skills={"describe_camera": "read", "recent_events": "read",
                           "site_lookup": "read", "open_gate": "actuate"})
    found = [p for p in validate(d, registry=reg).errors if p.path == path]
    assert found and says in found[0].message, validate(d, registry=reg).errors


def test_the_rfc_s_unquoted_yes_is_caught_with_the_fix():
    text = NIGHT.replace('replies: ["Yes", "No"]', "replies: [Yes, No]").replace('"Yes":', "Yes:")
    got = errors(text)
    assert any("ask_person.replies" in e and "true/false" in e for e in got)
    assert any(e.startswith("then[0].ai.unsure[0].True") and '"Yes":' in e for e in got)


def test_names_resolve_against_the_site_at_import():
    g = doc(GATE)
    site = Registry(apps={"gate-controller": AppInfo(actions={"open": True})}, cameras=["door"],
                    webhooks=[], lists=[])
    got = errors(g, site)
    assert "when.event.cameras: 'gate' is not a camera role on this site" in got
    g["then"][0]["ai"]["cab"] = [{"webhook": {"to": "ticketing"}},
                                 {"app": {"id": "gate-controller", "action": "close"}}]
    got = errors(g, site)
    assert "then[0].ai.cab[0].webhook.to: 'ticketing' is not an approved destination on this site" \
        in got
    assert any("has no action 'close'" in e for e in got)
    d = doc()
    assert "when.alert.app: 'intrusion-detection' is not installed on this site" in errors(d, site)


def test_every_problem_at_once_and_bad_yaml_has_a_line():
    d = doc()
    d.update(workflow="v0", state="x")
    d["limits"]["per_hour"] = -1
    assert len(validate(d).errors) == 3
    report = validate("workflow: v1\nname: [unclosed\n")
    assert not report.ok and report.errors[0].path.startswith("line ")
    assert validate("- just a list").errors == [Problem("", "a workflow file is a mapping of keys")]


def test_the_report_keeps_the_parsed_file_and_durations_parse():
    assert validate(GATE).doc["name"] == "Gate concierge"
    assert [parse_duration(v) for v in ("30s", "2m", "1h")] == [30, 120, 3600]
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_the_report_says_whether_the_file_moves_things():
    assert validate(GATE, registry=SITE).moves_things is True     # opens the gate
    assert validate(NIGHT).moves_things is False
    g = doc(GATE)
    g["then"][0]["ai"]["delivery"][0]["Let in"] = [
        {"app": {"id": "gate-controller", "action": "status"}}]
    assert validate(g, registry=SITE).moves_things is False
