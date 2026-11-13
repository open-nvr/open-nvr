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

import ast
import asyncio
import contextvars
import re
import base64
import json
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

#: How far back ``search_footage`` looks when the caller names no window.
#:
#: Seven days. Not a retention policy and not a limit — a search that
#: comes back empty inside this window is re-run over all history before
#: anything is reported, so nothing is hidden by it. What it buys is the
#: common case: an unbounded free-text search is a full scan of the
#: event store, and this is the one query a person sits through with the
#: conversation paused.
#:
#: The number comes from what people actually ask a camera about — "did
#: anyone come by", "was there a van this week" — and from the shape of
#: the measurement in bench/app_search_latency.py, where a window moves
#: free text off the linear curve. If ``footage_search_sources["widened"]``
#: climbs on a real site, this is too short for how that site is asked
#: about and should go up.
DEFAULT_SEARCH_MINUTES = 7 * 24 * 60


# ── Tool definitions in OpenAI / Pipecat function-calling shape ────
#
# The model-facing text (names, descriptions, argument schemas) lives in
# the SDK, shared with core's workflow engine (RFC #620); the handlers
# below stay the agent's own. tests/test_tool_definition_parity.py keeps
# the two in step.
from opennvr_app_sdk.agent.tool_definitions import (  # noqa: E402
    READ_TOOL_NAMES,
    build_tool_definitions,
)


# ── Tool handlers ──────────────────────────────────────────────────


#: The question the current turn is answering, for tools that decide by
#: it (face matching only when somebody asked WHO). A context variable,
#: not a slot on the tools object: turns are not serialised — a background
#: task or a scheduled report runs its own turn while a person's is in
#: flight — and each asyncio task sees the value ITS turn set. ``None``
#: means nobody told this task (the streaming /ws path calls tool handlers
#: straight from the pipeline), and the tool falls back to matching, as
#: it always did before the question could decide.
_question: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "camera_agent_question", default=None)

#: How THIS task's most recent describe was answered: "vlm" (the caption /
#: VQA adapter), "detector-fallback" (the object detector stood in), or
#: "no-frame" (offline or unconfigured camera — nothing was looked at).
#: Task-local for the reason ``_question`` is: ``last_vision_error`` is
#: the site's health dot, shared by every turn, so a background turn's
#: failed describe could relabel a person's successful one in its trace.
_describe_path: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "camera_agent_describe_path", default=None)

_WHO_RE = re.compile(r"\b(who|whom|whose|recogni[sz]e|name)\b")
#: A capitalised word mid-sentence — "was Priya here" from an STT that
#: capitalises names; the words below have that shape and are not names.
_NAME_RE = re.compile(r"(?<=[a-z0-9,] )([A-Z][a-z]{2,})\b")
_NOT_NAMES = frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
    "camera", "cameras", "cam", "front", "back", "side", "door", "gate", "garage",
    "driveway", "yard", "porch", "street", "kitchen", "office", "lobby",
    "please", "okay", "thanks", "hey", "there", "what", "who", "how", "when",
    "where", "which", "yes", "the", "any", "some", "today", "tonight",
})
#: "a Car", "the Door", "my Garage": a determiner precedes a noun, not a name.
_DETERMINERS = frozenset({"a", "an", "the", "this", "that", "my", "your", "our",
                          "any", "some", "no", "every", "each"})


def asked_who(text: str | None) -> bool | None:
    """Did the question ask WHO — the word, or a name? ``None`` when there
    is no question to read (see ``_question``)."""
    if text is None:
        return None
    if _WHO_RE.search(text.lower()):
        return True
    for m in _NAME_RE.finditer(text):
        word = m.group(1)
        if word.lower() in _NOT_NAMES:
            continue
        before = text[:m.start()].rstrip().split()
        if before and before[-1].lower().strip(",") in _DETERMINERS:
            continue
        return True
    return False


