# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tool implementations the camera-agent exposes to the LLM.

Each tool is a coroutine ``handler(args: dict) -> str`` that takes the
arguments the LLM emitted and returns a short text string the LLM
will read back as a ``role: "tool"`` message. The string should be
plain prose, not JSON — the LLM consumes it as natural language and
then phrases the answer to the user.

Four tools:

* ``describe_camera`` — BLIP scene caption on a live frame.
* ``detect_objects`` — YOLOv8 object detection on a live frame.
* ``recognize_faces`` — InsightFace recognition on a live frame.
* ``recent_events`` — recent inference events from the NATS ring
  buffer (no live inference — answers "what happened earlier?").

All four use ``CameraContext`` for shared frame caching + camera
metadata + the event ring.
"""
from __future__ import annotations

import base64
import logging
import math
import time
from typing import Any

from adapter_clients import KaicAdapterClient
from context import CameraContext
from frame_sources import FrameSourceError
# Tier-0 consumption helpers live in the App SDK so every app shares one
# implementation (best-frame fetch + event snapshot); re-exported here so
# ``from tools import make_best_frame_fetch`` keeps working for the agent.
from opennvr_app_sdk import make_best_frame_fetch, snapshot_from_event  # noqa: F401

logger = logging.getLogger(__name__)


# ── Tool definitions in OpenAI / Pipecat function-calling shape ────


def build_tool_definitions(
    cameras: list[str], enabled: list[str] | None = None
) -> list[dict[str, Any]]:
    """Build the OpenAI-style ``tools`` list. Camera IDs are baked
    into the enum so the model can't invent unknown camera names.

    ``enabled`` optionally restricts the exposed tools by name. Fewer
    tools mean a shorter prompt (faster CPU prefill) AND fewer wrong-tool
    picks by small models — so the standard demo advertises only the tools
    that actually work (object detection + scene description), instead of
    face recognition / footage search whose adapters aren't registered.
    """
    camera_enum = list(cameras) or ["__no_cameras_configured__"]
    # Cameras can be a single id, "all", or several at once via camera_ids.
    camera_enum_all = camera_enum + ["all"]
    _camera_prop = {
        "type": "string",
        "enum": camera_enum_all,
        "description": "A camera id, or 'all' for every camera.",
    }
    _camera_ids_prop = {
        "type": "array",
        "items": {"type": "string", "enum": camera_enum_all},
        "description": "Optional: several cameras at once, e.g. ['cam1','cam2']. Use instead of camera_id for multiple.",
    }
    all_tools = [
        {
            "type": "function",
            "function": {
                "name": "describe_camera",
                "description": (
                    "Describe what's visible on one camera, several, or all of "
                    "them, OR answer a specific question about the scene. Use "
                    "for 'what's on the porch?', 'what is the person wearing?', "
                    "'what is he doing?', 'is the gate open?'. Pass the user's "
                    "actual question in 'question' so the vision model can answer "
                    "it directly."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_id": _camera_prop,
                        "camera_ids": _camera_ids_prop,
                        "question": {
                            "type": "string",
                            "description": (
                                "Optional: the specific question to answer about "
                                "the scene, e.g. 'what is the person wearing?'."
                            ),
                        },
                    },
                    "required": ["camera_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "camera_snapshot",
                "description": (
                    "Count objects or check presence on one camera, several, or "
                    "all — from the always-on detection stream, WITHOUT running a "
                    "new inference (instant, no model call). PREFER this for "
                    "counting and presence: 'how many people/cars?', 'is anyone "
                    "at the door?', 'is there a package?'. Use describe_camera "
                    "only when the answer needs appearance (colour, clothing, "
                    "what someone is doing)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_id": _camera_prop,
                        "camera_ids": _camera_ids_prop,
                    },
                    "required": ["camera_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "detect_objects",
                "description": (
                    "Detect and count objects (people, cars, packages, "
                    "animals) on one camera, several, or all of them, by running "
                    "a FRESH detection. Prefer camera_snapshot for counts when it "
                    "has data; use this when you need a live re-check. Use for "
                    "'is there a package?' / 'how many people across all cameras?'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_id": _camera_prop,
                        "camera_ids": _camera_ids_prop,
                    },
                    "required": ["camera_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recognize_faces",
                "description": (
                    "Recognize faces on one camera, several, or all of them; "
                    "returns a name if known, else 'unknown'. Use for 'who's "
                    "at the door?'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_id": _camera_prop,
                        "camera_ids": _camera_ids_prop,
                    },
                    "required": ["camera_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_footage",
                "description": (
                    "Search recorded footage for past events with specific "
                    "attributes the live tools can't answer — 'did a red "
                    "truck come by earlier?'. Pass keywords (object + "
                    "descriptors). Returns matches newest-first."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Object + attributes, e.g. ['red', 'truck']."
                            ),
                        },
                        "within_minutes": {
                            "type": "number",
                            "description": (
                                "Minutes back to search. Omit for no limit."
                            ),
                        },
                        "camera_id": {
                            "type": "string",
                            "enum": camera_enum + ["__any__"],
                            "description": "Filter to one camera or '__any__'.",
                        },
                    },
                    "required": ["keywords"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_history",
                "description": (
                    "Search the NVR's MEMORY: past visits of people, cars, "
                    "dogs — any detected object — each with the best photo "
                    "kept. Use for 'did anyone come between 3 and 4pm?', "
                    "'which cars entered today?', 'was a dog here yesterday?'. "
                    "For people it also face-matches the kept photos and "
                    "names anyone recognised."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                            "description": "What to look for: person, car, truck, dog, … (default person).",
                        },
                        "start_time": {
                            "type": "string",
                            "description": "Window start, ISO 8601 WITH timezone offset (e.g. 2026-08-12T15:00:00+05:30). Omit for open start.",
                        },
                        "end_time": {
                            "type": "string",
                            "description": "Window end, ISO 8601 with timezone offset. Omit for 'until now'.",
                        },
                        "camera_id": _camera_prop,
                        "identify_faces": {
                            "type": "boolean",
                            "description": "For person searches: face-match the kept photos (default true).",
                        },
                        "plate": {
                            "type": "string",
                            "description": "Vehicle searches: find visits whose read plate contains this text, e.g. 'KA01' or '1234'.",
                        },
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "describe_event",
                "description": (
                    "Describe WHAT HAPPENED in a past event from its kept "
                    "photo — 'what was the person doing?'. Pass the [#id] "
                    "printed by search_history. Optional question for a "
                    "specific detail (what were they wearing / carrying)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_id": {
                            "type": "integer",
                            "description": "Event id — the [#id] from search_history.",
                        },
                        "question": {
                            "type": "string",
                            "description": "Optional specific question about the kept photo.",
                        },
                    },
                    "required": ["event_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "describe_window",
                "description": (
                    "Describe WHAT SOMEONE WAS DOING over a past time window by "
                    "reviewing the RECORDED FOOTAGE — samples frames across the "
                    "span and narrates the behavior. Use for 'what were they "
                    "doing between 3:12 and 3:16?' after search_history gives "
                    "the span and camera. Heavier than describe_event; for "
                    "detail across a span, not a single moment."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_id": _camera_prop,
                        "start_time": {"type": "string", "description": "Window start, ISO 8601 with tz offset."},
                        "end_time": {"type": "string", "description": "Window end, ISO 8601 with tz offset."},
                        "question": {"type": "string", "description": "Optional specific question about the activity."},
                    },
                    "required": ["camera_id", "start_time", "end_time"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recent_events",
                "description": (
                    "Look back at recent inference events on the cameras. "
                    "Use for 'did anyone come earlier?'. Returns events "
                    "newest-first."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_id": {
                            "type": "string",
                            "enum": camera_enum + ["__any__"],
                            "description": "One camera, or '__any__' for all.",
                        },
                        "window_seconds": {
                            "type": "number",
                            "description": (
                                "How far back, in SECONDS (60=1min, "
                                "3600=1hr)."
                            ),
                        },
                    },
                    "required": ["camera_id", "window_seconds"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recent_plates",
                "description": (
                    "Look back at recently READ license plates (live "
                    "plate.recognized events from the platform's OCR "
                    "chain). Use for 'what plates came today?', 'was "
                    "plate AB12 here?', 'any trucks at the gate this "
                    "hour?'. Newest-first with camera, plate text, and "
                    "confidence. For history beyond the last hours, use "
                    "search_history with a plate filter instead."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "camera_id": {
                            "type": "string",
                            "enum": camera_enum + ["__any__"],
                            "description": "One camera, or '__any__' for all.",
                        },
                        "window_seconds": {
                            "type": "number",
                            "description": (
                                "How far back, in SECONDS (3600=1hr, "
                                "86400=1day)."
                            ),
                        },
                        "plate": {
                            "type": "string",
                            "description": (
                                "Optional: match plates CONTAINING this "
                                "text (case-insensitive), e.g. '234' for "
                                "'ends in 234'. Omit for all plates."
                            ),
                        },
                    },
                    "required": ["camera_id", "window_seconds"],
                },
            },
        },
    ]
    if enabled is None:
        return all_tools
    allow = set(enabled)
    return [t for t in all_tools if t["function"]["name"] in allow]


# ── Tool handlers ──────────────────────────────────────────────────


class CameraTools:
    """Holds references to the context + KAI-C clients and exposes
    one coroutine per tool. Pipecat's LLM service calls these via
    ``register_function``."""

    def __init__(
        self,
        *,
        context: CameraContext,
        caption_client: KaicAdapterClient,
        detection_client: KaicAdapterClient,
        recognition_client: KaicAdapterClient,
        footage_index: Any = None,
        best_frame_fetch: Any = None,
        resolve_camera: Any = None,
        events_client: Any = None,
    ) -> None:
        self._ctx = context
        self._caption = caption_client
        self._detect = detection_client
        self._recognise = recognition_client
        # Map the agent's camera id → the pipeline's camera id (the id on the Tier-0
        # bus subject / best-frame endpoint). MUST match the mapping used to build
        # best_frame_fetch, so camera_snapshot and describe_camera agree on which
        # camera they're reading. Identity by default.
        self._resolve_camera = resolve_camera or (lambda cid: cid)
        # Optional async callable(camera_id) -> jpeg bytes | None. When set,
        # describe_camera runs the VLM on Tier-0's BEST frame (clean, representative)
        # instead of an arbitrary live grab — more accurate and cheaper. None (or a
        # miss) falls back to the live frame, so behaviour is unchanged without it.
        self._best_frame_fetch = best_frame_fetch
        # Optional read-only FootageIndex (footage_index.FootageIndex).
        # When None or unavailable, search_footage reports that cleanly.
        self._footage_index = footage_index
        # Optional SDK EventsClient — the platform's memory (canonical event
        # store). Powers search_history: past visits with best-frame evidence,
        # optionally face-matched. None = tool reports history isn't enabled.
        self._events = events_client
        # Cameras touched by the most recent tool call — read by /converse
        # so the UI can show which camera(s) the agent is working on.
        self.last_cameras_used: list[str] = []
        # Stored best-frame photos this turn dug out of the events store, so
        # an answer about the PAST can show what it is talking about. Kept
        # apart from the live per-turn frame cache on purpose: these are
        # historical, and each carries its own timestamp caption so the UI
        # can never present a photo from 16:25 as the current view.
        self.last_evidence_frames: list[dict] = []
        # Why the last describe fell back from the VLM ("" = it didn't) —
        # surfaced by the system self-check so degradation is visible.
        self.last_vision_error: str | None = None

    # ── describe_camera ────────────────────────────────────────────

    async def describe_camera(self, args: dict[str, Any]) -> str:
        cams = self._resolve_cameras(args)
        if isinstance(cams, str):  # ERROR
            return cams
        # Optional VQA question ("what is he wearing?", "is the gate open?").
        # A vision-language adapter (Moondream / SmolVLM / Qwen-VL) answers it
        # grounded in the frame; a plain captioner (BLIP) ignores it and still
        # returns a scene caption. Either way the agent gets a real answer
        # instead of guessing (test-report S-6).
        question = str(args.get("question") or "").strip() or None
        clauses = [await self._describe_one(c, question) for c in cams]
        return self._join_clauses(clauses)

    async def _best_frame(self, camera_id: str) -> bytes | None:
        """Tier-0's best frame for the camera as JPEG, or None. Best-effort —
        any failure returns None so the caller falls back to a live grab."""
        if self._best_frame_fetch is None:
            return None
        try:
            return await self._best_frame_fetch(camera_id)
        except Exception:
            logger.debug("best-frame fetch failed for %s; using live frame",
                         camera_id, exc_info=True)
            return None

    async def _best_frame_if_active(self, camera_id: str) -> bytes | None:
        """Tier-0's best frame ONLY while a track is currently active on this
        camera; otherwise ``None`` so the caller takes a fresh live look.

        The best frame is a curated crop of a RECENT detection. It is a good,
        cheap stand-in for the live scene while something is actively happening,
        but for a quiet/static scene it is stale — a person sitting still
        produces no fresh track, so the best frame would answer "what do you see
        now" with a PAST moment. Gate it on a fresh Tier-0 event (same freshness
        rule as camera_snapshot); when the scene is static, return None so the
        caller grabs a live frame.
        """
        pipeline_cam = self._resolve_camera(camera_id)
        event = self._ctx.latest_inference(pipeline_cam, adapter="tier0")
        if event is None or (time.time() - event.received_at) > self._SNAPSHOT_MAX_AGE_S:
            return None
        return await self._best_frame(camera_id)

    async def _describe_one(self, camera_id: str, question: str | None = None) -> str:
        self.last_vision_error = None    # per-call: set again only on fallback
        # "What do you see now" must look at a FRESH LIVE frame. Tier-0's best
        # frame is a curated crop of a RECENT detection — a clean, cheap stand-in
        # only while a track is ACTIVE right now; for a quiet/static scene it is a
        # PAST frame (a person sitting still produces no fresh track), so it would
        # describe an earlier moment. Use the best frame only when a Tier-0 track
        # is currently active; otherwise take a live grab.
        frame = await self._best_frame_if_active(camera_id)
        if frame is None:
            try:
                frame = await self._ctx.get_frame(camera_id)
            except LookupError:
                return f"{camera_id} is not configured"
            except FrameSourceError as exc:
                logger.warning("VISION DEGRADED: %s frame fetch failed (camera offline / bad RTSP path?): %s", camera_id, exc)
                return f"{camera_id} appears to be offline"
        # Prefer a real scene caption / VQA answer when the caption adapter is
        # available. Send the task explicitly for symmetry with
        # recognize_faces and so the wire shape is legible in audit logs.
        try:
            # A specific question → ask the adapter to ANSWER it (VQA); an
            # open-ended request → a scene caption. Crucially, do NOT pin
            # task="scene_caption" when there's a question: a VQA adapter
            # (moondream) only answers when the task ISN'T scene_caption, while a
            # pure captioner (BLIP) defaults to captioning when no task is given.
            # So omitting the task lets moondream do VQA and BLIP still caption —
            # otherwise every question got the same generic caption back.
            extra: dict[str, Any] = {}
            if question:
                extra["question"] = question
                extra["prompt"] = question
            else:
                extra["task"] = "scene_caption"
            response = await self._caption.infer(frame_jpeg=frame, extra=extra)
            result = response.get("result") or {}
            # VQA adapters return ``answer``; captioners return ``caption``.
            caption = (result.get("answer") or result.get("caption") or "").strip()
            if caption:
                return f"{camera_id}: {caption}"
        except Exception as exc:
            # No caption adapter reachable (not registered, sovereignty 403,
            # adapter down). Fall back to the object detector — but say so:
            # a fallback that talks like full vision invents details the
            # system never saw (field case: a confident wrong shirt color
            # while the VLM had never received a single request).
            reason = self._vision_error_reason(exc)
            self.last_vision_error = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning(
                "VISION DEGRADED: describe_camera caption adapter unavailable for %s "
                "(%s); falling back to object detection",
                camera_id, reason,
            )
            return await self._describe_via_detection(
                camera_id, frame, degraded_reason=reason
            )
        return await self._describe_via_detection(camera_id, frame)

    @staticmethod
    def _vision_error_reason(exc: Exception) -> str:
        """One short, operator-meaningful phrase for WHY vision is degraded."""
        text = str(exc)
        lowered = text.lower()
        if "await operator" in lowered or "approval" in lowered:
            return ("adapter awaiting operator approval in KAI-C "
                    "(AI models page → grant permissions)")
        if "sovereign" in lowered or "egress" in lowered:
            return "blocked by KAI-C sovereignty policy"
        if "403" in text:
            return "refused by KAI-C (403)"
        if "404" in text or "not registered" in text.lower():
            return "caption adapter not registered with KAI-C"
        # A 503 from KAI-C is the adapter ANSWERING that it isn't ready yet
        # — not a transport failure. Reporting the auto-pull window as
        # "unreachable" sent an operator hunting Docker networking for a
        # model that was simply still downloading; name the real state so
        # the answer is "wait" rather than "debug the network".
        if "model_not_pulled" in lowered or "auto-pull" in lowered:
            return "vision model still downloading (auto-pull running — retry shortly)"
        if "503" in text:
            return "caption adapter warming up (503 — retry shortly)"
        if "timed out" in text.lower() or "timeout" in text.lower():
            return "caption adapter timed out"
        # KAI-C proxies /infer with its own 30s budget: a 502 means KAI-C
        # got no answer from the adapter in time. That is a down adapter OR
        # a VLM too slow for this host (a large model on CPU can take
        # minutes per frame), and the operator needs to know it's both.
        if "502" in text:
            return ("caption adapter gave KAI-C no answer in time "
                    "(adapter down, or the VLM is too slow for this host)")
        return "caption adapter unreachable"

    async def _describe_via_detection(
        self, camera_id: str, frame: bytes, degraded_reason: str | None = None,
    ) -> str:
        """Best-effort scene description built from the object detector,
        used when no caption adapter is available. When this IS a degraded
        fallback, the answer says so — honesty about a missing subsystem
        beats a confident guess."""
        try:
            response = await self._detect.infer(frame_jpeg=frame)
        except Exception:
            logger.exception("describe_camera: detection fallback failed")
            if degraded_reason:
                return (f"{camera_id}: I can't see right now — my vision model "
                        f"is unavailable ({degraded_reason}) and object "
                        f"detection also failed")
            return f"{camera_id}: scene description unavailable right now"
        summary = self._summarize_detections(
            (response.get("result") or {}).get("detections") or []
        )
        if degraded_reason:
            base = (f"I can see {summary}" if summary
                    else "no objects detected")
            return (f"{camera_id}: {base} — note: my vision model is "
                    f"unavailable ({degraded_reason}), so I'm answering from "
                    f"object detection only and can't describe details")
        if not summary:
            return f"{camera_id}: nothing notable visible"
        return f"{camera_id}: I can see {summary}"

    async def _vlm_caption(self, frame: bytes, question: str | None) -> str | None:
        """Run the caption/VQA adapter on a frame; return the text, or None when
        no caption adapter is available or it returns nothing. Same VQA-vs-
        caption selection as describe_camera (a question -> VQA; none -> caption)."""
        extra: dict[str, Any] = {}
        if question:
            extra["question"] = question
            extra["prompt"] = question
        else:
            extra["task"] = "scene_caption"
        try:
            response = await self._caption.infer(frame_jpeg=frame, extra=extra)
        except Exception:
            return None
        result = response.get("result") or {}
        return (result.get("answer") or result.get("caption") or "").strip() or None

    async def describe_event(self, args: dict[str, Any]) -> str:
        """Describe a PAST event from its stored evidence crop — the best frame
        captured WHEN IT HAPPENED. Answers "what was the person doing?" for a
        remembered visit without a live look. Chain after search_history, which
        prints each visit's [#id]."""
        if self._events is None:
            return ("History isn't enabled on this deployment, so I can't look "
                    "back at a past event.")
        try:
            event_id = int(args.get("event_id"))
        except (TypeError, ValueError):
            return ("I need the event's id (the [#id] from a history search) to "
                    "describe it.")
        question = str(args.get("question") or "").strip() or None
        crop = await self._events.evidence(event_id)
        if not crop:
            return f"I don't have a stored photo for event {event_id}."
        caption = await self._vlm_caption(crop, question)
        if caption is None:
            return (f"Event {event_id}: a photo is kept but scene description "
                    "isn't available right now.")
        return f"Event {event_id}: {caption}"

    # describe_window samples this many frames across the requested span.
    _WINDOW_SAMPLES = 5

    async def describe_window(self, args: dict[str, Any]) -> str:
        """Describe WHAT HAPPENED over a past window from the RECORDED FOOTAGE —
        sample a few frames across [start,end] and narrate the behavior over
        time. Heavier than describe_event (one still); use when the user wants
        detail across a span. Chain after search_history (which gives the span
        and camera)."""
        from datetime import datetime as _dt, timedelta as _td

        if self._events is None:
            return ("History isn't enabled on this deployment, so I can't review "
                    "past footage.")
        cam = args.get("camera_id")
        if cam in (None, "", "all", "__any__") or not self._ctx.known_camera(str(cam)):
            return "I need a specific camera to review footage for a time window."
        try:
            server_cam = int(self._resolve_camera(str(cam)))
        except (TypeError, ValueError):
            return f"Camera '{cam}' has no server-side id."
        try:
            start = _dt.fromisoformat(str(args.get("start_time")).replace("Z", "+00:00"))
            end = _dt.fromisoformat(str(args.get("end_time")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return "I need a valid start_time and end_time (ISO 8601) to review footage."
        if end <= start:
            return "The end time must be after the start time."
        self.last_cameras_used = [str(cam)]
        question = str(args.get("question") or "").strip() or None

        n = self._WINDOW_SAMPLES
        step = (end - start).total_seconds() / max(1, n - 1)
        observations: list[tuple[str, str]] = []
        for i in range(n):
            ts = start + _td(seconds=step * i)
            frame = await self._events.recording_frame(server_cam, ts.isoformat())
            if not frame:
                continue
            caption = await self._vlm_caption(frame, question)
            if caption:
                observations.append((self._clock_phrase(ts.isoformat()), caption))
        if not observations:
            return ("I couldn't pull footage for that window — no recording, or "
                    "the frames weren't readable.")
        # Collapse consecutive identical captions into a short narrative.
        lines = []
        last = None
        for when, what in observations:
            if what != last:
                lines.append(f"{when}, {what}")
                last = what
        return "Reviewing the footage: " + "; ".join(lines) + "."

    # Irregular plurals worth getting right for the COCO labels the
    # detector emits most; everything else just takes a trailing 's'.
    _IRREGULAR_PLURALS = {"person": "people", "man": "men", "woman": "women"}

    @classmethod
    def _summarize_detections(cls, detections: list[dict[str, Any]]) -> str:
        """Group identical labels into a short, speakable phrase, e.g.
        'a person, 2 cars'. Capped to keep the spoken reply short.

        Detections are first de-duplicated by IoU per label: the YOLOv8
        adapter doesn't always run NMS, so it can emit several heavily
        overlapping boxes for the SAME object. Counting those raw would
        make the agent say "10 people" when one person is on screen.
        """
        deduped = cls._dedup_detections(detections[:64])
        counts: dict[str, int] = {}
        for det in deduped:
            label = str(det.get("label") or det.get("class") or "?").strip()
            if label:
                counts[label] = counts.get(label, 0) + 1
        parts: list[str] = []
        for label, count in sorted(counts.items()):
            if count == 1:
                article = "an" if label[:1].lower() in "aeiou" else "a"
                parts.append(f"{article} {label}")
            else:
                plural = cls._IRREGULAR_PLURALS.get(label, f"{label}s")
                parts.append(f"{count} {plural}")
        return ", ".join(parts[:8])

    @classmethod
    def _dedup_detections(
        cls, detections: list[dict[str, Any]], iou_threshold: float = 0.55
    ) -> list[dict[str, Any]]:
        """Greedy per-label NMS: drop boxes that overlap an already-kept
        box of the same label by more than ``iou_threshold``."""
        kept: list[dict[str, Any]] = []
        # Highest-confidence first so the survivor of each overlap cluster
        # is the strongest detection.
        ordered = sorted(
            detections,
            key=lambda d: float(d.get("confidence") or d.get("score") or 0.0),
            reverse=True,
        )
        for det in ordered:
            label = str(det.get("label") or det.get("class") or "?").strip()
            box = det.get("bbox") or {}
            if not isinstance(box, dict):
                kept.append(det)
                continue
            dup = False
            for other in kept:
                same_label = str(
                    other.get("label") or other.get("class") or "?"
                ).strip() == label
                if same_label and cls._iou(box, other.get("bbox") or {}) > iou_threshold:
                    dup = True
                    break
            if not dup:
                kept.append(det)
        return kept

    @staticmethod
    def _iou(a: dict[str, Any], b: dict[str, Any]) -> float:
        """IoU of two center-form normalized boxes ({x, y, w, h})."""
        try:
            ax1, ay1 = a["x"] - a["w"] / 2, a["y"] - a["h"] / 2
            ax2, ay2 = a["x"] + a["w"] / 2, a["y"] + a["h"] / 2
            bx1, by1 = b["x"] - b["w"] / 2, b["y"] - b["h"] / 2
            bx2, by2 = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
        except (KeyError, TypeError):
            return 0.0
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
        inter = iw * ih
        if inter <= 0:
            return 0.0
        area_a = (ax2 - ax1) * (ay2 - ay1)
        area_b = (bx2 - bx1) * (by2 - by1)
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0

    # ── detect_objects ─────────────────────────────────────────────

    async def detect_objects(self, args: dict[str, Any]) -> str:
        cams = self._resolve_cameras(args)
        if isinstance(cams, str):  # ERROR
            return cams
        clauses = [await self._detect_one(c) for c in cams]
        return self._join_clauses(clauses)

    async def _detect_one(self, camera_id: str) -> str:
        try:
            frame = await self._ctx.get_frame(camera_id)
        except LookupError:
            return f"{camera_id} is not configured"
        except FrameSourceError as exc:
            logger.warning("VISION DEGRADED: %s frame fetch failed (camera offline / bad RTSP path?): %s", camera_id, exc)
            return f"{camera_id} appears to be offline"
        try:
            response = await self._detect.infer(frame_jpeg=frame)
        except Exception:
            logger.exception("detect_objects: detector call failed for %s", camera_id)
            return f"{camera_id}: detector unavailable"
        detections = ((response.get("result") or {}).get("detections")) or []
        if not detections:
            return f"{camera_id}: no objects"
        return f"{camera_id}: {self._summarize_detections(detections)}"

    # ── camera_snapshot (metadata from Tier-0, no inference) ────────

    async def camera_snapshot(self, args: dict[str, Any]) -> str:
        """Answer count/presence from the always-on Tier-0 detection stream —
        no new inference. Reads the latest published tracks off the event ring."""
        cams = self._resolve_cameras(args)
        if isinstance(cams, str):  # ERROR
            return cams
        clauses = [self._snapshot_one(c) for c in cams]
        return self._join_clauses(clauses)

    # Tier-0 publishes a track list every frame while objects are present; it stops
    # (no empty events) once they leave. So an event older than this means the scene
    # is stale — treat it as "nothing there now" rather than reporting a departed
    # object as present.
    _SNAPSHOT_MAX_AGE_S = 10.0

    def _snapshot_one(self, camera_id: str) -> str:
        # Resolve to the pipeline's camera id — the Tier-0 event ring is keyed by
        # the id on the bus subject, same id the best-frame path uses.
        pipeline_cam = self._resolve_camera(camera_id)
        event = self._ctx.latest_inference(pipeline_cam, adapter="tier0")
        if event is None:
            # No Tier-0 stream for this camera (not analyzed, or bus not wired) —
            # say so plainly so the LLM can fall back to a live tool if it must.
            return f"{camera_id}: no live detection data (try describe_camera)"
        if (time.time() - event.received_at) > self._SNAPSHOT_MAX_AGE_S:
            return f"{camera_id}: nothing detected recently (try describe_camera for a live look)"
        summary = snapshot_from_event(event.raw or {}).describe()   # SDK: counts→phrase
        if not summary:
            return f"{camera_id}: nothing detected right now"
        age = max(0, int(time.time() - event.received_at))
        return f"{camera_id}: {summary} (from live detection {age}s ago)"

    # ── recognize_faces ────────────────────────────────────────────

    async def recognize_faces(self, args: dict[str, Any]) -> str:
        cams = self._resolve_cameras(args)
        if isinstance(cams, str):  # ERROR
            return cams
        clauses = [await self._recognize_one(c) for c in cams]
        return self._join_clauses(clauses)

    async def _recognize_one(self, camera_id: str) -> str:
        try:
            frame = await self._ctx.get_frame(camera_id)
        except LookupError:
            return f"{camera_id} is not configured"
        except FrameSourceError as exc:
            logger.warning("VISION DEGRADED: %s frame fetch failed (camera offline / bad RTSP path?): %s", camera_id, exc)
            return f"{camera_id} appears to be offline"
        try:
            response = await self._recognise.infer(
                frame_jpeg=frame,
                extra={"task": "face_recognition"},
            )
        except Exception:
            logger.warning("VISION DEGRADED: recognize_faces recognition adapter unavailable (not registered?)")
            return f"{camera_id}: face recognition isn't enabled"
        result = response.get("result") or {}
        if result.get("recognized"):
            name = result.get("name") or result.get("person_id") or "someone"
            category = result.get("category") or "unknown category"
            similarity = result.get("similarity")
            sim_phrase = f", similarity {similarity:.2f}" if isinstance(
                similarity, (int, float)
            ) else ""
            return f"{camera_id}: recognised {name} ({category}{sim_phrase})"
        if "face_bbox" in result and result.get("face_bbox"):
            return f"{camera_id}: a face is visible but not registered"
        return f"{camera_id}: no face detected"

    # ── recent_events ──────────────────────────────────────────────

    async def recent_events(self, args: dict[str, Any]) -> str:
        camera_arg = args.get("camera_id")
        try:
            window = float(args.get("window_seconds", 0))
        except (TypeError, ValueError):
            return "ERROR: window_seconds must be a number."
        if window <= 0:
            return "ERROR: window_seconds must be positive."

        camera_id: str | None
        if camera_arg in (None, "", "__any__"):
            camera_id = None
        elif isinstance(camera_arg, str) and self._ctx.known_camera(camera_arg):
            camera_id = camera_arg
        else:
            return (
                f"ERROR: unknown camera_id {camera_arg!r}. Use one of "
                f"{sorted(c.camera_id for c in self._ctx.cameras)} "
                f"or '__any__'."
            )

        events = self._ctx.recent_events(
            camera_id=camera_id, window_seconds=window
        )
        if not events:
            scope = camera_id or "any camera"
            mins = int(window / 60) or 1
            return f"No events on {scope} in the last {mins} minute(s)."
        # Wall-clock deltas for human readability; CameraContext
        # stamps received_at with time.time(). Cap at 6 entries so
        # the tool message stays short.
        import time as _time
        now = _time.time()
        lines = [
            f"{int(now - e.received_at)}s ago — {e.camera_id}: {e.summary}"
            for e in events[:6]
        ]
        return "Recent events:\n" + "\n".join(lines)

    # ── recent_plates (RFC-0002 Phase 4: the contract event) ───────

    async def recent_plates(self, args: dict[str, Any]) -> str:
        """Report recently read plates from the live plate.recognized.v1
        ring — producer-independent by contract: the answer is the same
        whether Tier-1 dispatch or core's enrichment ran the OCR."""
        camera_arg = args.get("camera_id")
        try:
            window = float(args.get("window_seconds", 0))
        except (TypeError, ValueError):
            return "ERROR: window_seconds must be a number."
        if not math.isfinite(window) or window <= 0:
            return "ERROR: window_seconds must be a positive finite number."
        window = min(window, 7 * 86400.0)

        camera_id: str | None
        if camera_arg in (None, "", "__any__"):
            camera_id = None
        elif isinstance(camera_arg, str) and self._ctx.known_camera(camera_arg):
            camera_id = camera_arg
        else:
            return (
                f"ERROR: unknown camera_id {camera_arg!r}. Use one of "
                f"{sorted(c.camera_id for c in self._ctx.cameras)} "
                f"or '__any__'."
            )
        plate_arg = args.get("plate")
        plate = str(plate_arg).strip() if plate_arg not in (None, "") else None

        reads = self._ctx.recent_plates(
            camera_id=camera_id, plate=plate, window_seconds=window
        )
        if not reads:
            scope = camera_id or "any camera"
            mins = int(window / 60) or 1
            hint = (
                " (Note: the live event bus isn't configured — for stored "
                "history use search_history with a plate filter.)"
                if not getattr(self._ctx, "nats_wired", True) else
                " For older reads, use search_history with a plate filter."
            )
            what = f"plates matching '{plate}'" if plate else "plate reads"
            return f"No {what} on {scope} in the last {mins} minute(s).{hint}"
        import time as _time
        now = _time.time()
        lines = []
        for r in reads[:8]:
            conf = f", conf {r.confidence:.2f}" if r.confidence is not None else ""
            veh = f" ({r.vehicle_label})" if r.vehicle_label else ""
            lines.append(
                f"{int(now - r.received_at)}s ago — {r.camera_id}: "
                f"{r.plate_text}{veh}{conf}"
            )
        more = f" (+{len(reads) - 8} more)" if len(reads) > 8 else ""
        return "Recent plate reads:\n" + "\n".join(lines) + more

    # ── search_footage ─────────────────────────────────────────────

    async def search_footage(self, args: dict[str, Any]) -> str:
        if self._footage_index is None or not getattr(
            self._footage_index, "available", False
        ):
            return (
                "Footage search isn't available — the footage-search index "
                "is not configured or hasn't been built yet."
            )
        keywords = args.get("keywords")
        if isinstance(keywords, str):
            keywords = [keywords]
        if not isinstance(keywords, list) or not keywords:
            return "ERROR: search_footage needs a 'keywords' list, e.g. ['red', 'truck']."
        keywords = [str(k).strip() for k in keywords if str(k).strip()]
        if not keywords:
            return "ERROR: no usable keywords provided."

        within_minutes: float | None
        raw_within = args.get("within_minutes")
        if raw_within in (None, ""):
            within_minutes = None
        else:
            try:
                within_minutes = float(raw_within)
            except (TypeError, ValueError):
                return "ERROR: within_minutes must be a number of minutes."

        camera_arg = args.get("camera_id")
        camera_id: str | None
        if camera_arg in (None, "", "__any__"):
            camera_id = None
        elif isinstance(camera_arg, str) and self._ctx.known_camera(camera_arg):
            camera_id = camera_arg
        else:
            return (
                f"ERROR: unknown camera_id {camera_arg!r}. Use one of "
                f"{sorted(c.camera_id for c in self._ctx.cameras)} or '__any__'."
            )

        try:
            hits = self._footage_index.search(
                keywords=keywords, within_minutes=within_minutes,
                camera_id=camera_id,
            )
        except Exception:
            logger.exception("search_footage: index query failed")
            return "Footage search failed."

        if not hits:
            phrase = " ".join(keywords)
            return f"No recorded footage matched {phrase!r}."

        import time as _time
        now = _time.time()
        lines = []
        for h in hits:
            mins = max(0, int((now - h.ts) / 60))
            descr = h.caption or (" ".join(h.labels) or "match")
            lines.append(f"{mins} min ago on {h.camera_id}: {descr}")
        return "Found in recorded footage:\n" + "\n".join(lines)

    # ── Helpers ────────────────────────────────────────────────────

    # ── search_history (canonical event store — RFC-0001 C1) ──────

    async def search_history(self, args: dict[str, Any]) -> str:
        """Answer "who/what came between X and Y?" from the events store.

        Reads remembered visits (one row per object's stay, with its best
        photo). For person queries it can face-match the evidence crops via
        the recognition adapter — "yes, I saw Priya at 15:12" — capped at a
        few crops so a busy window can't stall the conversation.
        """
        if self._events is None:
            return ("History isn't enabled on this deployment "
                    "(events store not configured).")
        label = str(args.get("label") or "person").strip().lower()
        plate = args.get("plate")
        start = args.get("start_time")
        end = args.get("end_time")
        camera_arg = args.get("camera_id")
        camera_id = None
        if camera_arg not in (None, "", "all", "__any__"):
            cam = str(camera_arg)
            if not self._ctx.known_camera(cam):
                return f"ERROR: unknown camera '{cam}'."
            # The store keys visits by the server-side camera id (same id the
            # internal endpoint returns) — resolve like best-frame does.
            resolved = self._resolve_camera(cam)
            try:
                camera_id = int(resolved)
            except (TypeError, ValueError):
                return f"ERROR: camera '{cam}' has no server-side id."
        try:
            events = await self._events.search(
                label=label, camera_id=camera_id, plate=plate,
                start=start, end=end, limit=25,
            )
        except Exception:
            events = None
        if events is None:
            # Failure is NOT an empty window: "nothing came" and "I couldn't
            # check" must be different answers in a security product.
            logger.warning("search_history: events store unreachable or query rejected")
            return ("I couldn't check the history just now (store unreachable "
                    "or the time range wasn't understood) — please try again.")
        if not events:
            window = self._window_phrase(start, end)
            return (f"No {label} visits remembered{window}."
                    + self._in_progress_note(label, camera_arg))

        clauses = []
        for e in events[:10]:
            t0 = self._clock_phrase(e.started_at)
            t1 = self._clock_phrase(e.ended_at)
            span = f"{t0}–{t1}" if t1 and t1 != t0 else t0
            plate_bit = f", plate {e.plate_text}" if getattr(e, "plate_text", None) else ""
            clauses.append(
                f"[#{e.id}] {span} on camera {e.camera_id}{plate_bit}"
                + (" (photo kept)" if e.has_evidence else "")
            )
        live_note = self._in_progress_note(label, camera_arg)
        summary = (f"I remember {len(events)} {label} visit(s)"
                   f"{self._window_phrase(start, end)}: " + "; ".join(clauses) + ".")

        # Hand the remembered photos to the UI. The answer names times and
        # says "(photo kept)" — the photo is right there in the store and was
        # already being fetched to face-match, so showing it costs one small
        # extra read and turns "I saw someone at 16:25" into something the
        # operator can actually check.
        await self._attach_evidence_frames(events)
        summary += live_note

        # Face-match the evidence for person questions (best-effort, capped).
        if label == "person" and bool(args.get("identify_faces", True)):
            names = await self._identify_visit_faces(events)
            if names:
                summary += " Recognised: " + ", ".join(sorted(names)) + "."
        return summary

    def _in_progress_note(self, label: str, camera_arg) -> str:
        """A visit enters the store only when it ENDS, so history alone answers
        'did anyone come today?' with a straight no while the person is
        literally standing on camera. Seen in the field: the operator walked in
        to test, asked, and was denied — the visit was still being written.
        Cross-check the live Tier-0 ring and say so.
        """
        try:
            cam_filter = None
            if camera_arg not in (None, "", "all", "__any__"):
                cam_filter = str(camera_arg)
            events = self._ctx.recent_events(
                camera_id=cam_filter, window_seconds=45.0)
        except Exception:
            return ""            # the note is a bonus; history stays the answer
        cams: list[str] = []
        for ev in events or ():
            if getattr(ev, "adapter", None) != "tier0":
                continue
            tracks = (getattr(ev, "raw", None) or {}).get("tracks") or []
            if any(str(t.get("label") or "").strip().lower() == label
                   for t in tracks if isinstance(t, dict)):
                cam = str(getattr(ev, "camera_id", "") or "")
                if cam and cam not in cams:
                    cams.append(cam)
        if not cams:
            return ""
        return (f" And right now a {label} IS on {', '.join(sorted(cams))} — "
                f"that visit is still in progress and enters history when it ends.")

    async def _attach_evidence_frames(self, events, cap: int = 3) -> None:
        """Publish up to ``cap`` remembered best-frames onto this turn.

        Each carries its own timestamp caption. That is not decoration: every
        frame the UI has ever rendered in a chat bubble was the LIVE view, so
        an uncaptioned crop from 16:25 sitting in an answer about this
        afternoon would read as "here is your camera now" — the wrong thing to
        get wrong in a security product.
        """
        if self._events is None:
            return
        for e in events:
            if len(self.last_evidence_frames) >= cap:
                break
            if not getattr(e, "has_evidence", False):
                continue
            try:
                crop = await self._events.evidence(e.id)
            except Exception:
                logger.warning("search_history: evidence fetch failed for #%s", e.id)
                continue
            if not crop or len(crop) > 2_000_000:      # same cap as live frames
                continue
            self.last_evidence_frames.append({
                "camera_id": str(e.camera_id),
                "caption": f"#{e.id} · {self._clock_phrase(e.started_at)}",
                "jpeg_b64": base64.b64encode(crop).decode("ascii"),
            })

    async def _identify_visit_faces(self, events, cap: int = 4) -> set:
        names: set = set()
        checked = 0
        for e in events:
            if checked >= cap:
                break
            if not e.has_evidence:
                continue
            try:
                crop = await self._events.evidence(e.id)
            except Exception:
                # A photo we cannot fetch costs a NAME, not the answer. This
                # was unguarded, so one bad read turned "I remember 3 visits
                # at 15:12, 15:40 and 16:25" into no answer at all.
                logger.warning("search_history: evidence fetch failed for #%s", e.id)
                continue
            if not crop:
                continue
            checked += 1
            try:
                response = await self._recognise.infer(
                    frame_jpeg=crop, extra={"task": "face_recognition"}
                )
            except Exception:
                logger.warning("search_history: recognition adapter unavailable")
                break
            result = response.get("result") or {}
            if result.get("recognized"):
                names.add(str(result.get("name") or result.get("person_id") or "someone"))
        return names

    @staticmethod
    def _clock_phrase(iso: str | None) -> str:
        """UTC-stored timestamp → the agent host's LOCAL clock time for
        speech ("15:12" must mean the listener's 15:12, not UTC's)."""
        if not iso:
            return "?"
        try:
            from datetime import datetime as _dt
            t = _dt.fromisoformat(iso)
            if t.tzinfo is not None:
                t = t.astimezone()
            return t.strftime("%H:%M")
        except ValueError:
            return iso

    @staticmethod
    def _window_phrase(start, end) -> str:
        if start and end:
            return f" between {start} and {end}"
        if start:
            return f" since {start}"
        if end:
            return f" before {end}"
        return " (all time)"

    def _require_camera(self, args: dict[str, Any]) -> str:
        camera_id = args.get("camera_id")
        if not isinstance(camera_id, str) or not camera_id:
            return "ERROR: camera_id is required."
        if not self._ctx.known_camera(camera_id):
            return (
                f"ERROR: camera_id {camera_id!r} is not configured. "
                f"Available: "
                f"{sorted(c.camera_id for c in self._ctx.cameras)}."
            )
        return camera_id

    # Values that mean "every configured camera".
    _ALL_TOKENS = frozenset({"all", "__all__", "all_cameras", "every", "everything"})

    def _resolve_cameras(self, args: dict[str, Any]) -> "list[str] | str":
        """Resolve a tool call's camera selector to a concrete list.

        Accepts ``camera_ids`` (a list), or ``camera_id`` as a single id,
        ``"all"`` (every camera), or a comma-separated string. Returns the
        ordered, de-duplicated list, or an ``ERROR:`` string the LLM can
        relay. Records the result in ``last_cameras_used`` for the UI."""
        known = [c.camera_id for c in self._ctx.cameras]
        if not known:
            return "ERROR: no cameras are configured."

        raw = args.get("camera_ids")
        if raw is None:
            cid = args.get("camera_id")
            if isinstance(cid, str) and cid.strip().lower() in self._ALL_TOKENS:
                self.last_cameras_used = list(known)
                return list(known)
            if isinstance(cid, str) and "," in cid:
                raw = [p.strip() for p in cid.split(",") if p.strip()]
            elif isinstance(cid, str) and cid:
                raw = [cid]
            else:
                return "ERROR: camera_id (or camera_ids) is required."

        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return "ERROR: camera_ids must be a list of camera names."

        resolved: list[str] = []
        for item in raw:
            name = str(item).strip()
            if name.lower() in self._ALL_TOKENS:
                resolved = list(known)
                break
            if not self._ctx.known_camera(name):
                return (
                    f"ERROR: camera {name!r} is not configured. Available: "
                    f"{known} (or 'all')."
                )
            if name not in resolved:
                resolved.append(name)
        if not resolved:
            return "ERROR: no valid cameras in the request."
        self.last_cameras_used = list(resolved)
        return resolved

    @staticmethod
    def _join_clauses(clauses: list[str]) -> str:
        """Combine per-camera result clauses into one speakable string."""
        if len(clauses) == 1:
            return "On " + clauses[0] + "."
        return "Across " + str(len(clauses)) + " cameras — " + "; ".join(clauses) + "."
