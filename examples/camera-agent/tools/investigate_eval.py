#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Investigate eval: can a model run an Agentic Workflows AI step on its
own, choose the tools, decide right, and stay inside the hardware limits?

RFC #620 offers Investigate on a site only when its language model passes
the Investigate cases in this harness. Each case in investigate_cases.yml
is one AI step as a workflow would run it: a goal, instructions, the
site's facts, the triggering event, the decision options, a short tool
list, and FIXTURE tool results (recorded outputs). Only the model varies,
so a run needs no NVR, no cameras and no agent, and is cheap enough to
repeat on every small model.

    python tools/investigate_eval.py --url http://localhost:11434 \\
        --model qwen3:1.7b --hardware cpu --repeat 5 --out run.json

Each step runs through the SDK's run_investigate(), the code a workflow
runs (tool phase, then one decision call with no tools and a JSON
schema), so the gate measures what ships. The harness supplies only the
fixture tools and the scoring. It once carried its own copy of the loop,
and the two drifted: a rewording in the copy cost granite4:micro 7 runs
in 60 before anyone saw it (#674). There is no router and no forced
grounding: in a workflow, every tool call is the model's own.

What a run measures, per case and repeat:

  pass      the decision and its fields are right AND the tools a good
            run needs were called (a right answer without looking is a
            guess, and guesses do not count)
  decided   the decision and fields alone, needs aside
  limits    the step finished inside the hardware class's tool calls and
            seconds; a step that ran out decides "unsure"
  signals   three candidate confidence sources, for the study that picks
            the default (design note, "Confidence"): the model's own
            number, agreement across k decision samples, and an evidence
            score from the tool results. AUROC per signal says which one
            separates right decisions from wrong ones.

The gate is the Wilson lower bound of the pass rate, not the ratio (as
agreed on #620): with 20 runs, 16/20 is 80% but its lower bound is 58%.

With --samples k the step draws its decision k times and the agreement
signal is the share that agree; the draws count against the step's
seconds, as they do in a workflow. The floor is 0 here, so a draw never
turns an answer into "unsure"; the AUROC columns measure the floor's use.

The pure parts (case loading, the fixture tools, scoring, the summary)
are tested in tests/test_investigate_eval.py, and so is the loop, with a
scripted model.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import pathlib
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# The sibling harness has the Wilson interval and the YAML loader.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import eval_harness as eh
from opennvr_app_sdk.agent import (
    READ_TOOL_NAMES,
    Decision,
    DecisionSpec,
    Limits,
    LLMClient,
    OllamaChat,
    OpenAIChat,
    Suspended,
    Tool,
    ToolResult,
    build_tool_definitions,
    run_investigate,
)

#: RFC #620's hardware-class limits for one AI step. "none" is for
#: measurement only: the GPU's tool calls and no clock, so a slow model's
#: decisions can be judged apart from its speed. It never qualifies.
LIMITS = {"gpu": {"tool_calls": 6, "seconds": 45.0},
          "cpu": {"tool_calls": 4, "seconds": 120.0},
          "none": {"tool_calls": 6, "seconds": math.inf}}

#: Keep an Investigate tool list short: small models choose worse from
#: long lists (camera-agent eval, #617).
MAX_TOOLS = 5

UNSURE = "unsure"

_CASE_KEYS = {"id", "template", "note", "goal", "instructions", "site_facts",
              "cameras", "event", "options", "fields", "tools", "fixtures", "expect"}
_EXPECT_KEYS = {"decision", "fields", "needs", "forbid"}


class CaseError(ValueError):
    """A case file that cannot be run as written."""


# ── cases ──────────────────────────────────────────────────────────────


def load_cases(path: str | pathlib.Path) -> list[dict[str, Any]]:
    p = pathlib.Path(path)
    text = p.read_text(encoding="utf-8")
    raw = json.loads(text) if p.suffix.lower() == ".json" else eh._load_yaml(text)
    return normalise_cases(raw)


def _as_list(v: Any) -> list[Any]:
    return list(v) if isinstance(v, (list, tuple)) else [v]


def normalise_cases(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise CaseError("the case file must be a non-empty list of cases")
    cases, seen = [], set()
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            raise CaseError(f"case #{i + 1} is not a mapping")
        cid = str(c.get("id") or "").strip()
        if not cid or cid in seen:
            raise CaseError(f"case #{i + 1}: missing or duplicate id {cid!r}")
        seen.add(cid)
        unknown = set(c) - _CASE_KEYS
        if unknown:
            raise CaseError(f"{cid}: unknown keys {sorted(unknown)}")
        for key in ("goal", "event", "options", "tools", "expect"):
            if not c.get(key):
                raise CaseError(f"{cid}: '{key}' is required")
        options = [str(o) for o in c["options"]]
        if UNSURE in options:
            raise CaseError(f"{cid}: 'unsure' is always an option; do not list it")
        tools = [str(t) for t in c["tools"]]
        bad = [t for t in tools if t not in READ_TOOL_NAMES]
        if bad:
            raise CaseError(f"{cid}: not read tools: {bad}")
        if len(tools) > MAX_TOOLS:
            raise CaseError(f"{cid}: {len(tools)} tools; an Investigate list is at most {MAX_TOOLS}")
        fields = {str(k): [str(x) for x in _as_list(v)] for k, v in (c.get("fields") or {}).items()}
        fixtures = c.get("fixtures") or {}
        stray = set(fixtures) - set(tools)
        if stray:
            raise CaseError(f"{cid}: fixtures for tools not on the list: {sorted(stray)}")
        expect = c["expect"]
        if not isinstance(expect, dict) or set(expect) - _EXPECT_KEYS or "decision" not in expect:
            raise CaseError(f"{cid}: expect needs 'decision' and only {sorted(_EXPECT_KEYS)}")
        allowed = {*options, UNSURE}
        for d in _as_list(expect["decision"]):
            if d not in allowed:
                raise CaseError(f"{cid}: expected decision {d!r} is not an option")
        for k, v in (expect.get("fields") or {}).items():
            if k not in fields or any(str(x) not in fields[k] for x in _as_list(v)):
                raise CaseError(f"{cid}: expected field {k}={v!r} is not in 'fields'")
        for need in expect.get("needs") or []:
            if not set(map(str, _as_list(need))) <= set(tools):
                raise CaseError(f"{cid}: needs {need!r}, which is not on the tool list")
        cameras = [str(x) for x in (c.get("cameras") or [])] or sorted(
            {str(c["event"].get("camera"))} - {"None"}) or ["front"]
        cases.append({
            "id": cid, "template": c.get("template"), "goal": str(c["goal"]).strip(),
            "instructions": str(c.get("instructions") or "").strip(),
            "site_facts": str(c.get("site_facts") or "").strip(),
            "cameras": cameras, "event": c["event"], "options": options,
            "fields": fields, "tools": tools, "fixtures": fixtures,
            "expect": {
                "decision": [str(d) for d in _as_list(expect["decision"])],
                "fields": {k: [str(x).lower() for x in _as_list(v)]
                           for k, v in (expect.get("fields") or {}).items()},
                "needs": [[str(x) for x in _as_list(n)] for n in expect.get("needs") or []],
                "forbid": [str(x) for x in expect.get("forbid") or []],
            },
        })
    return cases


# ── fixture tools ─────────────────────────────────────────────────────


def fixture_result(case: dict[str, Any], name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """The recorded result for a tool call, and whether it worked.

    A fixture is a string (the result, whatever the arguments), or a list
    of ``{when: {arg: value}, result | error: text}`` tried in order, the
    first whose ``when`` matches winning; an entry with no ``when`` is the
    default. ``error`` is a failed call. A listed tool with no fixture
    finds nothing."""
    fx = case["fixtures"].get(name)
    if fx is None:
        return "No data.", True
    if isinstance(fx, str):
        return fx, True
    entries = fx if isinstance(fx, list) else [fx]
    for e in entries:
        when = e.get("when") or {}
        if all(_arg_matches(args.get(k), v) for k, v in when.items()):
            if "error" in e:
                return f"ERROR: {e['error']}", False
            return str(e.get("result", "No data.")), True
    return "No data.", True


def _arg_matches(actual: Any, expected: Any) -> bool:
    if isinstance(actual, list):
        return any(_arg_matches(a, expected) for a in actual)
    return any(str(actual).strip().lower() == str(x).lower() for x in _as_list(expected))


# ── the step ───────────────────────────────────────────────────────────


def load_rules(path: str | pathlib.Path) -> dict[str, dict[str, Any]]:
    """The rule tables, per template: ``{template: {"facts", "rules"}}``.
    Each is checked by building a spec from it, so a table that cannot
    work fails here, before any model call."""
    raw = eh._load_yaml(pathlib.Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise CaseError(f"{path}: a mapping of template -> {{facts, rules}}")
    tables = {}
    for template, t in raw.items():
        if not isinstance(t, dict) or set(t) != {"facts", "rules"}:
            raise CaseError(f"{path}: {template} needs exactly 'facts' and 'rules'")
        facts = {str(k): [str(x) for x in _as_list(v)] for k, v in t["facts"].items()}
        tables[str(template)] = {"facts": facts, "rules": list(t["rules"])}
    return tables


def spec_for(case: dict[str, Any], tables: dict[str, dict[str, Any]] | None = None) -> DecisionSpec:
    """The case's decision as the SDK's spec. The floor is 0, so the
    harness scores what the outcome was; whether a floor on any
    confidence signal would have helped is what the AUROC columns say.

    With a rule table for the case's template, the template's facts join
    the case's fields and the table decides: the model never chooses."""
    fields = {k: tuple(v) for k, v in case["fields"].items()}
    table = (tables or {}).get(case["template"])
    if table is None:
        return DecisionSpec(options=tuple(case["options"]), fields=fields, floor=0.0)
    for k, v in table["facts"].items():
        if k in fields and set(fields[k]) != set(v):
            raise CaseError(f"{case['id']}: field {k} differs from the template's fact")
        fields[k] = tuple(v)
    return DecisionSpec(options=tuple(case["options"]), fields=fields, floor=0.0,
                        rules=tuple(table["rules"]))


def tools_for(case: dict[str, Any]) -> list[Tool]:
    """The case's read tools: the shared definitions, answered from the
    case's fixtures instead of a live NVR."""
    out = []
    for d in build_tool_definitions(case["cameras"], enabled=case["tools"]):
        name = d["function"]["name"]

        async def handler(args: dict[str, Any], name: str = name) -> ToolResult:
            text, ok = fixture_result(case, name, args)
            return ToolResult(text, ok)
        out.append(Tool.from_definition(d, handler))
    return out


@dataclass
class StepResult:
    decision: str = UNSURE
    fields: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    self_reported: float | None = None
    unsure_reason: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)   # the tool rows
    model_calls: int = 0
    seconds: float = 0.0
    limit_hit: str | None = None
    samples: list[str] = field(default_factory=list)            # runs saved before the SDK
    agreement: float | None = None
    error: str | None = None


def step_result(d: Decision) -> StepResult:
    """The SDK's decision as the record the scoring reads."""
    tools = [r for r in d.trace if r.kind == "tool"]
    limit = None
    if d.unsure_reason == "over_budget":
        limit = "seconds"
    elif any(r.status == "refused" and "limit" in r.result for r in tools):
        limit = "tool_calls"
    failed_llm = [r.result for r in d.trace if r.kind == "llm" and r.status == "failed"]
    return StepResult(
        decision=d.decision, fields=d.fields, reason=d.reason,
        self_reported=d.self_reported, unsure_reason=d.unsure_reason,
        calls=[{"name": r.name, "args": r.args_asked, "status": r.status} for r in tools],
        model_calls=d.usage.model_calls, seconds=d.usage.seconds, limit_hit=limit,
        agreement=None if d.unsure else d.confidence,
        error=(failed_llm[-1] if failed_llm else "") if d.unsure_reason == "llm_down" else None)


async def run_step(llm: LLMClient, case: dict[str, Any], *, hardware: str = "cpu",
                   samples: int = 1, model_name: str = "llm",
                   rules: dict[str, dict[str, Any]] | None = None,
                   clock: Callable[[], float] = time.monotonic) -> StepResult:
    """One AI step on the case, run by the SDK's run_investigate, the code
    a workflow runs, inside the hardware class's limits. Never raises."""
    lim = LIMITS[hardware]
    out = await run_investigate(
        llm, spec_for(case, rules), goal=case["goal"], instructions=case["instructions"],
        site_facts=case["site_facts"], inputs=case["event"], tools=tools_for(case),
        limits=Limits(tool_calls=lim["tool_calls"], seconds=lim["seconds"]),
        samples=samples, model_name=model_name, clock=clock)
    if isinstance(out, Suspended):      # the eval's tools are read tools; never expected
        return StepResult(unsure_reason="error", error="the step suspended on an ask tool")
    return step_result(out)


# ── scoring ────────────────────────────────────────────────────────────


def evidence_score(calls: list[dict[str, Any]]) -> float:
    """What the step can know about its own evidence at run time: the
    share of its tool calls that returned something. No call at all is 0."""
    if not calls:
        return 0.0
    return sum(1 for c in calls if c["status"] == "ok") / len(calls)


def score(case: dict[str, Any], r: StepResult) -> dict[str, Any]:
    exp = case["expect"]
    called_ok = {c["name"] for c in r.calls if c["status"] == "ok"}
    called = {c["name"] for c in r.calls}
    right_decision = r.decision in exp["decision"]
    right_fields = all(str(r.fields.get(k, "")).lower() in v for k, v in exp["fields"].items())
    # Fields only count when the decision has them to give; "unsure" may omit them.
    decided = right_decision and (right_fields or r.decision == UNSURE)
    needs_met = all(any(n in called_ok for n in alts) for alts in exp["needs"])
    forbidden = sorted(called & set(exp["forbid"]))
    samples = r.samples or []
    if r.agreement is not None:
        agreement = r.agreement
    else:
        agreement = (samples.count(r.decision) / len(samples)) if samples else 0.0
    return {
        "case": case["id"], "template": case["template"],
        "pass": bool(decided and needs_met and not forbidden and r.error is None),
        "decided": bool(decided), "needs_met": needs_met, "forbidden": forbidden,
        "decision": r.decision, "fields": r.fields, "reason": r.reason,
        "unsure": r.decision == UNSURE, "unsure_reason": r.unsure_reason,
        "tool_calls": r.calls, "model_calls": r.model_calls,
        "seconds": round(r.seconds, 2), "limit_hit": r.limit_hit,
        "signals": {"self_reported": r.self_reported, "agreement": agreement,
                    "evidence": evidence_score(r.calls)},
        "samples": samples, "error": r.error,
    }


def auroc(scores: list[float], labels: list[bool]) -> float | None:
    """Probability that a right run scores above a wrong one (ties half).
    None when every run is right, or every one wrong: nothing to separate."""
    pos = [s for s, y in zip(scores, labels, strict=True) if y]
    neg = [s for s, y in zip(scores, labels, strict=True) if not y]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def summarise(runs: list[dict[str, Any]], *, hardware: str, gate: float) -> dict[str, Any]:
    n = len(runs)
    k = sum(1 for r in runs if r["pass"])
    lo, hi = eh.wilson(k, n)
    secs = [r["seconds"] for r in runs if r["error"] is None]
    within = [r for r in runs if not r["limit_hit"]]
    # Confidence signals are judged on the runs that decided something:
    # a run that said "unsure" made no claim to be confident about.
    claimed = [r for r in runs if not r["unsure"] and r["error"] is None]
    labels = [r["decided"] for r in claimed]
    signals = {}
    for name in ("self_reported", "agreement", "evidence"):
        pairs = [(r["signals"][name], y) for r, y in zip(claimed, labels, strict=True)
                 if r["signals"][name] is not None]
        signals[name] = auroc([p[0] for p in pairs], [p[1] for p in pairs]) if pairs else None
    by_case: dict[str, dict[str, Any]] = {}
    for r in runs:
        c = by_case.setdefault(r["case"], {"n": 0, "pass": 0, "decisions": {}})
        c["n"] += 1
        c["pass"] += int(r["pass"])
        c["decisions"][r["decision"]] = c["decisions"].get(r["decision"], 0) + 1
    p95 = _pct(secs, 0.95)
    return {
        "n": n, "pass": k, "pass_ci": (lo, hi), "gate": gate,
        "decided": sum(1 for r in runs if r["decided"]),
        "needs_met": sum(1 for r in runs if r["needs_met"]),
        "unsure": sum(1 for r in runs if r["unsure"]),
        "limit_hits": n - len(within), "errors": sum(1 for r in runs if r["error"]),
        "seconds_p50": statistics.median(secs) if secs else None, "seconds_p95": p95,
        "auroc": signals, "cases": by_case,
        "qualifies": bool(hardware != "none" and n and lo >= gate and p95 is not None
                          and p95 <= LIMITS[hardware]["seconds"]),
    }


def format_report(s: dict[str, Any], meta: dict[str, Any]) -> str:
    def rate(k: int) -> str:
        return f"{k}/{s['n']} ({100 * k / s['n']:.0f}%)" if s["n"] else "-"

    lo, hi = s["pass_ci"]
    au = ", ".join(f"{k} {'-' if v is None else f'{v:.2f}'}" for k, v in s["auroc"].items())
    lim = LIMITS[meta["hardware"]]
    limits = (f"{meta['hardware'].upper()} limits ({lim['tool_calls']} tool calls / "
              f"{lim['seconds']:.0f} s)" if meta["hardware"] != "none"
              else f"no time limit ({lim['tool_calls']} tool calls), quality only")
    lines = [
        f"Investigate eval: {meta['model']} on {limits}, {meta['repeat']} repeat(s)",
        "",
        f"  pass        {rate(s['pass'])}   95% CI {lo:.2f}-{hi:.2f}",
        f"  decided     {rate(s['decided'])}   (decision and fields, needs aside)",
        f"  needs met   {rate(s['needs_met'])}",
        f"  unsure      {rate(s['unsure'])}",
        f"  limit hits  {rate(s['limit_hits'])}   errors {s['errors']}",
        f"  seconds     p50 {_fmt(s['seconds_p50'])}  p95 {_fmt(s['seconds_p95'])}",
        f"  AUROC       {au}",
        "",
    ]
    for cid, c in s["cases"].items():
        ds = ", ".join(f"{d} x{n}" for d, n in sorted(c["decisions"].items()))
        lines.append(f"  {cid:<36} {c['pass']}/{c['n']}   {ds}")
    if meta["hardware"] == "none":
        return "\n".join(lines)
    verdict = "QUALIFIES" if s["qualifies"] else "does not qualify"
    lines += ["", (f"  {verdict}: needs the pass rate's lower bound >= {s['gate']:.2f} "
                   f"and p95 within {lim['seconds']:.0f} s.")]
    return "\n".join(lines)


def _sha256(path: str) -> str:
    import hashlib
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def _fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}s"


# ── running it ─────────────────────────────────────────────────────────


async def run(llm: LLMClient, cases: list[dict[str, Any]], *, repeat: int, hardware: str,
              samples: int, model_name: str = "llm",
              rules: dict[str, dict[str, Any]] | None = None,
              progress: Callable[[str], None] = lambda _: None) -> list[dict[str, Any]]:
    runs = []
    # Load the model before the clock starts: a cold load on CPU takes
    # minutes and would count against the first case's seconds.
    await llm.chat(messages=[{"role": "user", "content": "ok"}], max_tokens=1)
    for case in cases:
        for i in range(repeat):
            r = await run_step(llm, case, hardware=hardware, samples=samples,
                               model_name=model_name, rules=rules)
            row = score(case, r)
            row["repeat"] = i
            runs.append(row)
            progress(f"{case['id']} #{i + 1}: {row['decision']}"
                     f" {'PASS' if row['pass'] else 'fail'} {row['seconds']:.1f}s"
                     + (f" ({row['limit_hit']} limit)" if row["limit_hit"] else ""))
    return runs


def rescore(saved: dict[str, Any], cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score a saved run again against the current case file, so fixing a
    case's expected answer does not mean running the model again. Runs of
    cases no longer in the file are dropped."""
    by_id = {c["id"]: c for c in cases}
    out = []
    for row in saved["runs"]:
        case = by_id.get(row["case"])
        if case is None:
            continue
        r = StepResult(
            decision=row["decision"], fields=row["fields"], reason=row["reason"],
            self_reported=row["signals"]["self_reported"], unsure_reason=row["unsure_reason"],
            calls=row["tool_calls"], model_calls=row["model_calls"], seconds=row["seconds"],
            limit_hit=row["limit_hit"], samples=row["samples"],
            agreement=row["signals"]["agreement"], error=row["error"])
        out.append({**score(case, r), "repeat": row.get("repeat", 0)})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--model")
    ap.add_argument("--api", choices=["ollama", "openai"], default="ollama")
    ap.add_argument("--api-key")
    ap.add_argument("--hardware", choices=sorted(LIMITS), default="cpu")
    ap.add_argument("--cases", default=str(pathlib.Path(__file__).with_name("investigate_cases.yml")))
    ap.add_argument("--only", action="append", default=[], help="case id (repeatable)")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--samples", type=int, default=3,
                    help="decision draws for the agreement signal (1 = off)")
    ap.add_argument("--think", choices=["on", "off", "default"], default="off")
    ap.add_argument("--num-thread", type=int)
    ap.add_argument("--gate", type=float, default=0.8)
    ap.add_argument("--rules", metavar="RULES_YML",
                    help="rule tables (tools/investigate_rules.yml): the model reports facts "
                         "and the template's rules decide")
    ap.add_argument("--out")
    ap.add_argument("--rescore", metavar="RUN_JSON",
                    help="score a saved --out file against the current cases; no model is called")
    a = ap.parse_args(argv)

    cases = load_cases(a.cases)
    rules = load_rules(a.rules) if a.rules else None
    if rules:
        for c in cases:
            spec_for(c, rules)      # a case that cannot take its table fails now
    if a.only:
        cases = [c for c in cases if c["id"] in a.only]
        if not cases:
            ap.error("no case matches --only")
    if a.rescore:
        saved = json.loads(pathlib.Path(a.rescore).read_text())
        meta = saved["meta"]
        runs = rescore(saved, cases)
        print(format_report(summarise(runs, hardware=meta["hardware"], gate=a.gate), meta))
        return 0
    if not a.model:
        ap.error("--model is required (or --rescore a saved run)")
    if a.api == "ollama":
        think = {"on": True, "off": False, "default": None}[a.think]
        llm: Any = OllamaChat(a.url, model=a.model, think=think, num_thread=a.num_thread)
    else:
        llm = OpenAIChat(a.url, model=a.model, api_key=a.api_key)

    async def go() -> list[dict[str, Any]]:
        async with llm:
            return await run(llm, cases, repeat=a.repeat, hardware=a.hardware,
                             samples=a.samples, model_name=a.model, rules=rules,
                             progress=lambda m: print(m, file=sys.stderr, flush=True))

    runs = asyncio.run(go())
    summary = summarise(runs, hardware=a.hardware, gate=a.gate)
    meta = {"model": a.model, "hardware": a.hardware, "repeat": a.repeat,
            "samples": a.samples, "engine": "opennvr_app_sdk.agent.run_investigate",
            "rules": a.rules, "rules_sha256": _sha256(a.rules) if a.rules else None,
            "think": a.think,
            "cases": a.cases}
    print(format_report(summary, meta))
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps(
            {"meta": meta, "summary": summary, "runs": runs}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
