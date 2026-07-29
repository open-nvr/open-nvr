# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The agent brain: routing → tools → LLM tool-calling loop.

One ``AgentBrain`` instance serves every surface (the /ask chat endpoint and
the /voice endpoint use the same instance). Behaviour:

* Clearly-visual questions take the deterministic fast-path straight to the
  ``look_at_camera`` tool — a 3B model is unreliable at picking the vision
  tool over competitors, and skipping the extra LLM round-trip is also lower
  latency.
* Otherwise, if the LLM adapter is healthy the model runs a bounded
  tool-calling loop over the fixed registry.
* If the LLM adapter is down, the deterministic router still answers camera
  commands (list/status/time) so the agent degrades instead of dying.
"""
from __future__ import annotations

import json
import re
import time

from adapter_clients import LlmClient, VlmClient
from context import CameraContext
from routing import IntentRouter
from tools import ToolContext, ToolExecutor, ToolRegistry, register_tools

import logging

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "You are a local voice assistant for a camera-monitoring (NVR) system. "
    "Answer in one or two short spoken sentences -- no markdown, no lists, no filler. "
    "You cannot see camera images yourself: to answer anything visual (what is "
    "visible, who/what is there, is a room empty, what someone holds/wears, is a "
    "camera's view clear), call look_at_camera. Use the other tools for status and "
    "time. Recording is ALWAYS ON (24/7, automatic) for every camera and CANNOT be "
    "started, stopped, enabled, or disabled -- if asked to start/stop/enable/disable "
    "recording, just say recording is always on. Prefer tools over guessing. Never "
    "invent camera observations and never claim an action succeeded unless the tool "
    "result says ok. If a tool reports an error, tell the user briefly. State "
    "uncertainty plainly. When only one camera exists and the user says 'the "
    "camera', use it. You cannot act AFTER replying, so never say 'please wait', "
    "'one moment', or promise to check something later -- call the needed tool NOW "
    "and answer in the same turn. Never mention tool names, function calls, or "
    "camera_id values to the user -- speak plain results only."
)

# A small model sometimes SAYS it will check ("please wait while I get that")
# without emitting a tool call — which ends the turn with nothing done. When a
# tool-less reply sounds like that, we nudge once and give it another round.
_UNFINISHED_RE = re.compile(
    r"\b(please wait|one moment|hold on|just a (moment|second)|"
    r"let me (check|look|get|see)|i(?:'|’)?ll (check|look|get)|"
    r"while i (get|check|look)|checking now)\b",
    re.IGNORECASE,
)
NUDGE_MESSAGE = (
    "Do not describe what you are going to do. Use your tools yourself right "
    "now, then reply with only the final spoken answer for the user."
)

# …and sometimes it NARRATES the tool call to the user instead of making it
# ("Call look_at_camera with camera_id 'cp2s' to see what is visible").
# Tool names and argument names must never surface in a spoken answer.
META_NUDGE_MESSAGE = (
    "Never tell the user about tools, functions, or camera_id. Use the tool "
    "yourself and reply with only the plain final answer."
)


def sounds_unfinished(text: str) -> bool:
    return bool(_UNFINISHED_RE.search(text or ""))


def mentions_tools(text: str, tool_names: list[str]) -> bool:
    low = (text or "").lower()
    if "camera_id" in low or "tool" in low or "function" in low:
        return True
    return any(name.lower() in low for name in tool_names)


class AgentBrain:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.context = CameraContext(
            cameras=cfg.cameras,
            opennvr_cameras_url=cfg.opennvr_cameras_url,
            opennvr_api_key=cfg.opennvr_api_key,
            frame_cache_ttl_seconds=cfg.frame_cache_ttl_seconds,
        )
        self.vlm = VlmClient(cfg.vlm_url, cfg.adapter_token,
                             timeout_s=cfg.adapter_timeout_seconds)
        self.llm = LlmClient(cfg.llm_url, cfg.adapter_token,
                             timeout_s=cfg.adapter_timeout_seconds)
        self.registry = ToolRegistry()
        register_tools(
            self.registry,
            ToolContext(
                context=self.context,
                vlm=self.vlm,
                multi_frame_count=cfg.multi_frame_count,
                multi_frame_interval_ms=cfg.multi_frame_interval_ms,
                default_camera_fn=self.context.default_camera,
            ),
        )
        self.executor = ToolExecutor(self.registry,
                                     default_timeout_s=cfg.tool_timeout_seconds)
        self.router = IntentRouter(
            known_ids_fn=self.context.known_ids,
            default_camera_fn=self.context.default_camera,
            known_names_fn=self.context.known_names,
            min_confidence=cfg.routing_min_confidence,
        )
        self.llm_up = False
        # Short rolling chat history so follow-ups ("but you just said two
        # cameras…") make sense. Kept small on purpose: every entry is
        # re-prefilled by the CPU llama-server on the next turn.
        self._history: list[dict] = []

    def _remember(self, question: str, answer: str) -> None:
        self._history.append({"role": "user", "content": question[:400]})
        self._history.append({"role": "assistant", "content": (answer or "")[:400]})
        del self._history[:-8]   # last 4 exchanges

    # ---- lifecycle ------------------------------------------------------- #
    async def setup(self) -> None:
        try:
            cams = await self.context.list_cameras()
            logger.info("cameras: %s",
                        ", ".join(f"{c.camera_id}({c.state.value})" for c in cams) or "none")
        except Exception as exc:
            logger.warning("camera roster not available yet: %s", exc)
        self.llm_up = await self.llm.health()
        if self.llm_up:
            # Warm the prompt cache so the first real query isn't a cold-start
            # timeout (system prompt + tool schemas are slow to eval cold on CPU).
            try:
                await self.llm.chat([{"role": "user", "content": "ok"}],
                                    max_tokens=1, temperature=0.0)
            except Exception:
                pass
        logger.info("LLM adapter: %s", "up (warmed)" if self.llm_up else "not reachable")

    async def status(self) -> dict:
        return {
            "cameras": self.context.known_ids(),
            "default_camera": self.context.default_camera(),
            "llm": self.llm_up,
            "vlm": await self.vlm.health(),
        }

    async def close(self) -> None:
        for closer in (self.llm.aclose(), self.vlm.aclose(), self.context.aclose()):
            try:
                await closer
            except Exception:
                pass

    # ---- answering ------------------------------------------------------- #
    async def ask(self, text: str) -> str:
        """Answer a user turn, logging the Q, the A, tools used, and latency."""
        logger.info("Q: %s", text)
        t0 = time.monotonic()
        answer = await self._answer(text)
        logger.info("A: %s  (%.1fs)", answer, time.monotonic() - t0)
        return answer

    def _camera_hint(self) -> str:
        names = list(self.context.known_names())
        return f" I have {', '.join(names)}." if names else ""

    async def _answer(self, text: str) -> str:
        decision = await self.router.route(text)
        # A confident clarification ("which camera should I look at?") is a
        # better answer than whatever a 3B model does with an ambiguous
        # camera reference — answer it directly, with the camera names.
        if (decision.route in ("clarification", "reject")
                and decision.clarification
                and decision.confidence >= 0.7):
            answer = decision.clarification
            if "camera" in answer.lower():
                answer = answer + self._camera_hint()
            self._remember(text, answer)
            return answer
        if decision.route == "vision" and decision.camera_id:
            logger.info("route=vision camera=%s (fast-path -> look_at_camera)",
                        decision.camera_id)
            result = await self.executor.execute("look_at_camera", {
                "camera_id": decision.camera_id,
                "question": decision.question,
                "temporal": decision.requires_multiple_frames,
            })
            data = result.to_model_json()
            answer = data.get("answer") or data.get("error") or "I couldn't analyse that view."
            self._remember(text, answer)
            return answer
        if not self.llm_up:
            # One retry per turn: the adapter may have come up since startup.
            self.llm_up = await self.llm.health()
        if self.llm_up:
            answer = await self._ask_llm(text)
        else:
            answer = await self._ask_deterministic(decision)
        self._remember(text, answer)
        return answer

    async def _ask_llm(self, text: str) -> str:
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    *self._history,
                    {"role": "user", "content": text}]
        tools = self.registry.openai_tools()
        nudged = False
        meta_nudged = False
        for _ in range(6):  # bounded tool-calling loop (incl. nudge rounds)
            resp = await self.llm.chat(messages, tools=tools,
                                       temperature=self.cfg.llm_temperature,
                                       max_tokens=self.cfg.llm_max_tokens)
            msg = resp["choices"][0]["message"]
            calls = msg.get("tool_calls")
            if not calls:
                content = (msg.get("content") or "").strip()
                # "Please wait while I check…" with no tool call = a promise
                # the model can't keep. Nudge once and let it actually do it.
                if content and not nudged and sounds_unfinished(content):
                    logger.info("nudging: reply promised action without a tool call (%r)",
                                content[:80])
                    nudged = True
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": NUDGE_MESSAGE})
                    continue
                # "Call look_at_camera with camera_id 'x'…" — narrating the
                # tool instead of using it. Retry once; if it persists, give
                # a useful clarification instead of leaking tool-speak.
                if content and mentions_tools(content, self.registry.names()):
                    if not meta_nudged:
                        logger.info("nudging: reply narrated tools instead of acting (%r)",
                                    content[:80])
                        meta_nudged = True
                        messages.append({"role": "assistant", "content": content})
                        messages.append({"role": "user", "content": META_NUDGE_MESSAGE})
                        continue
                    return ("Which camera should I look at?" + self._camera_hint())
                return content or "(no answer)"
            messages.append(msg)
            for call in calls:
                fn = call["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await self.executor.execute(fn["name"], args)
                logger.info("tool %s(%s) -> %s", fn["name"], args,
                            "ok" if result.ok else f"error: {result.error}")
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": json.dumps(result.to_model_json())})
        return "(stopped after too many tool calls)"

    async def _ask_deterministic(self, decision) -> str:
        if decision.route == "tool" and decision.tool_call:
            result = await self.executor.execute(decision.tool_call.name,
                                                 decision.tool_call.args)
            data = result.to_model_json()
            # Short spoken-friendly phrasing for the common commands.
            if decision.tool_call.name == "list_cameras" and result.ok:
                cams = data.get("cameras", [])
                if not cams:
                    return "No cameras are configured."
                return "I can see " + ", ".join(
                    f"{c['camera_id'].replace('_', ' ')} ({c['state']})" for c in cams
                ) + "."
            if decision.tool_call.name == "get_camera_status" and result.ok:
                return (f"{data['camera_id'].replace('_', ' ')} is {data['state']}"
                        + (" and recording" if data.get("recording") else "") + ".")
            if decision.tool_call.name == "current_time" and result.ok:
                return f"It's {data['spoken']} on {data['date']}."
            return json.dumps(data)
        if decision.route in ("clarification", "reject"):
            return decision.clarification or "Sorry, I didn't understand."
        return ("The language model isn't reachable yet, so I can only answer "
                "camera commands right now.")
