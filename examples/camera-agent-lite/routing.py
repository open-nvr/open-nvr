# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic intent routing — the lite agent's latency fast-path.

Stage 1 (deterministic, ~microseconds): keyword/command/camera-reference
rules. Clearly-visual questions go straight to ``look_at_camera`` without an
LLM round-trip, and camera commands (list/status/time) still work when the
LLM adapter is down. An optional stage 2 (a small-LLM router) can be plugged
in via ``llm_router`` for low-confidence turns; without it the best
deterministic guess stands.

Latency rule: NEVER call the LLM just to discover whether vision is needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import logging

logger = logging.getLogger(__name__)


# ── Keyword / pattern tables ───────────────────────────────────────

VISION_TERMS = {
    "see", "seeing", "visible", "wearing", "holding", "happening", "room",
    "entrance", "door", "person", "people", "someone", "anyone", "standing",
    "sitting", "screen", "object", "empty", "look", "looking", "front",
    "carrying", "colour", "color",
}
VISION_PHRASES = [
    "what do you see", "what is in front of", "looking at", "what is happening",
    "what's happening", "is anyone", "is someone", "is the room", "near the",
    "how many people", "what is the person",
]

# Temporal / motion terms -> may need multiple frames.
MOTION_TERMS = {
    "move", "moving", "moved", "movement", "direction", "enter", "entered",
    "entering", "exit", "exited", "leaving", "left", "falling", "fell",
    "walking", "running", "approaching", "toward", "towards",
}

# Direct application commands -> tool route (regex, first match wins).
# NOTE: recording is always-on 24/7 in OpenNVR and cannot be toggled, so there
# are no start/stop-recording commands here.
COMMAND_PATTERNS: list[tuple[str, str]] = [
    (r"\b(?:list|show|which)\s+cameras?\b", "list_cameras"),
    (r"\bcamera\s+status\b|\bstatus\s+of\s+(?:the\s+)?camera\b", "get_camera_status"),
    (r"\bis\s+camera\b.*\b(online|offline|up|down|working)\b", "get_camera_status"),
]

# Direct system tools.
SYSTEM_PATTERNS: list[tuple[str, str]] = [
    (r"\b(current\s+time|what\s+time|the\s+time)\b", "current_time"),
    (r"\b(current\s+date|what\s+(?:day|date)|today'?s\s+date)\b", "current_time"),
    (r"\b(system\s+status|how\s+are\s+you\s+doing|health)\b", "system_status"),
]

_WORD_RE = re.compile(r"[a-zA-Z']+")


def tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


def vision_score(text: str) -> float:
    # Only actual visual cues count -- a bare "camera N" mention is NOT a
    # vision signal (e.g. "is camera one online" must not route to the VLM).
    t = (text or "").lower()
    toks = tokens(t)
    hits = len(toks & VISION_TERMS)
    phrase_hits = sum(1 for p in VISION_PHRASES if p in t)
    score = hits + 2 * phrase_hits
    return min(1.0, score / 3.0)


def needs_multiple_frames(text: str) -> bool:
    return bool(tokens(text) & MOTION_TERMS)


def match_command(text: str) -> Optional[str]:
    t = (text or "").lower()
    for pattern, tool in COMMAND_PATTERNS:
        if re.search(pattern, t):
            return tool
    return None


def match_system(text: str) -> Optional[str]:
    t = (text or "").lower()
    for pattern, tool in SYSTEM_PATTERNS:
        if re.search(pattern, t):
            return tool
    return None


# ── Camera reference parsing ───────────────────────────────────────

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
}

# 'camera 2', 'camera two', 'cam number 3', 'camera #4'
_CAM_RE = re.compile(
    r"\b(?:camera|cam)\s*(?:number|no\.?|#)?\s*"
    r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"first|second|third|fourth|fifth)\b",
    re.IGNORECASE,
)
_BARE_CAM_RE = re.compile(r"\b(?:the\s+)?(?:camera|cam)\b", re.IGNORECASE)


@dataclass
class CameraReference:
    camera_id: Optional[str]      # e.g. 'camera_2', or None if unresolved
    number: Optional[int]         # parsed camera number, if any
    bare: bool                    # user said 'the camera' with no number


def parse_camera_reference(
    text: str, known_ids: Optional[list[str]] = None
) -> CameraReference:
    m = _CAM_RE.search(text or "")
    if m:
        token = m.group(1).lower()
        num = int(token) if token.isdigit() else _NUM_WORDS.get(token)
        if num is not None:
            candidate = f"camera_{num}"
            if known_ids is None or candidate in known_ids:
                return CameraReference(camera_id=candidate, number=num, bare=False)
            return CameraReference(camera_id=None, number=num, bare=False)
    if _BARE_CAM_RE.search(text or ""):
        return CameraReference(camera_id=None, number=None, bare=True)
    return CameraReference(camera_id=None, number=None, bare=False)


# ── Tool-call building ─────────────────────────────────────────────


