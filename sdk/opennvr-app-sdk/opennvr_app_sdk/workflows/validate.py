# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""Check a whole workflow file: the public contract, ``workflow: v1``.

RFC #620 makes the workflow file a public, versioned contract: the
engine, the builder and any outside tool run this same check, so they
refuse the same files for the same reasons. Every problem comes back with
the path of the key at fault (``then[0].ai.rules``, ``when.alert.app``),
which an editor maps to a line.

The file, as RFC #620's section 5 has it, with the decisions since::

    workflow: v1                      # the contract version; required
    name: Night intrusion triage
    limits: { cooldown: 2m, one_at_a_time: track, per_hour: 20, ai_per_hour: 40 }
    when:                             # exactly one trigger
      alert: { app: intrusion-detection, cameras: [yard-east, yard-west] }
    if:                               # guards, all must hold (optional)
      - site_mode: [armed_home, armed_away]
      - time: { from: "22:00", to: "06:00" }
    then:                             # the steps, top to bottom
      - ai: { mode: investigate, ..., alert: [ alert: { severity: medium } ] }
      - ask_person: { who: role:guard, message: "Threat?", replies: ["Yes", "No"] }
        "Yes": [ alert: { severity: high, title: "Intruder on {camera}" } ]
    always: [ log: "{reason}" ]       # runs after whichever path ran (optional)

**Triggers** (``when``, one of): ``alert`` (an app's alert: ``app``,
optional ``type`` and ``cameras``), ``event`` (a contracted event:
``subject`` such as ``plate.read.v1``, optional ``cameras``),
``camera_offline`` (``cameras``, optional ``for``), ``schedule`` (``at``
"HH:MM", optional ``days``) and ``button`` (optional ``label``). Zone
presence comes through the intrusion and loitering apps' alerts (RFC
#620, corrections, 1).

