# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the agent's turns cost, by tier and by tool, from the traces it
already keeps — so latency is readable at GET /turn-stats without pulling
an eval log off the box.

A turn's trace is the ordered list of steps _run_conversation_turn builds:
``{"step": "route", "detail": "tier0 describe_camera"}``, LLM iterations
``{"step": "llm", "ms": …}``, one entry per tool ``{"step": <tool>, "by":
"router" | "model", "ok": bool, "ms": …}`` and the closing ``{"step":
"reply", "detail": <source>}``. record() keeps a compact summary of the
last ``maxlen`` turns in memory (nothing persisted; a restart starts
over), summary() aggregates them.
"""
from __future__ import annotations

import re
import time
from collections import deque
from typing import Any

_TIER_RE = re.compile(r"^tier(\d)")
_NOT_TOOLS = {"route", "prompt", "llm", "reply", "forced", "working", "stt", "tts"}


def _pct(values: list[int], q: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[k]


class TurnStats:
    """A ring of turn summaries and the aggregates over it."""

    def __init__(self, maxlen: int = 1000) -> None:
        self._turns: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.started_at = time.time()

    @staticmethod
    def summarise(trace: list[dict[str, Any]], total_ms: int, source: str | None) -> dict[str, Any]:
        tier: int | None = None
        routed_tool: str | None = None
        hint: str | None = None
        llm_ms = 0
        llm_calls = 0
        tools: list[dict[str, Any]] = []
        reply_source: str | None = None
        for step in trace or ():
            name = str(step.get("step") or "")
            if name == "route":
                detail = str(step.get("detail") or "")
                m = _TIER_RE.match(detail)
                tier = int(m.group(1)) if m else None
                rest = detail.split(" ", 1)[1] if " " in detail else ""
                for tok in rest.split():
                    if tok.startswith("hint="):
                        hint = tok[5:]
                    elif tok not in ("ask",) and routed_tool is None:
                        routed_tool = tok
            elif name == "llm":
                llm_ms += int(step.get("ms") or 0)
                llm_calls += 1
            elif name == "reply":
                reply_source = str(step.get("detail") or "") or None
            elif name and name not in _NOT_TOOLS:
                tools.append({"tool": name, "by": step.get("by"), "ok": step.get("ok", True),
                              "ms": int(step.get("ms") or 0)})
        return {
            "at": time.time(),
            "source": source or "background",
            "tier": tier,
            "routed_tool": routed_tool,
            "hint": hint,
            "tools": tools,
            "llm_ms": llm_ms,
            "llm_calls": llm_calls,
            "tool_ms": sum(t["ms"] for t in tools),
            "total_ms": int(total_ms),
            "reply_source": reply_source,
        }

    def record(self, trace: list[dict[str, Any]], total_ms: int, source: str | None) -> dict[str, Any]:
        row = self.summarise(trace, total_ms, source)
        self._turns.append(row)
        return row

    def summary(self, recent: int = 20) -> dict[str, Any]:
        turns = list(self._turns)
        by_tier: dict[str, Any] = {}
        for key, label in ((0, "tier0"), (1, "tier1"), (2, "tier2"), (None, "none")):
            rows = [t for t in turns if t["tier"] == key]
            if not rows:
                continue
            by_tier[label] = {
                "turns": len(rows),
                "total_ms_p50": _pct([t["total_ms"] for t in rows], 0.5),
                "total_ms_p95": _pct([t["total_ms"] for t in rows], 0.95),
                "llm_ms_p50": _pct([t["llm_ms"] for t in rows], 0.5),
                "tool_ms_p50": _pct([t["tool_ms"] for t in rows], 0.5),
                "llm_calls_mean": round(sum(t["llm_calls"] for t in rows) / len(rows), 2),
            }
        by_tool: dict[str, Any] = {}
        for t in turns:
            for call in t["tools"]:
                d = by_tool.setdefault(call["tool"], {"calls": 0, "failed": 0, "by_router": 0,
                                                       "by_model": 0, "_ms": []})
                d["calls"] += 1
                d["failed"] += 0 if call.get("ok", True) else 1
                d["by_router" if call.get("by") == "router" else "by_model"] += 1
                d["_ms"].append(call["ms"])
        for d in by_tool.values():
            ms = d.pop("_ms")
            d["ms_p50"] = _pct(ms, 0.5)
            d["ms_p95"] = _pct(ms, 0.95)
        sources: dict[str, int] = {}
        replies: dict[str, int] = {}
        for t in turns:
            sources[t["source"]] = sources.get(t["source"], 0) + 1
            replies[t["reply_source"] or "none"] = replies.get(t["reply_source"] or "none", 0) + 1
        return {
            "since": self.started_at,
            "turns": len(turns),
            "by_tier": by_tier,
            "by_tool": dict(sorted(by_tool.items(), key=lambda kv: -kv[1]["calls"])),
            "sources": sources,
            "reply_sources": replies,
            "recent": [
                {k: v for k, v in t.items() if k != "tools"} | {"tools": [c["tool"] for c in t["tools"]]}
                for t in turns[-recent:]
            ],
        }
