# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keep a turn's prompt inside a window the runtime cannot grow.

Ollama sizes its window to ``llm_num_ctx`` and the configs set 8,192, so
the system prompt, every tool schema and eight messages of history (about
5,000 tokens on a box with fourteen tools) always fit. A model compiled
for an NPU has its window fixed at build time: the Gemma bundle on the
Ventuno Q holds 4,096 tokens and the runtime REFUSES a prompt that does
not fit, it does not truncate (bench/ventuno-q-npu-agent-llm-2026-11-07.md).
Measured there: the system prompt and thirteen schemas fit with no
history; seven schemas and eight turns of history fit; all fourteen do
not.

So when ``llm_window_tokens`` is set, :func:`fit` sends the model only
what fits, dropping what the question least needs first:

* **tool schemas**, by rank: the router's hint, then the tools the
  question's words point at (an alarm word → the alarm tools, a past
  tense → the history tools), then a fixed priority order of the ones
  most questions need (the first seven of it chose the right tool on
  10 of 10 evaluation questions on the NPU). A tool left out cannot be
  called that turn, which is the same as the cloud config's
  ``enabled_tools`` list, only decided per question.
* **history**, oldest first, in whole user/assistant pairs, after a floor
  of the newest two turns (four messages) so a follow-up ("and the
  gate?") keeps its antecedent.

Tokens are estimated from characters (the runtime's counter is not
available): four characters a token, measured against the bundle's
refusals, with a tenth of the window kept for the chat template and a
reserve for one tool result so the loop's second iteration (result in,
answer out) fits too. Unset (0, the default) nothing is touched, and an
Ollama host keeps its cached prefix.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

#: Characters per token for the estimate. Measured on the Ventuno Q
#: bundle: 3,282 + 11,912 characters (system + 13 schemas) fit in 4,096
#: tokens, 12,749 characters of schemas did not — between 3.7 and 4.0.
CHARS_PER_TOKEN = 4.0
#: Share of the window kept for what the chat template adds around the
#: messages (role markers, the tool preamble).
TEMPLATE_MARGIN = 0.10
#: Tokens kept for one tool call and its result (results are cut at
#: 1,200 characters) so the iteration after a call still fits.
RESULT_RESERVE = 400
#: Per-message overhead of the template (role, separators).
MESSAGE_OVERHEAD = 4

#: The order tools are kept in when the window cannot hold them all:
#: the ones most questions need first. The first seven are the set the
#: NPU spike ran, 10 of 10 right tool choices on the evaluation questions.
PRIORITY: tuple[str, ...] = (
    "describe_camera", "detect_objects", "search_history", "recent_events",
    "create_alarm", "create_monitor", "list_apps",
    "search_footage", "describe_window", "stop_alarm", "stop_monitor",
    "app_status", "recent_app_alerts", "describe_event",
    "create_background_task", "create_report", "stop_report",
    "list_people", "enroll_face", "forget_face",
    "list_standing", "system_status", "show_camera",
)

#: What a question says → the tools it most likely needs. A boost in the
#: ranking, never a filter: a question that matches nothing still gets
#: the priority order. Grow from real misses in the logs, like the
#: router's exemplars.
_BOOSTS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\b(alarm|siren|wake me|alert me|if (?:you )?(?:see|spot)|whenever|"
                r"after \d|before \d|between \d|at night|overnight watch)\b", re.I),
     ("create_alarm", "stop_alarm")),
    (re.compile(r"\b(notify|monitor|watch(?:ing)?|keep (?:an )?eye|let me know|"
                r"tell me (?:when|if)|more than|gather|crowd|loiter)\b", re.I),
     ("create_monitor", "stop_monitor")),
    (re.compile(r"\b(stop|cancel|disarm|turn (?:it )?off|mute|silence|remove)\b", re.I),
     ("stop_alarm", "stop_monitor", "stop_report")),
    (re.compile(r"\b(what|which|list|any|do i have)\b.*\b(alarms?|watch(?:es)?|monitors?|reports?|"
                r"notifications?|set up)\b", re.I),
     ("list_standing",)),
    (re.compile(r"\b(offline|online|disconnected|recording|disk|storage|space|healthy|health|"
                r"status|everything (?:ok|okay|fine|alright)|working)\b", re.I),
     ("system_status",)),
    (re.compile(r"^\s*(show|open|switch to|pull up|bring up|display)\b", re.I),
     ("show_camera", "describe_camera")),
    (re.compile(r"\b(apps?|plugins?|skills?|plates?|number ?plates?|licen[cs]e|"
                r"registration|package|parcel|delivery)\b", re.I),
     ("list_apps", "app_status", "recent_app_alerts")),
    (re.compile(r"\b(report|summar(?:y|ize|ise)|digest|every (?:morning|evening|day|"
                r"hour|night)|daily|weekly)\b", re.I),
     ("create_report", "stop_report", "create_background_task")),
    (re.compile(r"\b(who|whom|whose|names?|recogni[sz]e|faces?|enrol+|remember|forget)\b", re.I),
     ("list_people", "enroll_face", "forget_face", "search_history")),
    (re.compile(r"\b(did|was|were|has|have|had|came|come|arrived?|visited?|entered?|"
                r"earlier|yesterday|last night|this morning|this afternoon|tonight|"
                r"overnight|ago|so far|history|happened|events?|clips?|footage|"
                r"recording)\b", re.I),
     ("search_history", "recent_events", "search_footage", "describe_window", "describe_event")),
    (re.compile(r"\b(right now|now|currently|at the moment|is there|is anyone|anybody|"
                r"how many|see|look|describe|wearing|doing|happening|going on|"
                r"clear|empty)\b", re.I),
     ("describe_camera", "detect_objects")),
)


