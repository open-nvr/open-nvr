# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""The tools an Investigate step may call, and the rules they obey.

RFC #620: "AI informs and recommends. People and plain rules move
things." So a :class:`Tool` reads, or asks a person; there is no third
kind. Building one with any other ``effect`` is a ``TypeError``, so an
actuator cannot reach an AI step even if a validator upstream has a bug.

A tool's ``mode`` is how it may run: ``auto`` runs, ``ask`` waits for a
person first (the step suspends, see :func:`run_investigate`), and
``unavailable`` is refused. A site's policy and a step's override combine
with :func:`effective_mode`, which only ever tightens.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

Mode = Literal["auto", "ask", "unavailable"]
Effect = Literal["read", "ask_person"]

_MODES: tuple[Mode, ...] = ("auto", "ask", "unavailable")   # loosest to strictest
_EFFECTS: tuple[Effect, ...] = ("read", "ask_person")


@dataclass
class ToolResult:
    """What a tool call returned: text for the model, and whether it worked."""

    content: str
    ok: bool = True

    @classmethod
    def failed(cls, message: str) -> ToolResult:
        return cls(content=f"ERROR: {message}", ok=False)


Handler = Callable[[dict[str, Any]], Awaitable["ToolResult | str"]]


@dataclass(frozen=True)
class Tool:
    """One tool on an Investigate step's list.

    ``parameters`` is the JSON schema of the arguments, as advertised to
    the model; the SDK's shared read-tool definitions supply it for the
    ten read tools (:func:`build_tool_definitions`). ``handler`` gets the
    arguments as the model asked for them and returns a
    :class:`ToolResult` or plain text.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler = field(compare=False)
    mode: Mode = "auto"
    effect: Effect = "read"

    def __post_init__(self) -> None:
        if self.effect not in _EFFECTS:
            raise TypeError(
                f"tool {self.name!r}: effect {self.effect!r} is not allowed; an AI step's "
                "tools read or ask a person, they never move anything")
        if self.mode not in _MODES:
            raise ValueError(f"tool {self.name!r}: mode must be one of {_MODES}")
        if not self.name or not self.description.strip():
            raise ValueError("a tool needs a name and a description")
        if self.parameters.get("type") != "object":
            raise ValueError(f"tool {self.name!r}: parameters must be an object schema")

    @classmethod
    def from_definition(cls, definition: dict[str, Any], handler: Handler,
                        mode: Mode = "auto", effect: Effect = "read") -> Tool:
        """A tool from a function-calling definition, such as one of
        :func:`build_tool_definitions`'s."""
        f = definition["function"]
        return cls(name=f["name"], description=f["description"], parameters=f["parameters"],
                   handler=handler, mode=mode, effect=effect)

    def definition(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def effective_mode(*modes: Mode | None) -> Mode:
    """The strictest of the given modes (a site's policy, a step's
    override, ...). None is no opinion; an unknown mode is treated as
    ``unavailable``, the safe side."""
    rank = 0
    for m in modes:
        if m is None:
            continue
        rank = max(rank, _MODES.index(m) if m in _MODES else len(_MODES) - 1)
    return _MODES[rank]


def check_arguments(schema: dict[str, Any], args: Any) -> str | None:
    """Why ``args`` does not fit the tool's schema, or None when it does.

    Deliberately shallow, with no dependency: an object, the required
    keys present, no unknown keys, and the top-level types and enums
    right. That is what small models get wrong; a handler still checks
    its own arguments."""
    if not isinstance(args, dict):
        return "the arguments must be a JSON object"
    props = schema.get("properties") or {}
    missing = [k for k in schema.get("required") or [] if k not in args]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}"
    unknown = [k for k in args if k not in props]
    if unknown:
        return f"unknown argument(s): {', '.join(unknown)}; allowed: {', '.join(props)}"
    for key, value in args.items():
        problem = _check_value(props[key], value)
        if problem:
            return f"{key}: {problem}"
    return None


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float),
    "boolean": (bool,), "array": (list,), "object": (dict,),
}


def _check_value(prop: dict[str, Any], value: Any) -> str | None:
    want = prop.get("type")
    if want in _TYPES:
        ok = isinstance(value, _TYPES[want]) and not (
            want in ("integer", "number") and isinstance(value, bool))
        if not ok:
            return f"must be {want}, got {type(value).__name__}"
    if "enum" in prop and value not in prop["enum"]:
        return f"must be one of {prop['enum']}"
    items = prop.get("items")
    if want == "array" and isinstance(items, dict) and "enum" in items:
        bad = [v for v in value if v not in items["enum"]]
        if bad:
            return f"{bad} not among {items['enum']}"
    return None
