# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""``opennvr-app ai try``: run a workflow's Look step on your own pictures.

Write the step as it goes in a workflow file (RFC #620), point it at a
few snapshots and a local model, and see what it decides, why, which rule
fired and how long it took, before any engine or workflow exists::

    opennvr-app ai try gate.yml snap1.jpg snap2.jpg --model qwen3-vl:2b-instruct

The file is the ``ai:`` step itself, a mapping with an ``ai:`` key, or a
workflow whose first ``ai:`` step under ``then`` is tried. Paths are not
run, so an outcome may leave its path out. The step is checked exactly
as the engine checks it, and runs through the same
:func:`~opennvr_app_sdk.workflows.run_ai_step`.

Only Look: an Investigate step needs the site's tools, which only the
engine has. On CPU, a vision model encodes every pixel it is sent, and a
1080p snapshot alone can use most of a step's 120 s; ``--max-side``
(768 by default) shrinks each picture first when Pillow is installed.
"""
from __future__ import annotations

import asyncio
import io
import json
import pathlib
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

import yaml

from .agent import UNSURE, LLMClient, LLMError, OllamaChat, OpenAIChat
from .workflows import AIStep, AIStepError, run_ai_step

#: The long side a picture is shrunk to by default: on CPU, 768 px kept a
#: Look step at a p95 of 52 s where full 1080p ran out of 120 s
#: (examples/camera-agent/MODELS_AND_LATENCY.md, the public night-yard cases).
DEFAULT_MAX_SIDE = 768


class TryError(ValueError):
    """A step or a picture that cannot be tried; the message says why."""


def load_step(path: str | pathlib.Path) -> AIStep:
    """The Look step in ``path``, checked as the engine checks it."""
    path = pathlib.Path(path)
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as err:
        raise TryError(f"{path}: {err}") from err
    raw, where = _find_step(doc, path)
    if raw.get("mode") == "investigate":
        raise TryError(f"{where}: `ai try` runs Look steps; an Investigate step needs the "
                       "site's tools, which only the engine has")
    raw = dict(raw)
    for outcome in [*(raw.get("decision") or []), UNSURE]:
        if isinstance(outcome, str):
            raw.setdefault(outcome, [])          # paths are not run here
    try:
        return AIStep.from_dict(raw, where=where)
    except AIStepError as err:
        raise TryError("\n".join(err.problems)) from err


def _find_step(doc: Any, path: pathlib.Path) -> tuple[dict[str, Any], str]:
    if isinstance(doc, dict) and "mode" in doc:
        return doc, "ai"
    if isinstance(doc, dict) and isinstance(doc.get("ai"), dict):
        return doc["ai"], "ai"
    if isinstance(doc, dict) and isinstance(doc.get("then"), list):
        for i, step in enumerate(doc["then"]):
            if isinstance(step, dict) and isinstance(step.get("ai"), dict):
                return step["ai"], f"then[{i}].ai"
    raise TryError(f"{path}: no AI step found (a step with `mode:`, an `ai:` key, "
                   "or a workflow with an `ai:` step under `then`)")


def parse_inputs(pairs: Sequence[str]) -> dict[str, str]:
    """``["plate=KA01AB1234"]`` -> ``{"plate": "KA01AB1234"}``: the event's
    values, which the step sees as data, never as instructions."""
    out = {}
    for p in pairs:
        key, sep, value = p.partition("=")
        if not sep or not key.strip():
            raise TryError(f"--input {p!r}: expected name=value")
        out[key.strip()] = value.strip()
    return out


def shrink(image: bytes, max_side: int | None) -> tuple[bytes, bool]:
    """``(picture, shrunk)``: the picture with its long side at most
    ``max_side`` px, as JPEG. Unchanged when it is small enough, when
    ``max_side`` is falsy, or when Pillow is not installed."""
    if not max_side:
        return image, False
    try:
        from PIL import Image
    except ImportError:
        return image, False
    im = Image.open(io.BytesIO(image))
    if max(im.size) <= max_side:
        return image, False
    im = im.convert("RGB")
    im.thumbnail((max_side, max_side))
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=90)
    return out.getvalue(), True


def _pillow() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


async def try_step(step: AIStep, client: LLMClient, images: Sequence[str | pathlib.Path], *,
                   inputs: dict[str, str] | None = None, hardware: str = "cpu",
                   model_name: str = "llm", max_side: int | None = DEFAULT_MAX_SIDE,
                   on_result: Callable[[dict[str, Any]], None] = lambda _: None
                   ) -> list[dict[str, Any]]:
    """Run ``step`` on each picture in turn; one result dict per picture."""
    # Load the model before the clock starts, as it is loaded in a workflow.
    await client.chat(messages=[{"role": "user", "content": "ok"}], max_tokens=1)
    out = []
    for path in images:
        picture, shrunk = shrink(pathlib.Path(path).read_bytes(), max_side)
        t0 = time.monotonic()
        r = await run_ai_step(step, client, inputs=inputs or {}, images=[picture],
                              hardware=hardware, model_name=model_name)
        row = r.row()
        trace = r.trace_rows()
        raw = next((t["result"] for t in reversed(trace) if t["kind"] == "llm"), "")
        res = {"image": str(path), "shrunk": shrunk, "outcome": r.outcome,
               "fields": row["fields"], "reason": row["reason"], "rule": row["rule"],
               "confidence": row["confidence"], "unsure_reason": row["unsure_reason"],
               "proposed": row["proposed"], "seconds": round(time.monotonic() - t0, 1),
               "model_calls": row["usage"]["model_calls"],
               "raw": raw[:500] if row["unsure_reason"] == "invalid_output" else None}
        out.append(res)
        on_result(res)
    return out


def format_result(res: dict[str, Any]) -> str:
    facts = "  ".join(f"{k}={v}" for k, v in res["fields"].items())
    rule = f"  (rule {res['rule']})" if res["rule"] else ""
    lines = [f"{pathlib.Path(res['image']).name}: {res['outcome']}"
             + (f"  {facts}" if facts else "") + rule + f"  {res['seconds']:.1f} s"]
    if res["reason"]:
        lines.append(f"  reason: {res['reason']}")
    if res["unsure_reason"]:
        why = {"below_floor": f"the draws did not agree enough (it proposed {res['proposed']})",
               "invalid_output": "the model's answer was not one the step allows",
               "llm_down": "the model could not be reached",
               "over_budget": "the step ran out of time",
               "no_rule": "the facts matched none of the step's rules"}.get(
                   res["unsure_reason"], res["unsure_reason"])
        lines.append(f"  unsure: {why}")
        if res["raw"]:
            lines.append(f"  the model said: {res['raw'][:200]}")
    return "\n".join(lines)


def run_try(args: Any, *, client: LLMClient | None = None) -> int:
    """The ``opennvr-app ai try`` command."""
    try:
        step = load_step(args.step)
        inputs = parse_inputs(args.input)
        missing = [p for p in args.images if not pathlib.Path(p).is_file()]
        if missing:
            raise TryError(f"no picture at {', '.join(missing)}")
    except TryError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    max_side = args.max_side or None
    if max_side and not _pillow():
        print("note: Pillow is not installed, so pictures go at full size; on CPU a 1080p "
              "picture can use most of the step's time (pip install pillow).", file=sys.stderr)
    if client is None:
        if args.api == "ollama":
            client = OllamaChat(args.url, model=args.model, think=False)
        else:
            client = OpenAIChat(args.url, model=args.model, api_key=args.api_key)
    show = (lambda r: None) if args.json else (lambda r: print(format_result(r) + "\n"))

    async def go() -> list[dict[str, Any]]:
        async with client:
            return await try_step(step, client, args.images, inputs=inputs,
                                  hardware=args.hardware, model_name=args.model,
                                  max_side=max_side, on_result=show)

    if not args.json:
        limits = step.limits(args.hardware)
        print(f"Look step, {args.model} on {args.hardware.upper()} "
              f"({limits.seconds:.0f} s a step); outcomes {[*step.spec.options, UNSURE]}\n")
    try:
        results = asyncio.run(go())
    except (LLMError, TimeoutError, OSError) as err:
        print(f"error: the model at {args.url} did not answer ({err}). Is it running, "
              f"and is {args.model} pulled?", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(results, indent=1))
    return 0


def add_parser(sub: Any) -> None:
    """``opennvr-app ai try``, added to the command's subparsers."""
    ai = sub.add_parser("ai", help="try a workflow's AI step on your own pictures")
    ai_sub = ai.add_subparsers(dest="ai_command", required=True)
    t = ai_sub.add_parser("try", help="run a Look step on pictures with a local model",
                          description="Run a workflow's Look step on your own pictures with "
                                      "a local model: what it decides, why, which rule fired "
                                      "and how long it took.")
    t.add_argument("step", help="a YAML file: the ai: step, or a workflow containing one")
    t.add_argument("images", nargs="+", help="JPEG or PNG snapshots")
    t.add_argument("--model", required=True, help="a vision model, e.g. qwen3-vl:2b-instruct")
    t.add_argument("--url", default="http://localhost:11434", help="the model server")
    t.add_argument("--api", choices=("ollama", "openai"), default="ollama")
    t.add_argument("--api-key", default=None)
    t.add_argument("--hardware", choices=("cpu", "gpu"), default="cpu",
                   help="the limits and agreement draws the step gets (default: cpu)")
    t.add_argument("--input", action="append", default=[], metavar="NAME=VALUE",
                   help="an event value the step sees as data, e.g. plate=KA01AB1234")
    t.add_argument("--max-side", type=int, default=DEFAULT_MAX_SIDE, metavar="PX",
                   help=f"shrink pictures to this long side first (default {DEFAULT_MAX_SIDE}; "
                        "0 = send as is; needs Pillow)")
    t.add_argument("--json", action="store_true", help="print the results as JSON")
