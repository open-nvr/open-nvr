#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Look eval: does a vision model answer a workflow's Look step right on a
site's own pictures, inside the hardware class's time?

RFC #620 offers Look everywhere, and its gate follows from Look cases on
real images. A case is one picture from a site's camera, the Look step it
is for (tools/look_templates.yml, written as a workflow's ``ai:`` step)
and the right answer, written by someone who knows. The cases are a CSV,
so they can be kept in a spreadsheet:

    file,template,expect,inputs,note
    gate/0001.jpg,gate_who,delivery; company=swiggy,,orange bag
    yard/0001.jpg,yard_threat,who=guard,,night IR

``expect`` is the decision as a bare word and fields as ``name=value``,
separated by ``;``; give only what the label says (a cab need not name a
company). ``inputs`` are the event's values the step sees as data
(``plate=KA01AB1234``). ``file`` is relative to the CSV, or to --images.

    python tools/look_eval.py --model qwen2.5vl:3b --cases site/cases.csv \\
        --hardware cpu --repeat 3 --out look-qwen2.5vl-cpu.json

Each case runs through the SDK's run_ai_step(), the code a workflow runs
(on GPU it draws the decision three times, on CPU once, as a workflow
does), so the gate measures what ships. A run passes when the decision and
every field the label names are right; "unsure" fails unless the label
says unsure. The gate is the Wilson lower bound of the pass rate (>= 0.80
by default), with p95 inside the hardware class's seconds.

The pictures are a site's own and stay out of the repository; the --out
file holds file names, answers and timings, never image bytes. Tested in
tests/test_look_eval.py with a scripted model.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import math
import pathlib
import statistics
import sys
import time
from collections.abc import Callable
from typing import Any

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import eval_harness as eh
from opennvr_app_sdk.agent import UNSURE, Limits, LLMClient, OllamaChat, OpenAIChat
from opennvr_app_sdk.workflows import AIStep, run_ai_step

HERE = pathlib.Path(__file__).resolve().parent
_COLUMNS = ("file", "template", "expect")
_IMAGE_TYPES = {".jpg", ".jpeg", ".png"}


class CaseError(ValueError):
    pass


def load_templates(path: str | pathlib.Path) -> dict[str, AIStep]:
    """The Look steps by name, each checked as a workflow's ``ai:`` step.
    Outcomes without a path get an empty one: the eval scores decisions."""
    raw = yaml.safe_load(pathlib.Path(path).read_text()) or {}
    out = {}
    for name, step in raw.items():
        if not isinstance(step, dict) or step.get("mode") != "look":
            raise CaseError(f"{name}: a template is a Look step (mode: look)")
        step = dict(step)
        for o in [*(step.get("decision") or []), UNSURE]:
            step.setdefault(o, [])
        out[name] = AIStep.from_dict(step, where=name)
    return out


def parse_pairs(text: str, *, bare: str | None = None) -> dict[str, str]:
    """``"delivery; company=swiggy"`` -> ``{bare: "delivery", "company":
    "swiggy"}``. Values are lower-cased and trimmed, as labels are typed."""
    out: dict[str, str] = {}
    for part in (text or "").split(";"):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        if not sep:
            if bare is None:
                raise CaseError(f"{part!r}: expected name=value")
            key, value = bare, part
        key, value = key.strip(), value.strip()
        if key in out:
            raise CaseError(f"{key!r} given twice")
        out[key] = value.lower() if key != "plate" else value
    return out


def load_cases(path: str | pathlib.Path, templates: dict[str, AIStep], *,
               images: str | pathlib.Path | None = None) -> list[dict[str, Any]]:
    """The CSV's cases, every row checked against its template before any
    model is called: a typo in a label must not cost a night's run."""
    path = pathlib.Path(path)
    base = pathlib.Path(images) if images else path.parent
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in _COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise CaseError(f"{path}: missing column(s) {missing}; need {list(_COLUMNS)}")
        rows = list(reader)
    cases, problems, seen = [], [], set()
    for line, row in enumerate(rows, start=2):        # the header is line 1
        where = f"{path.name}:{line}"
        try:
            cases.append(_case(row, templates, base, where))
        except CaseError as err:
            problems.append(f"{where}: {err}")
            continue
        if cases[-1]["id"] in seen:
            problems.append(f"{where}: {row['file']} is listed twice for {row['template']}")
        seen.add(cases[-1]["id"])
    if problems:
        raise CaseError("\n".join(problems))
    if not cases:
        raise CaseError(f"{path}: no cases")
    return cases


def _case(row: dict[str, str], templates: dict[str, AIStep], base: pathlib.Path,
          where: str) -> dict[str, Any]:
    name = (row.get("template") or "").strip()
    step = templates.get(name)
    if step is None:
        raise CaseError(f"template {name!r} is not one of {sorted(templates)}")
    file = (row.get("file") or "").strip()
    image = base / file
    if not file or not image.is_file():
        raise CaseError(f"no image at {image}")
    if image.suffix.lower() not in _IMAGE_TYPES:
        raise CaseError(f"{file}: a JPEG or PNG")
    expect = parse_pairs(row.get("expect") or "", bare="decision")
    decision = expect.pop("decision", None)
    spec = step.spec
    if decision is not None and decision not in (*spec.options, UNSURE):
        raise CaseError(f"decision {decision!r} is not one of {[*spec.options, UNSURE]}")
    for k, v in expect.items():
        if k not in spec.fields:
            raise CaseError(f"{k!r} is not a field of {name} ({sorted(spec.fields)})")
        if v not in spec.fields[k]:
            raise CaseError(f"{k}={v!r} is not one of {list(spec.fields[k])}")
    if decision is None and spec.rules:
        decision = spec.outcome(expect)       # who=stranger -> alert, by the step's rules
    if decision is None and not expect:
        raise CaseError("expect names nothing to check")
    return {"id": f"{name}:{file}", "template": name, "file": file, "image": str(image),
            "decision": decision, "fields": expect,
            "inputs": parse_pairs(row.get("inputs") or ""),
            "note": (row.get("note") or "").strip()}


# ── scoring ────────────────────────────────────────────────────────────


def score(case: dict[str, Any], got: dict[str, Any]) -> dict[str, Any]:
    decision_ok = case["decision"] is None or got["outcome"] == case["decision"]
    wrong = [k for k, v in case["fields"].items() if got["fields"].get(k) != v]
    return {"case": case["id"], "template": case["template"],
            "expected": case["decision"], "expected_fields": case["fields"],
            "decision": got["outcome"], "fields": got["fields"], "reason": got["reason"],
            "unsure_reason": got["unsure_reason"], "rule": got["rule"],
            "confidence": got["confidence"], "seconds": got["seconds"],
            "error": got.get("error"), "raw": got.get("raw"), "wrong_fields": wrong,
            "pass": decision_ok and not wrong}


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def _tally(runs: list[dict[str, Any]]) -> dict[str, Any]:
    n, k = len(runs), sum(1 for r in runs if r["pass"])
    lo, hi = eh.wilson(k, n)
    secs = [r["seconds"] for r in runs if r["error"] is None]
    confusion: dict[str, dict[str, int]] = {}
    for r in runs:
        exp = _label(r["expected"], r.get("expected_fields") or {})
        got = _label(r["decision"], {k: (r.get("fields") or {}).get(k, "-")
                                     for k in r.get("expected_fields") or {}})
        row = confusion.setdefault(exp, {})
        row[got] = row.get(got, 0) + 1
    return {"n": n, "pass": k, "pass_ci": (lo, hi),
            "unsure": sum(1 for r in runs if r["decision"] == UNSURE),
            "errors": sum(1 for r in runs if r["error"]),
            "seconds_p50": statistics.median(secs) if secs else None,
            "seconds_p95": _pct(secs, 0.95), "confusion": confusion}


def _label(decision: str | None, fields: dict[str, str]) -> str:
    """``alert who=stranger``: the decision and the facts a label names, so
    the confusion table shows a wrong fact even when the outcome is right."""
    return " ".join([str(decision), *(f"{k}={v}" for k, v in sorted(fields.items()))])


def summarise(runs: list[dict[str, Any]], *, hardware: str, gate: float) -> dict[str, Any]:
    out = _tally(runs)
    templates = sorted({r["template"] for r in runs})
    out["templates"] = {t: _tally([r for r in runs if r["template"] == t]) for t in templates}
    limit = Limits.for_hardware(hardware).seconds
    for t in [out, *out["templates"].values()]:
        t["qualifies"] = bool(t["n"] and t["pass_ci"][0] >= gate
                              and t["seconds_p95"] is not None and t["seconds_p95"] <= limit)
    out.update(gate=gate, seconds_limit=limit)
    return out


def format_report(s: dict[str, Any], meta: dict[str, Any]) -> str:
    def line(name: str, t: dict[str, Any]) -> str:
        lo, hi = t["pass_ci"]
        pct = f"{100 * t['pass'] / t['n']:.0f}%" if t["n"] else "-"
        return (f"  {name:<14} {t['pass']}/{t['n']} ({pct})  95% CI {lo:.2f}-{hi:.2f}  "
                f"unsure {t['unsure']}  p50 {_fmt(t['seconds_p50'])} "
                f"p95 {_fmt(t['seconds_p95'])}  {'QUALIFIES' if t['qualifies'] else 'no'}")

    lines = [(f"Look eval: {meta['model']} on {meta['hardware'].upper()} "
              f"({s['seconds_limit']:.0f} s a step), {meta['repeat']} repeat(s)"
              + (f", pictures at most {meta['max_side']} px" if meta.get("max_side") else "")),
             "",
             line("all", s)]
    for name, t in s["templates"].items():
        lines.append(line(name, t))
    for name, t in s["templates"].items():
        lines += ["", f"  {name}: expected -> answered"]
        for exp, got in sorted(t["confusion"].items()):
            lines.append(f"    {exp:<28} " + ", ".join(f"{d} x{n}" for d, n in sorted(got.items())))
    lines += ["", (f"  The gate: the pass rate's lower bound >= {s['gate']:.2f} "
                   f"and p95 within {s['seconds_limit']:.0f} s.")]
    return "\n".join(lines)


def _fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}s"