def estimate_tokens(value: Any) -> int:
    """Tokens a string or a JSON-able value is likely to take."""
    if value is None:
        return 0
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _name(tool: dict[str, Any]) -> str:
    return str(((tool.get("function") or {}).get("name")) or tool.get("name") or "")


def rank_tools(user_text: str, tools: list[dict[str, Any]],
               hint: str | None = None) -> list[dict[str, Any]]:
    """The tools in the order they should be kept for this question:
    the router's hint, the ones the question's words point at (in
    priority order among themselves), then the rest by priority; names
    the priority list does not know go last, in their advertised order."""
    boosted: list[str] = []
    for pattern, names in _BOOSTS:
        if pattern.search(user_text or ""):
            boosted.extend(n for n in names if n not in boosted)
    order = {name: i for i, name in enumerate(PRIORITY)}
    unknown = len(PRIORITY)

    def key(item: tuple[int, dict[str, Any]]) -> tuple[int, int, int, int]:
        pos, tool = item
        name = _name(tool)
        return (0 if name == hint else 1,
                0 if name in boosted else 1,
                order.get(name, unknown),
                pos)

    return [t for _, t in sorted(enumerate(tools), key=key)]


@dataclass
class Fit:
    tools: list[dict[str, Any]]
    history: list[dict[str, Any]]
    dropped_tools: list[str] = field(default_factory=list)
    dropped_history: int = 0
    tokens: int = 0          # the estimate for what is sent
    budget: int = 0          # what it had to fit in

    @property
    def trimmed(self) -> bool:
        return bool(self.dropped_tools or self.dropped_history)


def fit(*, system: str, history: list[dict[str, Any]], user_turn: str,
        tools: list[dict[str, Any]] | None, window: int, max_tokens: int,
        user_text: str | None = None, hint: str | None = None,
        min_tools: int = 2, history_floor: int = 4) -> Fit:
    """What to send so the prompt and the answer fit in ``window`` tokens.

    ``window`` of 0 means no window: everything comes back as it was.
    Tools are ranked by :func:`rank_tools` and kept in their ADVERTISED
    order (the model reads the list as a menu; the ranking only decides
    membership). At least ``min_tools`` are kept even when the estimate
    says they do not fit, so the model can always act: a refusal from
    the runtime is then the honest outcome, not a silently toolless turn.
    """
    tools = list(tools or [])
    history = list(history or [])
    if not window or window <= 0:
        return Fit(tools=tools, history=history)
    budget = int(window * (1 - TEMPLATE_MARGIN)) - int(max_tokens or 0) - RESULT_RESERVE
    fixed = estimate_tokens(system) + estimate_tokens(user_turn) + 2 * MESSAGE_OVERHEAD
    avail = max(0, budget - fixed)

    def msg_cost(m: dict[str, Any]) -> int:
        return estimate_tokens(m.get("content")) + estimate_tokens(m.get("tool_calls")) + MESSAGE_OVERHEAD

    # The newest messages are kept first; the tools get what is left after
    # that floor, the older history what is left after the tools.
    floor = history[-history_floor:] if history_floor > 0 else []
    floor_cost = sum(msg_cost(m) for m in floor)
    tool_budget = max(0, avail - min(floor_cost, avail // 3))
    kept: list[dict[str, Any]] = []
    used = 0
    dropped: list[str] = []
    for tool in rank_tools(user_text or "", tools, hint):
        cost = estimate_tokens(tool)
        if used + cost <= tool_budget or len(kept) < min_tools:
            kept.append(tool)
            used += cost
        else:
            dropped.append(_name(tool))
    kept_ids = {id(t) for t in kept}
    kept = [t for t in tools if id(t) in kept_ids]

    hist_budget = max(0, avail - used)
    kept_hist: list[dict[str, Any]] = []
    spent = 0
    for m in reversed(history):
        cost = msg_cost(m)
        if spent + cost > hist_budget:
            break
        kept_hist.insert(0, m)
        spent += cost
    # Whole pairs: a reply without the question it answered misleads more
    # than it helps, so a leading assistant turn goes too.
    while kept_hist and kept_hist[0].get("role") == "assistant":
        kept_hist.pop(0)
    return Fit(tools=kept, history=kept_hist, dropped_tools=dropped,
               dropped_history=len(history) - len(kept_hist),
               tokens=fixed + used + sum(msg_cost(m) for m in kept_hist), budget=budget)


def shrink_for_retry(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                     *, history_len: int, user_text: str = "", hint: str | None = None
                     ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, str] | None:
    """After the runtime refused the prompt as too long mid-turn (a tool
    result pushed it over): what to retry with, or None when there is
    nothing left to drop. First the history goes, then all but the three
    best-ranked tools. ``messages`` is [system, *history, user, *turn]."""
    if history_len > 0 and len(messages) > 1 + history_len:
        return [messages[0]] + messages[1 + history_len:], tools, "history dropped"
    if tools and len(tools) > 3:
        ranked = rank_tools(user_text, tools, hint)[:3]
        ids = {id(t) for t in ranked}
        return messages, [t for t in tools if id(t) in ids], "tools cut to three"
    return None