**Conditions** (the ``if:`` guards and an ``if`` step): ``site_mode``
(modes), ``time`` (``from``/``to``), ``camera`` (roles), ``value``
(``of`` a field or event value, ``in`` a list) and ``list`` (``of`` a
value, ``in`` a site list's name).

**Steps**: ``ai`` (its outcomes' paths inside it, see :mod:`.ai_step`);
``if`` with ``then`` and an optional ``else``; ``for_each`` (``cameras``)
with ``do``; ``ask_person`` (``who``, ``message``, ``replies``, optional
``timeout`` and ``default``) with a path per reply and ``no_answer``; and
the actions ``alert``, ``notify``, ``log``, ``save_clip``,
``ptz_preset``, ``webhook``, ``home_assistant``, ``site_mode``, ``app``
(an installed app's action) and ``wait``. ``call`` is Phase 3. A step with
paths runs the one chosen, then the flow carries on with the next step
(the RFC's Gate concierge: Look, then ask the guard whatever it saw);
paths never loop back.

**The safety rule** (RFC #620, section 7): an action that moves things
(``site_mode``, ``home_assistant``, and an app action that moves things)
may only follow an Ask a person reply or a plain rule (an ``if``, or the
file's ``if:`` guards) on the same path. An AI step's outcome starts a
new path that no rule has checked yet, so a moving action straight under
it is refused, and so is one under ``no_answer``, which no person chose.
``critical`` alerts follow the same rule; an AI-decided ``high`` is
allowed but warned, because the engine caps it at ``medium`` until the
step has earned it.

Cameras are referenced by role (``gate``, ``yard-east``), never by id, so
a file moves between sites. Tools resolve by skill name, apps by id; when
the site's :class:`Registry` is given, a missing one is an error with its
name, and :attr:`Report.needs` says what the file needs either way.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

from .ai_step import check_ai_step

CONTRACT = "v1"
TRIGGERS = ("alert", "event", "camera_offline", "schedule", "button")
CONDITIONS = ("site_mode", "time", "camera", "value", "list")
ACTIONS = ("alert", "notify", "log", "save_clip", "ptz_preset", "webhook",
           "home_assistant", "site_mode", "app", "wait")
#: Core actions that move things. An app action moves things when its
#: app says so (or when the app is unknown: the safe side).
MOVING_ACTIONS = frozenset({"site_mode", "home_assistant"})
SEVERITIES = ("low", "medium", "high", "critical")
ONE_AT_A_TIME = ("camera", "track", "plate", "none")
LIMIT_DEFAULTS = {"cooldown": "1m", "one_at_a_time": "camera", "per_hour": 30, "ai_per_hour": 60}

_STEP_KEYS = frozenset({"ai", "if", "for_each", "ask_person", "call", *ACTIONS})
_TOP = frozenset({"workflow", "name", "description", "state", "limits", "when", "if", "then",
                  "always"})
_ROLE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_DURATION = re.compile(r"^\d+(?:s|m|h)$")
_CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_SUBJECT = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)*\.v\d+$")


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a file: where, and why."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


@dataclass(frozen=True)
class AppInfo:
    """What the site knows of an installed app: its actions, each with
    whether it moves things (a gate, a lock, a siren)."""

    actions: Mapping[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class Registry:
    """What the site has, for resolving names at import. Each part is
    optional; one left out is not checked.

    ``skills``: a skill's name to its effect, or just the names (read).
    ``apps``: an app id to its :class:`AppInfo`. ``cameras``: the roles.
    ``webhooks``: the admin-approved destination names. ``lists``: the
    site lists' names."""

    skills: Mapping[str, str] | Iterable[str] | None = None
    apps: Mapping[str, AppInfo] | None = None
    cameras: Iterable[str] | None = None
    webhooks: Iterable[str] | None = None
    lists: Iterable[str] | None = None


@dataclass
class Report:
    """The check's answer. ``ok`` when there are no errors; warnings do
    not stop a file. ``needs`` names the skills, apps, camera roles,
    webhooks and lists the file uses, for the import screen.
    ``moves_things`` is whether any action in it moves things: turning
    such a workflow On is an admin's decision (RFC #620, section 7)."""

    errors: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)
    needs: dict[str, list[str]] = field(default_factory=dict)
    moves_things: bool = False
    doc: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def validate(source: str | Mapping[str, Any], *, registry: Registry | None = None) -> Report:
    """Check a workflow file (YAML text, or the mapping it parses to)."""
    report = Report()
    if isinstance(source, str):
        try:
            doc = yaml.safe_load(source)
        except yaml.YAMLError as err:
            mark = getattr(err, "problem_mark", None)
            where = f"line {mark.line + 1}" if mark else ""
            report.errors.append(Problem(where, f"not valid YAML: {getattr(err, 'problem', err)}"))
            return report
    else:
        doc = source
    if not isinstance(doc, Mapping):
        report.errors.append(Problem("", "a workflow file is a mapping of keys"))
        return report
    _Checker(registry or Registry(), report).file(doc)
    report.doc = dict(doc)
    report.needs = {k: sorted(v) for k, v in report.needs.items() if v}
    return report


class _Checker:
    def __init__(self, reg: Registry, report: Report) -> None:
        self.reg = reg
        self.report = report
        self.needs: dict[str, set[str]] = {k: set() for k in
                                           ("skills", "apps", "cameras", "webhooks", "lists")}
        report.needs = self.needs          # type: ignore[assignment]
        self.skills = (None if reg.skills is None else dict(reg.skills)
                       if isinstance(reg.skills, Mapping) else {s: "read" for s in reg.skills})
        self.cameras = None if reg.cameras is None else set(reg.cameras)
        self.webhooks = None if reg.webhooks is None else set(reg.webhooks)
        self.lists = None if reg.lists is None else set(reg.lists)

    def err(self, path: str, message: str) -> None:
        self.report.errors.append(Problem(path, message))

    def warn(self, path: str, message: str) -> None:
        self.report.warnings.append(Problem(path, message))

    # ── the file ──────────────────────────────────────────────────────

    def file(self, doc: Mapping[str, Any]) -> None:
        if doc.get("workflow") != CONTRACT:
            self.err("workflow", f"must be {CONTRACT!r}, the contract version "
                                 f"(got {doc.get('workflow')!r})")
        for key in doc:
            if key not in _TOP:
                self.err(str(key), f"unknown key; a workflow has {sorted(_TOP)}"
                         + _bool_key_hint(key))
        name = doc.get("name")
        if not isinstance(name, str) or not name.strip():
            self.err("name", "a workflow needs a name")
        if "state" in doc and doc["state"] not in ("off", "test", "on"):
            self.err("state", "must be off, test or on")
        self.limits(doc.get("limits", {}))
        self.trigger(doc.get("when"))
        guards = doc.get("if")
        if guards is not None:
            self.conditions(guards, "if")
        then = doc.get("then")
        if not isinstance(then, list) or not then:
            self.err("then", "a workflow needs steps: a list under then")
        else:
            self.path(then, "then", guarded=guards is not None, ai_decided=False)
        if "always" in doc:
            if not isinstance(doc["always"], list):
                self.err("always", "a list of steps")
            else:
                self.path(doc["always"], "always", guarded=guards is not None, ai_decided=False)

    def limits(self, limits: Any) -> None:
        if not isinstance(limits, Mapping):
            self.err("limits", "a mapping of cooldown, one_at_a_time, per_hour, ai_per_hour")
            return
        for k, v in limits.items():
            p = f"limits.{k}"
            if k == "cooldown":
                self.duration(v, p)
            elif k == "one_at_a_time":
                if v not in ONE_AT_A_TIME:
                    self.err(p, f"one of {list(ONE_AT_A_TIME)}")
            elif k in ("per_hour", "ai_per_hour"):
                if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                    self.err(p, "a whole number of runs, at least 1")
            else:
                self.err(p, f"unknown limit; the limits are {sorted(LIMIT_DEFAULTS)}")

    # ── triggers and conditions ───────────────────────────────────────

    def trigger(self, when: Any) -> None:
        if not isinstance(when, Mapping) or len(when) != 1:
            self.err("when", f"exactly one trigger, one of {list(TRIGGERS)}")
            return
        (kind, spec), = when.items()
        p = f"when.{kind}"
        if kind not in TRIGGERS:
            self.err(p, f"unknown trigger; one of {list(TRIGGERS)}"
                     + ("; zone presence comes through the intrusion and loitering apps' "
                        "alerts" if "zone" in str(kind) else ""))
            return
        spec = {} if spec is None else spec
        if not isinstance(spec, Mapping):
            self.err(p, "a mapping of the trigger's settings")
            return
        allowed = {"alert": {"app", "type", "cameras"}, "event": {"subject", "cameras"},
                   "camera_offline": {"cameras", "for"}, "schedule": {"at", "days"},
                   "button": {"label"}}[kind]
        for k in spec:
            if k not in allowed:
                self.err(f"{p}.{k}", f"not a setting of {kind}; it has {sorted(allowed)}")
        if kind == "alert":
            if not _text(spec.get("app")):
                self.err(f"{p}.app", "the app whose alerts start this workflow")
            else:
                self.app_id(spec["app"], f"{p}.app")
        if kind == "event" and not _SUBJECT.match(str(spec.get("subject") or "")):
            self.err(f"{p}.subject", "a contracted event, such as plate.read.v1")
        if kind == "camera_offline":
            if "cameras" not in spec:
                self.err(f"{p}.cameras", "the camera roles to watch")
            if "for" in spec:
                self.duration(spec["for"], f"{p}.for")
        if kind == "schedule":
            if not _CLOCK.match(str(spec.get("at") or "")):
                self.err(f"{p}.at", 'a time of day, "HH:MM" (quoted)')
            days = spec.get("days")
            if days is not None and not (isinstance(days, list)
                                         and all(d in _DAYS for d in days)):
                self.err(f"{p}.days", f"a list of {list(_DAYS)}")
        if "cameras" in spec:
            self.camera_roles(spec["cameras"], f"{p}.cameras")

    def conditions(self, conds: Any, p: str) -> None:
        items = conds if isinstance(conds, list) else [conds]
        if not items:
            self.err(p, "at least one condition")
        for i, c in enumerate(items):
            cp = f"{p}[{i}]" if isinstance(conds, list) else p
            if not isinstance(c, Mapping) or len(c) != 1:
                self.err(cp, f"one condition, one of {list(CONDITIONS)}")
                continue
            (kind, v), = c.items()
            kp = f"{cp}.{kind}"
            if kind == "site_mode":
                if not (isinstance(v, list) and v and all(_text(m) for m in v)):
                    self.err(kp, "a list of site modes")
            elif kind == "time":
                if not (isinstance(v, Mapping) and set(v) == {"from", "to"}
                        and all(_CLOCK.match(str(v[k])) for k in ("from", "to"))):
                    self.err(kp, 'from and to, each "HH:MM" (quoted)')
            elif kind == "camera":
                self.camera_roles(v, kp)
            elif kind == "value":
                if not (isinstance(v, Mapping) and set(v) == {"of", "in"} and _text(v["of"])
                        and isinstance(v["in"], list) and v["in"]):
                    self.err(kp, "of (a field or event value) and in (a list of values)")
            elif kind == "list":
                if not (isinstance(v, Mapping) and set(v) == {"of", "in"} and _text(v["of"])
                        and _text(v["in"])):
                    self.err(kp, "of (a value) and in (a site list's name)")
                else:
                    self.needs["lists"].add(v["in"])
                    if self.lists is not None and v["in"] not in self.lists:
                        self.err(f"{kp}.in", f"{v['in']!r} is not a list on this site")
            else:
                self.err(kp, f"unknown condition; one of {list(CONDITIONS)}")

    # ── steps ─────────────────────────────────────────────────────────

    def path(self, steps: list[Any], p: str, *, guarded: bool, ai_decided: bool) -> None:
        """A list of steps. ``guarded``: a person or a plain rule chose
        this path. ``ai_decided``: an AI step's outcome did, with no rule
        or person since."""
        for i, step in enumerate(steps):
            sp = f"{p}[{i}]"
            if not isinstance(step, Mapping) or not step:
                self.err(sp, "a step is a mapping: one step key, and its paths")
                continue
            kinds = [k for k in step if k in _STEP_KEYS]
            if len(kinds) > 1:
                self.err(sp, f"one step at a time; this has {kinds}")
                continue
            kind = kinds[0] if kinds else next(iter(step))
            if kind == "ai":
                self.ai(step, sp)
            elif kind == "if":
                self.if_step(step, sp, ai_decided=ai_decided)
            elif kind == "for_each":
                self.for_each(step, sp, guarded=guarded, ai_decided=ai_decided)
            elif kind == "ask_person":
                self.ask_person(step, sp, guarded=guarded)
            elif kind == "call":
                self.err(f"{sp}.call", "the Call step comes in Phase 3")
            elif kind in ACTIONS:
                if len(step) != 1:
                    extra = [k for k in step if k != kind]
                    self.err(f"{sp}.{extra[0]}", f"an action has no paths; {kind} is the step"
                             + _bool_key_hint(extra[0]))
                self.action(kind, step[kind], f"{sp}.{kind}", guarded=guarded,
                            ai_decided=ai_decided)
            else:
                self.err(f"{sp}.{kind}", "unknown step; the steps are ai, if, for_each, "
                                         f"ask_person and the actions {list(ACTIONS)}"
                         + _bool_key_hint(kind))

    def ai(self, step: Mapping[str, Any], sp: str) -> None:
        if len(step) != 1:
            self.err(sp, "an ai step's outcome paths go inside it, under ai:")
        raw = step["ai"]
        for problem in check_ai_step(raw, skills=self.reg.skills, where=f"{sp}.ai"):
            path, _, message = problem.partition(": ")
            self.err(path, message)
        if not isinstance(raw, Mapping):
            return
        for tool in raw.get("tools") or []:
            if isinstance(tool, str):
                self.needs["skills"].add(tool)
        decision = raw.get("decision") if isinstance(raw.get("decision"), list) else []
        for outcome in [*decision, "unsure"]:
            steps = raw.get(outcome) if isinstance(outcome, str) else None
            if isinstance(steps, list):
                # An AI outcome is a new path: no rule or person has checked it.
                self.path(steps, f"{sp}.ai.{outcome}", guarded=False, ai_decided=True)

    def if_step(self, step: Mapping[str, Any], sp: str, *, ai_decided: bool) -> None:
        self.conditions(step["if"], f"{sp}.if")
        for k in step:
            if k not in ("if", "then", "else"):
                self.err(f"{sp}.{k}", "an if step has then and else" + _bool_key_hint(k))
        if not isinstance(step.get("then"), list):
            self.err(f"{sp}.then", "the steps when the condition holds: a list")
        for k in ("then", "else"):
            if isinstance(step.get(k), list):
                self.path(step[k], f"{sp}.{k}", guarded=True, ai_decided=False)
            elif k in step:
                self.err(f"{sp}.{k}", "a list of steps")

    def for_each(self, step: Mapping[str, Any], sp: str, *, guarded: bool,
                 ai_decided: bool) -> None:
        spec = step["for_each"]
        if not (isinstance(spec, Mapping) and set(spec) == {"cameras"}):
            self.err(f"{sp}.for_each", "cameras: the camera roles to repeat over")
        else:
            self.camera_roles(spec["cameras"], f"{sp}.for_each.cameras")
        for k in step:
            if k not in ("for_each", "do"):
                self.err(f"{sp}.{k}", "a for_each step has do" + _bool_key_hint(k))
        if not isinstance(step.get("do"), list) or not step.get("do"):
            self.err(f"{sp}.do", "the steps to repeat: a list")
        else:
            self.path(step["do"], f"{sp}.do", guarded=guarded, ai_decided=ai_decided)

    def ask_person(self, step: Mapping[str, Any], sp: str, *, guarded: bool) -> None:
        spec = step["ask_person"]
        ap = f"{sp}.ask_person"
        replies: list[str] = []
        if not isinstance(spec, Mapping):
            self.err(ap, "who, message and replies")
        else:
            for k in spec:
                if k not in ("who", "message", "replies", "timeout", "default"):
                    self.err(f"{ap}.{k}", "ask_person has who, message, replies, timeout "
                                          "and default")
            if not _text(spec.get("who")):
                self.err(f"{ap}.who", "a person or a role, such as role:guard")
            if not _text(spec.get("message")):
                self.err(f"{ap}.message", "the question")
            raw = spec.get("replies")
            if not (isinstance(raw, list) and raw and all(_text(r) for r in raw)):
                self.err(f"{ap}.replies", "a list of reply buttons, quoted"
                         + ("; YAML reads Yes/No unquoted as true/false"
                            if isinstance(raw, list) and any(isinstance(r, bool) for r in raw)
                            else ""))
            else:
                replies = list(raw)
                if len(set(replies)) != len(replies):
                    self.err(f"{ap}.replies", "replies must differ")
                if "no_answer" in replies:
                    self.err(f"{ap}.replies", "no_answer is the timeout's path, not a reply")
            if "timeout" in spec:
                self.duration(spec["timeout"], f"{ap}.timeout")
            if "default" in spec and replies and spec["default"] not in replies:
                self.err(f"{ap}.default", "one of the replies (with none, no answer is no)")
        for k in step:
            if k == "ask_person":
                continue
            kp = f"{sp}.{k}"
            if isinstance(k, bool):
                self.err(kp, "YAML reads Yes/No unquoted as true/false: quote the reply "
                             'as a key, "Yes":')
                continue
            if k != "no_answer" and replies and k not in replies:
                self.err(kp, f"not one of the replies {replies}")
                continue
            if not isinstance(step[k], list):
                self.err(kp, "a list of steps")
                continue
            # A reply is a person's choice; no answer is nobody's.
            self.path(step[k], kp, guarded=(k != "no_answer") or guarded, ai_decided=False)

    def action(self, kind: str, spec: Any, ap: str, *, guarded: bool, ai_decided: bool) -> None:
        moves = kind in MOVING_ACTIONS
        if kind == "alert":
            if not isinstance(spec, Mapping) or spec.get("severity") not in SEVERITIES:
                self.err(f"{ap}.severity", f"one of {list(SEVERITIES)}")
            elif spec["severity"] == "critical" and not guarded:
                self.err(f"{ap}.severity", "a critical alert needs a person or a plain rule "
                                           "on its path (ask_person or if)")
            elif spec["severity"] == "high" and ai_decided:
                self.warn(f"{ap}.severity", "an AI-decided alert is capped at medium until "
                                            "the step has 80% marked accuracy")
            if isinstance(spec, Mapping) and not _text(spec.get("title")):
                self.err(f"{ap}.title", "the alert's title")
        elif kind == "notify":
            if not (isinstance(spec, Mapping) and _text(spec.get("who"))
                    and _text(spec.get("message"))):
                self.err(ap, "who and message")
        elif kind == "log":
            if not _text(spec):
                self.err(ap, "the text to log")
        elif kind in ("save_clip", "ptz_preset"):
            if not isinstance(spec, Mapping) or "camera" not in spec:
                self.err(f"{ap}.camera", "the camera role")
            else:
                self.camera_roles([spec["camera"]], f"{ap}.camera")
            if kind == "ptz_preset" and not (isinstance(spec, Mapping) and _text(spec.get("preset"))):
                self.err(f"{ap}.preset", "the preset's name")
        elif kind == "webhook":
            to = spec.get("to") if isinstance(spec, Mapping) else None
            if not _text(to):
                self.err(f"{ap}.to", "an admin-approved destination's name")
            else:
                self.needs["webhooks"].add(to)
                if self.webhooks is not None and to not in self.webhooks:
                    self.err(f"{ap}.to", f"{to!r} is not an approved destination on this site")
        elif kind == "home_assistant":
            if not (isinstance(spec, Mapping) and _text(spec.get("service"))):
                self.err(f"{ap}.service", "the Home Assistant service, such as lock.unlock")
        elif kind == "site_mode":
            if not _text(spec):
                self.err(ap, "the mode to set")
        elif kind == "app":
            moves = self.app_action(spec, ap)
        elif kind == "wait":
            self.duration(spec, ap)
        if moves:
            self.report.moves_things = True
        if moves and not guarded:
            self.err(ap, "this moves things, so it must follow an Ask a person reply or a plain "
                         "rule (if) on the same path" + (", not an AI outcome" if ai_decided
                                                         else ""))

    def app_action(self, spec: Any, ap: str) -> bool:
        """Checks ``app: {id, action, args}``; returns whether it moves
        things (an unknown app or action is taken to move things)."""
        if not (isinstance(spec, Mapping) and _text(spec.get("id"))
                and _text(spec.get("action"))):
            self.err(ap, "id (the app) and action, and optional args")
            return True
        for k in spec:
            if k not in ("id", "action", "args"):
                self.err(f"{ap}.{k}", "an app action has id, action and args")
        if "args" in spec and not isinstance(spec["args"], Mapping):
            self.err(f"{ap}.args", "a mapping")
        info = self.app_id(spec["id"], f"{ap}.id")
        if info is None:
            return True
        if spec["action"] not in info.actions:
            self.err(f"{ap}.action", f"{spec['id']} has no action {spec['action']!r} "
                                     f"(it has {sorted(info.actions)})")
            return True
        return bool(info.actions[spec["action"]])

    # ── names ─────────────────────────────────────────────────────────

    def app_id(self, app: str, p: str) -> AppInfo | None:
        self.needs["apps"].add(app)
        if self.reg.apps is None:
            return None
        info = self.reg.apps.get(app)
        if info is None:
            self.err(p, f"{app!r} is not installed on this site")
        return info

    def camera_roles(self, roles: Any, p: str) -> None:
        if not (isinstance(roles, list) and roles):
            self.err(p, "a list of camera roles")
            return
        for r in roles:
            if isinstance(r, int) or (isinstance(r, str) and r.isdigit()):
                self.err(p, f"{r!r}: reference cameras by role (gate, yard-east), never by id, "
                            "so the workflow moves between sites")
            elif not (isinstance(r, str) and _ROLE.match(r)):
                self.err(p, f"{r!r}: a camera role, lower-case words joined by -")
            else:
                self.needs["cameras"].add(r)
                if self.cameras is not None and r not in self.cameras:
                    self.err(p, f"{r!r} is not a camera role on this site")

    def duration(self, v: Any, p: str) -> None:
        if not (isinstance(v, str) and _DURATION.match(v)):
            self.err(p, f"a duration such as 30s, 2m or 1h (got {v!r})")


def _text(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _bool_key_hint(key: Any) -> str:
    return ("; YAML reads Yes/No/On/Off unquoted as true/false: quote it"
            if isinstance(key, bool) else "")


def parse_duration(v: str) -> int:
    """``"2m"`` -> 120 seconds; for the engine, after :func:`validate`."""
    if not _DURATION.match(v):
        raise ValueError(f"not a duration: {v!r}")
    return int(v[:-1]) * {"s": 1, "m": 60, "h": 3600}[v[-1]]