# ── running it ─────────────────────────────────────────────────────────


def shrink(image: bytes, max_side: int | None) -> bytes:
    """The picture with its long side at most ``max_side`` pixels, as JPEG.
    A vision model encodes every pixel it is sent: on CPU a 1080p snapshot
    alone can use most of a step's seconds (MODELS_AND_LATENCY.md)."""
    if not max_side:
        return image
    from PIL import Image
    im = Image.open(io.BytesIO(image))
    if max(im.size) <= max_side:
        return image
    im = im.convert("RGB")
    im.thumbnail((max_side, max_side))
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=90)
    return out.getvalue()


async def run_case(llm: LLMClient, step: AIStep, case: dict[str, Any], *, hardware: str,
                   model_name: str, max_side: int | None = None) -> dict[str, Any]:
    image = shrink(pathlib.Path(case["image"]).read_bytes(), max_side)
    t0 = time.monotonic()
    try:
        r = await run_ai_step(step, llm, inputs=case["inputs"], images=[image],
                              hardware=hardware, model_name=model_name)
    except Exception as err:  # noqa: BLE001 - run_ai_step never raises; a harness bug must not stop a night's run
        return {"outcome": None, "fields": {}, "reason": "", "unsure_reason": None,
                "rule": None, "confidence": None, "seconds": time.monotonic() - t0,
                "error": repr(err)}
    row = r.row()
    # What the model said when it was not an answer: the reason to look at.
    raw = next((t["result"][:500] for t in reversed(r.trace_rows()) if t["kind"] == "llm"),
               None) if row["unsure_reason"] == "invalid_output" else None
    return {"outcome": r.outcome, "fields": row["fields"], "reason": row["reason"],
            "unsure_reason": row["unsure_reason"], "rule": row["rule"],
            "confidence": row["confidence"], "seconds": row["usage"]["seconds"], "raw": raw}