@dataclass
class ToolCall:
    name: str
    args: dict = field(default_factory=dict)


# Tools that require a camera_id argument.
_NEEDS_CAMERA = {"get_camera_status"}


def build_tool_call(
    tool_name: str,
    text: str,
    known_ids: Optional[list[str]] = None,
    default_camera: Optional[str] = None,
) -> tuple[Optional[ToolCall], Optional[str]]:
    """Return (ToolCall, None) or (None, clarification_reason)."""
    args: dict = {}

    if tool_name in _NEEDS_CAMERA:
        ref = parse_camera_reference(text, known_ids)
        cam = ref.camera_id or (default_camera if ref.bare else None)
        if cam is None:
            if ref.number is not None:
                return None, f"camera_{ref.number} is not available"
            return None, "which camera?"
        args["camera_id"] = cam

    return ToolCall(name=tool_name, args=args), None


# ── The router ─────────────────────────────────────────────────────

# route is one of: text | vision | tool | clarification | reject
LlmRouterFn = Callable[[str], Awaitable[dict]]


@dataclass
class RouteDecision:
    route: str
    question: str
    camera_id: Optional[str] = None
    requires_multiple_frames: bool = False
    tool_call: Optional[ToolCall] = None
    confidence: float = 1.0
    clarification: Optional[str] = None
    stage: str = "deterministic"       # deterministic | llm


class IntentRouter:
    def __init__(
        self,
        known_ids_fn: Callable[[], list[str]],
        default_camera_fn: Callable[[], Optional[str]],
        *,
        min_confidence: float = 0.6,
        llm_router: Optional[LlmRouterFn] = None,
    ) -> None:
        self._known_ids_fn = known_ids_fn
        self._default_camera_fn = default_camera_fn
        self._min_confidence = min_confidence
        self._llm_router = llm_router

    async def route(self, text: str) -> RouteDecision:
        text = (text or "").strip()
        if not text:
            return RouteDecision("reject", text, confidence=1.0,
                                 clarification="I didn't catch that.")

        decision = self._stage1(text)
        if decision.confidence >= self._min_confidence:
            return decision

        if self._llm_router is not None:
            llm_decision = await self._stage2(text)
            if llm_decision is not None:
                return llm_decision
        return decision  # best deterministic guess

    # ---- stage 1 --------------------------------------------------------- #
    def _stage1(self, text: str) -> RouteDecision:
        known = self._known_ids_fn()
        default_cam = self._default_camera_fn()

        # 1) system tools (time/date/status) - unambiguous
        sys_tool = match_system(text)
        if sys_tool:
            call, _ = build_tool_call(sys_tool, text, known, default_cam)
            return RouteDecision("tool", text, tool_call=call, confidence=0.95)

        # 2) explicit application commands
        cmd_tool = match_command(text)
        if cmd_tool:
            call, clar = build_tool_call(cmd_tool, text, known, default_cam)
            if call is None:
                return RouteDecision("clarification", text, confidence=0.9,
                                     clarification=clar)
            return RouteDecision("tool", text, tool_call=call,
                                 camera_id=call.args.get("camera_id"), confidence=0.9)

        # 3) vision -- requires an actual visual cue (vscore>0), not just a
        #    camera mention, so "is camera one online" doesn't hit the VLM.
        vscore = vision_score(text)
        ref = parse_camera_reference(text, known)
        cam = ref.camera_id or (default_cam if (ref.bare or vscore >= 0.66) else None)
        if vscore > 0 and (vscore >= 0.66 or ref.number is not None or ref.bare):
            if cam is None and ref.number is not None:
                return RouteDecision("clarification", text, confidence=0.85,
                                     clarification=f"camera_{ref.number} is not available")
            if cam is None:
                cam = default_cam
            if cam is None:
                return RouteDecision("clarification", text, confidence=0.7,
                                     clarification="which camera should I look at?")
            return RouteDecision(
                "vision", text, camera_id=cam,
                requires_multiple_frames=needs_multiple_frames(text),
                confidence=max(0.7, vscore),
            )

        # 4) low-confidence -> let stage 2 decide (text is the safe default)
        conf = 0.55 if vscore > 0 else 0.65
        return RouteDecision("text", text, confidence=conf)

    # ---- stage 2 --------------------------------------------------------- #
    async def _stage2(self, text: str) -> Optional[RouteDecision]:
        try:
            data = await self._llm_router(text)
        except Exception as exc:
            logger.warning("LLM router failed, keeping deterministic route: %s", exc)
            return None
        route = data.get("route", "text")
        if route not in {"text", "vision", "tool", "clarification", "reject"}:
            route = "text"
        cam = data.get("camera_id")
        known = self._known_ids_fn()
        if cam and known and cam not in known:
            cam = self._default_camera_fn()
        return RouteDecision(
            route=route,
            question=data.get("question", text),
            camera_id=cam,
            requires_multiple_frames=bool(data.get("requires_multiple_frames", False)),
            confidence=0.75,
            stage="llm",
        )
