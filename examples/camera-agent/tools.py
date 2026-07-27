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

import logging
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
    ) -> None:
        self._ctx = context
        self._caption = caption_client
        self._detect = detection_client
        self._recognise = recognition_client
        # Optional async callable(camera_id) -> jpeg bytes | None. When set,
        # describe_camera runs the VLM on Tier-0's BEST frame (clean, representative)
        # instead of an arbitrary live grab — more accurate and cheaper. None (or a
        # miss) falls back to the live frame, so behaviour is unchanged without it.
        self._best_frame_fetch = best_frame_fetch
        # Optional read-only FootageIndex (footage_index.FootageIndex).
        # When None or unavailable, search_footage reports that cleanly.
        self._footage_index = footage_index
        # Cameras touched by the most recent tool call — read by /converse
        # so the UI can show which camera(s) the agent is working on.
        self.last_cameras_used: list[str] = []

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

    async def _describe_one(self, camera_id: str, question: str | None = None) -> str:
        # Prefer Tier-0's best frame (clean, already-selected) over an arbitrary
        # live grab; fall back to a live frame when no best frame is available.
        frame = await self._best_frame(camera_id)
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
        except Exception:
            # No caption adapter registered (e.g. the standard stack ships
            # only the object detector). Fall back to describing the scene
            # from detected objects so the user still gets a useful answer
            # instead of an error.
            logger.warning(
                "VISION DEGRADED: describe_camera caption adapter unavailable for %s "
                "(not registered with KAI-C?); falling back to object detection",
                camera_id,
            )
        return await self._describe_via_detection(camera_id, frame)

    async def _describe_via_detection(self, camera_id: str, frame: bytes) -> str:
        """Best-effort scene description built from the object detector,
        used when no caption adapter is available."""
        try:
            response = await self._detect.infer(frame_jpeg=frame)
        except Exception:
            logger.exception("describe_camera: detection fallback failed")
            return f"{camera_id}: scene description unavailable right now"
        summary = self._summarize_detections(
            (response.get("result") or {}).get("detections") or []
        )
        if not summary:
            return f"{camera_id}: nothing notable visible"
        return f"{camera_id}: I can see {summary}"

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

    def _snapshot_one(self, camera_id: str) -> str:
        event = self._ctx.latest_inference(camera_id, adapter="tier0")
        if event is None:
            # No Tier-0 stream for this camera (not analyzed, or bus not wired) —
            # say so plainly so the LLM can fall back to a live tool if it must.
            return f"{camera_id}: no live detection data (try describe_camera)"
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