async def run(llm: LLMClient, templates: dict[str, AIStep], cases: list[dict[str, Any]], *,
              repeat: int, hardware: str, model_name: str = "llm",
              max_side: int | None = None,
              progress: Callable[[str], None] = lambda _: None) -> list[dict[str, Any]]:
    # Load the model before the clock starts: a cold load counts against
    # nothing in a workflow, which runs with the model resident.
    await llm.chat(messages=[{"role": "user", "content": "ok"}], max_tokens=1)
    runs = []
    for case in cases:
        for i in range(repeat):
            got = await run_case(llm, templates[case["template"]], case, hardware=hardware,
                                 model_name=model_name, max_side=max_side)
            row = {**score(case, got), "repeat": i}
            runs.append(row)
            progress(f"{case['id']} #{i + 1}: {row['decision']} "
                     f"{'PASS' if row['pass'] else 'fail'} {row['seconds']:.1f}s")
    return runs


def rescore(saved: dict[str, Any], cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score a saved run again against the current labels, so correcting a
    label does not mean running the model again."""
    by_id = {c["id"]: c for c in cases}
    out = []
    for row in saved["runs"]:
        case = by_id.get(row["case"])
        if case is not None:
            got = {"outcome": row["decision"], **{k: row[k] for k in (
                "fields", "reason", "unsure_reason", "rule", "confidence", "seconds", "error")}}
            out.append({**score(case, got), "repeat": row.get("repeat", 0)})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", required=True, help="the labels, a CSV (see the module notes)")
    ap.add_argument("--images", help="where the files are, if not beside the CSV")
    ap.add_argument("--templates", default=str(HERE / "look_templates.yml"))
    ap.add_argument("--only", action="append", default=[], help="template name (repeatable)")
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--model")
    ap.add_argument("--api", choices=["ollama", "openai"], default="ollama")
    ap.add_argument("--api-key")
    ap.add_argument("--hardware", choices=["cpu", "gpu"], default="cpu")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--max-side", type=int, metavar="PX",
                    help="shrink each picture so its long side is at most PX before the model "
                         "sees it (as an engine would); off by default")
    ap.add_argument("--think", choices=["on", "off", "default"], default="off")
    ap.add_argument("--num-thread", type=int)
    ap.add_argument("--gate", type=float, default=0.8)
    ap.add_argument("--check", action="store_true",
                    help="check the templates and the cases, then stop; no model is called")
    ap.add_argument("--out")
    ap.add_argument("--rescore", metavar="RUN_JSON",
                    help="score a saved --out file against the current labels; no model is called")
    a = ap.parse_args(argv)

    try:
        templates = load_templates(a.templates)
        cases = load_cases(a.cases, templates, images=a.images)
    except (CaseError, ValueError) as err:
        print(f"look_eval: {err}", file=sys.stderr)
        return 2
    if a.only:
        cases = [c for c in cases if c["template"] in a.only]
        if not cases:
            ap.error("no case matches --only")
    if a.check:
        counts = {t: sum(1 for c in cases if c["template"] == t) for t in sorted(templates)}
        print(f"{len(cases)} cases OK: " + ", ".join(f"{t} {n}" for t, n in counts.items()))
        return 0
    if a.rescore:
        saved = json.loads(pathlib.Path(a.rescore).read_text())
        meta = saved["meta"]
        runs = rescore(saved, cases)
        print(format_report(summarise(runs, hardware=meta["hardware"], gate=a.gate), meta))
        return 0
    if not a.model:
        ap.error("--model is required (or --check, or --rescore a saved run)")
    if a.api == "ollama":
        think = {"on": True, "off": False, "default": None}[a.think]
        llm: Any = OllamaChat(a.url, model=a.model, think=think, num_thread=a.num_thread)
    else:
        llm = OpenAIChat(a.url, model=a.model, api_key=a.api_key)

    async def go() -> list[dict[str, Any]]:
        async with llm:
            return await run(llm, templates, cases, repeat=a.repeat, hardware=a.hardware,
                             model_name=a.model, max_side=a.max_side,
                             progress=lambda m: print(m, file=sys.stderr, flush=True))

    runs = asyncio.run(go())
    summary = summarise(runs, hardware=a.hardware, gate=a.gate)
    meta = {"model": a.model, "hardware": a.hardware, "repeat": a.repeat, "think": a.think,
            "max_side": a.max_side,
            "engine": "opennvr_app_sdk.workflows.run_ai_step", "cases": a.cases,
            "templates": a.templates, "templates_sha256": _sha256(a.templates)}
    print(format_report(summary, meta))
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps(
            {"meta": meta, "summary": summary, "runs": runs}, indent=1, default=str))
    return 0


def _sha256(path: str) -> str:
    import hashlib
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