def _as_bool(value: Any) -> bool | None:
    """A tool argument a small model may hand over as the STRING "false".
    ``None`` = not given, or not readable as a boolean."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "y", "on"):
            return True
        if v in ("false", "0", "no", "n", "off", ""):
            return False
    return None


def _frames_alike(a: bytes, b: bytes, threshold: float = 8.0) -> bool:
    """Do two JPEGs show the same scene? Mean absolute difference of their
    32×18 grey thumbnails, below ``threshold``. Anything that is not a
    decodable image (tests pass fixture bytes) is "different", so nothing
    is ever skipped by mistake."""
    try:
        import io

        from PIL import Image, ImageChops, ImageStat

        ia = Image.open(io.BytesIO(a)).convert("L").resize((32, 18))
        ib = Image.open(io.BytesIO(b)).convert("L").resize((32, 18))
        return ImageStat.Stat(ImageChops.difference(ia, ib)).mean[0] < threshold
    except Exception:  # noqa: BLE001 — not an image, or Pillow missing
        return False


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
        best_frame_fetch: Any = None,
        resolve_camera: Any = None,
        pipeline_camera: Any = None,
        events_client: Any = None,
        timeline: Any = None,
        description_fetch: Any = None,
    ) -> None:
        self._ctx = context
        # Core's shared live description (RFC-0004 step 3): async
        # (camera_id, max_age_s) -> dict | None. The plain "what do you
        # see" look goes there; a question still goes to the VQA model.
        self._description_fetch = description_fetch
        self._caption = caption_client
        self._detect = detection_client
        self._recognise = recognition_client
        # Map the agent's camera id → the pipeline's camera id (the id on the Tier-0
        # bus subject / best-frame endpoint). MUST match the mapping used to build
        # best_frame_fetch, so camera_snapshot and describe_camera agree on which
        # camera they're reading. Identity by default.
        self._resolve_camera = resolve_camera or (lambda cid: cid)
        # Tier-0 (detect-pipeline) knows a camera as ``cam<server id>`` — the
        # id on its bus subjects, its metrics and its /best_frame and
        # /latest_frame endpoints. The server-side id ``_resolve_camera``
        # gives is for the platform's own endpoints. Measured on a Ventuno Q
        # (2026-11-08): resolving to "23" made every /best_frame and
        # /latest_frame fetch a 404 and every live look a 6 s RTSP grab.
        self._pipeline_camera = pipeline_camera or (lambda cid: cid)
        # Optional async callable(camera_id) -> jpeg bytes | None. When set,
        # describe_camera runs the VLM on Tier-0's BEST frame (clean, representative)
        # instead of an arbitrary live grab — more accurate and cheaper. None (or a
        # miss) falls back to the live frame, so behaviour is unchanged without it.
        self._best_frame_fetch = best_frame_fetch
        # Optional SDK EventsClient — the platform's memory (canonical event
        # store). Powers search_history: past visits with best-frame evidence,
        # optionally face-matched. None = tool reports history isn't enabled.
        self._events = events_client
        # SDK TimelineAPI — the platform's search over the canonical event
        # store (``/api/v1/internal/app/search``, the operator Search
        # page's own query). The ONLY source search_footage has.
        self._timeline = timeline
        # How search_footage has been answered since start-up.
        #
        #   canonical     the store answered (the normal path)
        #   unanswerable  the store could not be reached, and rather than
        #                 guess, the agent said so
        #
        # There used to be a third, `index_fallback`, counting queries the
        # private SQLite index served during an outage. It was here to
        # answer whether that index still earned a second store with its
        # own retention policy, by evidence rather than opinion. The
        # question closed itself: footage-search 2.0.0 deleted the index,
        # so nothing writes the file any more and a fallback to it would
        # be a fallback to whatever was true on the day of the upgrade.
        # Stale footage descriptions served as current ones is a worse
        # outage than no answer, so the honest answer is the only one
        # left.
        #
        # `widened` counts queries where the default recent window came
        # back empty and the search was re-run over all history. It is a
        # TUNING SIGNAL, not an error count: if it climbs, the default
        # window is too short for how this site is actually asked about,
        # and every one of those conversations paid two queries instead
        # of one. If it stays near zero, the window is doing its job.
        self.footage_search_sources: dict[str, int] = {
            "canonical": 0, "unanswerable": 0, "widened": 0,
        }
        # Cameras touched by the most recent tool call — read by /converse
        # so the UI can show which camera(s) the agent is working on.
        self.last_cameras_used: list[str] = []
        # Stored best-frame photos this turn dug out of the events store, so
        # an answer about the PAST can show what it is talking about. Kept
        # apart from the live per-turn frame cache on purpose: these are
        # historical, and each carries its own timestamp caption so the UI
        # can never present a photo from 16:25 as the current view.
        self.last_evidence_frames: list[dict] = []
        #: The utterance the current turn is answering (set by the turn).
        # Why the last describe fell back from the VLM ("" = it didn't) —
        # surfaced by the system self-check so degradation is visible.
        self.last_vision_error: str | None = None

    # ── describe_camera ────────────────────────────────────────────

    async def describe_camera(self, args: dict[str, Any]) -> str:
        # Reset at ENTRY, not only in _describe_one: a call that fails to
        # resolve its camera looked at nothing and must not carry the
        # previous describe's path (that of an earlier call in this turn,
        # or of the scheduler's previous report, which reuses the task).
        _describe_path.set(None)
        cams = self._resolve_cameras(args)
        if isinstance(cams, str):  # ERROR
            return cams
        # Optional VQA question ("what is he wearing?", "is the gate open?").
        # A vision-language adapter (Moondream / SmolVLM / Qwen-VL) answers it
        # grounded in the frame; a plain captioner (BLIP) ignores it and still
        # returns a scene caption. Either way the agent gets a real answer
        # instead of guessing (test-report S-6).
        question = str(args.get("question") or "").strip() or None
        clauses: list[str] = []
        paths: list[str] = []
        for cam in cams:
            clauses.append(await self._describe_one(cam, question))
            path = _describe_path.get()
            if path and path not in paths:
                paths.append(path)
        # Every path that answered part of it, in order: "all" over two
        # cameras where one fell back reads "vlm+detector-fallback", not
        # just whichever camera came last.
        _describe_path.set("+".join(paths) or None)
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
        pipeline_cam = self._pipeline_camera(camera_id)
        event = self._ctx.latest_inference(pipeline_cam, adapter="tier0")
        if event is None or (time.time() - event.received_at) > self._SNAPSHOT_MAX_AGE_S:
            return None
        return await self._best_frame(camera_id)

    #: How old core's description may be for "what do you see now".
    _DESCRIPTION_MAX_AGE_S = 30.0

    async def _describe_via_core(self, camera_id: str) -> str | None:
        """The plain look, from core's one shared description. None when
        core did not answer (the caller falls back to its own look and
        says so in the path); a stale answer is given honestly, with its
        age, rather than presenting an old frame as now."""
        if self._description_fetch is None:
            return None
        try:
            body = await self._description_fetch(camera_id, self._DESCRIPTION_MAX_AGE_S)
        except Exception:  # noqa: BLE001
            logger.debug("core description failed for %s", camera_id, exc_info=True)
            return None
        if not body or not body.get("text"):
            return None
        source = str(body.get("source") or "")
        text = str(body["text"]).strip()
        if source in ("visit", "fresh"):
            _describe_path.set(f"core:{source}")
            return f"{camera_id}: {text}"
        if source == "stale":
            age = body.get("age_s")
            ago = f"{int(age)} s ago" if isinstance(age, (int, float)) else "earlier"
            reason = str(body.get("reason") or "no fresh look")
            _describe_path.set("core:stale")
            return f"{camera_id} (as of {ago}; {reason}): {text}"
        return None

    async def _describe_one(self, camera_id: str, question: str | None = None) -> str:
        self.last_vision_error = None    # per-call: set again only on fallback
        _describe_path.set(None)
        if question is None:
            answered = await self._describe_via_core(camera_id)
            if answered is not None:
                return answered
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
                _describe_path.set("no-frame")
                return f"{camera_id} is not configured"
            except FrameSourceError as exc:
                logger.warning("VISION DEGRADED: %s frame fetch failed (camera offline / bad RTSP path?): %s", camera_id, exc)
                _describe_path.set("no-frame")
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
                _describe_path.set("vlm")
                return f"{camera_id}: {caption}"
        except Exception as exc:
            # No caption adapter reachable (not registered, sovereignty 403,
            # adapter down). Fall back to the object detector — but say so:
            # a fallback that talks like full vision invents details the
            # system never saw (field case: a confident wrong shirt color
            # while the VLM had never received a single request).
            reason = self._vision_error_reason(exc)
            self.last_vision_error = f"{type(exc).__name__}: {exc}"[:300]
            _describe_path.set("detector-fallback")
            logger.warning(
                "VISION DEGRADED: describe_camera caption adapter unavailable for %s "
                "(%s); falling back to object detection",
                camera_id, reason,
            )
            return await self._describe_via_detection(
                camera_id, frame, degraded_reason=reason
            )
        # The adapter answered, but with nothing: the detector stands in.
        _describe_path.set("detector-fallback")
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
    #: Mean per-pixel difference (0–255, on a 32×18 grey thumbnail) below
    #: which two sampled frames are the same scene and the second one
    #: inherits the first one's caption instead of costing another VLM
    #: call. An empty room drifts 1–3 over an hour of daylight; a person
    #: in frame moves it past 10.
    _WINDOW_SAME_SCENE = 8.0

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
        instants = [start + _td(seconds=step * i) for i in range(n)]
        # The frames together (each is one ffmpeg seek in core, ~0.75 s on
        # the Ventuno Q), then one caption per CHANGE of scene: five
        # frames of the same empty office cost five captions (12 s for a
        # one-hour window, eval 2026-11-09); now they cost one.
        frames = await asyncio.gather(
            *(self._events.recording_frame(server_cam, ts.isoformat()) for ts in instants),
            return_exceptions=True)
        observations: list[tuple[str, str]] = []
        last_frame: bytes | None = None
        last_caption: str | None = None
        for ts, frame in zip(instants, frames):
            if not frame or isinstance(frame, BaseException):
                continue
            if last_frame is not None and _frames_alike(last_frame, frame, self._WINDOW_SAME_SCENE):
                caption = last_caption
            else:
                caption = await self._vlm_caption(frame, question)
                last_frame, last_caption = frame, caption
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

    #: IoU above which two boxes of DIFFERENT labels are one object — the
    #: same rule Tier-0 applies (detect_pipeline.pipeline.CROSS_LABEL_IOU),
    #: so a vehicle the detector reads as both "car" and "truck" counts
    #: once here as it draws once on the live overlay. Well above the
    #: per-label threshold: a person in front of a car overlaps it only
    #: partially and both must survive.
    CROSS_LABEL_IOU = 0.85

    @classmethod
    def _dedup_detections(
        cls, detections: list[dict[str, Any]], iou_threshold: float = 0.55,
        cross_label_iou: float | None = CROSS_LABEL_IOU,
    ) -> list[dict[str, Any]]:
        """Greedy NMS: drop boxes that overlap an already-kept box of the
        same label by more than ``iou_threshold``, or of ANY label by more
        than ``cross_label_iou`` (None disables the cross-label rule)."""
        kept: list[dict[str, Any]] = []
        cross = (cross_label_iou if cross_label_iou is not None
                 and 0.0 < cross_label_iou < 1.0 else None)
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
                limit = iou_threshold if same_label else cross
                if limit is not None and cls._iou(box, other.get("bbox") or {}) > limit:
                    dup = True
                    break
            if not dup:
                kept.append(det)
        return kept

    @staticmethod
    def _iou(a: dict[str, Any], b: dict[str, Any]) -> float:
        """IoU of two normalized boxes in the adapter contract's shape:
        ``{x, y, w, h}`` with ``x, y`` the TOP-LEFT corner (§5.1
        NormalizedBBox). This used to read them as centre-form, which
        shifted every box by half its size before comparing — harmless
        for two near-identical boxes, wrong for anything else."""
        try:
            ax1, ay1 = float(a["x"]), float(a["y"])
            ax2, ay2 = ax1 + float(a["w"]), ay1 + float(a["h"])
            bx1, by1 = float(b["x"]), float(b["y"])
            bx2, by2 = bx1 + float(b["w"]), by1 + float(b["h"])
        except (KeyError, TypeError, ValueError):
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
        pipeline_cam = self._pipeline_camera(camera_id)
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
        if not args.get("window_seconds") and args.get("start_time"):
            # The model sometimes hands this tool a start_time (describe_window's
            # shape). Take it as "since then" rather than failing the call.
            try:
                from datetime import datetime as _dt
                start = _dt.fromisoformat(str(args["start_time"]).replace("Z", "+00:00"))
                now = _dt.now(start.tzinfo) if start.tzinfo else _dt.now()
                args = {**args, "window_seconds": max(60.0, (now - start).total_seconds())}
            except (TypeError, ValueError):
                pass
        try:
            window = float(args.get("window_seconds", 0))
        except (TypeError, ValueError):
            return "ERROR: window_seconds must be a number."
        if window <= 0:
            return "ERROR: window_seconds must be positive."

        camera_id: str | None
        if camera_arg in (None, "", "__any__", "all"):
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
        if camera_arg in (None, "", "__any__", "all"):
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
        """Search recorded footage by words.

        Answered from the PLATFORM's canonical store (``timeline.find``,
        the same query the operator's Search page runs). There is no
        second source and, deliberately, no fallback.

        It used to read footage-search's private SQLite index, first as
        the only source and then as an outage fallback. That index is
        gone: footage-search 2.0.0 deleted it, so the file is either
        absent or frozen at whatever was true on the day of the upgrade.
        Falling back to it would mean answering "was anyone at the gate
        last night?" from a database that stopped being written in
        August — and doing so during an outage, when nobody is in a
        position to notice. In a security product a stale answer
        presented as a current one is worse than no answer, so when the
        store cannot be reached this says so.

        The index was the wrong shape besides. One row per analyzed
        FRAME, hence its 60-second coalescing hack; no camera scoping of
        its own; a second 30-day retention policy on a second store; no
        evidence photo, plate or skill claims. The canonical store is
        one row per VISIT, scoped by the predicate everything else uses,
        and each hit carries its evidence frame and what each skill
        claimed about it.
        """
        if self._timeline is None:
            self.footage_search_sources["unanswerable"] += 1
            return (
                "Footage search isn't available — this agent has no "
                "connection to the OpenNVR event store."
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
        if camera_arg in (None, "", "__any__", "all"):
            camera_id = None
        elif isinstance(camera_arg, str) and self._ctx.known_camera(camera_arg):
            camera_id = camera_arg
        else:
            return (
                f"ERROR: unknown camera_id {camera_arg!r}. Use one of "
                f"{sorted(c.camera_id for c in self._ctx.cameras)} or '__any__'."
            )

        phrase = " ".join(keywords)

        # ── The canonical store first ────────────────────────────────
        if self._timeline is not None:
            start = None
            if within_minutes is not None:
                from datetime import UTC, datetime, timedelta
                start = datetime.now(UTC) - timedelta(minutes=within_minutes)
            # No try/except here, deliberately, and it is the whole
            # reason this repoint took a month to discover it had never
            # run. The call used to be wrapped in `except Exception:
            # answer = None`, which is the same branch an unreachable
            # store takes — so when the call raised AttributeError
            # because `find` did not exist on the client at all, every
            # query fell quietly through to the SQLite index and the
            # repoint read as done. `get_json` already returns None for
            # anything that goes wrong on the wire, which is the only
            # failure this code knows how to answer for. A failure it
            # cannot answer for should be loud.
            # The agent's handles are operator-chosen names
            # ("front_door"); the platform scopes by server-side id.
            # `_resolve_camera` returns the handle unchanged when a
            # camera has no server-side id yet, which the SDK would
            # reject — so an unmapped camera narrows to nothing here
            # rather than being sent as a filter the store cannot read.
            server_cam: int | None = None
            if camera_id is not None:
                resolved = self._resolve_camera(camera_id)
                try:
                    server_cam = int(resolved)
                except (TypeError, ValueError):
                    return (f"ERROR: camera '{camera_id}' has no server-side "
                            "id, so recorded footage cannot be searched for "
                            "it.")
            # A SEARCH WITH NO WINDOW IS A FULL SCAN, and this is the
            # one search a person waits through with the conversation
            # paused. Measured on a synthetic store (bench/
            # app_search_latency.py): free text with no window grows
            # linearly — 14ms at 5k visits, 222ms at 200k — while the
            # same text inside a window stays in the tens of
            # milliseconds. On a site with a year of history that is the
            # difference between an answer and a silence.
            #
            # So an omitted window becomes a recent one. What makes that
            # safe rather than merely fast is the SECOND pass below: a
            # narrowed search that finds nothing is widened to all
            # history before anything is reported. Otherwise this would
            # be trading latency for wrong answers — "no red truck came
            # by" when one did, six weeks ago — which is the worse
            # failure by a distance in a security product.
            #
            # The cost is paid only when the fast path came back empty,
            # which is exactly the case where the slower query is the
            # one actually being asked for.
            # Two flags, not one. `defaulted` says the caller gave no
            # window so a recent one was assumed; `widened` says that
            # window came back empty and the whole history was searched
            # after all. Only the second changes what the ANSWER means —
            # conflating them labels a hit from this morning as older
            # than a week.
            defaulted = start is None
            widened = False
            if defaulted:
                from datetime import UTC, datetime, timedelta
                start = datetime.now(UTC) - timedelta(minutes=DEFAULT_SEARCH_MINUTES)

            answer = await asyncio.to_thread(
                self._timeline.find, phrase,
                camera=[server_cam] if server_cam is not None else None,
                start=start, limit=10,
            )

            if (defaulted and answer is not None
                    and not (answer.get("results") or [])):
                # Nothing recent. Ask the expensive question now, because
                # now it is the question.
                answer = await asyncio.to_thread(
                    self._timeline.find, phrase,
                    camera=[server_cam] if server_cam is not None else None,
                    start=None, limit=10,
                )
                widened = True
                self.footage_search_sources["widened"] += 1

            # None means "could not reach the store", which is NOT the
            # same as "nothing matched" — in a security product those are
            # different answers, so only the second one is reported as a
            # result. The first is stated plainly, because there is
            # nowhere honest left to fall through to.
            if answer is None:
                self.footage_search_sources["unanswerable"] += 1
                return ("Could not reach the event store, so I cannot say "
                        "whether anything matched.")

            self.footage_search_sources["canonical"] += 1
            results = answer.get("results") or []
            if not results:
                # Say WHAT was searched. "No recorded footage matched"
                # after a week-long look and after an all-history look
                # are different facts, and an operator acting on the
                # first one needs to know it was the first one.
                if widened:
                    return (f"No recorded footage matched {phrase!r}, "
                            f"searching all stored history.")
                return f"No recorded footage matched {phrase!r}."
            lines = []
            for r in results:
                when = r.get("started_at") or ""
                cam = r.get("camera_id")
                descr = (r.get("caption") or r.get("label") or "match")
                extra = ""
                if r.get("plate_text"):
                    extra += f", plate {r['plate_text']}"
                if r.get("has_evidence"):
                    extra += " (photo kept)"
                lines.append(f"[#{r.get('id')}] {when} on camera {cam}: {descr}{extra}")
            total = answer.get("total")
            head = "Found in recorded footage"
            if widened:
                # The fast window found nothing and the wide one did, so
                # the match is older than a week. That is part of the
                # answer, not an implementation detail: "yes, a red
                # truck" means something different when it was in March.
                head = "Found in recorded footage (nothing in the last "
                head += f"{DEFAULT_SEARCH_MINUTES // 1440} days, so I "
                head += "searched further back)"
            if isinstance(total, int) and total > len(results):
                head += f" ({len(results)} of {total})"
            return head + ":\n" + "\n".join(lines)

    # ── Helpers ────────────────────────────────────────────────────

    # ── search_history (canonical event store — RFC-0001 C1) ──────

    @property
    def describe_path(self) -> str | None:
        """How this task's last describe was answered (see ``_describe_path``)."""
        return _describe_path.get()

    @property
    def current_question(self) -> str | None:
        """The question this task's turn is answering (see ``_question``)."""
        return _question.get()

    @current_question.setter
    def current_question(self, text: str | None) -> None:
        _question.set(text)

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
        attrs = [str(a).strip().lower()
                 for a in (args.get("attr") or []) if str(a).strip()]
        # AN ATTRIBUTE THIS TRANSPORT CANNOT CARRY IS NOT AN ATTRIBUTE
        # OF ZERO VISITS.
        #
        # `attr` reaches the events endpoint only through an app-SDK that
        # accepts it. On an older SDK the filter would be dropped
        # silently and this tool would answer "did you see a BLUE car"
        # with every car it found — which is exactly the bug being
        # fixed, moved one layer down. Refuse instead, and name the
        # reason: a limitation an operator can read is worth more than a
        # confident answer to a question nobody asked.
        if attrs and not self._events_supports("attrs"):
            return ("I can't filter by what something looked like on this "
                    "deployment — the app SDK here is older than the "
                    f"attribute filter ({', '.join(attrs)}). I can still "
                    "search by object, camera, time and plate.")
        try:
            events = await self._events.search(
                label=label, camera_id=camera_id, plate=plate,
                start=start, end=end, limit=25,
                **({"attrs": attrs} if attrs else {}),
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
            if attrs:
                # "No blue cars" and "nobody ever looked at what colour
                # they were" are opposite answers behind an identical
                # empty list, and only one of them is an answer.
                # ...unless core is describing them right now because
                # this very question asked: then "not yet" is the answer.
                pending_note = self._pending_note(events)
                return (f"No {label} visits described as "
                        f"{' and '.join(attrs)}{window}."
                        + (pending_note or
                           " Note that only visits a skill has described can "
                           "match that — if nothing here runs colour or type "
                           "descriptions, this is not the same as there having "
                           "been none.")
                        + self._in_progress_note(label, camera_arg, attrs))
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
        live_note = self._in_progress_note(label, camera_arg, attrs)
        described = f" described as {' and '.join(attrs)}" if attrs else ""
        summary = (f"I remember {len(events)} {label} visit(s){described}"
                   f"{self._window_phrase(start, end)}: "
                   + "; ".join(clauses) + ".")

        # Hand the remembered photos to the UI. The answer names times and
        # says "(photo kept)" — the photo is right there in the store, and
        # turns "I saw someone at 16:25" into something the operator can
        # check. Fetched ONCE and in parallel, shared with face-matching:
        # the two used to fetch every crop twice, one visit at a time —
        # 12 s of a 71 s field turn on a busy box.
        # Face-match when asked WHO. The model may say so explicitly; when
        # it says nothing (small models routinely omit optional booleans)
        # the question itself decides — "was Priya here" must not turn
        # into a list of times with no name because a flag went missing.
        flag = _as_bool(args.get("identify_faces"))
        if flag is None:
            who = asked_who(self.current_question)
            flag = True if who is None else who    # no question to read: match, as before
        identify = label == "person" and flag
        crops = await self._fetch_evidence(
            events, cap=max(self.EVIDENCE_FRAMES_CAP, self.FACE_ID_CAP if identify else 0))
        self._attach_evidence_frames(events, crops)
        summary += live_note + self._pending_note(events)

        # Face-match only when the question asked WHO — a recognition call
        # per photo is the wrong price for "did anyone come".
        if identify:
            names = await self._identify_visit_faces(events, crops)
            if names:
                summary += " Recognised: " + ", ".join(sorted(names)) + "."
        return summary

    def _pending_note(self, events=None) -> str:
        """"Not yet" as a sentence. Core's `pending` (on the rows the SDK
        returned, or on the client for an older SDK) says how many
        matching visits nobody has described yet and that they are being
        described now; the agent says it in the answer so "no blue car"
        is never said about visits nobody has looked at. Empty when
        nothing is pending."""
        pending = getattr(events, "pending", None)
        if pending is None:
            pending = getattr(self._events, "last_pending", None)
        if not isinstance(pending, dict):
            return ""
        missing = int(pending.get("missing") or 0)
        queued = int(pending.get("requested") or 0) + int(pending.get("already_queued") or 0)
        if missing <= 0 or queued <= 0:
            return ""
        eta = float(pending.get("eta_s") or 0)
        if eta >= 90:
            when = f"about {max(1, round(eta / 60))} minutes"
        elif eta > 0:
            when = f"about {max(5, int(round(eta / 5.0) * 5))} seconds"
        else:
            when = "shortly"
        more = f" (the newest {queued} first)" if missing > queued else ""
        plural = missing != 1
        return (f" {missing} visit{'s' if plural else ''} in that window "
                f"{'have' if plural else 'has'} not been described yet — "
                f"I'm describing them now{more}, {when}. Ask me again then.")

    def _events_supports(self, param: str) -> bool:
        """Does the installed app SDK's events client accept ``param``?

        Asked of the object rather than pinned to a version, because the
        agent runs against whatever SDK the deployment installed and a
        version pin would turn "your SDK is older" into an install
        failure. A client that takes **kwargs is taken at its word.
        """
        try:
            import inspect

            sig = inspect.signature(self._events.search)
        except (TypeError, ValueError):  # builtins, C, or a mock
            return True
        if param in sig.parameters:
            return True
        return any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in sig.parameters.values())

    def _in_progress_note(self, label: str, camera_arg,
                          attrs: list[str] | tuple[str, ...] = ()) -> str:
        """A visit enters the store only when it ENDS, so history alone answers
        'did anyone come today?' with a straight no while the person is
        literally standing on camera. Seen in the field: the operator walked in
        to test, asked, and was denied — the visit was still being written.
        Cross-check the live Tier-0 ring and say so.

        ``attrs`` is what the question narrowed by, and the ring CANNOT
        evaluate it. Tier-0 emits a class and a box; it does not know
        colour, type or who somebody is. A test here already says a cat on
        camera is not evidence about a person question — this is that same
        rule one level down, which was missed: a car on camera is not
        evidence about a RED car question. So when the question carried
        attributes, the note says which part of it the live view cannot
        answer instead of asserting a relevance it does not have.

        The note is also written to survive being quoted ALONE. It is
        appended to an answer, and a small model summarising the tool
        result can keep this sentence and drop the one before it — which
        is exactly what happened in the field, turning "no red car in the
        last hour" into "I see a car on cam1 right now". A sentence that
        misleads when read on its own is a sentence that will mislead.
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
        where = ', '.join(sorted(cams))
        if attrs:
            # Named, not hidden: "a car, I can't tell if it's red" is a
            # different and honest answer to "was there a red car".
            return (f" Separately, and not an answer to that question: a "
                    f"{label} IS on {where} right now, but nothing has "
                    f"described it, so I cannot tell whether it is "
                    f"{' and '.join(attrs)}. That visit enters history when "
                    f"it ends.")
        return (f" Separately, right now a {label} IS on {where} — "
                f"that visit is still in progress and enters history when it ends.")

    #: How many remembered photos an answer shows, and how many it
    #: face-matches when asked who. Both bound one turn's evidence work.
    EVIDENCE_FRAMES_CAP = 3
    FACE_ID_CAP = 4
    #: Extra crops fetched so a failed read does not reduce the count.
    EVIDENCE_SLACK = 2

    async def _fetch_evidence(self, events, cap: int) -> dict[int, bytes]:
        """The kept crops of the first ``cap`` visits that have one, fetched
        concurrently. One fetch per visit per turn, shared by the frames
        shown and the faces matched. Fetches a little past ``cap`` so one
        bad read (a 404, an oversize file) does not cost a photo: the
        callers take the first ``cap`` that came back, in order."""
        if self._events is None or cap <= 0:
            return {}
        wanted = [e for e in events if getattr(e, "has_evidence", False)][:cap + self.EVIDENCE_SLACK]
        if not wanted:
            return {}

        async def one(e):
            try:
                return e.id, await self._events.evidence(e.id)
            except Exception:
                # A photo we cannot fetch costs a frame or a NAME, not the
                # answer. One bad read once turned "I remember 3 visits at
                # 15:12, 15:40 and 16:25" into no answer at all.
                logger.warning("search_history: evidence fetch failed for #%s", e.id)
                return e.id, None

        out: dict[int, bytes] = {}
        for eid, crop in await asyncio.gather(*(one(e) for e in wanted)):
            if crop and len(crop) <= 2_000_000:          # same cap as live frames
                out[eid] = crop
        return out

    def _attach_evidence_frames(self, events, crops: dict[int, bytes]) -> None:
        """Publish up to EVIDENCE_FRAMES_CAP remembered best-frames onto this turn.

        Each carries its own timestamp caption. That is not decoration: every
        frame the UI has ever rendered in a chat bubble was the LIVE view, so
        an uncaptioned crop from 16:25 sitting in an answer about this
        afternoon would read as "here is your camera now" — the wrong thing to
        get wrong in a security product.
        """
        for e in events:
            if len(self.last_evidence_frames) >= self.EVIDENCE_FRAMES_CAP:
                break
            crop = crops.get(e.id)
            if not crop:
                continue
            self.last_evidence_frames.append({
                "camera_id": str(e.camera_id),
                "caption": f"#{e.id} · {self._clock_phrase(e.started_at)}",
                "jpeg_b64": base64.b64encode(crop).decode("ascii"),
            })

    async def _identify_visit_faces(self, events, crops: dict[int, bytes]) -> set:
        """Names recognised on up to FACE_ID_CAP of the fetched crops — the
        recognitions run concurrently, and one failing does not cost the
        others their name."""
        picked = [crops[e.id] for e in events if crops.get(e.id)][:self.FACE_ID_CAP]
        if not picked or self._recognise is None:
            return set()

        async def one(crop):
            try:
                response = await self._recognise.infer(
                    frame_jpeg=crop, extra={"task": "face_recognition"})
            except Exception:
                logger.warning("search_history: recognition adapter unavailable")
                return None
            result = (response or {}).get("result") or {}
            if result.get("recognized"):
                return str(result.get("name") or result.get("person_id") or "someone")
            return None

        return {n for n in await asyncio.gather(*(one(c) for c in picked)) if n}

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
            raw = _camera_list_from_text(raw)
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


def _camera_list_from_text(text: str) -> list[str] | None:
    """``camera_ids`` that a small model sent as text instead of a list.

    llama3.2:1b sends ``"['front_door']"`` — the tool description's own
    example, ``['cam1','cam2']``, copied as a string — and the whole string
    used to be looked up as one camera name, so every such call failed.
    A JSON or Python list literal is read as the list it spells, a comma
    list is split, anything else is one name. Literals only: nothing is
    evaluated. None when the text is a literal but not a list of names."""
    text = text.strip()
    if not text.startswith("["):
        return [p.strip() for p in text.split(",") if p.strip()]
    if len(text) > 1000:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return None
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    return None
