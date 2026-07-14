# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
camera-agent — a voice agent that grounds its answers in live OpenNVR
camera feeds via tool calling.

Pipeline:
    WebSocket transport (browser ⇄ server raw PCM 16k mono)
        ↓
    SileroVADAnalyzer (turn detection)
        ↓
    OpenNvrWhisperSTT (Whisper adapter → text)
        ↓
    LLM context aggregator (Pipecat)
        ↓
    OpenNvrOllamaLLM (Ollama adapter + 4 registered tools)
        ↓
    OpenNvrPiperTTS (Piper adapter → PCM audio)
        ↓
    WebSocket transport (audio back to browser)

Run:
    python camera_agent.py --config config.yml

Then visit http://localhost:9100/demo in your browser, click "Start",
and speak.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import difflib
import json
import logging
import math
import re
import signal
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response

from adapter_clients import (
    AppRegistryClient,
    KaicAdapterClient,
    KaicCapabilitiesClient,
    OllamaClient,
    OpenAILLMClient,
    OpennvrAuthClient,
    OpennvrRecordingsClient,
    PiperClient,
    SyntheticDetectionClient,
    WhisperClient,
)
from context import (
    CameraContext,
    CameraSpec,
    run_app_alert_subscriber,
    run_event_subscriber,
)
from frame_sources import build_frame_source, discover_local_cameras
from monitor_host import MonitorHost
from tools import CameraTools, build_tool_definitions

logger = logging.getLogger("camera-agent")


# ── Config ──────────────────────────────────────────────────────────


@dataclass
class AppConfig:
    """Operator-tunable settings. Validated in ``load_config``."""

    # KAI-C (for the vision tool calls — auditable).
    kaic_url: str
    kaic_api_key: str

    # Optional single BASE URL for the OpenNVR deployment. When set, any UNSET
    # sibling URL that points at the SAME deployment is derived from it, so a
    # typical single-deployment install only has to configure one address:
    #   opennvr_api_url ← opennvr_base_url   (the server API origin)
    #   opennvr_ui_url  ← opennvr_base_url   (the main OpenNVR web UI)
    # kaic_url and nats_inference_url are DELIBERATELY not derived — the KAI-C
    # inference gateway and the NATS event bus are separate services on their
    # own ports, so they stay explicit even when they live in the same
    # deployment. Any per-field value set explicitly always wins over the base
    # (the base only fills in the blanks). Unset → each sibling keeps its own
    # value / stays None, i.e. fully backward-compatible.
    opennvr_base_url: str | None = None
    detection_adapter: str = "yolov8"
    recognition_adapter: str = "insightface"
    caption_adapter: str = "blip"

    # Direct adapter URLs (streaming voice path — bypasses KAI-C in v0.1).
    whisper_url: str = "http://127.0.0.1:9003"
    whisper_token: str = ""
    # Drop Whisper transcripts that are almost certainly background noise /
    # silence hallucinations ("Thank you.", "you", "Thanks for watching") so
    # ambient noise doesn't start a spurious conversation turn.
    stt_noise_filter: bool = True
    # Wake-word gate (voice only). When True, the agent only answers an
    # utterance that's addressed to it by name ("Hey <name>, …") — so it
    # doesn't respond to the TV, a side conversation, or its own echoed reply.
    # The UI exposes a per-session toggle that overrides this default.
    wake_word_required: bool = True
    # Extra wake words/phrases on top of the persona name + built-in aliases —
    # e.g. ["computer", "hey camera"] for words STT transcribes more reliably.
    wake_words: list[str] | None = None
    # Wake-word matching: the name's known spellings match EXACTLY, plus a TIGHT
    # fuzzy safety net (0.85) that catches close STT drift (e.g. a name → its common misspelling)
    # but still rejects real words (current/clearing/camera all score ≤0.67). Set to
    # 1.0 for exact-only, or lower (e.g. 0.72) to be more forgiving.
    wake_fuzzy: float = 0.85
    # Require an address word ("hey/ok/hi") before the name (the "Hey Siri"
    # model). On = far fewer false wakes (a bare word that sounds like the name
    # won't trigger). Off = the bare name also wakes her.
    wake_require_prefix: bool = True
    ollama_url: str = "http://127.0.0.1:9004"
    ollama_token: str = ""

    # LLM brain provider. "ollama" (default) → local Ollama at ollama_url.
    # "openai" → any OpenAI-compatible chat API at llm_base_url (cloud or a
    # local OpenAI-API server) with llm_api_key. The cloud/hybrid path gives
    # stronger, lower-latency tool-calling without a local LLM (issue #82).
    llm_provider: str = "ollama"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    piper_url: str = "http://127.0.0.1:9001"
    piper_token: str = ""

    # LLM tuning.
    llm_model: str = "qwen2.5:1.5b"
    llm_temperature: float = 0.4
    llm_max_tokens: int = 256
    # Reasoning toggle for "thinking" models (Qwen3 etc.). Leave None for
    # non-thinking models (no effect). Set False to force snappy, non-thinking
    # tool-calling (appends Qwen3's ``/no_think`` switch); True to allow it.
    llm_think: bool | None = None
    # Limited-hardware knobs (local Ollama only). llm_num_threads caps CPU cores
    # (None = all). llm_num_ctx sizes the context window (lower = less RAM /
    # faster prefill, but must hold the prompt).
    llm_num_threads: int | None = None
    llm_num_ctx: int = 4096
    # How long Ollama keeps the model resident (sent on every request so a
    # host-run Ollama stays warm even without OLLAMA_KEEP_ALIVE in its env).
    # -1 = forever (default). A duration string like "5m" frees RAM when idle.
    llm_keep_alive: str | float = -1

    # Text/chat mode: the UI defaults to a text box (no mic) and the voice
    # adapters (Whisper/Piper) aren't required. The lighter on-ramp — used by
    # the camera-agent-chat compose profile (config.docker.chat.yml).
    text_mode: bool = False

    # Which tools to advertise to the LLM. None = all. Restricting this
    # shortens the prompt (faster CPU prefill) and stops small models
    # picking tools whose adapters aren't deployed. See build_tool_definitions.
    enabled_tools: list[str] | None = None

    # Caching / event ring.
    frame_cache_ttl_seconds: float = 2.0
    event_ring_size: int = 256

    # Optional NATS for the recent_events tool.
    nats_inference_url: str | None = None
    nats_inference_token: str | None = None

    # Optional path to the footage-search SQLite index. When set and the
    # file exists, the agent gains a ``search_footage`` tool that answers
    # natural-language questions about the recorded past ("did a red
    # truck come by earlier?"). Build the index with the footage-search
    # example's ``index`` subcommand.
    footage_index_path: str | None = None

    # Optional OpenNVR camera discovery. Docker uses this so camera-agent can
    # reuse cameras configured in OpenNVR instead of duplicating RTSP URLs.
    opennvr_cameras_url: str | None = None
    opennvr_api_key: str | None = None

    # Optional OpenNVR server BASE URL (e.g. "http://127.0.0.1:8000") for the
    # APP DOOR: when set, the agent discovers installed catalog apps via
    # GET {opennvr_api_url}/api/v1/apps and can relay one app's live state via
    # GET {opennvr_api_url}/api/v1/apps/{id}/status — surfacing every installed
    # app as a READ-ONLY conversational skill. Auth is X-Internal-Api-Key: the
    # key is opennvr_api_key, falling back to kaic_api_key (the same deployment
    # INTERNAL_API_KEY) or the INTERNAL_API_KEY env var. The agent only READS
    # here — it never enables / disables / configures an app (that stays an
    # operator action; the agent guides). Unset → no app skills, cleanly.
    opennvr_api_url: str | None = None

    # Authentication for the agent's own HTTP surface. "none" (default) =
    # today's open loopback demo. "opennvr" = every non-page endpoint
    # requires an OpenNVR-issued bearer token, validated against
    # {opennvr_api_url}/api/v1/auth/me and mapped to tiers by role NAME:
    # superuser/admin -> admin, operator -> operator, else viewer. The
    # agent never mints tokens of its own — the login endpoints are thin
    # proxies, so revocation/MFA stay the server's job and a mobile app
    # speaks the identical bearer contract. See AGENT_DESIGN.md.
    auth_mode: str = "none"

    # Per-site target→annunciation defaults, merged over the built-ins
    # (fire/smoke/flame/gas → siren). A farm adds  snake: siren ; a bank
    # arms its after-hours person alarm as  person: siren . An explicit
    # ring on the alarm always wins; this only decides the DEFAULT.
    alarm_ring_defaults: dict[str, str] | None = None

    # Public base URL of THIS agent (e.g. "https://agent.nvr.example"), used
    # only to put a tap-to-open deep link (/demo/camera/{id}) into outgoing
    # notifications — a phone push lands on the right camera's screen.
    # Unset = notifications carry no link.
    agent_public_url: str | None = None

    # Optional base URL of the main OpenNVR UI (e.g. "https://nvr.example"),
    # used only to build a deep link into the AI Adapters view when a skill is
    # greyed out for want of a backing adapter. The agent GUIDES the operator
    # there — it never enables or approves an adapter itself (that stays an
    # operator action behind OpenNVR's permission gate). Unset → no link, the
    # skills panel just names the suggested adapter(s) in text.
    opennvr_ui_url: str | None = None

    # Optional emergency contacts for alarms, keyed by alarm target/name
    # (e.g. {"fire": "+1-555-0100"}). The actual call-out is a documented
    # future integration (see ALARMS.md); for now an armed alarm with a
    # matching contact records "would alert <number>" when it fires.
    emergency_contacts: dict[str, str] | None = None

    # Identity used for the optional wake word ("Hey <agent_name>") and event
    # metadata. The demo presents the agent as "the OpenNVR Agent" (chat
    # label: "agent") and never uses this as a persona name. The default value
    # stays "Camera Agent" for infra/wake-word compat. If you turn on a
    # named wake word, pick a name STT transcribes reliably and register its
    # spellings in wake_words (an unusual name is often mis-heard).
    agent_name: str = "Camera Agent"
    # Spoken voice (the Piper voice configured in the ai-adapter). Independent of
    # the name — switch to "male" for a male voice without changing the name.
    voice_gender: str = "neutral"
    # Talking-avatar VIDEO in the demo. When true the UI plays the bundled clips
    # (demo/avatar/{idle,speaking,thinking}.{webm,mp4}) — swap in your own (e.g. a
    # HeyGen export). When false the UI uses the built-in animated SVG face only.
    avatar_video: bool = True

    # External notifications: webhook URLs alarms/watches fan out to so alerts
    # reach you when the browser tab is closed (Slack/Discord/n8n/Home
    # Assistant/custom all accept a JSON POST). ``notify_events`` selects which
    # categories are sent (default alarms + watch notifications). See
    # NOTIFICATIONS.md.
    notify_webhooks: list[str] | None = None
    notify_events: list[str] | None = None
    # Apprise URLs (https://github.com/caronc/apprise) — one line per service:
    # mailto://, tgram://, ntfy://, pover://, twilio://, … 100+ services
    # through one optional dependency (`pip install apprise`). Same event
    # filter as the webhooks; URLs no-op with a warning if the package is
    # missing. See NOTIFICATIONS.md.
    notify_apprise: list[str] | None = None

    # Optional path to a JSON file that persists alarms, watches, and report
    # schedules so they survive a restart. Unset → in-memory only.
    state_path: str | None = None

    # Optional face-management API (enroll/list/forget known people) for the
    # watchlist. Points at the recognition adapter's faces REST API. Unset →
    # the enroll/list/forget tools report face management isn't configured.
    # See FACES.md for the expected contract.
    faces_url: str | None = None
    faces_token: str | None = None

    # HTTP listen address.
    host: str = "127.0.0.1"
    port: int = 9100

    # System prompt + cameras.
    system_prompt: str = ""
    cameras: list[CameraSpec] = None  # type: ignore[assignment]

    # Run on whatever hardware you're already on: if no cameras are configured
    # and this is set, the agent discovers a local capture device (laptop
    # webcam, USB/Pi camera, drone /dev/video node) and uses it. Zero camera
    # provisioning — great for devs and edge devices. ``auto_discover_all``
    # exposes every enumerated device instead of just the first.
    auto_discover_cameras: bool = False
    auto_discover_all: bool = False

    # Demo mode: serve scripted ``synth:`` cameras and read detections from
    # their embedded ground truth instead of calling KAI-C/YOLO. Lets the whole
    # agent run with no cameras/adapters — for recording the demo or a quick try.
    synthetic_detection: bool = False


_DEFAULT_SYSTEM_PROMPT = (
    "You are a concise voice assistant for a home security camera system. "
    "You have NO knowledge of what any camera currently shows — the ONLY way "
    "to know is to call a tool.\n\n"
    "RULES:\n"
    "- For ANY question about what a camera sees, what is happening, who or "
    "what is present, or how many of something, you MUST call detect_objects "
    "or describe_camera BEFORE answering.\n"
    "- NEVER invent, guess, or describe what is on a camera from imagination. "
    "If you have not called a tool this turn, you do not know.\n"
    "- Base your answer ONLY on the tool result, in 1-2 short spoken sentences.\n"
    "- If a tool says a camera cannot be reached, tell the user that camera "
    "appears to be offline."
)

# The agent's persona name follows the configured voice gender. The actual
# spoken voice is whichever Piper voice the ai-adapter serves; ``voice_gender``
# here selects the matching pronouns the agent uses.
# The agent has no persona name: it presents as "the OpenNVR Agent" (chat
# label "agent"). ``agent_name`` below is a technical default used only for event
# metadata and the OPTIONAL wake word — it keeps the legacy "Camera Agent"
# value for infra/wake-word compat. The voice is a separate choice.
DEFAULT_AGENT_NAME = "Camera Agent"
DEFAULT_VOICE_GENDER = "neutral"

# ── Skills ──────────────────────────────────────────────────────────
# A "skill" is a user-facing capability the agent carries. Each maps to the
# tool(s) it exposes to the LLM; switching a skill off drops those tools from
# the advertised set (the agent "reconfigures" — see _configure_tools), so the
# model can no longer call them. Some skills need a backend (a recognition
# adapter, the event bus, or a footage index) and can't be enabled until it's
# configured. First tool in each list is the "primary" (used for gating).
SKILL_TOOLS: dict[str, list[str]] = {
    "see": ["describe_camera"],
    "count": ["detect_objects"],
    "faces": ["recognize_faces", "enroll_face", "list_people", "forget_face"],
    "events": ["recent_events"],
    "footage": ["search_footage"],
    "alarm": ["create_alarm", "stop_alarm"],
    "watch": ["create_monitor", "stop_monitor"],
    "report": ["create_report", "stop_report"],
    "task": ["create_background_task"],
    "apps": ["list_apps", "app_status", "recent_app_alerts"],
}
# id, icon, name, example question, requirement key ("" = always available)
_SKILL_META: list[tuple[str, str, str, str, str]] = [
    ("see", "🎥", "See what's happening now",
     "What's happening at the front door?", ""),
    ("count", "🔢", "Detect & count people and objects",
     "How many people are in the back yard?", ""),
    ("faces", "🧑", "Recognise & enroll faces",
     "Who's at the front door?", "faces"),
    ("events", "⏱", "Look back at recent events",
     "Did anyone come to the door in the last 30 minutes?", "events"),
    ("footage", "🔎", "Search recorded footage",
     "Did a red truck come by earlier today?", "footage"),
    ("alarm", "🔔", "Set alarms",
     "Alarm me if someone is at the door after 10pm.", ""),
    ("watch", "👁", "Watch & count over time",
     "Watch the driveway and tell me if more than 3 cars show up.", ""),
    ("report", "📋", "Schedule reports",
     "Every morning at 7, summarise overnight activity.", ""),
    ("task", "⚙", "Run longer searches in the background",
     "Check every camera for anyone in a red shirt.", ""),
    ("apps", "🧩", "Query installed catalog apps",
     "What apps are running, and is the loitering detector healthy?", "apps"),
]
# Which KAI-C task strings (``tasks_advertised``, aggregated live from
# GET /api/v1/ai/capabilities) back each skill. Advisory display data
# only: ``skill_requirement_met`` stays the enable gate, so a briefly
# unreachable KAI-C never disables a working tool — the UI just loses
# the live "is an adapter for this actually registered?" signal.
# ``see`` is satisfied by EITHER a captioning or a VQA adapter; the
# converged watch monitors (count/crossing → occupancy/line_crossing
# SDK rules) ride on object detection. Skills not listed here (events,
# footage, alarm, report, task) don't consume KAI-C inference.
#
# This map is now DERIVED from the bundled canonical task registry
# (``tasks.yml``, a copy of ``server/config/tasks.yml``): each task's
# ``agent_skill`` says which skill it backs, and its ``aliases`` are
# folded in so an adapter advertising EITHER spelling (e.g. a
# ``scene_caption`` or an ``image_captioning`` adapter) satisfies the
# ``see`` skill. The upshot: adding a task to tasks.yml with an
# ``agent_skill`` makes it an agent skill backing with NO code change
# here. ``_SKILL_BACKING_FALLBACK`` is the last-known-good hardcoded map,
# used verbatim if the bundled file is missing/unreadable (never crash).

# ``watch`` has no ``agent_skill`` of its own in tasks.yml (its converged
# count/crossing monitors ride on the same detection ``count`` uses), so
# it inherits whatever tasks back the named skill.
_SKILL_TASK_INHERITS: dict[str, str] = {"watch": "count"}

_SKILL_BACKING_FALLBACK: dict[str, list[str]] = {
    "see": ["image_captioning", "vqa"],
    "count": ["object_detection"],
    "faces": ["face_recognition"],
    "watch": ["object_detection"],
}

TASKS_REGISTRY_PATH = Path(__file__).resolve().parent / "tasks.yml"


def _derive_skill_backing_tasks() -> dict[str, list[str]]:
    """Invert the bundled task registry's ``agent_skill`` field into
    ``skill id -> [canonical task + its aliases]``.

    Both the canonical name and every alias are included so live-adapter
    matching succeeds regardless of which spelling an adapter advertises
    (contract §4: the canonical-alias case, e.g. scene_caption ≡
    image_captioning). Skills that inherit another skill's backing
    (``_SKILL_TASK_INHERITS``) get its derived tasks. On any error —
    missing file, bad YAML — falls back to the hardcoded map so the agent
    never fails to start over a taxonomy file."""
    try:
        raw = yaml.safe_load(TASKS_REGISTRY_PATH.read_text()) or []
        derived: dict[str, list[str]] = {}
        for entry in raw:
            skill = entry.get("agent_skill")
            if not skill:
                continue
            names = [entry["task"], *(entry.get("aliases") or [])]
            for name in names:
                if name not in derived.setdefault(skill, []):
                    derived[skill].append(name)
        for skill, source in _SKILL_TASK_INHERITS.items():
            if source in derived:
                derived[skill] = list(derived[source])
        if not derived:                       # empty/degenerate file → fallback
            return dict(_SKILL_BACKING_FALLBACK)
        return derived
    except Exception:                          # pragma: no cover - defensive
        logger.warning(
            "could not load bundled tasks.yml (%s); using the hardcoded "
            "skill-backing map", TASKS_REGISTRY_PATH, exc_info=True,
        )
        return dict(_SKILL_BACKING_FALLBACK)


_SKILL_BACKING_TASKS: dict[str, list[str]] = _derive_skill_backing_tasks()


def _derive_skill_suggested_adapters() -> dict[str, list[str]]:
    """Union the ``suggested_adapters`` of every task backing each skill,
    from the bundled task registry — ``skill id -> [adapter names]``, order
    preserved and deduped.

    This is the editorial "which adapter provides this skill" answer the
    agent surfaces when a skill is greyed out (no live adapter advertises a
    backing task). It's GUIDANCE ONLY: it names adapters for the operator to
    enable in the AI Adapters view; the agent never enables one. Inherited
    skills (``_SKILL_TASK_INHERITS``) pick up their source's adapters. On any
    error the map is simply empty — a missing suggestion never breaks the UI."""
    try:
        raw = yaml.safe_load(TASKS_REGISTRY_PATH.read_text()) or []
        by_task: dict[str, list[str]] = {}
        for entry in raw:
            names = [entry["task"], *(entry.get("aliases") or [])]
            adapters = entry.get("suggested_adapters") or []
            for name in names:
                by_task[name] = list(adapters)
        derived: dict[str, list[str]] = {}
        for skill, tasks in _SKILL_BACKING_TASKS.items():
            seen: list[str] = []
            for task in tasks:
                for adapter in by_task.get(task, []):
                    if adapter not in seen:
                        seen.append(adapter)
            derived[skill] = seen
        return derived
    except Exception:                          # pragma: no cover - defensive
        logger.warning(
            "could not derive skill suggested adapters from %s",
            TASKS_REGISTRY_PATH, exc_info=True,
        )
        return {}


_SKILL_SUGGESTED_ADAPTERS: dict[str, list[str]] = _derive_skill_suggested_adapters()


def _derive_skill_suggested_apps() -> dict[str, list[str]]:
    """Union the ``suggested_apps`` of every task backing each skill, from
    the bundled task registry — ``skill id -> [app ids]``, order preserved
    and deduped.

    The app-install twin of ``_derive_skill_suggested_adapters``: when a
    skill is greyed out for want of a backing adapter, the agent can point
    the operator either at a raw adapter to enable OR at a packaged catalog
    app that delivers the same capability (e.g. object_detection →
    intrusion-detection / occupancy-counting). GUIDANCE ONLY: the agent
    never installs an app — the link opens the App Catalog. Inherited
    skills (``_SKILL_TASK_INHERITS``) pick up their source's apps. On any
    error the map is empty — a missing suggestion never breaks the UI."""
    try:
        raw = yaml.safe_load(TASKS_REGISTRY_PATH.read_text()) or []
        by_task: dict[str, list[str]] = {}
        for entry in raw:
            names = [entry["task"], *(entry.get("aliases") or [])]
            apps = entry.get("suggested_apps") or []
            for name in names:
                by_task[name] = list(apps)
        derived: dict[str, list[str]] = {}
        for skill, tasks in _SKILL_BACKING_TASKS.items():
            seen: list[str] = []
            for task in tasks:
                for app in by_task.get(task, []):
                    if app not in seen:
                        seen.append(app)
            derived[skill] = seen
        return derived
    except Exception:                          # pragma: no cover - defensive
        logger.warning(
            "could not derive skill suggested apps from %s",
            TASKS_REGISTRY_PATH, exc_info=True,
        )
        return {}


_SKILL_SUGGESTED_APPS: dict[str, list[str]] = _derive_skill_suggested_apps()


def _derive_skill_hardware() -> dict[str, list[dict[str, Any]]]:
    """The ``hardware`` blocks of every task backing each skill, from the
    bundled task registry — ``skill id -> [{task, label, adapters, min,
    recommended, note}]``, canonical tasks only (aliases share the
    canonical's block, so listing both would double-count).

    Drives the demo UI's "Hardware" panel: editorial guidance about what
    the enabled skills run on vs. what makes them fast. ADVISORY ONLY —
    fallbacks always work; nothing gates on this. Inherited skills
    (``_SKILL_TASK_INHERITS``) pick up their source's rows. On any error
    the map is empty — a missing hint never breaks the UI."""
    try:
        raw = yaml.safe_load(TASKS_REGISTRY_PATH.read_text()) or []
        derived: dict[str, list[dict[str, Any]]] = {}
        for entry in raw:
            skill = entry.get("agent_skill")
            hw = entry.get("hardware")
            if not skill or not isinstance(hw, dict):
                continue
            derived.setdefault(skill, []).append({
                "task": entry["task"],
                "label": entry.get("label") or entry["task"],
                "adapters": list(entry.get("suggested_adapters") or []),
                "min": str(hw.get("min") or ""),
                "recommended": str(hw.get("recommended") or ""),
                "note": str(hw.get("note") or ""),
            })
        for skill, source in _SKILL_TASK_INHERITS.items():
            if source in derived:
                derived[skill] = [dict(r) for r in derived[source]]
        return derived
    except Exception:                          # pragma: no cover - defensive
        logger.warning(
            "could not derive skill hardware hints from %s",
            TASKS_REGISTRY_PATH, exc_info=True,
        )
        return {}


_SKILL_HARDWARE: dict[str, list[dict[str, Any]]] = _derive_skill_hardware()


# Phrases Whisper commonly hallucinates from silence / background noise / the
# end of an utterance. Matched case-insensitively after stripping punctuation,
# so noise doesn't trigger a turn. (English-only; extend per deployment.)
_STT_NOISE_PHRASES = frozenset({
    "", "you", "thank you", "thanks", "thank you very much", "thanks for watching",
    "thank you for watching", "please subscribe", "subscribe", "like and subscribe",
    "bye", "bye bye", "goodbye", "okay", "ok", "uh", "um", "hmm", "mm", "mhm",
    "yeah", "yep", "so", "the", "a", "i", "and", "music", "applause", "silence",
    "subtitles by the amara.org community", "transcription by", "amara.org",
})


def looks_like_noise(transcript: str) -> bool:
    """True if a transcript is almost certainly a noise/silence hallucination
    rather than a real spoken question — so the voice loop can ignore it and
    keep listening instead of answering phantom turns."""
    t = (transcript or "").strip().lower()
    if not t:
        return True
    norm = t.strip(" .,!?-—…\"'()[]*").strip()
    if norm in _STT_NOISE_PHRASES:
        return True
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 2:                       # punctuation / single letter
        return True
    words = norm.split()
    if len(words) <= 2 and all(w in _STT_NOISE_PHRASES for w in words):
        return True
    return False


def _humanize_for_speech(text: str, cameras=None) -> str:
    """Make a raw tool-result string sound natural when spoken: refer to each
    camera by its location ("the front door") instead of its raw id
    ("front_door"), drop underscores, and turn the "name: detail" colon into a
    comma. Used for the fallback reply so the user never hears
    "On front_door colon 2 people"."""
    s = text or ""
    if cameras:
        # Replace longer ids first so "front_door_2" isn't half-matched.
        for cam in sorted(cameras, key=lambda c: len(c.camera_id), reverse=True):
            role = (getattr(cam, "role", "") or "").strip()
            if role and role != "(no role configured)":
                s = s.replace(cam.camera_id, role)
    return s.replace("_", " ").replace(": ", ", ")


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>.*$", re.DOTALL | re.IGNORECASE)


def _strip_think(text: str) -> str:
    """Remove Qwen3-style ``<think>…</think>`` reasoning so it never reaches the
    user/TTS. Handles a *truncated* block too (the model ran out of tokens
    mid-thought, leaving an unclosed ``<think>`` with no answer after it).
    Returns whatever real answer remains (often empty when all the budget went
    to thinking — the caller then falls back to the tool result)."""
    s = _THINK_BLOCK_RE.sub("", text or "")
    s = _THINK_OPEN_RE.sub("", s)
    return re.sub(r"</?think>", "", s, flags=re.IGNORECASE)


def _clean_for_speech(text: str, cameras=None) -> str:
    """Make an LLM reply speakable: strip ``<think>`` reasoning + markdown, and
    map any raw camera id the model echoed ("cam1") to its location."""
    s = _strip_think(text or "")
    s = re.sub(r"[*`#]+", "", s)
    if cameras:
        for cam in sorted(cameras, key=lambda c: len(c.camera_id), reverse=True):
            role = (getattr(cam, "role", "") or "").strip()
            if role and role != "(no role configured)":
                s = s.replace(cam.camera_id, role)
    return s.strip()


def agent_name_for(name: str | None) -> str:
    """Normalise the configured agent name, falling back to the default."""
    return (name or "").strip() or DEFAULT_AGENT_NAME


# ── Wake-word gating (voice) ────────────────────────────────────────
# Only treat a spoken utterance as a question when it's addressed to the
# agent by name ("Hey <name>, what's at the door?"). Without this, ANY
# speech in the room — the TV, a side conversation, or the agent re-hearing
# its own reply through the speakers — becomes a turn, which is the single
# biggest cause of spurious/looping/"hallucinated" answers in a live room.
# Transcript-side: reuses Whisper, adds no model, stays fully local.

# EXACT spelling variants Whisper produces for each persona name — all count as
# the wake word, matched exactly (no fuzzy). Pick a name made of clear sounds and
# add the 2-3 spellings STT actually emits (watch the "wake score" log when you
# test). Avoid real-word spellings (e.g. "mirror") that would false-wake.
_WAKE_ALIASES = {
    # "Kiran" — STT usually writes it as the familiar "Kieran"; register both
    # (plus "keeran") so the transcript gate matches regardless of spelling.
    "kiran": ["kiran", "kieran", "keeran"],
}
# Words the user might say to address the agent before the name.
_ADDRESS_WORDS = {"hey", "hi", "hello", "ok", "okay", "yo", "hai"}
_WAKE_LEAD = r"(?:hey|hi|hello|ok|okay|yo|hai)?[\s,]*"      # prefix optional
_WAKE_LEAD_REQ = r"(?:hey|hi|hello|ok|okay|yo|hai)[\s,]+"   # prefix REQUIRED


def wake_phrases(agent_name: str, extra: list[str] | None = None) -> list[str]:
    """Lowercase names that invoke the agent (persona name + known aliases +
    any operator-configured ``wake_words``)."""
    base = (agent_name or "").strip().lower()
    names = list(_WAKE_ALIASES.get(base, []))
    if base and base not in names:
        names.insert(0, base)
    for w in (extra or []):
        w = str(w).strip().lower()
        if w and w not in names:
            names.insert(0, w)
    return names


def match_wake(
    transcript: str, agent_name: str,
    extra_words: list[str] | None = None, fuzzy: float = 0.85,
    require_prefix: bool = True,
) -> tuple[bool, str]:
    """Return ``(invoked, question)``.

    ``invoked`` is True when the transcript opens by addressing the agent by
    name. ``question`` is the remainder with the wake phrase + leading
    "hey"/punctuation stripped — '' when only the name was said.

    Two passes: an exact word match on the name/aliases (also catches "hey
    <name>, <question>" in one breath), then an optional FUZZY pass (when
    wake_fuzzy < 1.0) — a tight safety net for close STT drift the registered
    spellings miss. The fuzzy pass compares the leading 1-2 words to the wake
    phrases and accepts a close-enough match.
    """
    t = (transcript or "").strip()
    if not t:
        return False, ""
    low = t.lower()
    # Exact pass matches the name + aliases + operator wake_words; the fuzzy
    # pass uses ONLY the persona name + built-in aliases. Operator-chosen
    # wake_words (e.g. "camera") are reliable English words that DON'T need
    # fuzzy tolerance — fuzzying them would false-wake on look-alikes
    # ("cam" vs "calm"). So they match exactly only.
    names_exact = wake_phrases(agent_name, extra_words)
    names_fuzzy = wake_phrases(agent_name)
    # When require_prefix is on, the utterance MUST open with an address word
    # ("hey/ok/hi …") before the name — so a bare word that merely sounds like
    # the name ("sit" → "Sita") can't false-wake. This is the "Hey Siri" model.
    lead_re = _WAKE_LEAD_REQ if require_prefix else _WAKE_LEAD

    # Pass 1 — exact name/alias/wake-word at the start. Longest phrases first so
    # a multi-word alias ("shaila ja") wins over its prefix ("shaila").
    for name in sorted(names_exact, key=len, reverse=True):
        m = re.search(r"^[\s,]*" + lead_re + r"\b" + re.escape(name) + r"\b", low)
        if m:
            rest = re.sub(r"^[\s,.:;!?-]+", "", t[m.end():])
            return True, rest.strip()

    # Exact-only by default (fuzzy disabled) — accurate, no look-alike wakes.
    if fuzzy >= 1.0:
        return False, ""

    # Pass 2 — fuzzy on the leading word(s) (opt-in; handles STT mis-transcription
    # of the persona NAME only, when wake_fuzzy < 1.0 is configured).
    words = re.findall(r"[a-z']+", low)
    if not words:
        return False, ""
    has_addr = words[0] in _ADDRESS_WORDS
    if require_prefix and not has_addr:
        return False, ""              # no "hey/ok/…" → not addressed
    lead = 1 if (has_addr and len(words) > 1) else 0
    cands: list[tuple[int, str]] = []
    if len(words) > lead:
        cands.append((lead + 1, words[lead]))                       # one word
    if len(words) > lead + 1:                                       # two words,
        cands.append((lead + 2, words[lead] + words[lead + 1]))     # joined ("shylaja")
        cands.append((lead + 2, words[lead] + " " + words[lead + 1]))
    best, drop = 0.0, 0
    for ncount, cand in cands:
        if len(cand) < 3:
            continue
        for name in names_fuzzy:
            r = difflib.SequenceMatcher(None, cand, name).ratio()
            if r > best:
                best, drop = r, ncount
    if best >= fuzzy:
        rest = " ".join(t.split()[drop:])
        rest = re.sub(r"^[\s,.:;!?-]+", "", rest)
        return True, rest.strip()
    return False, ""


def wake_best_score(transcript: str, agent_name: str,
                    extra_words: list[str] | None = None) -> float:
    """Best fuzzy similarity of the leading word(s) to any wake phrase — for
    logging near-misses so an operator can see what STT actually heard."""
    low = (transcript or "").strip().lower()
    words = re.findall(r"[a-z']+", low)
    if not words:
        return 0.0
    names = wake_phrases(agent_name, extra_words)
    lead = 1 if (words[0] in _ADDRESS_WORDS and len(words) > 1) else 0
    cands = [words[lead]] if len(words) > lead else []
    if len(words) > lead + 1:
        cands.append(words[lead] + words[lead + 1])
    best = 0.0
    for cand in cands:
        for name in names:
            best = max(best, difflib.SequenceMatcher(None, cand, name).ratio())
    return round(best, 2)


def _frames_for(runtime, max_frames: int = 3) -> list[dict]:
    """The JPEG frame(s) the tools actually looked at this turn, base64-encoded,
    so the UI can SHOW what the agent saw in the chat. Reads the per-turn frame
    cache (populated by the vision tools) for the cameras in last_cameras_used —
    no extra fetch. Capped in count and size to keep the response small."""
    roles = {c.camera_id: c.role for c in runtime.cfg.cameras}
    out: list[dict] = []
    seen: set[str] = set()
    for cid in getattr(runtime.tools, "last_cameras_used", []) or []:
        if cid in seen:
            continue
        seen.add(cid)
        try:
            frame = runtime.context.get_cached_frame(cid)
        except Exception:
            frame = None
        if not frame or len(frame) > 2_000_000:   # skip missing / oversized
            continue
        out.append({
            "camera_id": cid,
            "role": roles.get(cid, cid),
            "jpeg_b64": base64.b64encode(frame).decode("ascii"),
        })
        if len(out) >= max_frames:
            break
    return out


def greeting_for(name: str | None = None) -> str:
    # No persona name by design — the agent introduces itself by the product
    # name, "the OpenNVR Agent" (formerly "Camera Agent"), described as your
    # camera agent. (``name`` is accepted for call-site compat.)
    # Short by design: this is SPOKEN (Piper) before the first
    # interaction — every extra word delays the user's first question.
    return (
        "Hi, I'm the OpenNVR Agent — your camera agent. "
        "Ask me anything about your cameras; I can also set alarms, "
        "watches, and reports."
    )


# ── Background task system (in-memory) ─────────────────────────────────


@dataclass
class AgentTask:
    """A long-running request the agent is working on in the background."""

    id: int
    query: str
    status: str = "queued"  # queued | running | done | error
    result: str | None = None
    created_at: float = 0.0
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "query": self.query,
            "status": self.status,
            "result": self.result,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class TaskManager:
    """Runs the agent's longer jobs as background asyncio tasks so the
    conversation never blocks. In-memory only — tasks reset on restart.

    Each task runs a full tool-calling turn for its query (with the
    create_background_task tool removed, so a task can't spawn more
    tasks), and stores the final answer. The UI polls ``list()`` and
    surfaces completions back into the chat."""

    def __init__(self, runtime: "CameraAgentRuntime", *, max_tasks: int = 50) -> None:
        self._runtime = runtime
        self._tasks: dict[int, AgentTask] = {}
        self._order: list[int] = []
        self._next_id = 1
        self._max = max_tasks

    def create(self, query: str) -> AgentTask:
        import time

        task = AgentTask(id=self._next_id, query=query.strip(), created_at=time.time())
        self._next_id += 1
        self._tasks[task.id] = task
        self._order.append(task.id)
        # Trim oldest beyond the cap.
        while len(self._order) > self._max:
            old = self._order.pop(0)
            self._tasks.pop(old, None)
        asyncio.create_task(self._run(task), name=f"agent-task-{task.id}")
        logger.info("task #%d queued: %r", task.id, task.query)
        return task

    async def _run(self, task: AgentTask) -> None:
        import time

        task.status = "running"
        try:
            task.result = await _run_conversation_turn(
                self._runtime, [], task.query,
                tool_definitions=self._runtime.background_tool_definitions,
            )
            task.status = "done"
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("task #%d failed", task.id)
            task.result = f"I hit an error working on that: {exc}"
            task.status = "error"
        finally:
            task.finished_at = time.time()
            logger.info("task #%d %s", task.id, task.status)
            self._runtime.notifier.fire({
                "type": "task", "title": f"Task #{task.id} {task.status}",
                "text": task.result or "", "severity": "info", "ts": task.finished_at,
            })

    def get(self, task_id: int) -> AgentTask | None:
        return self._tasks.get(task_id)

    def list(self) -> list[dict[str, Any]]:
        return [self._tasks[i].to_dict() for i in self._order if i in self._tasks]


# ── Standing monitors (watch / count) ─────────────────────────────────


@dataclass
class Monitor:
    """A standing watch the agent keeps on one or more cameras.

    kind="notify": alert when ``target`` appears.
    kind="count":  keep a live + peak count of ``target`` per camera
                   (periodic snapshot counts — not turnstile/line-crossing).
    """

    id: int
    kind: str            # "notify" | "count" | "crossing"
    camera_ids: list[str]
    target: str
    description: str
    interval_s: float
    active: bool = True
    created_at: float = 0.0
    line: list[float] | None = None   # crossing: [x1,y1,x2,y2] normalized
    current: dict[str, int] = field(default_factory=dict)
    peak: dict[str, int] = field(default_factory=dict)
    # Per-camera LineCounter — LEGACY-loop crossing monitors only. Converged
    # crossing monitors (kind in _CONVERGED) tally in MonitorHost's
    # HostedMonitor.tallies instead; this stays {} for them. Never serialized.
    counters: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id, "kind": self.kind, "camera_ids": self.camera_ids,
            "target": self.target, "description": self.description,
            "interval_s": self.interval_s, "active": self.active,
            "created_at": self.created_at,
            "current": self.current, "peak": self.peak,
        }
        if self.line:
            d["line"] = self.line
        return d


def _line_side(line: tuple, x: float, y: float) -> float:
    """Signed side of point (x,y) relative to the directed line
    (x1,y1)->(x2,y2). >0 one side, <0 the other, 0 on the line."""
    (x1, y1), (x2, y2) = line
    return (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)


class LineCounter:
    """Counts unique tracks crossing a virtual line, with direction.

    Fed per-poll lists of tracked detections ({id, x, y} with x,y as
    normalized [0,1] frame coordinates of the box center). When a track
    moves from one side of the line to the other, it's counted as ``in``
    (crossing to the positive side) or ``out``. Requires a tracker that
    yields stable per-object ids across frames (e.g. the ByteTrack
    adapter) and a poll rate high enough to see objects on both sides —
    see COUNTING.md.
    """

    def __init__(self, line: tuple) -> None:
        self._line = line
        self._last_side: dict[Any, float] = {}
        self.in_count = 0
        self.out_count = 0

    def update(self, tracks: list[dict[str, Any]]) -> None:
        for t in tracks:
            tid = t.get("id")
            if tid is None:
                continue
            s = _line_side(self._line, float(t.get("x", 0.0)), float(t.get("y", 0.0)))
            prev = self._last_side.get(tid)
            if prev is not None and prev != 0 and s != 0 and (prev > 0) != (s > 0):
                if s > 0:
                    self.in_count += 1
                else:
                    self.out_count += 1
            self._last_side[tid] = s

    def totals(self) -> dict[str, int]:
        return {"in": self.in_count, "out": self.out_count, "net": self.in_count - self.out_count}


class MonitorManager:
    """Runs the agent's standing watches. The registry (ids, list,
    persistence, notifications) is unchanged, but the RULES are converged
    onto the App SDK (app-sdk-spec §07 "one rule library, two front doors"):

    * kind="count" / kind="crossing" → SDK detectors hosted in-process by
      :class:`monitor_host.MonitorHost` (the occupancy-counting and
      line-crossing example rule classes, driven by the agent's frame
      source + detection client).
    * kind="notify" → the legacy poll loop below (cooldown-refire presence
      has no SDK archetype yet; see monitor_host.py's module docstring).
    """

    # kind → MonitorHost rule for the SDK-converged monitor kinds.
    _CONVERGED = {"count": "occupancy", "crossing": "line_crossing"}

    def __init__(self, runtime: "CameraAgentRuntime", *, default_interval: float = 8.0,
                 notify_cooldown: float = 30.0, max_monitors: int = 20) -> None:
        self._runtime = runtime
        self._monitors: dict[int, Monitor] = {}
        self._order: list[int] = []
        self._tasks: dict[int, asyncio.Task] = {}
        self._next_id = 1
        self._default_interval = default_interval
        self._cooldown = notify_cooldown
        self._max = max_monitors
        self._last_notified: dict[tuple[int, str], float] = {}
        # Bounded: the feed only ever serves the newest 50 (notifications()),
        # but appends arrive from monitor notify paths AND the app-alert
        # relay — an unbounded list in a long-running agent is a slow leak.
        self._notifications: deque[dict[str, Any]] = deque(maxlen=200)
        self._next_note_id = 1
        # SDK front door. Everything is late-bound through ``runtime`` so
        # tests that monkeypatch ``context.get_frame`` / ``detection_client
        # .infer`` after construction are honored, exactly like the legacy
        # loop's attribute lookups per poll.
        self.host = MonitorHost(
            get_frame=lambda cam: runtime.context.get_frame(cam, allow_pinned=False),
            infer=lambda **kw: runtime.detection_client.infer(**kw),
            notify=self._hosted_notify,
            dedup=lambda dets: runtime.tools._dedup_detections(dets),
            stop_check=lambda: runtime._stop_event.is_set(),
            busy_check=lambda: runtime.interactive_busy(),
            default_interval_s=default_interval,
        )

    def create(self, *, kind: str, camera_ids: list[str], target: str,
               description: str = "", interval_s: float | None = None,
               line: list[float] | None = None) -> Monitor:
        import time

        mon = Monitor(
            id=self._next_id, kind=kind, camera_ids=list(camera_ids),
            target=target.strip().lower(), description=description.strip(),
            interval_s=float(interval_s or self._default_interval),
            created_at=time.time(), line=line,
        )
        if kind in self._CONVERGED:
            # SDK front door (§07): instantiate the example app's rule
            # class via MonitorHost instead of the bespoke loop. May raise
            # ValueError on bad params — BEFORE the monitor is registered,
            # so a rejected request leaves no orphan row or task.
            params: dict[str, Any] = {
                "target": mon.target, "interval_s": mon.interval_s,
            }
            if kind == "crossing":
                params["line"] = list(line or [])

            def _sink(cam: str, current: int, peak_candidate: int,
                      _mon: Monitor = mon) -> None:
                _mon.current[cam] = current
                _mon.peak[cam] = max(_mon.peak.get(cam, 0), peak_candidate)

            self.host.create(
                self._CONVERGED[kind], list(camera_ids), params,
                monitor_id=mon.id, counts_sink=_sink,
            )
        self._next_id += 1
        self._monitors[mon.id] = mon
        self._order.append(mon.id)
        while len(self._order) > self._max:
            old = self._order.pop(0)
            self.stop(old)
            self._monitors.pop(old, None)
        if kind not in self._CONVERGED:
            # Legacy poll loop — only kind="notify" lands here now.
            self._tasks[mon.id] = asyncio.create_task(self._loop(mon), name=f"monitor-{mon.id}")
        logger.info("monitor #%d (%s %r on %s) started", mon.id, kind, target, camera_ids)
        return mon

    def stop(self, monitor_id: int) -> bool:
        mon = self._monitors.get(monitor_id)
        if not mon:
            return False
        mon.active = False
        self.host.stop(monitor_id)  # no-op for legacy (notify) monitors
        t = self._tasks.pop(monitor_id, None)
        if t:
            t.cancel()
        return True

    def _hosted_notify(self, monitor_id: int, alert: Any) -> None:
        """Alert bridge target: a hosted (SDK) monitor fired. Routes into
        the same notify machinery the legacy loop used — the in-memory
        notification feed the UI polls plus the webhook fan-out. Called
        synchronously from the detector's dispatch; never blocks (list
        append + fire-and-forget task)."""
        import time

        now = time.time()
        self._notifications.append({
            "id": self._next_note_id,
            "monitor_id": monitor_id,
            "text": f"{alert.title} — {alert.description}",
            "camera": getattr(alert, "camera_id", "") or "",
            "ts": now,
        })
        self._next_note_id += 1
        logger.info("monitor #%d alerted: %s", monitor_id, alert.title)
        self._runtime.notifier.fire({
            "type": "notify", "title": alert.title, "text": alert.description,
            "camera": alert.camera_id, "severity": alert.severity, "ts": now,
        })

    def stop_all(self) -> None:
        for mid in list(self._monitors):
            self.stop(mid)

    def export(self) -> list[dict[str, Any]]:
        return [{"kind": m.kind, "camera_ids": m.camera_ids, "target": m.target,
                 "interval_s": m.interval_s, "line": m.line}
                for m in self._monitors.values() if m.active]

    def restore(self, specs: list[dict[str, Any]]) -> None:
        # Collapse semantically-identical watches from the persisted state:
        # each one is a live detection loop, and testing tends to pile up
        # duplicates (same kind+target+cameras). Re-arm one per distinct rule.
        seen: set[tuple] = set()
        skipped = 0
        for s in specs or []:
            key = (str(s.get("kind")), str(s.get("target")),
                   frozenset(s.get("camera_ids") or []),
                   tuple(s.get("line") or ()))
            if key in seen:
                skipped += 1
                continue
            seen.add(key)
            try:
                self.create(kind=s["kind"], camera_ids=s["camera_ids"],
                            target=s["target"], interval_s=s.get("interval_s"),
                            line=s.get("line"))
            except Exception:  # pragma: no cover
                logger.exception("monitor restore failed for %r", s)
        if skipped:
            logger.info("monitor restore: dropped %d duplicate watch(es)", skipped)

    def list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in self._order:
            mon = self._monitors.get(i)
            if mon is None:
                continue
            d = mon.to_dict()
            hosted = self.host.get(i)
            if hosted is not None and hosted.error is not None:
                # The hosted poll task died (see MonitorHost._loop) —
                # surface that in /monitors instead of listing a zombie
                # as active.
                d["active"] = False
                d["status"] = f"error: {hosted.error}"
            out.append(d)
        return out

    def notifications(self) -> list[dict[str, Any]]:
        return list(self._notifications)[-50:]

    def _count_target(self, detections: list[dict[str, Any]], target: str) -> int:
        deduped = self._runtime.tools._dedup_detections(detections[:64])
        return sum(
            1 for d in deduped
            if str(d.get("label") or d.get("class") or "").strip().lower() == target
        )

    @staticmethod
    def _extract_tracks(result: dict[str, Any], target: str) -> list[dict[str, Any]]:
        """Pull tracked detections ({id, x, y} centers) out of an adapter
        result, filtered to ``target``. Coordinates are passed through in
        whatever space the tracker emits (assumed same as the line)."""
        raw = result.get("tracks") or result.get("detections") or []
        out: list[dict[str, Any]] = []
        for tr in raw:
            if not isinstance(tr, dict):
                continue
            label = str(tr.get("label") or tr.get("class") or "").strip().lower()
            if target and label and label != target:
                continue
            tid = tr.get("track_id", tr.get("id"))
            if tid is None:
                continue
            center = tr.get("center")
            if isinstance(center, (list, tuple)) and len(center) >= 2:
                x, y = float(center[0]), float(center[1])
            else:
                bb = tr.get("bbox") or {}
                if isinstance(bb, dict):
                    x = float(bb.get("x", 0)) + float(bb.get("w", 0)) / 2
                    y = float(bb.get("y", 0)) + float(bb.get("h", 0)) / 2
                elif isinstance(bb, (list, tuple)) and len(bb) >= 4:
                    x = float(bb[0]) + float(bb[2]) / 2
                    y = float(bb[1]) + float(bb[3]) / 2
                else:
                    continue
            out.append({"id": tid, "x": x, "y": y})
        return out

    # Legacy poll loop. Only kind="notify" monitors run it — "count" and
    # "crossing" are hosted SDK detectors now (see ``self.host``), so the
    # count/crossing branches in ``_poll`` below are kept only as the
    # reference semantics the convergence was tested against.
    async def _loop(self, mon: Monitor) -> None:
        try:
            while mon.active and not self._runtime._stop_event.is_set():
                # Yield to an in-flight interactive turn; catch up next cycle.
                if not self._runtime.interactive_busy():
                    for cam in mon.camera_ids:
                        if not mon.active:
                            break
                        await self._poll(mon, cam)
                await asyncio.sleep(mon.interval_s)
        except asyncio.CancelledError:  # pragma: no cover
            pass
        except Exception:  # pragma: no cover - defensive
            logger.exception("monitor #%d loop crashed", mon.id)

    async def _poll(self, mon: Monitor, cam: str) -> None:
        import time

        try:
            frame = await self._runtime.context.get_frame(cam, allow_pinned=False)
            extra = {"task": "track"} if mon.kind == "crossing" else None
            resp = await self._runtime.detection_client.infer(frame_jpeg=frame, extra=extra)
        except Exception as exc:
            logger.info("monitor #%d: poll of %s failed (%s)", mon.id, cam, exc)
            return
        result = resp.get("result") or {}

        if mon.kind == "crossing":
            counter = mon.counters.get(cam)
            if counter is not None:
                tracks = self._extract_tracks(result, mon.target)
                counter.update(tracks)
                t = counter.totals()
                mon.current[cam] = t["net"]
                mon.peak[cam] = max(mon.peak.get(cam, 0), t["in"])
            return

        detections = result.get("detections") or []
        count = self._count_target(detections, mon.target)
        mon.current[cam] = count
        mon.peak[cam] = max(mon.peak.get(cam, 0), count)
        if mon.kind == "notify" and count > 0:
            key = (mon.id, cam)
            now = time.time()
            if now - self._last_notified.get(key, 0.0) >= self._cooldown:
                self._last_notified[key] = now
                noun = mon.target if count == 1 else f"{count} {mon.target}s"
                self._notifications.append({
                    "id": self._next_note_id,
                    "monitor_id": mon.id,
                    "text": f"Heads up — I see {noun} on {cam}.",
                    "camera": cam,
                    "ts": now,
                })
                self._next_note_id += 1
                logger.info("monitor #%d notified: %s on %s", mon.id, mon.target, cam)
                self._runtime.notifier.fire({
                    "type": "notify", "title": "Camera watch",
                    "text": f"{noun} on {cam}", "camera": cam,
                    "severity": "info", "ts": now,
                })


def _create_monitor_tool(camera_enum_all: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "create_monitor",
            "description": (
                "Set up a STANDING watch on one or more cameras (or 'all'). "
                "kind='notify' alerts when the target appears; kind='count' "
                "keeps a live snapshot count; kind='crossing' counts people/"
                "objects crossing a line (needs a 'line'). Use for ongoing "
                "requests like 'notify me when you see a person on cam1', "
                "'count people on gate 2', 'count people entering at the door'. "
                "NOT for one-off 'what do you see right now' questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["notify", "count", "crossing"]},
                    "target": {"type": "string",
                               "description": "What to watch for, e.g. 'person', 'car', 'dog'."},
                    "camera_id": {"type": "string", "enum": camera_enum_all,
                                  "description": "A camera id, or 'all'."},
                    "camera_ids": {"type": "array", "items": {"type": "string", "enum": camera_enum_all},
                                   "description": "Optional: several cameras at once."},
                    "line": {"type": "array", "items": {"type": "number"},
                             "description": "For kind='crossing': [x1,y1,x2,y2] of the counting line in normalized 0-1 frame coords."},
                },
                "required": ["kind", "target", "camera_id"],
            },
        },
    }


def _stop_monitor_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "stop_monitor",
            "description": "Stop a standing monitor by its numeric id.",
            "parameters": {
                "type": "object",
                "properties": {"monitor_id": {"type": "integer"}},
                "required": ["monitor_id"],
            },
        },
    }


# ── Alarms (condition → ring) ──────────────────────────────────────────


def _parse_hhmm(value: Any) -> int | None:
    """Parse 'HH:MM' (24h) to minutes-since-midnight, else None."""
    if not value:
        return None
    try:
        h, m = str(value).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except (ValueError, AttributeError):
        pass
    return None


@dataclass
class Alarm:
    """A high-severity rule: when ``target`` appears on a watched camera
    (optionally only within a time window), the alarm RINGS until a human
    acknowledges it. Optionally tied to an emergency contact (the actual
    call-out is a documented future integration — see ALARMS.md)."""

    id: int
    name: str
    target: str
    camera_ids: list[str]
    after_min: int | None = None
    before_min: int | None = None
    emergency_contact: str | None = None
    # Annunciation level (a real CCTV concept — not every alarm deserves
    # the siren). Four levels, alarm-panel style:
    #   "siren"  — CRITICAL: latches + rings until a human silences it
    #              (fire, a snake on the farm, a person in the bank
    #              after hours).
    #   "pulse"  — URGENT: latches + pulses loud, then STANDS DOWN on
    #              its own after pulse_seconds (default 60) — demands
    #              attention without demanding an acknowledgement.
    #   "chime"  — NOTICE: one ding per re-arm window, no latch.
    #   "silent" — LOG: records + pushes only.
    ring: str = "siren"
    active: bool = True
    triggered: bool = False
    created_at: float = 0.0
    last_triggered: float | None = None
    trigger_count: int = 0
    last_ack: float = 0.0

    def window_label(self) -> str:
        def fmt(m):
            return f"{m // 60:02d}:{m % 60:02d}"
        if self.after_min is not None and self.before_min is not None:
            return f"between {fmt(self.after_min)} and {fmt(self.before_min)}"
        if self.after_min is not None:
            return f"after {fmt(self.after_min)}"
        if self.before_min is not None:
            return f"before {fmt(self.before_min)}"
        return "any time"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "target": self.target,
            "camera_ids": self.camera_ids, "window": self.window_label(),
            "ring": self.ring,
            "active": self.active, "triggered": self.triggered,
            "trigger_count": self.trigger_count,
            "emergency_contact_configured": bool(self.emergency_contact),
            "created_at": self.created_at, "last_triggered": self.last_triggered,
        }


class AlarmManager:
    """Polls watched cameras and rings when an alarm's condition is met.
    Mirrors MonitorManager but raises sticky, acknowledgeable alarm events
    (the UI plays a siren while any alarm is ``triggered``). In-memory only."""

    def __init__(self, runtime: "CameraAgentRuntime", *, interval: float = 5.0,
                 rearm_cooldown: float = 20.0, max_alarms: int = 20,
                 pulse_seconds: float = 60.0) -> None:
        self._pulse = pulse_seconds
        self._runtime = runtime
        self._alarms: dict[int, Alarm] = {}
        self._order: list[int] = []
        self._tasks: dict[int, asyncio.Task] = {}
        self._events: list[dict[str, Any]] = []
        self._next_id = 1
        self._next_event_id = 1
        self._interval = interval
        self._rearm = rearm_cooldown
        self._max = max_alarms

    def create(self, *, name: str, target: str, camera_ids: list[str],
               after_min: int | None = None, before_min: int | None = None,
               emergency_contact: str | None = None,
               ring: str = "siren") -> Alarm:
        import time

        if ring not in ("siren", "pulse", "chime", "silent"):
            ring = "siren"
        alarm = Alarm(
            id=self._next_id, name=name.strip() or "Alarm",
            target=target.strip().lower(), camera_ids=list(camera_ids),
            after_min=after_min, before_min=before_min, ring=ring,
            emergency_contact=emergency_contact, created_at=time.time(),
        )
        self._next_id += 1
        self._alarms[alarm.id] = alarm
        self._order.append(alarm.id)
        while len(self._order) > self._max:
            old = self._order.pop(0)
            self.stop(old)
            self._alarms.pop(old, None)
        self._tasks[alarm.id] = asyncio.create_task(self._loop(alarm), name=f"alarm-{alarm.id}")
        logger.info("alarm #%d %r (%s on %s, %s) armed", alarm.id, alarm.name,
                    alarm.target, camera_ids, alarm.window_label())
        return alarm

    def stop(self, alarm_id: int) -> bool:
        alarm = self._alarms.get(alarm_id)
        if not alarm:
            return False
        alarm.active = False
        alarm.triggered = False
        t = self._tasks.pop(alarm_id, None)
        if t:
            t.cancel()
        return True

    def acknowledge(self, alarm_id: int | None = None) -> int:
        """Silence one alarm (or all when id is None). Returns how many."""
        import time

        n = 0
        for aid, alarm in self._alarms.items():
            if alarm_id in (None, aid) and alarm.triggered:
                alarm.triggered = False
                alarm.last_ack = time.time()
                n += 1
        return n

    def stop_all(self) -> None:
        for aid in list(self._alarms):
            self.stop(aid)

    def export(self) -> list[dict[str, Any]]:
        return [{"name": a.name, "target": a.target, "camera_ids": a.camera_ids,
                 "after_min": a.after_min, "before_min": a.before_min,
                 "ring": a.ring}
                for a in self._alarms.values() if a.active]

    def restore(self, specs: list[dict[str, Any]], *, contact_for=None) -> None:
        # Collapse duplicate alarms (same name+target+cameras+window): each is
        # a 5-second detection loop, and a testing session accumulates them.
        seen: set[tuple] = set()
        skipped = 0
        for s in specs or []:
            key = (str(s.get("name") or "").strip().lower(), str(s.get("target")),
                   frozenset(s.get("camera_ids") or []),
                   s.get("after_min"), s.get("before_min"))
            if key in seen:
                skipped += 1
                continue
            seen.add(key)
            try:
                self.create(name=s["name"], target=s["target"], camera_ids=s["camera_ids"],
                            after_min=s.get("after_min"), before_min=s.get("before_min"),
                            ring=str(s.get("ring") or "siren"),
                            emergency_contact=(contact_for(s["name"], s["target"]) if contact_for else None))
            except Exception:  # pragma: no cover
                logger.exception("alarm restore failed for %r", s)
        if skipped:
            logger.info("alarm restore: dropped %d duplicate alarm(s)", skipped)

    def list(self) -> list[dict[str, Any]]:
        return [self._alarms[i].to_dict() for i in self._order if i in self._alarms]

    def events(self) -> list[dict[str, Any]]:
        return list(self._events[-50:])

    def _in_window(self, alarm: Alarm) -> bool:
        if alarm.after_min is None and alarm.before_min is None:
            return True
        import datetime

        now = datetime.datetime.now().time()
        mins = now.hour * 60 + now.minute
        a, b = alarm.after_min, alarm.before_min
        if a is not None and b is not None:
            return (a <= mins < b) if a <= b else (mins >= a or mins < b)
        if a is not None:
            return mins >= a
        return mins < b

    async def _loop(self, alarm: Alarm) -> None:
        try:
            while alarm.active and not self._runtime._stop_event.is_set():
                # Yield to an in-flight interactive turn — a skipped cycle is
                # caught on the next one (self._interval later). Stand-down and
                # window logic still run so timing stays correct.
                if self._in_window(alarm) and not self._runtime.interactive_busy():
                    for cam in alarm.camera_ids:
                        if not alarm.active:
                            break
                        await self._poll(alarm, cam)
                self._maybe_stand_down(alarm)
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:  # pragma: no cover
            pass
        except Exception:  # pragma: no cover
            logger.exception("alarm #%d loop crashed", alarm.id)

    def _maybe_stand_down(self, alarm: Alarm, now: float | None = None) -> None:
        """URGENT stands down on its own: a pulse alarm that has rung for
        pulse_seconds auto-acknowledges — loud enough to turn heads, no
        human click required. (CRITICAL sirens never do this.)"""
        if alarm.ring != "pulse" or not alarm.triggered:
            return
        if alarm.last_triggered is None:
            return
        import time as _t
        now = _t.time() if now is None else now
        if now - alarm.last_triggered >= self._pulse:
            alarm.triggered = False
            alarm.last_ack = now
            logger.info("alarm #%d (pulse) stood down", alarm.id)

    async def _poll(self, alarm: Alarm, cam: str) -> None:
        import time

        try:
            frame = await self._runtime.context.get_frame(cam, allow_pinned=False)
            resp = await self._runtime.detection_client.infer(frame_jpeg=frame)
        except Exception as exc:
            logger.info("alarm #%d: poll of %s failed (%s)", alarm.id, cam, exc)
            return
        detections = (resp.get("result") or {}).get("detections") or []
        present = self._runtime.monitors._count_target(detections, alarm.target) > 0
        now = time.time()
        if not present:
            return
        if alarm.ring in ("siren", "pulse"):
            # Latching: siren rings until a human acknowledges; pulse
            # stands down on its own (the loop auto-acks it after
            # pulse_seconds). Re-arm cooldown runs from the ack.
            if alarm.triggered or (now - alarm.last_ack) < self._rearm:
                return
            alarm.triggered = True
        else:
            # chime / silent: one EVENT per re-arm window, never a latch —
            # the cooldown anchors on the last firing (nothing to ack).
            anchor = max(alarm.last_ack, alarm.last_triggered or 0.0)
            if (now - anchor) < self._rearm:
                return
        alarm.last_triggered = now
        alarm.trigger_count += 1
        text = f"{alarm.name}: {alarm.target} detected on {cam}"
        if alarm.emergency_contact:
            text += f" (would alert {alarm.emergency_contact})"
        self._events.append({
            "id": self._next_event_id, "alarm_id": alarm.id, "name": alarm.name,
            "text": text, "camera": cam, "ts": now, "ring": alarm.ring,
            "emergency_contact": alarm.emergency_contact,
        })
        self._next_event_id += 1
        logger.warning("ALARM #%d TRIGGERED (%s): %s", alarm.id, alarm.ring, text)
        self._runtime.notifier.fire({
            "type": "alarm", "title": alarm.name, "text": text,
            "camera": cam,
            "severity": "critical" if alarm.ring == "siren" else "medium",
            "ts": now,
        })


def _create_alarm_tool(camera_enum_all: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "create_alarm",
            "description": (
                "Arm a high-severity ALARM that RINGS when something appears, "
                "optionally only within a time window. Use for 'sound a fire "
                "alarm if you see fire', 'alarm if a person is seen after 6pm', "
                "'alert me loudly if a car enters at night'. Different from a "
                "monitor: a 'siren' alarm rings until acknowledged."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short alarm name, e.g. 'Fire', 'After-hours intrusion'."},
                    "target": {"type": "string", "description": "What triggers it, e.g. 'fire', 'person', 'smoke'."},
                    "camera_id": {"type": "string", "enum": camera_enum_all, "description": "A camera id, or 'all'."},
                    "camera_ids": {"type": "array", "items": {"type": "string", "enum": camera_enum_all}},
                    "after": {"type": "string", "description": "Only active after this 24h time 'HH:MM' (e.g. '18:00' for after 6pm)."},
                    "before": {"type": "string", "description": "Only active before this 24h time 'HH:MM'."},
                    "ring": {"type": "string",
                             "enum": ["siren", "pulse", "chime", "silent"],
                             "description": "How it announces. 'siren' rings until "
                                            "silenced — fire/smoke/gas, dangerous animals, "
                                            "break-in-grade threats. 'pulse' is loud for a "
                                            "minute then stands down — urgent, not "
                                            "evacuation. 'chime' dings once — visitors, "
                                            "deliveries, vehicles. 'silent' = notifications "
                                            "only. Omit to pick by the site's defaults."},
                },
                "required": ["name", "target", "camera_id"],
            },
        },
    }


def _stop_alarm_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "stop_alarm",
            "description": "Disarm an alarm by its numeric id, or acknowledge/silence a ringing one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "alarm_id": {"type": "integer"},
                    "action": {"type": "string", "enum": ["disarm", "silence"],
                               "description": "'silence' stops the current ring but keeps the alarm armed; 'disarm' removes it."},
                },
                "required": ["alarm_id"],
            },
        },
    }


# ── External notifications (webhook fan-out) ──────────────────────────


class Notifier:
    """Fans alarm/watch events out to configured webhook URLs so alerts
    reach the operator when the browser tab is closed. Best-effort and
    non-blocking — failures are logged, never raised into the poll loops.

    The payload includes both ``text`` (Slack) and ``content`` (Discord)
    plus structured fields, so it works with Slack/Discord/Teams incoming
    webhooks, n8n/Home Assistant, and custom JSON consumers unchanged."""

    def __init__(self, runtime: "CameraAgentRuntime", *, webhooks=None, events=None,
                 apprise_urls=None) -> None:
        self._runtime = runtime
        self._webhooks = list(webhooks or [])
        self._events = set(events or ("alarm", "notify"))
        self._client = None
        self._deliveries: list[dict[str, Any]] = []
        # Apprise: 100+ services (mailto:// tgram:// ntfy:// pover:// …)
        # through one OPTIONAL dependency. URLs are held either way; the
        # library import is lazy so an unconfigured or undeployed apprise
        # never costs anything.
        self._apprise_urls = list(apprise_urls or [])
        self._apprise = None
        self._apprise_active = 0   # URLs the client actually accepted
        self._apprise_warned = False

    @property
    def enabled(self) -> bool:
        return bool(self._webhooks or self._apprise_urls)

    def _apprise_client(self):
        """Lazy optional import. None (with ONE warning) when the package
        isn't installed — configured Apprise URLs then no-op instead of
        taking the agent down. Invalid URLs are dropped at add() time;
        only the scheme is logged (Apprise URLs embed tokens)."""
        if not self._apprise_urls:
            return None
        if self._apprise is None:
            try:
                import apprise
            except ImportError:
                if not self._apprise_warned:
                    self._apprise_warned = True
                    logger.warning(
                        "notify: %d Apprise URL(s) configured but the "
                        "'apprise' package is not installed — "
                        "pip install apprise to activate them",
                        len(self._apprise_urls))
                return None
            client = apprise.Apprise()
            added = 0
            for url in self._apprise_urls:
                if client.add(url):
                    added += 1
                else:
                    scheme = str(url).split("://", 1)[0]
                    logger.warning("notify: invalid Apprise URL ignored "
                                   "(scheme %r)", scheme)
            self._apprise_active = added
            self._apprise = client
        return self._apprise

    def _http(self):
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=8.0, trust_env=False)
        return self._client

    def _format(self, event: dict[str, Any]) -> dict[str, Any]:
        title = str(event.get("title") or "OpenNVR alert")
        detail = str(event.get("text") or "")
        body = f"{title}: {detail}" if detail else title
        payload = {
            "text": body, "content": body,  # Slack / Discord
            "type": event.get("type"), "title": title, "detail": detail,
            "camera": event.get("camera"), "severity": event.get("severity"),
            "ts": event.get("ts"), "agent": self._runtime.agent_name,
        }
        # App-alert relay labels its source (``app:<id>``) so a webhook
        # consumer can tell "the PPE app flagged a violation" apart from the
        # agent's own watches. Only present for relayed app alerts.
        source = event.get("source")
        if source:
            payload["source"] = source
        # Tap-to-open: a phone push should land ON the camera's screen.
        base = getattr(self._runtime.cfg, "agent_public_url", None)
        cam = event.get("camera")
        if base and cam:
            payload["link"] = f"{str(base).rstrip('/')}/demo/camera/{cam}"
        return payload

    async def send(self, event: dict[str, Any]) -> int:
        kind = event.get("type")
        if kind != "test" and kind not in self._events:
            return 0
        payload = self._format(event)
        ok = 0
        for url in self._webhooks:
            try:
                resp = await self._http().post(url, json=payload)
                if resp.status_code < 400:
                    ok += 1
                else:
                    logger.warning("notify: %s returned HTTP %s", url, resp.status_code)
            except Exception as exc:
                logger.warning("notify: delivery to %s failed: %s", url, exc)
        # Apprise fan-out (email/Telegram/ntfy/…). The library is sync
        # blocking I/O, so it runs on a worker thread — the poll loops and
        # the chat never wait on an SMTP handshake. Group-level result:
        # apprise reports one bool for the batch.
        client = self._apprise_client()
        if client is not None:
            try:
                sent = await asyncio.to_thread(
                    client.notify,
                    title=payload["title"],
                    body=payload["detail"] or payload["title"],
                )
                if sent:
                    # Only URLs the client ACCEPTED count as delivered —
                    # invalid ones were dropped at add() time and reporting
                    # them ok would fake the operator's delivery-health view.
                    ok += self._apprise_active
                else:
                    logger.warning("notify: apprise delivery failed "
                                   "(%d URL(s))", self._apprise_active)
            except Exception as exc:
                logger.warning("notify: apprise delivery errored: %s", exc)
        import time as _t

        self._deliveries.append({"type": kind, "title": payload["title"],
                                 "ok": ok,
                                 "channels": len(self._webhooks) + len(self._apprise_urls),
                                 "ts": _t.time()})
        del self._deliveries[:-50]
        return ok

    def fire(self, event: dict[str, Any]) -> None:
        """Fire-and-forget from sync/poll contexts (never blocks)."""
        if not self.enabled:
            return
        try:
            asyncio.create_task(self.send(event))
        except RuntimeError:  # pragma: no cover - no running loop
            pass

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled,
                "channels": len(self._webhooks) + len(self._apprise_urls),
                "webhooks": len(self._webhooks),
                "apprise": len(self._apprise_urls),
                "events": sorted(self._events), "recent": self._deliveries[-20:]}


# ── Scheduled recurring reports ────────────────────────────────────────


@dataclass
class ReportSchedule:
    """A recurring summary: at a daily time or every N minutes, the agent
    runs ``query`` (a normal tool-grounded turn) and delivers the result."""

    id: int
    name: str
    query: str
    at_min: int | None = None       # daily, minutes since midnight
    every_minutes: int | None = None
    active: bool = True
    created_at: float = 0.0
    last_run: float | None = None
    last_result: str | None = None
    run_count: int = 0

    def schedule_label(self) -> str:
        if self.every_minutes:
            return f"every {self.every_minutes} min"
        if self.at_min is not None:
            return f"daily at {self.at_min // 60:02d}:{self.at_min % 60:02d}"
        return "manual"

    def due(self, now=None) -> bool:
        import datetime

        now = now or datetime.datetime.now()
        nowts = now.timestamp()
        if self.every_minutes:
            return self.last_run is None or (nowts - self.last_run) >= self.every_minutes * 60
        if self.at_min is not None:
            sched = now.replace(hour=self.at_min // 60, minute=self.at_min % 60,
                                second=0, microsecond=0)
            if now < sched:
                return False
            return self.last_run is None or self.last_run < sched.timestamp()
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "query": self.query,
            "schedule": self.schedule_label(), "active": self.active,
            "last_run": self.last_run, "last_result": self.last_result,
            "run_count": self.run_count, "created_at": self.created_at,
        }


class ReportScheduler:
    """Runs recurring reports. One background loop ticks every
    ``check_interval`` and runs any schedule that's due. Each report is a
    tool-grounded turn over ``query`` (with the agent-control tools removed),
    delivered to chat, the notifier, and the recent-reports list."""

    def __init__(self, runtime: "CameraAgentRuntime", *, check_interval: float = 30.0,
                 max_schedules: int = 20) -> None:
        self._runtime = runtime
        self._schedules: dict[int, ReportSchedule] = {}
        self._order: list[int] = []
        self._reports: list[dict[str, Any]] = []
        self._next_id = 1
        self._next_report_id = 1
        self._check_interval = check_interval
        self._max = max_schedules
        self._task: asyncio.Task | None = None

    def create(self, *, name: str, query: str, at_min: int | None = None,
               every_minutes: int | None = None) -> ReportSchedule:
        import time

        if at_min is None and not every_minutes:
            at_min = 8 * 60  # sensible default: 08:00 daily
        sched = ReportSchedule(id=self._next_id, name=name.strip() or "Report",
                               query=query.strip(), at_min=at_min,
                               every_minutes=every_minutes, created_at=time.time())
        self._next_id += 1
        self._schedules[sched.id] = sched
        self._order.append(sched.id)
        while len(self._order) > self._max:
            old = self._order.pop(0)
            self._schedules.pop(old, None)
        logger.info("report #%d %r scheduled (%s)", sched.id, sched.name, sched.schedule_label())
        return sched

    def stop(self, report_id: int) -> bool:
        sched = self._schedules.get(report_id)
        if not sched:
            return False
        sched.active = False
        return True

    def list(self) -> list[dict[str, Any]]:
        return [self._schedules[i].to_dict() for i in self._order if i in self._schedules]

    def export(self) -> list[dict[str, Any]]:
        return [{"name": s.name, "query": s.query, "at_min": s.at_min,
                 "every_minutes": s.every_minutes}
                for s in self._schedules.values() if s.active]

    def restore(self, specs: list[dict[str, Any]]) -> None:
        # Collapse duplicate schedules so a pile-up doesn't fan out repeated
        # (and identically-timed) report runs.
        seen: set[tuple] = set()
        skipped = 0
        for s in specs or []:
            key = (str(s.get("name") or "").strip().lower(), str(s.get("query")),
                   s.get("at_min"), s.get("every_minutes"))
            if key in seen:
                skipped += 1
                continue
            seen.add(key)
            try:
                self.create(name=s["name"], query=s["query"],
                            at_min=s.get("at_min"), every_minutes=s.get("every_minutes"))
            except Exception:  # pragma: no cover
                logger.exception("report restore failed for %r", s)
        if skipped:
            logger.info("report restore: dropped %d duplicate schedule(s)", skipped)

    def reports(self) -> list[dict[str, Any]]:
        return list(self._reports[-20:])

    def get(self, report_id: int) -> ReportSchedule | None:
        return self._schedules.get(report_id)

    async def run_now(self, report_id: int) -> str | None:
        sched = self._schedules.get(report_id)
        if not sched:
            return None
        return await self._run(sched)

    async def _run(self, sched: ReportSchedule) -> str:
        import time

        sched.last_run = time.time()
        sched.run_count += 1
        try:
            result = await _run_conversation_turn(
                self._runtime, [], sched.query,
                tool_definitions=self._runtime.background_tool_definitions,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("report #%d failed", sched.id)
            result = f"(report failed: {exc})"
        sched.last_result = result
        self._reports.append({"id": self._next_report_id, "schedule_id": sched.id,
                              "name": sched.name, "text": result, "ts": sched.last_run})
        self._next_report_id += 1
        del self._reports[:-50]
        self._runtime.notifier.fire({"type": "report", "title": sched.name,
                                     "text": result, "severity": "info", "ts": sched.last_run})
        logger.info("report #%d ran: %.80s", sched.id, result)
        return result

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="report-scheduler")

    def stop_all(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        try:
            while not self._runtime._stop_event.is_set():
                for sid in list(self._schedules):
                    s = self._schedules.get(sid)
                    if s and s.active and s.due():
                        await self._run(s)
                await asyncio.sleep(self._check_interval)
        except asyncio.CancelledError:  # pragma: no cover
            pass
        except Exception:  # pragma: no cover
            logger.exception("report scheduler loop crashed")


def _create_report_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "create_report",
            "description": (
                "Schedule a RECURRING summary report. Use for 'every morning "
                "summarize overnight activity', 'give me a daily 7am rundown', "
                "'every hour tell me how many people came by'. Provide a short "
                "name and the query to summarize; set 'at' (HH:MM, daily) or "
                "'every_minutes'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short report name, e.g. 'Morning rundown'."},
                    "query": {"type": "string", "description": "What to summarize, e.g. 'what the cameras saw overnight'."},
                    "at": {"type": "string", "description": "Daily run time, 24h 'HH:MM' (e.g. '07:00')."},
                    "every_minutes": {"type": "integer", "description": "Or run every N minutes instead of a daily time."},
                },
                "required": ["name", "query"],
            },
        },
    }


def _stop_report_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "stop_report",
            "description": "Cancel a scheduled report by its numeric id.",
            "parameters": {
                "type": "object",
                "properties": {"report_id": {"type": "integer"}},
                "required": ["report_id"],
            },
        },
    }


# ── Watchlist / face management ────────────────────────────────────────


class FaceClient:
    """Thin client for the recognition adapter's faces REST API
    (enroll / list / forget known people). The exact wire shape is
    documented in FACES.md; adjust if your adapter version differs."""

    def __init__(self, *, url: str, token: str | None = None, timeout_seconds: float = 30.0) -> None:
        self._base = url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._http = None

    def _client(self):
        import httpx

        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout, trust_env=False)
        return self._http

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["X-Internal-Api-Key"] = self._token
        return h

    async def enroll(self, *, name: str, frame_jpeg: bytes, category: str = "known") -> dict[str, Any]:
        body = {"name": name, "category": category,
                "frame_b64": base64.b64encode(frame_jpeg).decode("ascii")}
        resp = await self._client().post(f"{self._base}/faces", json=body, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def list_people(self) -> list[dict[str, Any]]:
        resp = await self._client().get(f"{self._base}/faces", headers=self._headers())
        resp.raise_for_status()
        data = resp.json()
        return data.get("people") or data.get("faces") or (data if isinstance(data, list) else [])

    async def forget(self, name: str) -> dict[str, Any]:
        from urllib.parse import quote

        resp = await self._client().delete(f"{self._base}/faces/{quote(name)}", headers=self._headers())
        resp.raise_for_status()
        return resp.json() if resp.content else {"ok": True}

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


def _enroll_face_tool(camera_enum_all: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "enroll_face",
            "description": (
                "Add a person to the watchlist by capturing their face from a "
                "camera right now. Use for 'remember this person as Mom', "
                "'enroll the person at the door as Alex'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The person's name."},
                    "camera_id": {"type": "string", "enum": camera_enum_all,
                                  "description": "Camera to capture the face from."},
                    "category": {"type": "string",
                                 "description": "Optional group, e.g. 'family', 'staff', 'blocked'."},
                },
                "required": ["name", "camera_id"],
            },
        },
    }


def _list_people_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "list_people",
            "description": "List the people currently enrolled in the watchlist.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _forget_face_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "forget_face",
            "description": "Remove a person from the watchlist by name.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    }


def _create_background_task_tool() -> dict[str, Any]:
    """OpenAI/Ollama function schema for the agent's background-task tool."""
    return {
        "type": "function",
        "function": {
            "name": "create_background_task",
            "description": (
                "Queue a long-running investigation that may take a while, "
                "such as searching recorded footage for a past event. Use this "
                "for questions about the RECORDED PAST (earlier, yesterday, a "
                "specific past time). Returns immediately; the answer is "
                "delivered to the user when the task finishes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Short natural-language description of what to look "
                            "for, e.g. 'a person in a red shirt on the porch "
                            "around 3am two days ago'."
                        ),
                    }
                },
                "required": ["query"],
            },
        },
    }


# ── App door tools (read-only orchestration of installed catalog apps) ──
# The agent discovers container apps it can't import and RELAYS their state.
# These tools READ ONLY — there is deliberately no enable/disable/configure
# tool (those stay operator actions; the agent guides). Gated behind the
# "apps" skill, which requires opennvr_api_url to be configured.


def _list_apps_tool() -> dict[str, Any]:
    """Function schema for listing the installed + enabled catalog apps."""
    return {
        "type": "function",
        "function": {
            "name": "list_apps",
            "description": (
                "List the OpenNVR catalog apps installed and ENABLED on this "
                "system (loitering detection, occupancy counting, PPE, etc.). "
                "Use to answer 'what apps are running?', 'do I have a loitering "
                "detector?', 'what can this system watch for automatically?'. "
                "Returns each app's id, name, summary, category, and what it "
                "emits (alert types). Read-only: you can report what's "
                "installed, but you CANNOT enable, disable, or configure an "
                "app — that's an operator action in the app catalog."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _app_status_tool() -> dict[str, Any]:
    """Function schema for reading one installed app's live state/health."""
    return {
        "type": "function",
        "function": {
            "name": "app_status",
            "description": (
                "Get the live state and health of one installed OpenNVR app by "
                "its id (from list_apps). Use for 'is the loitering detector "
                "healthy?', 'what is the occupancy app seeing right now?'. "
                "Returns the app's reported health + state. Read-only — you "
                "report status, you do not change the app's configuration."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_id": {
                        "type": "string",
                        "description": (
                            "The app's id as returned by list_apps, e.g. "
                            "'loitering-detection'."
                        ),
                    }
                },
                "required": ["app_id"],
            },
        },
    }


def _recent_app_alerts_tool() -> dict[str, Any]:
    """Function schema for the app-door ALERT RELAY read side: recent
    alerts fired by installed catalog apps, off the bus."""
    return {
        "type": "function",
        "function": {
            "name": "recent_app_alerts",
            "description": (
                "Look back at recent ALERTS fired by installed OpenNVR catalog "
                "apps (loitering, PPE violations, occupancy limits, etc.). Use "
                "for 'any alerts from my apps?', 'did the PPE app flag anything?', "
                "'has the loitering detector gone off?'. Returns alerts "
                "newest-first with the app, camera, severity, and what fired. "
                "Read-only: you RELAY what an app reported — you cannot arm, "
                "silence, or reconfigure the app."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app_id": {
                        "type": "string",
                        "description": (
                            "Optional: filter to one app's alerts by its id "
                            "(from list_apps), e.g. 'ppe-detection'. Omit for "
                            "alerts from all apps."
                        ),
                    },
                    "window_seconds": {
                        "type": "number",
                        "description": (
                            "How far back, in SECONDS (60=1min, 3600=1hr). "
                            "Defaults to 3600 (the last hour)."
                        ),
                    },
                },
            },
        },
    }


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise SystemExit(f"config file {path} did not parse to a dict")

    for required in ("kaic_url", "kaic_api_key"):
        if not raw.get(required):
            raise SystemExit(f"config: {required} is required")
    _auth_mode = str(raw.get("auth_mode") or "none").strip().lower()
    if _auth_mode not in ("none", "opennvr"):
        raise SystemExit("config: auth_mode must be 'none' or 'opennvr'")
    if _auth_mode == "opennvr" and not (raw.get("opennvr_api_url") or raw.get("opennvr_base_url")):
        raise SystemExit("config: auth_mode 'opennvr' requires opennvr_api_url "
                         "(the server that validates tokens)")

    cameras_raw = raw.get("cameras") or []
    cameras: list[CameraSpec] = []
    for entry in cameras_raw:
        if not isinstance(entry, dict):
            raise SystemExit("config: each camera must be a mapping")
        cam_id = entry.get("camera_id")
        url = entry.get("frame_url")
        if not cam_id or not url:
            raise SystemExit("config: camera entries need camera_id + frame_url")
        _oid = entry.get("opennvr_camera_id")
        cameras.append(CameraSpec(
            camera_id=str(cam_id),
            frame_url=str(url),
            role=str(entry.get("role") or "(no role configured)"),
            opennvr_camera_id=int(_oid) if _oid is not None else None,
        ))
    if not cameras and raw.get("auto_discover_cameras"):
        # Run on whatever hardware we're already on — laptop webcam, USB/Pi
        # camera, drone /dev/video node. No camera provisioning required.
        discovered = discover_local_cameras(
            all_devices=bool(raw.get("auto_discover_all"))
        )
        cameras = [
            CameraSpec(camera_id=cid, frame_url=url, role="local onboard camera")
            for cid, url in discovered
        ]
        logger.info(
            "auto-discovered %d local camera(s): %s",
            len(cameras), ", ".join(f"{c.camera_id}({c.frame_url})" for c in cameras),
        )
    if not cameras and raw.get("opennvr_cameras_url"):
        cameras = _load_opennvr_cameras(
            url=str(raw["opennvr_cameras_url"]),
            api_key=str(raw.get("opennvr_api_key") or raw.get("kaic_api_key") or ""),
        )
    # An empty camera list is allowed. The agent still serves the /demo
    # page and runs the full voice loop; vision tools simply report that
    # no cameras are configured until the operator adds some (the Docker
    # install ships ``cameras: []`` so the stack comes up cleanly before
    # any RTSP source is wired). Each entry that IS supplied is still
    # validated above.
    if not cameras:
        logger.warning(
            "config: no cameras configured — the agent will serve the demo "
            "and voice loop, but vision tools will report no cameras until "
            "you add some under 'cameras:' in the config."
        )

    def _str(key: str, default: str) -> str:
        val = raw.get(key, default)
        return str(val) if val is not None else default

    def _float(key: str, default: float) -> float:
        try:
            return float(raw.get(key, default))
        except (TypeError, ValueError):
            raise SystemExit(f"config: {key} must be a number; got {raw.get(key)!r}")

    def _int(key: str, default: int) -> int:
        try:
            return int(raw.get(key, default))
        except (TypeError, ValueError):
            raise SystemExit(f"config: {key} must be an integer; got {raw.get(key)!r}")

    # Note if the operator set a custom name — only relevant if they also turn on
    # the optional wake word, whose reliability depends on STT transcribing the
    # name consistently (an unusual name is often mis-heard).
    _name = str(raw.get("agent_name") or DEFAULT_AGENT_NAME).strip() or DEFAULT_AGENT_NAME
    if _name.lower() != DEFAULT_AGENT_NAME.lower():
        logger.info(
            "agent_name set to %r. If you enable the wake word, pick a name STT "
            "transcribes reliably and add the spellings it emits to wake_words "
            "(watch the 'wake score' log line while testing).", _name)

    # Single-base-URL ergonomics: when opennvr_base_url is set, DERIVE any unset
    # sibling that points at the same deployment (opennvr_api_url, opennvr_ui_url)
    # from it. Explicit per-field values always win — the base only fills blanks.
    # kaic_url and nats_inference_url are intentionally NOT derived (separate
    # services / ports); the relationship is documented on AppConfig.
    _base = raw.get("opennvr_base_url")
    _base = str(_base).rstrip("/") if _base else None
    _opennvr_api_url = raw.get("opennvr_api_url") or _base
    _opennvr_ui_url = raw.get("opennvr_ui_url") or _base

    return AppConfig(
        kaic_url=str(raw["kaic_url"]),
        kaic_api_key=str(raw["kaic_api_key"]),
        opennvr_base_url=_base,
        detection_adapter=_str("detection_adapter", "yolov8"),
        recognition_adapter=_str("recognition_adapter", "insightface"),
        caption_adapter=_str("caption_adapter", "blip"),
        whisper_url=_str("whisper_url", "http://127.0.0.1:9003"),
        whisper_token=_str("whisper_token", ""),
        stt_noise_filter=bool(raw.get("stt_noise_filter", True)),
        wake_word_required=bool(raw.get("wake_word_required", True)),
        wake_words=(list(raw["wake_words"])
                    if isinstance(raw.get("wake_words"), list) else None),
        wake_fuzzy=float(raw.get("wake_fuzzy", 0.85)),
        wake_require_prefix=bool(raw.get("wake_require_prefix", True)),
        ollama_url=_str("ollama_url", "http://127.0.0.1:9004"),
        ollama_token=_str("ollama_token", ""),
        llm_provider=_str("llm_provider", "ollama"),
        llm_base_url=raw.get("llm_base_url"),
        llm_api_key=raw.get("llm_api_key"),
        llm_think=(None if raw.get("llm_think") is None else bool(raw.get("llm_think"))),
        llm_num_threads=(int(raw["llm_num_threads"]) if raw.get("llm_num_threads") else None),
        llm_num_ctx=int(raw.get("llm_num_ctx") or 4096),
        llm_keep_alive=(raw["llm_keep_alive"] if raw.get("llm_keep_alive") is not None else -1),
        piper_url=_str("piper_url", "http://127.0.0.1:9001"),
        piper_token=_str("piper_token", ""),
        llm_model=_str("llm_model", "qwen2.5:1.5b"),
        llm_temperature=_float("llm_temperature", 0.4),
        llm_max_tokens=_int("llm_max_tokens", 256),
        enabled_tools=(
            list(raw["enabled_tools"])
            if isinstance(raw.get("enabled_tools"), list)
            else None
        ),
        text_mode=bool(raw.get("text_mode", False)),
        frame_cache_ttl_seconds=_float("frame_cache_ttl_seconds", 2.0),
        event_ring_size=_int("event_ring_size", 256),
        nats_inference_url=raw.get("nats_inference_url"),
        nats_inference_token=raw.get("nats_inference_token"),
        footage_index_path=raw.get("footage_index_path"),
        opennvr_cameras_url=raw.get("opennvr_cameras_url"),
        opennvr_api_key=raw.get("opennvr_api_key"),
        opennvr_api_url=_opennvr_api_url,
        opennvr_ui_url=_opennvr_ui_url,
        auth_mode=str(raw.get("auth_mode") or "none").strip().lower(),
        agent_public_url=raw.get("agent_public_url"),
        alarm_ring_defaults=(
            {str(k).strip().lower(): str(v).strip().lower()
             for k, v in raw["alarm_ring_defaults"].items()}
            if isinstance(raw.get("alarm_ring_defaults"), dict) else None
        ),
        emergency_contacts=(
            dict(raw["emergency_contacts"])
            if isinstance(raw.get("emergency_contacts"), dict) else None
        ),
        agent_name=_name,
        voice_gender=str(raw.get("voice_gender") or "neutral"),
        avatar_video=bool(raw.get("avatar_video", True)),
        state_path=raw.get("state_path"),
        faces_url=raw.get("faces_url"),
        faces_token=raw.get("faces_token"),
        notify_webhooks=(
            list(raw["notify_webhooks"])
            if isinstance(raw.get("notify_webhooks"), list) else None
        ),
        notify_events=(
            list(raw["notify_events"])
            if isinstance(raw.get("notify_events"), list) else None
        ),
        notify_apprise=(
            list(raw["notify_apprise"])
            if isinstance(raw.get("notify_apprise"), list) else None
        ),
        host=_str("host", "127.0.0.1"),
        port=_int("port", 9100),
        system_prompt=str(raw.get("system_prompt") or _DEFAULT_SYSTEM_PROMPT),
        cameras=cameras,
        auto_discover_cameras=bool(raw.get("auto_discover_cameras", False)),
        auto_discover_all=bool(raw.get("auto_discover_all", False)),
        synthetic_detection=bool(raw.get("synthetic_detection", False)),
    )


# How often the background reconcile re-checks OpenNVR for cameras. Fast while
# the agent has none yet (boot-order race: agent up before the core has cameras
# ready), then slow steady-state to pick up cameras added later.
_OPENNVR_CAMERA_RECONCILE_FAST = 5.0
_OPENNVR_CAMERA_RECONCILE_SLOW = 30.0


def _load_opennvr_cameras(*, url: str, api_key: str) -> list[CameraSpec]:
    """Load frame sources from OpenNVR's internal camera-agent endpoint."""
    import httpx

    headers = {"X-Internal-Api-Key": api_key}
    try:
        response = httpx.get(url, headers=headers, timeout=15.0, trust_env=False)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning(
            "config: could not load cameras from OpenNVR (%s): %s",
            url,
            exc,
        )
        return []

    raw_cameras = payload.get("cameras") if isinstance(payload, dict) else None
    if not isinstance(raw_cameras, list):
        logger.warning("config: OpenNVR cameras response had no 'cameras' list")
        return []

    cameras: list[CameraSpec] = []
    for entry in raw_cameras:
        if not isinstance(entry, dict):
            continue
        cam_id = entry.get("camera_id")
        frame_url = entry.get("frame_url")
        if not cam_id or not frame_url:
            continue
        role = entry.get("role") or entry.get("name") or "(OpenNVR camera)"
        # The internal endpoint returns the server-side Camera.id as
        # ``open_nvr_camera_id``; carry it so recordings/live playback can
        # resolve the OpenNVR camera (older configs used opennvr_camera_id).
        _oid = entry.get("open_nvr_camera_id", entry.get("opennvr_camera_id"))
        cameras.append(
            CameraSpec(
                camera_id=str(cam_id),
                frame_url=str(frame_url),
                role=str(role),
                opennvr_camera_id=int(_oid) if _oid not in (None, "") else None,
            )
        )
    logger.info("config: loaded %d camera(s) from OpenNVR", len(cameras))
    return cameras


# ── Runtime assembly ───────────────────────────────────────────────


class CameraAgentRuntime:
    """Owns the long-lived objects (context, clients, tool registry,
    NATS subscriber). One instance per process; each WebSocket
    conversation builds its own Pipecat pipeline on top of these
    shared pieces so per-call state stays per-call.
    """

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg

        self.context = CameraContext(
            cameras=cfg.cameras,
            frame_cache_ttl_seconds=cfg.frame_cache_ttl_seconds,
            event_ring_size=cfg.event_ring_size,
        )
        for cam in cfg.cameras:
            self.context.register_frame_source(
                cam.camera_id,
                build_frame_source(camera_id=cam.camera_id, url=cam.frame_url),
            )

        self.whisper = WhisperClient(url=cfg.whisper_url, token=cfg.whisper_token)
        if cfg.llm_provider.strip().lower() == "openai":
            self.ollama = OpenAILLMClient(
                base_url=cfg.llm_base_url or cfg.ollama_url,
                api_key=cfg.llm_api_key or cfg.ollama_token or None,
                model=cfg.llm_model,
            )
            logger.info("LLM brain: OpenAI-compatible at %s (model=%s)",
                        cfg.llm_base_url or cfg.ollama_url, cfg.llm_model)
        else:
            # Auto-disable thinking for Qwen3 (a thinking model) unless the
            # operator set llm_think explicitly — on CPU, thinking burns the
            # token budget and leaves no answer. Non-thinking models: send nothing.
            _think = cfg.llm_think
            if _think is None and "qwen3" in (cfg.llm_model or "").lower():
                _think = False
            self.ollama = OllamaClient(
                url=cfg.ollama_url, token=cfg.ollama_token, model=cfg.llm_model,
                num_thread=cfg.llm_num_threads, num_ctx=cfg.llm_num_ctx,
                think=_think, keep_alive=cfg.llm_keep_alive,
            )
        self.piper = PiperClient(url=cfg.piper_url, token=cfg.piper_token)

        self.caption_client = KaicAdapterClient(
            kaic_url=cfg.kaic_url,
            api_key=cfg.kaic_api_key,
            adapter_name=cfg.caption_adapter,
        )
        if cfg.synthetic_detection:
            # Demo mode: no KAI-C/YOLO — detections come from the synthetic
            # frames' embedded ground truth so the demo is deterministic.
            self.detection_client = SyntheticDetectionClient()
        else:
            self.detection_client = KaicAdapterClient(
                kaic_url=cfg.kaic_url,
                api_key=cfg.kaic_api_key,
                adapter_name=cfg.detection_adapter,
            )
        self.recognition_client = KaicAdapterClient(
            kaic_url=cfg.kaic_url,
            api_key=cfg.kaic_api_key,
            adapter_name=cfg.recognition_adapter,
        )
        # Live "skills as capabilities" signal: which tasks KAI-C's registered
        # adapters currently advertise (60s TTL, advisory — see skills_payload).
        self.kaic_capabilities = KaicCapabilitiesClient(
            kaic_url=cfg.kaic_url,
            api_key=cfg.kaic_api_key,
        )

        # App door (read-only): discover installed catalog apps and relay
        # their live state as conversational skills. Only constructed when
        # opennvr_api_url is configured — otherwise the app tools/skills
        # simply don't exist (clean fall-back). The key reuses the same
        # deployment INTERNAL_API_KEY the infer path uses: opennvr_api_key,
        # else kaic_api_key, else the INTERNAL_API_KEY env var.
        self.app_registry: AppRegistryClient | None = None
        if cfg.opennvr_api_url:
            import os

            app_key = (
                cfg.opennvr_api_key
                or cfg.kaic_api_key
                or os.environ.get("INTERNAL_API_KEY", "")
            )
            self.app_registry = AppRegistryClient(
                base_url=cfg.opennvr_api_url, api_key=app_key,
            )
            logger.info(
                "app door enabled: reading installed apps from %s "
                "(read-only — the agent never enables/disables/configures)",
                cfg.opennvr_api_url,
            )

        # Optional read-only footage-search index → enables search_footage.
        self.footage_index = None
        if cfg.footage_index_path:
            from footage_index import FootageIndex

            self.footage_index = FootageIndex(cfg.footage_index_path)
            if self.footage_index.available:
                logger.info(
                    "camera-agent: footage index loaded from %s; "
                    "search_footage tool enabled",
                    cfg.footage_index_path,
                )
            else:
                logger.info(
                    "camera-agent: footage_index_path set (%s) but the index "
                    "isn't readable yet; search_footage will report it's "
                    "unavailable until the footage-search indexer has run",
                    cfg.footage_index_path,
                )

        self.tools = CameraTools(
            context=self.context,
            caption_client=self.caption_client,
            detection_client=self.detection_client,
            recognition_client=self.recognition_client,
            footage_index=self.footage_index,
        )
        # Advertised tools are (re)built by _configure_tools from enabled_tools
        # minus any skills switched off at runtime. disabled_skills starts empty.
        self._camera_ids = [cam.camera_id for cam in cfg.cameras]
        self.disabled_skills: set[str] = set()
        # UI-edited target→annunciation overrides (admin action) — the
        # LAST layer over config + built-ins in ring_defaults(); durable
        # via persist()/load_state like the skill toggles.
        self._ring_overrides: dict[str, str] = {}
        self._configure_tools()
        self.tool_handlers = {
            "describe_camera": self.tools.describe_camera,
            "detect_objects": self.tools.detect_objects,
            "recognize_faces": self.tools.recognize_faces,
            "search_footage": self.tools.search_footage,
            "recent_events": self.tools.recent_events,
            "create_background_task": self._handle_create_task,
            "create_monitor": self._handle_create_monitor,
            "stop_monitor": self._handle_stop_monitor,
            "create_alarm": self._handle_create_alarm,
            "stop_alarm": self._handle_stop_alarm,
            "create_report": self._handle_create_report,
            "stop_report": self._handle_stop_report,
            "enroll_face": self._handle_enroll_face,
            "list_people": self._handle_list_people,
            "forget_face": self._handle_forget_face,
            "list_apps": self._handle_list_apps,
            "app_status": self._handle_app_status,
            "recent_app_alerts": self._handle_recent_app_alerts,
        }

        self.agent_name = agent_name_for(cfg.agent_name)
        self.faces = FaceClient(url=cfg.faces_url, token=cfg.faces_token) if cfg.faces_url else None
        self.notifier = Notifier(self, webhooks=cfg.notify_webhooks, events=cfg.notify_events,
                                 apprise_urls=cfg.notify_apprise)
        # Auth delegation to the main OpenNVR server (auth_mode="opennvr").
        # Constructed whenever the server URL is known so tests can exercise
        # it; only the middleware consults auth_mode.
        self.auth = (OpennvrAuthClient(base_url=cfg.opennvr_api_url)
                     if cfg.opennvr_api_url else None)
        self.recordings = (OpennvrRecordingsClient(base_url=cfg.opennvr_api_url)
                           if cfg.opennvr_api_url else None)
        self.tasks = TaskManager(self)
        self.monitors = MonitorManager(self)
        self.alarms = AlarmManager(self)
        self.reports = ReportScheduler(self)
        self._stop_event = asyncio.Event()
        # >0 while an interactive voice/chat turn is being served. STT, the
        # LLM, TTS, and the vision adapter all share one machine on a typical
        # single-box install, so background detection loops (alarms/monitors)
        # yield their per-cycle inference while the user is mid-turn — the
        # thing they're waiting on stays snappy. See interactive_turn().
        self._interactive_turns = 0
        self._subscriber_task: asyncio.Task | None = None
        self._app_alert_subscriber_task: asyncio.Task | None = None
        self._camera_reconcile_task: asyncio.Task | None = None
        # Push-side throttle for the app-alert relay: last relay time per
        # (app_id, camera_id). Reuses MonitorManager's notify cooldown.
        self._app_relay_last: dict[tuple[str, str], float] = {}

    def _configure_tools(self) -> None:
        """(Re)build the advertised tool lists from ``enabled_tools`` minus the
        tools of any switched-off skill. Called at startup and whenever a skill
        is toggled, so the LLM's callable tools change live (the "reconfigure").
        ``background_tool_definitions`` excludes the agent-control tools (a task
        must not spawn tasks / arm alarms)."""
        excluded: set[str] = set()
        for sid in self.disabled_skills:
            excluded.update(SKILL_TOOLS.get(sid, ()))
        # App door: never advertise the read-only app tools when the registry
        # isn't wired (opennvr_api_url unset) — there's nothing to read, so the
        # LLM shouldn't see them. When it IS wired, they're advertised and the
        # handlers degrade gracefully if the registry is momentarily down.
        if not self.skill_requirement_met("apps"):
            excluded.update(SKILL_TOOLS["apps"])
        allow = None if self.cfg.enabled_tools is None else set(self.cfg.enabled_tools)

        def keep(defn: dict[str, Any]) -> bool:
            name = defn["function"]["name"]
            return (allow is None or name in allow) and name not in excluded

        cam_all = self._camera_ids + ["all"]
        base = [t for t in build_tool_definitions(self._camera_ids, enabled=None)
                if keep(t)]
        control = [t for t in (
            _create_background_task_tool(),
            _create_monitor_tool(cam_all), _stop_monitor_tool(),
            _create_alarm_tool(cam_all), _stop_alarm_tool(),
            _create_report_tool(), _stop_report_tool(),
            _enroll_face_tool(cam_all), _list_people_tool(), _forget_face_tool(),
            _list_apps_tool(), _app_status_tool(), _recent_app_alerts_tool(),
        ) if keep(t)]
        self.background_tool_definitions = base
        self.tool_definitions = base + control
        # Viewer tier: look but not touch — read tools plus the READ-ONLY
        # control verbs (app door + face listing); every mutating verb
        # (arm alarms, create watches/tasks/reports, enroll/forget faces)
        # is operator+. Enforced at the TOOLSET so a viewer's chat can't
        # talk the agent into arming anything.
        _viewer_safe = {"list_apps", "app_status", "recent_app_alerts", "list_people"}
        self.viewer_tool_definitions = base + [
            t for t in control if t["function"]["name"] in _viewer_safe]

    _RING_BUILTIN_DEFAULTS = {"fire": "siren", "smoke": "siren",
                              "flame": "siren", "gas": "siren"}

    def ring_defaults(self) -> dict[str, str]:
        """Target→annunciation defaults: built-ins overlaid with the
        site's config (a farm's snake, a bank's person). Values are
        sanitized to the four known levels."""
        merged = dict(self._RING_BUILTIN_DEFAULTS)
        for layer in (self.cfg.alarm_ring_defaults or {}), self._ring_overrides:
            for k, v in layer.items():
                if v in ("siren", "pulse", "chime", "silent"):
                    merged[k] = v
        return merged

    def set_ring_overrides(self, overrides: dict[str, str]) -> dict[str, str]:
        """Replace the UI-edited overrides (sanitized), persist, return
        the merged map. Junk levels and blank targets are dropped."""
        clean = {}
        for k, v in (overrides or {}).items():
            k = str(k).strip().lower()
            v = str(v).strip().lower()
            if k and v in ("siren", "pulse", "chime", "silent"):
                clean[k] = v
        self._ring_overrides = clean
        self.persist()
        return self.ring_defaults()

    def events_feed(self, limit: int = 50) -> list[dict[str, Any]]:
        """ONE feed of what happened — alarm rings, watch hits, and app
        alerts relayed off the bus — newest first. The demo's Events card
        and its per-camera filter render this; nothing new is collected,
        it's a VIEW over the three streams the agent already records."""
        out: list[dict[str, Any]] = []
        for e in self.alarms.events():
            out.append({"ts": e.get("ts"), "camera_id": e.get("camera") or "",
                        "title": str(e.get("name") or "Alarm"),
                        "detail": str(e.get("text") or ""),
                        "severity": "critical", "kind": "alarm"})
        for n in self.monitors.notifications():
            out.append({"ts": n.get("ts"), "camera_id": n.get("camera") or "",
                        "title": str(n.get("text") or "Watch"),
                        "detail": "", "severity": "info", "kind": "watch"})
        for al in self.context.recent_app_alerts(window_seconds=24 * 3600):
            out.append({"ts": al.received_at, "camera_id": al.camera_id or "",
                        "title": str(al.title or al.app_id),
                        "detail": str(al.summary or ""),
                        "severity": str(al.severity or "info"),
                        "kind": "app", "source": al.app_id})
        out = [e for e in out if e.get("ts")]
        out.sort(key=lambda e: e["ts"], reverse=True)
        return out[:limit]

    def tools_for_tier(self, tier: str) -> list[dict[str, Any]]:
        """The toolset a caller of this permission tier may drive."""
        return (self.viewer_tool_definitions if tier == "viewer"
                else self.tool_definitions)

    # ── skills: capabilities the agent carries (toggle → reconfigure) ──
    def skill_requirement_met(self, req: str) -> bool:
        """Is the backend a skill needs actually wired up?"""
        if req == "faces":
            return self.faces is not None
        if req == "events":
            return bool(self.cfg.nats_inference_url)
        if req == "footage":
            return bool(getattr(self, "footage_index", None)
                        and self.footage_index.available)
        if req == "apps":
            # The app door is wired iff the OpenNVR server base URL is set —
            # then the AppRegistryClient exists. Configuration presence is the
            # gate; a momentarily-unreachable registry degrades at call time.
            return getattr(self, "app_registry", None) is not None
        return True   # no external requirement

    def skills_payload(self) -> list[dict[str, Any]]:
        """Catalogue for the UI: what each skill is, what it uses, and whether
        it's enabled / can be enabled.

        Each entry also carries the LIVE KAI-C view: ``backing_tasks`` (the
        task strings that would serve the skill) and ``tasks_available``
        (whether the last capabilities fetch saw an adapter advertising one
        of them) — so the UI can grey a skill out with a reason. Advisory
        only: ``skill_requirement_met`` remains the enable gate, and when
        KAI-C is unreachable (last fetch failed / not yet fetched) every
        skill reports ``tasks_available: true`` — i.e. exactly the previous
        config-based behavior, never a spuriously greyed-out tool."""
        live_tasks = self.kaic_capabilities.tasks_advertised   # None = unknown
        advertised = {t["function"]["name"] for t in self.tool_definitions}
        allow = None if self.cfg.enabled_tools is None else set(self.cfg.enabled_tools)
        uses = {
            "see": f"{self.cfg.caption_adapter} caption + {self.cfg.llm_model}",
            "count": f"{self.cfg.detection_adapter} detection",
            "faces": f"{self.cfg.recognition_adapter} recognition",
            "events": "inference event bus (NATS)",
            "footage": "footage-search index",
            "apps": "OpenNVR app registry + alert relay (read-only)",
        }
        hints = {
            "faces": "Set faces_url to the recognition adapter to enable.",
            "events": "Set nats_inference_url to stream inference events.",
            "footage": "Set footage_index_path (built by the footage-search example).",
            "apps": "Set opennvr_api_url to read installed catalog apps.",
        }
        out: list[dict[str, Any]] = []
        for sid, icon, name, example, req in _SKILL_META:
            primary = SKILL_TOOLS[sid][0]
            req_met = self.skill_requirement_met(req)
            allowed = allow is None or primary in allow
            available = req_met and allowed   # can it be turned on at all?
            # "enabled" = usable now: advertised to the LLM AND its backend wired.
            # (Vision tools are always advertised and degrade at call time, so a
            # missing backend must still read as not-enabled in the panel.)
            enabled = (primary in advertised) and req_met
            backing = _SKILL_BACKING_TASKS.get(sid, [])
            # Live availability from the tasks_advertised intersection. Unknown
            # (KAI-C unreachable) or nothing to back → don't grey anything out.
            tasks_available = (
                live_tasks is None or not backing
                or bool(set(backing) & live_tasks)
            )
            entry = {
                "id": sid, "icon": icon, "name": name, "example": example,
                "uses": uses.get(sid, f"agent app + {self.cfg.llm_model}"),
                "enabled": enabled, "available": available,
                "hint": "" if available else (hints.get(req, "Not enabled in config.")),
                "backing_tasks": backing, "tasks_available": tasks_available,
            }
            # When a skill is greyed out for want of a backing adapter, GUIDE
            # the operator: name the suggested adapter(s) and, if we know the
            # main UI's base URL, deep-link them to the AI Adapters view to
            # enable one. Guidance only — no auto-enable path exists here; the
            # link is a plain navigation target and enabling stays an operator
            # action behind OpenNVR's permission gate. Additive: not-greyed
            # skills omit both fields.
            if not tasks_available:
                entry["suggested_adapters"] = _SKILL_SUGGESTED_ADAPTERS.get(sid, [])
                entry["enable_url"] = (
                    f"{self.cfg.opennvr_ui_url.rstrip('/')}/ai-adapters"
                    if self.cfg.opennvr_ui_url else None
                )
                # App-install on-ramp, symmetric to the adapter one above:
                # name the catalog app(s) that package this capability and,
                # when we know the main UI's base URL, deep-link the App
                # Catalog. Guide-only — no install POST; the operator
                # installs from the catalog behind OpenNVR's permission gate.
                entry["suggested_apps"] = _SKILL_SUGGESTED_APPS.get(sid, [])
                entry["app_enable_url"] = (
                    f"{self.cfg.opennvr_ui_url.rstrip('/')}/app-catalog"
                    if self.cfg.opennvr_ui_url else None
                )
            if sid == "watch":
                # The converged monitors: which SDK rule classes back the
                # count/crossing kinds (spec §07 "one rule library, two doors").
                entry["rules"] = sorted(MonitorManager._CONVERGED.values())
            out.append(entry)

        # ── App door: each installed+enabled catalog app as a skill entry ──
        # Additive and clearly marked source:"app". These are container apps
        # the agent can't import — it READS them (list/status) and relays. Only
        # surfaced when the registry is wired AND reachable (apps_cached is a
        # list); unset or unreachable → no app entries (clean fall-back). Read
        # only: there is no enable/disable/configure path from these entries.
        out.extend(self._app_skill_entries())
        return out

    def _app_skill_entries(self) -> list[dict[str, Any]]:
        """Skill entries for installed+enabled catalog apps (source:"app").

        Reads the AppRegistryClient's cached list (the ``/skills`` endpoint
        refreshes it first, TTL'd, never raising). Returns [] when the app
        door is unwired or the registry is unreachable — so a down registry
        never removes the *generic* "apps" capability skill, it only omits
        the per-app entries."""
        registry = getattr(self, "app_registry", None)
        if registry is None:
            return []
        apps = registry.apps_cached          # None = unknown / unreachable
        if not apps:
            return []
        entries: list[dict[str, Any]] = []
        for a in apps:
            if not a.get("enabled"):
                continue
            app_id = str(a.get("id") or "")
            if not app_id:
                continue
            manifest = a.get("manifest") or {}
            name = str(a.get("name") or manifest.get("name") or app_id)
            summary = str(manifest.get("summary") or "").strip()
            category = a.get("category") or manifest.get("category") or "app"
            emits = [
                str(e.get("name"))
                for e in (manifest.get("emits") or [])
                if isinstance(e, dict) and e.get("name")
            ]
            entries.append({
                "id": f"app:{app_id}",
                "source": "app",           # clearly marks the app door origin
                "app_id": app_id,
                "icon": "🧩",
                "name": name,
                "category": category,
                "uses": f"installed app — {summary}" if summary
                        else "installed app",
                "summary": summary,
                "emits": emits,
                # Installed + enabled by an operator ⇒ enabled. The agent
                # can query it (read-only); it can't toggle or configure it.
                "enabled": True,
                "available": True,
                "read_only": True,
                "hint": "",
                "backing_tasks": [],
                "tasks_available": True,
            })
        return entries

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> bool:
        """Turn a skill on/off and reconfigure the toolset. Returns False if the
        skill is unknown or can't be enabled (backend not configured)."""
        if skill_id not in SKILL_TOOLS:
            return False
        if enabled:
            _, _, _, _, req = next(m for m in _SKILL_META if m[0] == skill_id)
            if not self.skill_requirement_met(req):
                return False
            self.disabled_skills.discard(skill_id)
        else:
            self.disabled_skills.add(skill_id)
        self._configure_tools()
        return True

    def hardware_recommendation(self) -> dict[str, Any]:
        """What the ENABLED skills run on vs. what makes them fast — the
        demo UI's "Hardware" panel. Editorial rows from tasks.yml
        (``_SKILL_HARDWARE``) + one row for the LLM every skill rides,
        + the live "running on GPU/CPU" line from KAI-C's declared
        adapter permissions. ADVISORY ONLY: fallbacks always work, so
        the panel says "would be faster", never "won't work"."""
        enabled = {s["id"] for s in self.skills_payload()
                   if s.get("enabled") and not str(s.get("id", "")).startswith("app:")}
        rows: list[dict[str, Any]] = []
        seen_tasks: set[str] = set()
        gpu_labels: list[str] = []
        for sid, _icon, _name, _example, _req in _SKILL_META:  # stable order
            if sid not in enabled:
                continue
            for row in _SKILL_HARDWARE.get(sid, []):
                if row["task"] in seen_tasks:  # inherits (watch↔count) share tasks
                    continue
                seen_tasks.add(row["task"])
                rows.append({**row, "skill": sid})
                if row["recommended"]:
                    gpu_labels.append(row["label"])
        # Every skill funnels through the local LLM — measured guidance
        # from MODELS_AND_LATENCY.md, not task-specific.
        rows.append({
            "skill": "*", "task": "llm",
            "label": f"Language model ({self.cfg.llm_model})",
            "adapters": ["ollama"],
            "min": "~4 GB RAM (small models)",
            "recommended": "GPU / Apple Silicon — ~10-20x faster generation",
            "note": "Memory-bandwidth-bound on CPU: a smaller model is both "
                    "lighter AND faster.",
        })
        gpu_map = self.kaic_capabilities.gpu_adapters
        if gpu_map is None:
            running_on = "unknown"
        else:
            running_on = "gpu" if any(gpu_map.values()) else "cpu"
        if running_on == "gpu":
            on_gpu = sorted(a for a, g in (gpu_map or {}).items() if g)
            summary = f"Running on GPU ({', '.join(on_gpu)})."
        elif running_on == "cpu":
            summary = "Running on CPU only"
            summary += (f" — an NVIDIA (CUDA) GPU would speed up "
                        f"{', '.join(gpu_labels[:3])}." if gpu_labels else ".")
        else:
            summary = "Adapter hardware not visible (KAI-C unreachable)"
            summary += (f" — a CUDA GPU is recommended for "
                        f"{', '.join(gpu_labels[:3])}." if gpu_labels else ".")
        return {
            "running_on": running_on,
            "gpu_adapters": sorted(a for a, g in (gpu_map or {}).items() if g),
            "gpu_recommended": bool(gpu_labels),
            "summary": summary,
            "rows": rows,
            "tips": [
                "Chat mode instead of voice skips Piper TTS — the measured "
                "CPU hog (~4 cores while speaking).",
                "On CPU the LLM is memory-bandwidth-bound: a smaller model "
                "is both lighter and faster.",
            ],
        }

    def restore_default_skills(self) -> int:
        """Clear every runtime skill toggle — the fresh-boot default is
        "nothing disabled". Returns how many toggles were cleared.

        Availability is a SEPARATE gate from the operator toggle: a skill
        whose backend isn't configured (faces without faces_url, …) stays
        unavailable after this, exactly as on a fresh boot — so restoring
        can never advertise a tool the deployment can't back."""
        n = len(self.disabled_skills)
        if n:
            self.disabled_skills.clear()
            self._configure_tools()
            self.persist()
        return n

    def _emergency_contact_for(self, name: str, target: str) -> str | None:
        contacts = self.cfg.emergency_contacts or {}
        for key in (target.lower(), name.lower()):
            if key in contacts:
                return str(contacts[key])
        return None

    async def _handle_create_alarm(self, args: dict[str, Any]) -> str:
        name = str(args.get("name") or "").strip() or "Alarm"
        target = str(args.get("target") or "").strip().lower()
        if not target:
            return "What should set off the alarm (e.g. fire, a person)?"
        cams = self.tools._resolve_cameras(args)
        if isinstance(cams, str):  # ERROR:
            return cams
        after_min = _parse_hhmm(args.get("after"))
        before_min = _parse_hhmm(args.get("before"))
        contact = self._emergency_contact_for(name, target)
        ring = str(args.get("ring") or "").strip().lower()
        if ring not in ("siren", "pulse", "chime", "silent"):
            # Site-configurable default (built-ins: fire-grade → siren);
            # everything unmapped is a doorbell-style chime.
            ring = self.ring_defaults().get(target, "chime")
        alarm = self.alarms.create(
            name=name, target=target, camera_ids=cams,
            after_min=after_min, before_min=before_min, emergency_contact=contact,
            ring=ring,
        )
        self.persist()
        where = "all cameras" if set(cams) == {c.camera_id for c in self.cfg.cameras} else ", ".join(cams)
        window = alarm.window_label()
        when = "" if window == "any time" else f" ({window})"
        extra = f" If it fires I'll flag the emergency contact ({contact})." if contact else ""
        style = {"siren": "siren until silenced",
                 "pulse": "loud for a minute, then stands down",
                 "chime": "a single chime",
                 "silent": "silent — notifications only"}[alarm.ring]
        return (f"Armed alarm #{alarm.id} '{name}' ({style}) — I'll fire it "
                f"if I see {target} on {where}{when}.{extra}")

    async def _handle_stop_alarm(self, args: dict[str, Any]) -> str:
        try:
            aid = int(args.get("alarm_id"))
        except (TypeError, ValueError):
            return "Which alarm? Tell me its number."
        action = str(args.get("action") or "disarm").strip().lower()
        if action == "silence":
            return ("Silenced." if self.alarms.acknowledge(aid)
                    else f"Alarm #{aid} isn't currently ringing.")
        ok = self.alarms.stop(aid)
        self.persist()
        return (f"Disarmed alarm #{aid}." if ok else f"I don't have an alarm #{aid}.")

    async def _handle_create_report(self, args: dict[str, Any]) -> str:
        name = str(args.get("name") or "").strip() or "Report"
        query = str(args.get("query") or "").strip()
        if not query:
            return "What should the report summarize?"
        at_min = _parse_hhmm(args.get("at"))
        every = args.get("every_minutes")
        try:
            every = int(every) if every else None
        except (TypeError, ValueError):
            every = None
        sched = self.reports.create(name=name, query=query, at_min=at_min, every_minutes=every)
        self.persist()
        return (f"Scheduled report #{sched.id} '{name}' — {sched.schedule_label()}. "
                f"I'll deliver it here (and to your alert channels) each time it runs.")

    async def _handle_stop_report(self, args: dict[str, Any]) -> str:
        try:
            rid = int(args.get("report_id"))
        except (TypeError, ValueError):
            return "Which report? Tell me its number."
        ok = self.reports.stop(rid)
        self.persist()
        return (f"Cancelled report #{rid}." if ok else f"I don't have a report #{rid}.")

    async def _handle_enroll_face(self, args: dict[str, Any]) -> str:
        if not self.faces:
            return "Face management isn't configured on this system."
        name = str(args.get("name") or "").strip()
        if not name:
            return "What's the person's name?"
        cam = str(args.get("camera_id") or "").strip()
        if not self.context.known_camera(cam):
            cams = [c.camera_id for c in self.cfg.cameras]
            return f"Which camera should I capture from? Available: {cams}."
        try:
            frame = await self.context.get_frame(cam)
        except Exception:
            return f"I couldn't get a clear image from {cam} to enroll {name}."
        try:
            await self.faces.enroll(name=name, frame_jpeg=frame,
                                    category=str(args.get("category") or "known"))
        except Exception:
            logger.exception("enroll_face failed")
            return f"I couldn't enroll {name} — the recognizer didn't accept the face."
        return f"Got it — I'll recognise {name} from now on."

    async def _handle_list_people(self, args: dict[str, Any]) -> str:
        if not self.faces:
            return "Face management isn't configured on this system."
        try:
            people = await self.faces.list_people()
        except Exception:
            logger.exception("list_people failed")
            return "I couldn't reach the recognizer to list people."
        names = [str(p.get("name") if isinstance(p, dict) else p) for p in people]
        return "I know: " + ", ".join(names) + "." if names else "No one is enrolled yet."

    async def _handle_forget_face(self, args: dict[str, Any]) -> str:
        if not self.faces:
            return "Face management isn't configured on this system."
        name = str(args.get("name") or "").strip()
        if not name:
            return "Whose face should I forget?"
        try:
            await self.faces.forget(name)
        except Exception:
            logger.exception("forget_face failed")
            return f"I couldn't remove {name}."
        return f"Removed {name} from the watchlist."

    # ── Persistence ───────────────────────────────────────────────
    def persist(self) -> None:
        """Write alarms/watches/reports to the state file (best-effort)."""
        if not self.cfg.state_path:
            return
        import json
        import os
        import tempfile

        data = {
            "monitors": self.monitors.export(),
            "alarms": self.alarms.export(),
            "reports": self.reports.export(),
            # Operator skill toggles are part of the durable agent state
            # too — without this a skill switched off at runtime silently
            # comes back on the next restart (its tools re-advertised).
            "disabled_skills": sorted(self.disabled_skills),
            "ring_overrides": dict(self._ring_overrides),
        }
        try:
            d = os.path.dirname(self.cfg.state_path) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.cfg.state_path)  # atomic
        except Exception:  # pragma: no cover - best effort
            logger.exception("persist failed")

    def load_state(self) -> None:
        """Re-arm persisted alarms/watches/reports on startup (needs a loop)."""
        import json
        import os

        if not self.cfg.state_path or not os.path.exists(self.cfg.state_path):
            return

        try:
            data = json.loads(open(self.cfg.state_path).read())
        except Exception:
            logger.exception("could not read state file %s", self.cfg.state_path)
            return
        self.monitors.restore(data.get("monitors") or [])
        self.alarms.restore(data.get("alarms") or [], contact_for=self._emergency_contact_for)
        self.reports.restore(data.get("reports") or [])
        # Re-apply persisted skill toggles through the normal path so the
        # advertised toolset reflects them (a disabled skill's tools stay
        # unadvertised). Ignore unknown ids so a stale state file can't
        # break boot.
        ro = data.get("ring_overrides")
        if isinstance(ro, dict):
            self._ring_overrides = {
                str(k).lower(): str(v).lower() for k, v in ro.items()
                if str(v).lower() in ("siren", "pulse", "chime", "silent")}
        restored_disabled: list[str] = []
        for sid in (data.get("disabled_skills") or []):
            if self.set_skill_enabled(sid, False):
                restored_disabled.append(sid)
        # Count what was actually armed (post-dedup), not the raw persisted
        # rows — and rewrite the state file so the collapsed set is durable
        # (the pile-up doesn't come back on the next restart).
        n_mon, n_alarm, n_report = (
            len(data.get("monitors") or []), len(data.get("alarms") or []),
            len(data.get("reports") or []))
        armed_mon, armed_alarm, armed_report = (
            len(self.monitors.export()), len(self.alarms.export()),
            len(self.reports.export()))
        if (armed_mon, armed_alarm, armed_report) != (n_mon, n_alarm, n_report):
            self.persist()
        logger.info(
            "restored %d watches, %d alarms, %d reports (from %d/%d/%d persisted), "
            "%d disabled skills from %s",
            armed_mon, armed_alarm, armed_report, n_mon, n_alarm, n_report,
            len(restored_disabled), self.cfg.state_path)

    async def _handle_create_monitor(self, args: dict[str, Any]) -> str:
        kind = str(args.get("kind") or "").strip().lower()
        if kind not in ("notify", "count", "crossing"):
            return "I can 'notify' you, keep a 'count', or count line 'crossing's — which would you like?"
        target = str(args.get("target") or "").strip().lower()
        if not target:
            return "What should I watch for (e.g. a person, a car)?"
        cams = self.tools._resolve_cameras(args)
        if isinstance(cams, str):  # ERROR:
            return cams
        line = args.get("line")
        if kind == "crossing":
            if not (isinstance(line, (list, tuple)) and len(line) == 4):
                return ("For crossing counts I need a line as [x1,y1,x2,y2] in "
                        "0-1 frame coordinates — where should the line go?")
            line = [float(v) for v in line]
        try:
            mon = self.monitors.create(
                kind=kind, camera_ids=cams, target=target,
                description=str(args.get("description") or "").strip(),
                line=line if kind == "crossing" else None,
            )
        except ValueError as exc:
            # The SDK rule rejected the params (e.g. a degenerate line
            # whose two points coincide). Relay the reason so the LLM can
            # ask the user to fix it. ERROR: prefix → HTTP 400 on /monitors.
            return f"ERROR: I couldn't set that watch up — {exc}"
        except RuntimeError:
            # The converged rule library module isn't shipped with this
            # build (monitor_host loads occupancy_counting.py /
            # line_crossing.py by file path from the sibling examples/
            # tree). Same relayable ERROR: shape as the ValueError path
            # so the conversation degrades gracefully instead of dying.
            rule = self.monitors._CONVERGED.get(kind, kind)
            logger.exception("create_monitor: rule library for %r unavailable", kind)
            return (f"ERROR: I couldn't set that watch up — this build "
                    f"doesn't include the '{rule}' rule library.")
        self.persist()
        where = "all cameras" if set(cams) == {c.camera_id for c in self.cfg.cameras} else ", ".join(cams)
        if kind == "notify":
            return (f"Done — watch #{mon.id} is live. I'll let you know whenever "
                    f"I see a {target} on {where}.")
        if kind == "crossing":
            return (f"Done — watch #{mon.id} is counting {target}s crossing the line "
                    f"on {where} (needs the tracking adapter for accurate counts).")
        return (f"Done — I'm now counting {target}s on {where} (watch #{mon.id}); "
                f"you'll see a live tally in the panel.")

    async def _handle_stop_monitor(self, args: dict[str, Any]) -> str:
        try:
            mid = int(args.get("monitor_id"))
        except (TypeError, ValueError):
            return "Which watch should I stop? Tell me its number."
        ok = self.monitors.stop(mid)
        self.persist()
        return (f"Stopped watch #{mid}." if ok else f"I don't have a watch #{mid} running.")

    # ── App door handlers (read-only) ─────────────────────────────
    # Relay installed catalog apps + their state. NO enable/disable/config
    # path exists here — those are operator actions in the app catalog.

    _APP_REGISTRY_UNREACHABLE = (
        "I couldn't reach the app registry right now, so I can't tell you "
        "about the installed apps. Please try again shortly."
    )

    async def _handle_list_apps(self, args: dict[str, Any]) -> str:
        """Tool handler: report the installed + ENABLED catalog apps. Reads
        the registry (cached, never raises); a down/unset registry yields a
        graceful message rather than an error."""
        if self.app_registry is None:
            return self._APP_REGISTRY_UNREACHABLE
        apps = await self.app_registry.list_apps()
        if apps is None:
            return self._APP_REGISTRY_UNREACHABLE
        enabled = [a for a in apps if a.get("enabled")]
        if not enabled:
            if apps:
                return (
                    "There are catalog apps installed, but none are currently "
                    "enabled. An operator can enable one from the app catalog."
                )
            return "No catalog apps are installed on this system yet."
        lines: list[str] = []
        for a in enabled:
            manifest = a.get("manifest") or {}
            summary = manifest.get("summary") or "(no summary)"
            category = a.get("category") or manifest.get("category") or "app"
            emits = [
                str(e.get("name"))
                for e in (manifest.get("emits") or [])
                if isinstance(e, dict) and e.get("name")
            ]
            emits_str = f"; alerts: {', '.join(emits)}" if emits else ""
            lines.append(
                f"- {a.get('name') or a.get('id')} ({category}): {summary}"
                f"{emits_str}"
            )
        return "Installed and enabled apps:\n" + "\n".join(lines)

    async def _handle_app_status(self, args: dict[str, Any]) -> str:
        """Tool handler: report one installed app's live state/health,
        proxied from the registry. Read-only; degrades gracefully."""
        if self.app_registry is None:
            return self._APP_REGISTRY_UNREACHABLE
        app_id = str(args.get("app_id") or "").strip()
        if not app_id:
            return "Tell me which app — I need its id (from list_apps)."
        status = await self.app_registry.app_status(app_id)
        if status is None:
            return self._APP_REGISTRY_UNREACHABLE
        health = status.get("health") or {}
        state = status.get("state")
        health_status = (
            health.get("status")
            or ("ready" if health.get("ready") else "not ready")
        )
        parts = [f"App '{app_id}' health: {health_status}."]
        if isinstance(state, dict) and state:
            summary = ", ".join(f"{k}={v}" for k, v in list(state.items())[:6])
            parts.append(f"State: {summary}.")
        elif state is None:
            parts.append("No live state was reported.")
        return " ".join(parts)

    async def _handle_recent_app_alerts(self, args: dict[str, Any]) -> str:
        """Tool handler (app-door ALERT RELAY, read side): report recent
        alerts fired by installed catalog apps, off the bus. Read-only —
        the agent RELAYS what an app reported; it never arms/silences it.
        Degrades to a graceful message when the alert bus isn't wired."""
        raw_window = args.get("window_seconds")
        if raw_window in (None, ""):
            window = 3600.0
        else:
            try:
                window = float(raw_window)
            except (TypeError, ValueError):
                return "ERROR: window_seconds must be a number."
        if not math.isfinite(window) or window <= 0:
            # json.loads accepts NaN/Infinity; Infinity would blow up the
            # int(window / 60) below (OverflowError), NaN would match nothing.
            return "ERROR: window_seconds must be a positive finite number."
        window = min(window, 7 * 86400.0)  # the ring holds hours, not weeks

        app_arg = args.get("app_id")
        app_id: str | None
        if app_arg in (None, "", "__any__"):
            app_id = None
        else:
            app_id = str(app_arg).strip() or None

        alerts = self.context.recent_app_alerts(
            app_id=app_id, window_seconds=window
        )
        if not alerts:
            scope = f"'{app_id}'" if app_id else "any app"
            mins = int(window / 60) or 1
            if not self.cfg.nats_inference_url:
                return (
                    "No app alerts — the alert bus isn't configured, so I'm "
                    "not receiving alerts from installed apps."
                )
            return f"No alerts from {scope} in the last {mins} minute(s)."
        import time as _time

        now = _time.time()
        lines = [
            f"{int(now - a.received_at)}s ago — [{a.severity}] app:{a.app_id} "
            f"on {a.camera_id}: {a.summary}"
            for a in alerts[:6]
        ]
        return "Recent app alerts:\n" + "\n".join(lines)

    def _relay_app_alert(self, alert: Any) -> None:
        """Notification bridge (app-door ALERT RELAY): a subscribed app
        fired an alert. Push it into the SAME notification feed the demo
        polls + the Notifier webhook fan-out, so 'the PPE app flagged a
        violation' surfaces proactively — app alerts are just another
        source, labelled ``app:<id>``. Read/relay only: this reports the
        alert; it never acts on the app. Called synchronously from the
        subscriber's handler; never blocks (list append + fire-and-forget
        webhook task).

        Throttled per (app, camera) with the same cooldown the legacy
        monitor notify path uses: this runs once per bus message, so
        without it a misbehaving app alerting in a loop would spam the
        feed and fire one webhook POST per message. The full alert
        stream stays queryable via recent_app_alerts — only the PUSH
        side is rate-limited."""
        import time as _t

        now = _t.time()
        key = (str(alert.app_id), str(alert.camera_id))
        if now - self._app_relay_last.get(key, 0.0) < self.monitors._cooldown:
            logger.debug(
                "app alert throttled (cooldown): app:%s on %s",
                alert.app_id, alert.camera_id,
            )
            return
        self._app_relay_last[key] = now
        self.monitors._notifications.append({
            "id": self.monitors._next_note_id,
            "source": f"app:{alert.app_id}",
            "text": f"{alert.title} — {alert.summary}",
            "ts": now,
        })
        self.monitors._next_note_id += 1
        logger.info(
            "app alert relayed: app:%s on %s — %s",
            alert.app_id, alert.camera_id, alert.title,
        )
        self.notifier.fire({
            "type": "notify", "title": f"App alert: {alert.title}",
            "text": alert.summary, "camera": alert.camera_id,
            "severity": alert.severity, "source": f"app:{alert.app_id}",
            "ts": now,
        })

    async def _handle_create_task(self, args: dict[str, Any]) -> str:
        """Tool handler: queue a long-running job and acknowledge so the
        conversation continues while it runs in the background."""
        query = str(args.get("query") or args.get("description") or "").strip()
        if not query:
            return "I need a bit more detail about what to look for."
        task = self.tasks.create(query)
        return (
            f"That one needs a closer look, so I've started it as background "
            f"task #{task.id}. I'll tell you as soon as I have the answer — "
            f"ask me anything else meanwhile."
        )

    # ── Lifecycle ─────────────────────────────────────────────────

    def list_local_devices(self, *, all_devices: bool = False) -> list[dict[str, str]]:
        """Discover camera(s) attached to THIS machine without registering them.
        Powers the demo's 'use this machine's camera' button."""
        return [
            {"camera_id": cid, "frame_url": url}
            for cid, url in discover_local_cameras(all_devices=all_devices)
        ]

    def use_local_cameras(self, *, all_devices: bool = False) -> list[CameraSpec]:
        """Discover local capture device(s) and register them at runtime so the
        agent starts using the machine's own camera with zero provisioning.
        Idempotent: cameras already registered are skipped. Returns the specs
        that were newly added."""
        added: list[CameraSpec] = []
        for cid, url in discover_local_cameras(all_devices=all_devices):
            if self.context.known_camera(cid):
                continue
            spec = CameraSpec(camera_id=cid, frame_url=url, role="local onboard camera")
            self.context.add_camera(spec)
            self.context.register_frame_source(
                cid, build_frame_source(camera_id=cid, url=url)
            )
            self.cfg.cameras.append(spec)
            added.append(spec)
        if added:
            logger.info(
                "registered %d local camera(s) at runtime: %s",
                len(added), ", ".join(f"{c.camera_id}({c.frame_url})" for c in added),
            )
        return added

    def _register_opennvr_cameras(self, specs: list[CameraSpec]) -> list[CameraSpec]:
        """Register OpenNVR-sourced camera specs that aren't known yet, wiring
        each one's frame source. ADD-ONLY on purpose: a transient empty or
        failed fetch must never tear down cameras that are already working."""
        added: list[CameraSpec] = []
        for spec in specs:
            if self.context.known_camera(spec.camera_id):
                continue
            self.context.add_camera(spec)
            self.context.register_frame_source(
                spec.camera_id,
                build_frame_source(camera_id=spec.camera_id, url=spec.frame_url),
            )
            self.cfg.cameras.append(spec)
            added.append(spec)
        return added

    def interactive_busy(self) -> bool:
        """True while a voice/chat turn is being served. Background detection
        loops check this and skip their (expensive) frame-fetch + inference for
        that cycle, freeing the shared local models for the user's turn."""
        return self._interactive_turns > 0

    @contextlib.contextmanager
    def interactive_turn(self):
        """Mark an interactive turn in flight for its duration."""
        self._interactive_turns += 1
        try:
            yield
        finally:
            self._interactive_turns = max(0, self._interactive_turns - 1)

    async def _reconcile_opennvr_cameras(self) -> None:
        """Keep the agent's camera list in sync with OpenNVR without a restart.

        The startup fetch is one-shot, so if the agent comes up before the
        OpenNVR core has its cameras ready (the usual boot-order race when the
        stack starts together), it would otherwise stay 'disconnected' until
        someone restarted it. This loop retries through that race and also
        picks up cameras registered in OpenNVR later."""
        url = self.cfg.opennvr_cameras_url
        if not url:
            return
        api_key = self.cfg.opennvr_api_key or self.cfg.kaic_api_key or ""
        announced_waiting = False
        while not self._stop_event.is_set():
            try:
                # The fetch is a blocking httpx call — off-thread it so we
                # never stall the event loop.
                specs = await asyncio.to_thread(
                    _load_opennvr_cameras, url=url, api_key=api_key
                )
            except Exception as exc:  # a reconcile error must never kill the loop
                logger.warning(
                    "camera-agent: OpenNVR camera reconcile failed: %s", exc
                )
                specs = []
            added = self._register_opennvr_cameras(specs)
            have_cameras = bool(self.cfg.cameras)
            if added:
                logger.info(
                    "camera-agent: picked up %d OpenNVR camera(s) without a "
                    "restart: %s",
                    len(added), ", ".join(c.camera_id for c in added),
                )
                announced_waiting = False
            elif not have_cameras and not announced_waiting:
                logger.info(
                    "camera-agent: no OpenNVR cameras yet (core may still be "
                    "starting); retrying every %.0fs",
                    _OPENNVR_CAMERA_RECONCILE_FAST,
                )
                announced_waiting = True
            delay = (
                _OPENNVR_CAMERA_RECONCILE_SLOW
                if have_cameras
                else _OPENNVR_CAMERA_RECONCILE_FAST
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def startup(self) -> None:
        if self.cfg.nats_inference_url:
            self._subscriber_task = asyncio.create_task(
                run_event_subscriber(
                    context=self.context,
                    nats_url=self.cfg.nats_inference_url,
                    nats_token=self.cfg.nats_inference_token,
                    stop_event=self._stop_event,
                ),
                name="camera-agent-nats-subscriber",
            )
            logger.info(
                "camera-agent: NATS subscriber started on %s",
                self.cfg.nats_inference_url,
            )
            # App-door ALERT RELAY: subscribe opennvr.alerts.app.> on the SAME
            # bus so app-emitted alerts reach the user both conversationally
            # (recent_app_alerts tool) and proactively (the notification feed,
            # via the _relay_app_alert bridge). Read/relay only — the agent
            # reports app alerts, it never acts on the app.
            self._app_alert_subscriber_task = asyncio.create_task(
                run_app_alert_subscriber(
                    context=self.context,
                    nats_url=self.cfg.nats_inference_url,
                    nats_token=self.cfg.nats_inference_token,
                    stop_event=self._stop_event,
                    on_alert=self._relay_app_alert,
                ),
                name="camera-agent-app-alert-subscriber",
            )
            logger.info(
                "camera-agent: app-alert subscriber started on %s "
                "(opennvr.alerts.app.>)",
                self.cfg.nats_inference_url,
            )
        else:
            logger.info(
                "camera-agent: NATS not configured; recent_events and "
                "recent_app_alerts tools will always report 'no events'"
            )

        # Pre-warm the LLM in the background so the FIRST real question
        # doesn't pay the ~80s cold-load (Ollama loads the model into RAM
        # + prefills on first inference). We fire a throwaway one-token
        # chat; with OLLAMA_KEEP_ALIVE=-1 the model then stays resident.
        # Best-effort: failures here must never block startup.
        self._warmup_task = asyncio.create_task(
            self._prewarm_llm(), name="camera-agent-llm-prewarm"
        )

        # Pre-warm the vision detector so its model is loaded (and its
        # weights downloaded) BEFORE the first real question. This is the
        # fix for "camera offline at startup": the ai-adapter reports
        # healthy before its model weights finish downloading, so the
        # first detect would otherwise fail. We send a tiny synthetic
        # frame and retry with backoff until the adapter answers.
        # Best-effort: never blocks startup.
        self._vision_warmup_task = asyncio.create_task(
            self._prewarm_vision(), name="camera-agent-vision-prewarm"
        )

        # Re-arm anything persisted from a previous run, then start the
        # recurring report scheduler.
        self.load_state()
        self.reports.start()

        # Keep cameras in sync with OpenNVR in the background so a boot-order
        # race (agent up before the core has cameras) self-heals without a
        # restart, and cameras added later appear on their own.
        if self.cfg.opennvr_cameras_url:
            self._camera_reconcile_task = asyncio.create_task(
                self._reconcile_opennvr_cameras(),
                name="camera-agent-opennvr-camera-reconcile",
            )

    async def _prewarm_llm(self) -> None:
        try:
            logger.info(
                "camera-agent: pre-warming LLM (loading model + caching the "
                "system+tools prompt prefix)…"
            )
            # Mirror the real turn's prompt shape (system prompt + tool
            # definitions) so Ollama caches that prefix's KV. Subsequent real
            # turns share the identical system+tools prefix, so even the FIRST
            # question skips the expensive cold prefill — not just the model
            # weight load. (KEEP_ALIVE=-1 keeps the cache resident.)
            await self.ollama.chat(
                messages=[
                    {"role": "system", "content": self.build_system_prompt()},
                    {"role": "user", "content": "hello"},
                ],
                tools=self.tool_definitions,
                max_tokens=1,
            )
            logger.info("camera-agent: LLM warm — first question will be fast.")
        except Exception as exc:
            logger.warning(
                "camera-agent: LLM pre-warm failed (%s); the first question "
                "will pay the cold-start cost instead.",
                exc,
            )

    # A small valid white JPEG (32x32) — just enough to make the detector
    # load its model; YOLOv8 returns no detections on it.
    _WARMUP_JPEG = base64.b64decode(
        "/9j/4AAQSkZJRgABAgAAAQABAAD//gARTGF2YzU4LjEzNC4xMDAA/9sAQwAIBAQEBAQFBQUF"
        "BQUGBgYGBgYGBgYGBgYGBwcHCAgIBwcHBgYHBwgICAgJCQkICAgICQkKCgoMDAsLDg4OEREU"
        "/8QASwABAQAAAAAAAAAAAAAAAAAAAAcBAQAAAAAAAAAAAAAAAAAAAAAQAQAAAAAAAAAAAAAA"
        "AAAAAAARAQAAAAAAAAAAAAAAAAAAAAD/wAARCAAgACADASIAAhEAAxEA/9oADAMBAAIRAxEA"
        "PwC/gAAAAAAA/9k="
    )

    async def _prewarm_vision(self, *, attempts: int = 10, backoff_s: float = 6.0) -> None:
        """Force the detection adapter to load its model before the first
        real question. Retries over a window so it bridges the background
        weight download (the ai-adapter is 'healthy' before weights land)."""
        for attempt in range(1, attempts + 1):
            try:
                await self.detection_client.infer(frame_jpeg=self._WARMUP_JPEG)
                logger.info("camera-agent: vision detector warm (attempt %d).", attempt)
                return
            except Exception as exc:
                logger.info(
                    "camera-agent: vision pre-warm attempt %d/%d not ready (%s)",
                    attempt, attempts, exc,
                )
                await asyncio.sleep(backoff_s)
        logger.warning(
            "camera-agent: vision detector did not warm up after %d attempts; "
            "the first camera question may report the camera offline until the "
            "adapter finishes loading its model.",
            attempts,
        )

    async def shutdown(self) -> None:
        self._stop_event.set()
        self.monitors.stop_all()
        self.alarms.stop_all()
        self.reports.stop_all()
        for task, label in (
            (self._subscriber_task, "subscriber"),
            (self._app_alert_subscriber_task, "app-alert subscriber"),
            (self._camera_reconcile_task, "camera reconcile"),
        ):
            if task is None:
                continue
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
            except Exception:
                logger.exception("%s shutdown raised", label)
        # Close all reusable HTTP clients so pytest / uvicorn don't
        # log warnings about unclosed AsyncClient instances on exit.
        closers = [
            self.whisper.aclose(), self.ollama.aclose(), self.piper.aclose(),
            self.caption_client.aclose(), self.detection_client.aclose(),
            self.recognition_client.aclose(), self.kaic_capabilities.aclose(),
        ]
        if self.faces:
            closers.append(self.faces.aclose())
        if self.auth:
            closers.append(self.auth.aclose())
        if self.recordings:
            closers.append(self.recordings.aclose())
        if self.app_registry:
            closers.append(self.app_registry.aclose())
        await asyncio.gather(*closers, return_exceptions=True)

    # ── System prompt construction ────────────────────────────────

    def unavailable_capabilities_hint(self) -> str:
        """A compact, dynamic system-prompt note about capabilities that are
        NOT available this turn, so the model can proactively (and honestly)
        tell the user "I can't do X — no adapter/app provides it" and point at
        the install path, WITHOUT ever claiming to perform it.

        Built from ``skills_payload()`` — the single source of truth for
        per-skill availability. A skill is listed here only when it's genuinely
        known-unavailable: either its backend requirement is unmet (``available``
        false) or KAI-C affirmatively reports no adapter advertising its backing
        task (``tasks_available`` false). This degrades cleanly: when KAI-C is
        unreachable ``tasks_available`` is True for every skill (see
        ``skills_payload``), so we never over-claim that a working tool is
        unavailable — an unknown capability just stays off this list.

        Guidance ONLY — this adds no tool and grants no enable/install action.
        Empty string when every capability is available (no tokens spent)."""
        # A prompt hint must never be able to break turn construction — if
        # skills_payload() ever raises, drop the hint rather than the prompt.
        try:
            payload = self.skills_payload()
        except Exception:
            logger.exception("unavailable_capabilities_hint: skills_payload failed")
            return ""
        lines: list[str] = []
        for s in payload:
            # Skip installed catalog-app entries — those are always available
            # (an operator enabled them) and carry no adapter/app suggestion.
            if s.get("source") == "app":
                continue
            unavailable = (not s.get("available")) or (not s.get("tasks_available"))
            if not unavailable:
                continue
            suggestion = ""
            adapters = s.get("suggested_adapters") or []
            apps = s.get("suggested_apps") or []
            if adapters:
                suggestion = (
                    f"suggest installing the {', '.join(adapters)} adapter"
                    f" via AI Adapters"
                )
            elif apps:
                suggestion = (
                    f"suggest installing the {', '.join(apps)} app"
                    f" via the App Catalog"
                )
            name = str(s.get("name") or s.get("id"))
            lines.append(
                f"- {name}: unavailable"
                + (f" — {suggestion}" if suggestion else "")
            )
        if not lines:
            return ""
        return (
            "UNAVAILABLE CAPABILITIES (no adapter/app currently provides these). "
            "If the user asks for one of these, say plainly that it isn't "
            "available right now, briefly "
            + ("suggest installing the named adapter/app in OpenNVR's AI "
               "Adapters / App Catalog")
            + ", and do NOT claim to perform it or make up a result:\n"
            + "\n".join(lines)
        )

    def build_system_prompt(self) -> str:
        """Compose the system prompt the LLM sees: the agent's identity + the
        operator's base prompt + a per-camera roster + task guidance."""
        roster = "\n".join(
            f"- {cam.camera_id}: {cam.role}" for cam in self.cfg.cameras
        )
        prompt = (
            f"You are the OpenNVR Agent, this system's camera agent. Speak in "
            f"the FIRST person — say 'I see…', 'I'm watching…', not in the "
            f"third person. If asked your name, say you're the OpenNVR Agent.\n\n"
            f"{self.cfg.system_prompt.strip()}\n\n"
            f"Cameras available to you:\n{roster}\n\n"
            f"Always pass one of the camera_id values exactly as listed "
            f"when calling a tool.\n\n"
            f"Your replies are SPOKEN ALOUD. Refer to each camera by its "
            f"location (e.g. 'the front door'), never its raw id like "
            f"'front_door'. Answer in 1-2 short, natural sentences a person "
            f"would say out loud — no ids, colons, lists, or markdown.\n"
            f"Give the ANSWER directly. Never reply with filler like 'I'll "
            f"check', 'let me see', or 'one moment' — if you need to look, call "
            f"the tool and then state what you found.\n\n"
            f"You can look at ONE camera, SEVERAL, or 'all' of them — pass "
            f"camera_id='all' (or a camera_ids list) when the user asks about "
            f"every camera or more than one.\n\n"
            f"For STANDING requests — 'notify me when you see…', 'let me know "
            f"if…', 'keep counting…', 'count people entering…', 'watch cam X "
            f"for…' — call create_monitor (kind 'notify', 'count', or "
            f"'crossing' for line entry counts) instead of a one-off detection, "
            f"and confirm what you'll watch. Use stop_monitor to cancel one.\n\n"
            f"For URGENT, ringing requests — 'sound an alarm if…', 'fire alarm', "
            f"'alert me loudly if…', 'alarm if a person is seen after 6pm' — call "
            f"create_alarm (not create_monitor). Extract any time window into "
            f"'after'/'before' as 24h 'HH:MM' (e.g. 6pm → after '18:00'). An "
            f"alarm rings until acknowledged; use stop_alarm to silence or "
            f"disarm it.\n\n"
            f"For RECURRING summaries — 'every morning summarize overnight "
            f"activity', 'daily 7am rundown', 'every hour tell me the count' — "
            f"call create_report (set 'at' HH:MM for a daily time, or "
            f"'every_minutes'). Use stop_report to cancel one.\n\n"
            f"For questions about the RECORDED PAST (e.g. 'earlier today', "
            f"'two days ago at 3am', 'last week') searching footage can take "
            f"a while. For those, call create_background_task with a short "
            f"description instead of answering immediately, tell the user you'll "
            f"look into it and get back to them, and keep the conversation going. "
            f"You'll deliver the result when the task finishes."
        )
        # Proactive honesty about capabilities that aren't wired this turn:
        # a compact, dynamic list so the model can say "I can't do X — install
        # the adapter/app" instead of hallucinating a result. Empty (and thus a
        # no-op) when everything is available, or when KAI-C is unreachable and
        # availability is unknown (we never over-claim unavailability).
        unavailable_hint = self.unavailable_capabilities_hint()
        if unavailable_hint:
            prompt += f"\n\n{unavailable_hint}"
        # Disable Qwen3-style "thinking" for snappy tool-calling when the
        # operator opted out. Only appended when llm_think is explicitly False,
        # so non-thinking models (qwen2.5, llama3.2, …) are unaffected.
        if self.cfg.llm_think is False:
            prompt += "\n\n/no_think"
        return prompt


# ── Pipecat pipeline factory ───────────────────────────────────────


def build_pipeline_task(runtime: CameraAgentRuntime, transport: Any) -> Any:
    """Construct one Pipecat pipeline per WebSocket conversation.
    Imported here (not at module top) so the camera-agent module
    stays importable in test environments without Pipecat."""
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.aggregators.openai_llm_context import (
        OpenAILLMContext,
    )
    # Context-aware aggregators (not the message-list variants).
    # The plain LLMUserResponseAggregator / LLMAssistantResponseAggregator
    # accept a ``List[dict]`` and call .append() on it; passing an
    # OpenAILLMContext to those crashes with AttributeError on the
    # first turn. The *Context* variants below take ``context=...``
    # and route .add_message() correctly, which also mirrors the
    # final assistant turn back into the context for observers.
    from pipecat.processors.aggregators.llm_response import (
        LLMUserContextAggregator,
        LLMAssistantContextAggregator,
    )

    from services import (
        OpenNvrOllamaLLM,
        OpenNvrPiperTTS,
        OpenNvrWhisperSTT,
    )

    stt = OpenNvrWhisperSTT(client=runtime.whisper)
    llm = OpenNvrOllamaLLM(
        client=runtime.ollama,
        tools=runtime.tool_definitions,
        tool_handlers=runtime.tool_handlers,
        temperature=runtime.cfg.llm_temperature,
        max_tokens=runtime.cfg.llm_max_tokens,
    )
    tts = OpenNvrPiperTTS(client=runtime.piper)

    context = OpenAILLMContext(messages=[
        {"role": "system", "content": runtime.build_system_prompt()},
    ])

    user_agg = LLMUserContextAggregator(context=context)
    assistant_agg = LLMAssistantContextAggregator(context=context)

    pipeline = Pipeline([
        transport.input(),
        stt,
        user_agg,
        llm,
        tts,
        transport.output(),
        assistant_agg,
    ])

    return PipelineTask(
        pipeline,
        params=PipelineParams(
            # Interruptions are DISABLED for v0.1: the bundled demo client
            # doesn't send proper cancel frames, so any speech/noise while
            # the agent is thinking would otherwise cancel the in-flight
            # reply before it reaches TTS. With this off, the agent always
            # finishes its answer, then listens again. (See README "No real
            # interrupts".)
            allow_interruptions=False,
            enable_metrics=True,
        ),
    )


# ── FastAPI app + WebSocket entry point ────────────────────────────


def build_app(runtime: CameraAgentRuntime) -> FastAPI:
    app = FastAPI(title="OpenNVR camera-agent", version="1.0.0")

    @app.on_event("startup")
    async def _startup() -> None:
        await runtime.startup()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await runtime.shutdown()

    # ── Auth gate (auth_mode="opennvr") ────────────────────────────
    # One HTTP middleware instead of per-route dependencies: a single
    # place to read, and no signature churn across ~40 endpoints. Tier
    # ranks: viewer < operator < admin. The PAGE SHELL stays open (it
    # renders the login overlay); every data/action endpoint is gated.
    _TIER_RANK = {"viewer": 0, "operator": 1, "admin": 2}
    _OPEN_PATHS = {"/health", "/auth/login", "/auth/refresh", "/agent"}

    def _user_tier(user: dict[str, Any]) -> str:
        if user.get("is_superuser"):
            return "admin"
        role = str(user.get("role_name") or "").lower()
        if role == "admin":
            return "admin"
        if role == "operator":
            return "operator"
        return "viewer"

    def _required_tier(method: str, path: str) -> str:
        if method == "POST" and (path.startswith("/skills/")
                                 or path == "/notify/test"):
            return "admin"          # governance surface
        if method == "PUT" and path == "/alarm-defaults":
            return "admin"          # site policy, same shelf as skills
        if method in ("POST", "DELETE") and (
            path.startswith(("/monitors", "/alarms", "/tasks", "/reports"))
            or path in ("/devices/use", "/reset")
        ):
            return "operator"       # arm/create/ack/remove verbs
        return "viewer"             # look + chat (/ask, /converse, /say, GETs)

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        if runtime.cfg.auth_mode != "opennvr":
            return await call_next(request)
        path = request.url.path
        if path in _OPEN_PATHS or path == "/demo" or path.startswith("/demo/"):
            return await call_next(request)
        authz = request.headers.get("authorization", "")
        token = authz[7:].strip() if authz.lower().startswith("bearer ") else ""
        user = await runtime.auth.me(token) if (token and runtime.auth) else None
        if user is None:
            return JSONResponse({"error": "authentication required"},
                                status_code=401)
        tier = _user_tier(user)
        need = _required_tier(request.method, path)
        if _TIER_RANK[tier] < _TIER_RANK[need]:
            return JSONResponse(
                {"error": f"requires the {need} role (you are {tier})"},
                status_code=403)
        request.state.agent_user = user
        request.state.agent_tier = tier
        return await call_next(request)

    # ── Interactive priority ───────────────────────────────────────
    # Bracket a discrete voice/chat turn so background detection loops
    # (alarms/monitors) yield the shared local models (STT/LLM/TTS/vision
    # all run on one box) for its duration — the user's turn stays snappy.
    # Streaming /ws is intentionally excluded: it's a long-lived session,
    # not a turn, so pausing background work for its whole lifetime would
    # be wrong.
    _INTERACTIVE_PATHS = {"/converse", "/ask", "/say"}

    @app.middleware("http")
    async def _interactive_priority(request: Request, call_next):
        if request.url.path in _INTERACTIVE_PATHS:
            with runtime.interactive_turn():
                return await call_next(request)
        return await call_next(request)

    @app.post("/auth/login")
    async def _auth_login(request: Request) -> JSONResponse:
        """Thin proxy to OpenNVR's /api/v1/auth/login-json — ONE origin
        for the page and future mobile app; status + body pass through
        verbatim so MFA / setup-required semantics stay the server's."""
        if runtime.cfg.auth_mode != "opennvr" or runtime.auth is None:
            return JSONResponse({"error": "auth is not enabled on this agent"},
                                status_code=404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        username = str((body or {}).get("username") or "")
        password = str((body or {}).get("password") or "")
        if not username or not password:
            return JSONResponse({"error": "username and password are required"},
                                status_code=400)
        status, data = await runtime.auth.login(
            username, password, totp_code=(body or {}).get("totp_code"))
        return JSONResponse(data, status_code=status)

    @app.post("/auth/refresh")
    async def _auth_refresh(request: Request) -> JSONResponse:
        if runtime.cfg.auth_mode != "opennvr" or runtime.auth is None:
            return JSONResponse({"error": "auth is not enabled on this agent"},
                                status_code=404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        rt_token = str((body or {}).get("refresh_token") or "")
        if not rt_token:
            return JSONResponse({"error": "refresh_token is required"},
                                status_code=400)
        status, data = await runtime.auth.refresh(rt_token)
        return JSONResponse(data, status_code=status)

    @app.get("/health")
    async def _health() -> dict[str, Any]:
        return {
            "status": "ok",
            "cameras": [cam.camera_id for cam in runtime.cfg.cameras],
            # The tools actually ADVERTISED to the LLM (honours enabled_tools),
            # not every registered handler (test-report #4).
            "tools": [t["function"]["name"] for t in runtime.tool_definitions],
            "llm_model": runtime.cfg.llm_model,
        }

    @app.get("/demo", response_class=HTMLResponse)
    async def _demo() -> HTMLResponse:
        return HTMLResponse(_load_demo_html())

    @app.get("/frame/{camera_id}")
    async def _frame(camera_id: str, at: float | None = None) -> Response:
        """JPEG for one camera. No ``at`` → the CURRENT frame (rides the
        context's frame cache, TTL ~2s, so a page of thumbnails coalesces
        with tool calls instead of hammering the camera). With ``at`` (a
        wall-clock ts from /timeline) → the nearest review-ring frame,
        for the per-camera screen's scrub-back. no-store either way."""
        if at is not None:
            if not runtime.context.known_camera(camera_id):
                return Response(status_code=404)
            jpeg = runtime.context.review_frame_at(camera_id, at)
            if jpeg is None:
                return Response(status_code=404)   # ring empty (fresh boot)
            return Response(content=jpeg, media_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})
        try:
            # allow_pinned=False: this endpoint claims LIVE — an operator
            # pin mid-turn must not freeze the thumbnails or the player.
            jpeg = await runtime.context.get_frame(camera_id, allow_pinned=False)
        except LookupError:
            return Response(status_code=404)
        except Exception:
            # FrameSourceError et al — camera off / unreachable. 503 lets
            # the UI show a quiet placeholder instead of a broken image.
            return Response(status_code=503)
        return Response(content=jpeg, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/timeline/{camera_id}")
    async def _timeline(camera_id: str) -> Any:
        """The per-camera screen's scrub-back data: the review ring's
        frame timestamps (the honest "last few minutes" — real NVR
        playback lives in the main OpenNVR UI) + recent detection
        events for the timeline's markers."""
        if not runtime.context.known_camera(camera_id):
            return JSONResponse({"error": "unknown camera"}, status_code=404)
        events = runtime.context.recent_events(
            camera_id=camera_id, window_seconds=15 * 60)
        return {
            "camera_id": camera_id,
            "frames": runtime.context.review_timestamps(camera_id),
            "events": [{"ts": e.received_at, "summary": e.summary,
                        "adapter": e.adapter} for e in events[:40]],
        }

    def _bearer_of(request: Request) -> str:
        authz = request.headers.get("authorization", "")
        return authz[7:].strip() if authz.lower().startswith("bearer ") else ""

    async def _recordings_path_for(request: Request, camera_id: str):
        """Shared resolution for the two recordings proxies. Returns
        (error_response | None, path). The agent stores NO video — these
        endpoints forward to the main server with the CALLER's token."""
        cam = runtime.context.get_camera(camera_id)
        if cam is None:
            return JSONResponse({"error": "unknown camera"}, status_code=404), None
        if runtime.recordings is None:
            return JSONResponse(
                {"error": "recordings need opennvr_api_url"}, status_code=404), None
        if cam.opennvr_camera_id is None:
            return JSONResponse(
                {"error": f"camera {camera_id!r} is not linked to an OpenNVR "
                          "camera — set opennvr_camera_id in its config entry",
                 "unlinked": True}, status_code=404), None
        token = _bearer_of(request)
        if not token:
            return JSONResponse({"error": "authentication required"},
                                status_code=401), None
        status, path = await runtime.recordings.resolve_path(
            token, cam.opennvr_camera_id)
        if status != 200:
            return JSONResponse({"error": "could not reach the OpenNVR server"},
                                status_code=status), None
        if not path:
            return JSONResponse(
                {"error": "no recordings for this camera yet", "recordings": []},
                status_code=200), None
        return None, (token, path)

    @app.get("/alarm-defaults")
    async def _alarm_defaults() -> dict[str, Any]:
        """The merged target→level map + which entries are UI overrides
        (the editor edits ONLY the overrides; config/built-ins show as
        inherited)."""
        return {"defaults": runtime.ring_defaults(),
                "overrides": dict(runtime._ring_overrides)}

    @app.put("/alarm-defaults")
    async def _put_alarm_defaults(request: Request) -> JSONResponse:
        """Replace the UI-edited overrides (ADMIN: this is site policy,
        same governance shelf as skill toggles)."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        overrides = (body or {}).get("overrides")
        if not isinstance(overrides, dict):
            return JSONResponse({"error": "overrides must be a mapping of "
                                          "target -> siren|pulse|chime|silent"},
                                status_code=400)
        merged = runtime.set_ring_overrides(overrides)
        return JSONResponse({"defaults": merged,
                             "overrides": dict(runtime._ring_overrides)})

    @app.get("/events")
    async def _events() -> dict[str, Any]:
        """The merged what-happened feed (alarms + watches + app alerts).
        Viewer tier: events are LOOK; acting on them stays operator+."""
        return {"events": runtime.events_feed()}

    @app.get("/recordings/{camera_id}")
    async def _recordings_list(camera_id: str, request: Request) -> Any:
        """Recorded segments for one agent camera — the camera screen's
        Recorded row. User-token pass-through to the server's playback
        list (viewer tier: recordings are LOOK, and the server enforces
        its own per-user camera permissions on top)."""
        err, ok = await _recordings_path_for(request, camera_id)
        if err is not None:
            return err
        token, path = ok
        status, data = await runtime.recordings.playback_list(token, path)
        if status != 200:
            return JSONResponse(data, status_code=status)
        return {"camera_id": camera_id, "path": path,
                "recordings": data.get("recordings") or []}

    @app.get("/recordings/{camera_id}/play")
    async def _recordings_play(camera_id: str, request: Request,
                               start: str, duration: float) -> Any:
        """A direct player URL for one segment (mobile-safe: plain URL,
        no auth headers needed by the video element)."""
        err, ok = await _recordings_path_for(request, camera_id)
        if err is not None:
            return err
        token, path = ok
        status, data = await runtime.recordings.playback_url(
            token, path, start, duration)
        return JSONResponse(data, status_code=status)

    @app.get("/streams/{camera_id}/live")
    async def _stream_live(camera_id: str, request: Request) -> Any:
        """Real live video for the camera screen: proxies the main
        server's stream info with the CALLER's token and returns a
        ready-to-POST WHEP URL (?jwt= appended — MediaMTX accepts the
        scoped token as a query param). The agent's snapshot polling
        stays as the fallback for unlinked cameras — this is what turns
        'laggy stills' into the same WebRTC the OpenNVR Live view plays."""
        cam = runtime.context.get_camera(camera_id)
        if cam is None:
            return JSONResponse({"error": "unknown camera"}, status_code=404)
        if runtime.recordings is None or cam.opennvr_camera_id is None:
            return JSONResponse(
                {"error": "live video needs opennvr_api_url + this camera's "
                          "opennvr_camera_id", "unlinked": True},
                status_code=404)
        token = _bearer_of(request)
        if not token:
            return JSONResponse({"error": "authentication required"},
                                status_code=401)
        status, data = await runtime.recordings.stream_info(
            token, cam.opennvr_camera_id)
        if status != 200:
            return JSONResponse(data, status_code=status)
        # Prefer the low-res substream when the server offers it: the agent's
        # live view decodes a smaller frame, so a WebRTC stream costs far less
        # CPU on a single-box install (where it shares the machine with the
        # STT/LLM/TTS). Falls back to the full-res main stream when absent.
        _urls = data.get("urls") or {}
        whep = (_urls.get("webrtc_sub") or _urls.get("webrtc") or "")
        mmtx_token = data.get("token") or ""
        if not whep:
            return JSONResponse({"error": "server returned no WebRTC URL"},
                                status_code=502)
        if mmtx_token:
            whep = f"{whep}{'&' if '?' in whep else '?'}jwt={mmtx_token}"
        return {"camera_id": camera_id,
                "whep_url": whep,
                "stream_name": data.get("stream_name") or "",
                "expires_in_minutes": data.get("expires_in_minutes")}

    @app.get("/demo/camera/{camera_id}", response_class=HTMLResponse)
    async def _demo_camera(camera_id: str) -> HTMLResponse:
        """Deep link into one camera's screen — same demo page; the page
        JS reads the path and opens that camera. 404 for unknown ids so
        a typo'd link fails loudly instead of showing an empty player."""
        if not runtime.context.known_camera(camera_id):
            return HTMLResponse("<h1>unknown camera</h1>", status_code=404)
        return HTMLResponse(_load_demo_html())

    @app.get("/cameras")
    async def _cameras() -> dict[str, Any]:
        """Camera roster for the demo dropdown (id + role description)."""
        return {
            "cameras": [
                {"camera_id": cam.camera_id, "role": cam.role}
                for cam in runtime.cfg.cameras
            ]
        }

    @app.get("/devices")
    async def _devices() -> dict[str, Any]:
        """Cameras attached to THIS machine (laptop webcam / USB / Pi / drone).
        Listed, not yet in use — POST /devices/use to start using them."""
        return {"devices": runtime.list_local_devices()}

    @app.post("/devices/use")
    async def _devices_use(request: Request) -> dict[str, Any]:
        """Register this machine's camera(s) at runtime — zero provisioning.
        Body: {"all_devices": false}. Returns the cameras now available."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        all_devices = bool((body or {}).get("all_devices", False))
        added = runtime.use_local_cameras(all_devices=all_devices)
        return {
            "added": [{"camera_id": c.camera_id, "frame_url": c.frame_url} for c in added],
            "cameras": [
                {"camera_id": c.camera_id, "role": c.role} for c in runtime.cfg.cameras
            ],
        }

    @app.get("/agent")
    async def _agent() -> dict[str, Any]:
        """Lightweight identity for the UI (name + voice + mode + wake) — no TTS."""
        wake = (runtime.cfg.wake_words[0] if runtime.cfg.wake_words
                else runtime.agent_name)
        return {"name": runtime.agent_name, "voice_gender": runtime.cfg.voice_gender,
                "text_mode": runtime.cfg.text_mode,
                "wake_phrase": f"Hey {wake.title()}",
                "wake_required": runtime.cfg.wake_word_required,
                "avatar_video": runtime.cfg.avatar_video,
                # Deep-link base for the main OpenNVR UI (recordings /
                # playback live there, not in this agent). "" = unset.
                "opennvr_ui_url": runtime.cfg.opennvr_ui_url or "",
                # "none" | "opennvr" — the page shows its login overlay
                # (and every client must send bearer tokens) when "opennvr".
                "auth_mode": runtime.cfg.auth_mode,
                # target→annunciation defaults (site-configurable) — the
                # alarm forms preselect from this map.
                "ring_defaults": runtime.ring_defaults()}

    @app.get("/skills")
    async def _skills() -> dict[str, Any]:
        """The agent's capabilities for the UI's Skills panel. Each entry reports
        what it ``uses`` (model/adapter/app), whether it's ``enabled`` now, and
        whether it's ``available`` to enable (its backend is configured) — with a
        ``hint`` otherwise, so the panel never promises something it can't do.
        Also refreshes (60s TTL, never raises) the live KAI-C capabilities view
        behind each entry's ``backing_tasks`` / ``tasks_available`` fields, and
        the installed-apps list behind the source:"app" entries (read-only)."""
        await runtime.kaic_capabilities.refresh()
        if runtime.app_registry is not None:
            await runtime.app_registry.list_apps()   # 60s TTL; never raises
        return {"skills": runtime.skills_payload()}

    @app.get("/hardware")
    async def _hardware() -> dict[str, Any]:
        """Hardware guidance for the ENABLED skills (the UI's Hardware
        panel): editorial rows from tasks.yml + the live GPU/CPU line
        from KAI-C's declared adapter permissions. Advisory only."""
        await runtime.kaic_capabilities.refresh()   # 60s TTL; never raises
        return runtime.hardware_recommendation()

    @app.post("/skills/restore")
    async def _skills_restore() -> JSONResponse:
        """One-click "Restore defaults" for the Skills panel: clear every
        runtime skill toggle. Fresh-boot state is all-on; backend-gated
        skills stay gated by availability, never by the toggle."""
        restored = runtime.restore_default_skills()
        await runtime.kaic_capabilities.refresh()   # 60s TTL; never raises
        if restored:
            logger.info("skills restored to defaults (%d toggle(s) cleared, "
                        "%d tools advertised)", restored,
                        len(runtime.tool_definitions))
        return JSONResponse({"restored": restored,
                             "skills": runtime.skills_payload()})

    @app.post("/skills/{skill_id}/{action}")
    async def _skill_toggle(skill_id: str, action: str) -> JSONResponse:
        """Turn a skill on/off. This reconfigures the agent's live toolset, so
        the LLM immediately can (or can't) use those tools."""
        if action not in ("enable", "disable"):
            return JSONResponse({"error": "action must be enable or disable"},
                                status_code=400)
        await runtime.kaic_capabilities.refresh()   # 60s TTL; never raises
        ok = runtime.set_skill_enabled(skill_id, action == "enable")
        if not ok:
            # Unknown skill, or its backend isn't configured yet.
            skill = next((s for s in runtime.skills_payload() if s["id"] == skill_id), None)
            if skill is None:
                return JSONResponse({"error": f"unknown skill {skill_id!r}"},
                                    status_code=404)
            return JSONResponse(
                {"error": "skill can't be enabled yet", "hint": skill["hint"]},
                status_code=409)
        # A toggle is durable agent state (persist() writes disabled_skills)
        # — without this, an off-switch silently reverts on restart unless
        # some later alarm/watch action happened to save state.
        runtime.persist()
        logger.info("skill %r %sd — tools reconfigured (%d advertised)",
                    skill_id, action, len(runtime.tool_definitions))
        return JSONResponse({"skills": runtime.skills_payload()})

    @app.get("/demo/avatar/{name}")
    async def _demo_avatar(name: str) -> Response:
        """Serve the bundled talking-avatar clips (idle/speaking/thinking ·
        webm/mp4). Whitelisted names only — no path traversal, no arbitrary file
        read. The clips are placeholders; replace the files to use your own
        avatar (e.g. a HeyGen export). ``thinking`` is optional — the UI falls
        back to the idle clip if it's absent."""
        from fastapi.responses import FileResponse
        allowed = {
            "idle.webm": "video/webm", "idle.mp4": "video/mp4",
            "speaking.webm": "video/webm", "speaking.mp4": "video/mp4",
            "thinking.webm": "video/webm", "thinking.mp4": "video/mp4",
        }
        media = allowed.get(name)
        path = Path(__file__).parent / "demo" / "avatar" / name
        if media is None or not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, media_type=media)

    @app.get("/notify")
    async def _notify_status() -> dict[str, Any]:
        """External-notification status (channel count + recent deliveries)."""
        return runtime.notifier.status()

    @app.post("/notify/test")
    async def _notify_test() -> JSONResponse:
        """Send a test notification to the configured webhooks."""
        if not runtime.notifier.enabled:
            return JSONResponse({"error": "no webhooks configured"}, status_code=400)
        ok = await runtime.notifier.send({
            "type": "test", "title": "Test notification",
            "text": f"This is a test from {runtime.agent_name}.", "severity": "info",
        })
        return JSONResponse({"delivered": ok, "channels": runtime.notifier.status()["channels"]})

    @app.get("/intro")
    async def _intro() -> JSONResponse:
        """The agent's greeting — text always; audio when Piper is reachable."""
        greeting = greeting_for(runtime.agent_name)
        audio_b64 = None
        try:
            audio = await runtime.piper.synthesize(greeting)
            if audio:
                audio_b64 = base64.b64encode(audio).decode("ascii")
        except Exception:
            logger.info("intro: TTS unavailable; returning text only")
        return JSONResponse({"name": runtime.agent_name, "text": greeting, "audio_b64": audio_b64})

    @app.get("/tasks")
    async def _tasks() -> dict[str, Any]:
        """The agent's background tasks (the UI polls this to surface results)."""
        return {"tasks": runtime.tasks.list()}

    @app.post("/tasks")
    async def _create_task(request: Request) -> JSONResponse:
        """Manually queue a background task (UI 'register a task' action)."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        query = str((body or {}).get("query") or "").strip()
        if not query:
            return JSONResponse({"error": "query is required"}, status_code=400)
        task = runtime.tasks.create(query)
        return JSONResponse(task.to_dict(), status_code=202)

    @app.post("/say")
    async def _say(request: Request) -> JSONResponse:
        """Synthesize arbitrary text so the UI can speak things the agent produces
        outside a /converse turn — e.g. announcing a finished background task
        aloud. Text-only fallback when Piper is unreachable."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = str((body or {}).get("text") or "").strip()[:600]
        if not text:
            return JSONResponse({"error": "text is required"}, status_code=400)
        audio_b64 = None
        try:
            audio = await runtime.piper.synthesize(text)
            if audio:
                audio_b64 = base64.b64encode(audio).decode("ascii")
        except Exception:
            logger.info("say: TTS unavailable; returning text-only")
        return JSONResponse({"audio_b64": audio_b64})

    @app.get("/monitors")
    async def _monitors() -> dict[str, Any]:
        """Standing monitors + any new notifications (UI polls this)."""
        return {
            "monitors": runtime.monitors.list(),
            "notifications": runtime.monitors.notifications(),
        }

    @app.post("/monitors")
    async def _create_monitor(request: Request) -> JSONResponse:
        """Manually register a monitor (UI 'add watch' action)."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        msg = await runtime._handle_create_monitor(body or {})
        if msg.startswith("ERROR:") or msg.endswith("?"):
            return JSONResponse({"error": msg}, status_code=400)
        return JSONResponse({"message": msg, "monitors": runtime.monitors.list()}, status_code=202)

    @app.delete("/monitors/{monitor_id}")
    async def _stop_monitor(monitor_id: int) -> JSONResponse:
        ok = runtime.monitors.stop(monitor_id)
        runtime.persist()
        return JSONResponse({"stopped": ok}, status_code=200 if ok else 404)

    @app.get("/alarms")
    async def _alarms() -> dict[str, Any]:
        """Armed alarms + recent trigger events (UI polls; rings while any
        alarm is triggered)."""
        alarms = runtime.alarms.list()
        return {
            "alarms": alarms,
            "events": runtime.alarms.events(),
            # Ring only for ACTIVE, triggered alarms. list() also returns
            # disarmed ones; without the active check a stale triggered flag on a
            # disarmed alarm left the siren banner up while the panel showed
            # "No alarms armed" (observed: banner stuck the whole session).
            "ringing": any(a["triggered"] and a["active"] for a in alarms),
            # which sound: a latched CRITICAL siren always wins over URGENT
            "ringing_kind": ("siren" if any(
                a["triggered"] and a["active"] and a.get("ring") != "pulse"
                for a in alarms) else ("pulse" if any(
                    a["triggered"] and a["active"] for a in alarms) else None)),
        }

    @app.post("/alarms")
    async def _create_alarm(request: Request) -> JSONResponse:
        """Arm an alarm (UI presets + 'add alarm' use this)."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        msg = await runtime._handle_create_alarm(body or {})
        if msg.startswith("ERROR:") or msg.endswith("?"):
            return JSONResponse({"error": msg}, status_code=400)
        return JSONResponse({"message": msg, "alarms": runtime.alarms.list()}, status_code=202)

    @app.post("/alarms/ack")
    async def _ack_alarms(request: Request) -> JSONResponse:
        """Acknowledge/silence a ringing alarm (or all). Keeps it armed."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        aid = body.get("alarm_id")
        n = runtime.alarms.acknowledge(int(aid) if aid is not None else None)
        return JSONResponse({"silenced": n})

    @app.delete("/alarms/{alarm_id}")
    async def _delete_alarm(alarm_id: int) -> JSONResponse:
        ok = runtime.alarms.stop(alarm_id)
        runtime.persist()
        return JSONResponse({"stopped": ok}, status_code=200 if ok else 404)

    @app.get("/reports")
    async def _reports() -> dict[str, Any]:
        """Scheduled reports + recently generated report results."""
        return {"schedules": runtime.reports.list(), "reports": runtime.reports.reports()}

    @app.post("/reports")
    async def _create_report(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        msg = await runtime._handle_create_report(body or {})
        if msg.endswith("?"):
            return JSONResponse({"error": msg}, status_code=400)
        return JSONResponse({"message": msg, "schedules": runtime.reports.list()}, status_code=202)

    @app.post("/reports/{report_id}/run")
    async def _run_report(report_id: int) -> JSONResponse:
        result = await runtime.reports.run_now(report_id)
        if result is None:
            return JSONResponse({"error": "no such report"}, status_code=404)
        return JSONResponse({"result": result})

    @app.delete("/reports/{report_id}")
    async def _delete_report(report_id: int) -> JSONResponse:
        ok = runtime.reports.stop(report_id)
        return JSONResponse({"stopped": ok}, status_code=200 if ok else 404)

    @app.get("/people")
    async def _people() -> JSONResponse:
        """Enrolled watchlist people (empty list when faces aren't configured)."""
        if not runtime.faces:
            return JSONResponse({"configured": False, "people": []})
        try:
            people = await runtime.faces.list_people()
        except Exception:
            return JSONResponse({"configured": True, "people": [], "error": "recognizer unreachable"})
        return JSONResponse({"configured": True, "people": people})

    @app.post("/people")
    async def _enroll(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        msg = await runtime._handle_enroll_face(body or {})
        ok = msg.startswith("Got it")
        return JSONResponse({"message": msg}, status_code=202 if ok else 400)

    @app.delete("/people/{name}")
    async def _forget(name: str) -> JSONResponse:
        msg = await runtime._handle_forget_face({"name": name})
        return JSONResponse({"message": msg})

    # Demo-local conversation memory (single user). Kept tiny and turn-text
    # only; reset via POST /reset. A multi-user UI would key this per session.
    demo_history: list[dict[str, str]] = []
    _MAX_HISTORY_TURNS = 8  # 4 user + 4 assistant

    @app.post("/reset")
    async def _reset() -> dict[str, str]:
        demo_history.clear()
        return {"status": "ok"}

    @app.post("/ask")
    async def _ask(request: Request) -> JSONResponse:
        """Lightweight TEXT turn: {text, camera?} in → {reply, cameras_used} out.

        The fast, low-resource path — no microphone, no Whisper, no Piper. Just
        the LLM tool-calling loop over the live vision tools. This is what the
        'lite' profile uses so the agent is useful in seconds on a modest box
        (issue #82) while the full voice loop stays opt-in."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = str((body or {}).get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "text is required"}, status_code=400)

        configured = {cam.camera_id for cam in runtime.cfg.cameras}
        camera_hint = None
        for part in str((body or {}).get("camera") or "").split(","):
            part = part.strip()
            if part in configured:
                camera_hint = part
                break

        # Optional pinned frame ("ask about THIS frame"): the UI sends the
        # exact JPEG the user clicked, base64. For this ONE turn the vision
        # tools see that frame for the pinned camera instead of fetching
        # live — cleared in the finally, so the next question is live again.
        pinned_jpeg: bytes | None = None
        raw_pin = (body or {}).get("pinned_jpeg_b64")
        if raw_pin:
            if not camera_hint:
                # Silently answering from LIVE frames while the caller
                # believes the pin applied would be a lie — fail loudly.
                return JSONResponse(
                    {"error": "pinned_jpeg_b64 requires a valid 'camera'"},
                    status_code=400)
            try:
                pinned_jpeg = base64.b64decode(str(raw_pin), validate=True)
            except (binascii.Error, ValueError):
                return JSONResponse({"error": "pinned_jpeg_b64 is not valid base64"},
                                    status_code=400)
            if len(pinned_jpeg) > 2_000_000:   # same cap as _frames_for
                return JSONResponse({"error": "pinned frame too large (2 MB cap)"},
                                    status_code=400)

        import time as _t
        t0 = _t.perf_counter()
        runtime.tools.last_cameras_used = []
        # Fresh frame per question (see /converse): each turn sees the current
        # moment, not a frame cached from a question seconds ago.
        runtime.context.invalidate_frame_cache()
        pin_token: int | None = None
        if pinned_jpeg is not None:
            pin_token = runtime.context.pin_frame(camera_hint, pinned_jpeg)
        try:
            reply = await _run_conversation_turn(
                runtime, demo_history, text, preferred_camera=camera_hint,
                tool_definitions=runtime.tools_for_tier(
                    getattr(request.state, "agent_tier", "admin")),
            )
            # Capture "what I saw" while the pin is still visible — a
            # racing thumbnail fetch may have overwritten the cache seed,
            # and the chat must show the frame the answer was ABOUT.
            frames = _frames_for(runtime)
        except Exception:
            logger.exception("ask: turn failed")
            return JSONResponse({"error": "assistant failed"}, status_code=502)
        finally:
            if pin_token is not None:
                # Token-scoped: a concurrent request's cleanup can never
                # wipe this turn's pin, and vice versa.
                runtime.context.clear_pins(pin_token)

        demo_history.append({"role": "user", "content": text})
        demo_history.append({"role": "assistant", "content": reply})
        del demo_history[:-_MAX_HISTORY_TURNS]

        return JSONResponse({
            "reply": reply,
            "cameras_used": list(runtime.tools.last_cameras_used),
            "frames": frames,
            "latency_ms": int((_t.perf_counter() - t0) * 1000),
        })

    @app.post("/converse")
    async def _converse(request: Request) -> JSONResponse:
        """Voice turn: audio blob in → {transcript, reply, audio_b64} out.

        Optional ``?camera=<id>`` query param is the UI-selected camera; it
        becomes the default when the user doesn't name a camera out loud."""
        blob = await request.body()
        if not blob:
            return JSONResponse(
                {"error": "empty audio"}, status_code=400
            )

        # UI-selected camera hint: one id, a comma list, or "all". Use the
        # first concrete configured camera as the grounding default; "all"
        # or empty leaves it to the agent.
        configured = {cam.camera_id for cam in runtime.cfg.cameras}
        raw_hint = request.query_params.get("camera") or ""
        camera_hint = None
        for part in raw_hint.split(","):
            part = part.strip()
            if part in configured:
                camera_hint = part
                break

        import time as _t

        t0 = _t.perf_counter()
        timings: dict[str, int] = {}

        def _mark(key: str, since: float) -> float:
            now = _t.perf_counter()
            timings[key] = int((now - since) * 1000)
            return now

        # 1) Normalise the recording to 16 kHz mono WAV (ffmpeg handles
        #    whatever container MediaRecorder produced).
        try:
            wav = await asyncio.to_thread(_transcode_to_wav16k, blob)
        except Exception as exc:
            logger.warning("converse: transcode failed: %s", exc)
            return JSONResponse({"error": "could not decode audio"}, status_code=400)
        t1 = _mark("transcode", t0)

        # 2) Transcribe.
        try:
            transcript = (await runtime.whisper.transcribe(wav)).strip()
        except Exception:
            logger.exception("converse: STT failed")
            return JSONResponse({"error": "transcription failed"}, status_code=502)
        t2 = _mark("stt", t1)
        logger.info("converse: transcript=%r", transcript)
        if not transcript or (runtime.cfg.stt_noise_filter and looks_like_noise(transcript)):
            # Nothing intelligible, or a noise/silence hallucination ("Thank
            # you.", "you", …) — tell the UI to keep listening rather than
            # sending the LLM a phantom turn triggered by background noise.
            if transcript:
                logger.info("converse: dropped noise hallucination %r", transcript)
            timings["total"] = int((_t.perf_counter() - t0) * 1000)
            logger.info("converse: timings_ms=%s (dropped: noise)", timings)
            return JSONResponse({"transcript": "", "reply": "", "audio_b64": None,
                                 "timings_ms": timings, "noise": bool(transcript)})

        # 3) Wake-word gate (voice only). Unless the UI turned it off, only
        #    respond when the utterance is addressed to the agent by name — so
        #    it ignores the TV, side-chatter, and its own echoed reply (the main
        #    source of spurious/looping answers in a live room).
        wake_q = request.query_params.get("wake")
        require_wake = (
            runtime.cfg.wake_word_required if wake_q is None
            else wake_q.strip().lower() not in ("0", "false", "off", "no")
        )
        question = transcript
        if require_wake:
            invoked, stripped = match_wake(
                transcript, runtime.agent_name,
                runtime.cfg.wake_words, runtime.cfg.wake_fuzzy,
                runtime.cfg.wake_require_prefix)
            if not invoked:
                # Log the near-miss score so an operator can see what STT heard
                # vs the wake word and tune wake_fuzzy / add a wake_words alias.
                score = wake_best_score(transcript, runtime.agent_name,
                                        runtime.cfg.wake_words)
                logger.info("converse: not addressed to %s (heard %r, wake "
                            "score=%.2f, need>=%.2f); ignoring",
                            runtime.agent_name, transcript, score,
                            runtime.cfg.wake_fuzzy)
                timings["total"] = int((_t.perf_counter() - t0) * 1000)
                logger.info("converse: timings_ms=%s (dropped: wake)", timings)
                return JSONResponse({"transcript": transcript, "reply": "",
                                     "audio_b64": None, "timings_ms": timings,
                                     "invoked": False})
            question = stripped

        # 4) Answer. A bare wake word ("Hey <name>") with no question ARMS the
        #    agent, Hey-Siri style: she acknowledges, and the UI then treats the
        #    NEXT utterance as the question without needing the wake word again.
        runtime.tools.last_cameras_used = []
        armed = bool(require_wake and not question)
        if armed:
            reply = "Yes?"
        else:
            # Fresh frame per question: drop any cached frame so each turn sees
            # the current moment, not a frame cached seconds ago (a cause of the
            # same answer repeating when the scene has actually changed).
            runtime.context.invalidate_frame_cache()
            try:
                reply = await _run_conversation_turn(
                    runtime, demo_history, question, preferred_camera=camera_hint,
                    tool_definitions=runtime.tools_for_tier(
                        getattr(request.state, "agent_tier", "admin")),
                )
            except Exception:
                logger.exception("converse: LLM turn failed")
                return JSONResponse({"error": "assistant failed"}, status_code=502)
        t3 = _mark("llm", t2)
        logger.info("converse: reply=%r", reply[:160])

        # Persist this turn's text (bounded). Skip the bare-wake-word ack.
        if question:
            demo_history.append({"role": "user", "content": question})
            demo_history.append({"role": "assistant", "content": reply})
            del demo_history[:-_MAX_HISTORY_TURNS]

        # 4) Synthesise the reply.
        audio_b64 = None
        try:
            audio = await runtime.piper.synthesize(reply)
            if audio:
                audio_b64 = base64.b64encode(audio).decode("ascii")
        except Exception:
            logger.exception("converse: TTS failed")  # text still returned
        _mark("tts", t3)
        timings["total"] = int((_t.perf_counter() - t0) * 1000)
        # Log the per-stage breakdown so a slow turn can be diagnosed straight
        # from the agent logs (grep "converse: timings_ms") instead of only the
        # browser's Network tab: transcode / stt / llm / tts / total, in ms.
        logger.info("converse: timings_ms=%s", timings)

        return JSONResponse({
            "transcript": transcript, "reply": reply, "audio_b64": audio_b64,
            "cameras_used": list(runtime.tools.last_cameras_used),
            "frames": _frames_for(runtime),
            "timings_ms": timings, "invoked": True, "armed": armed,
        })

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket) -> None:
        # The ``websocket: WebSocket`` annotation is REQUIRED — without it
        # FastAPI treats ``websocket`` as a query parameter and rejects the
        # handshake with 403 Forbidden before this handler runs.
        # WebSockets bypass the HTTP auth middleware, so the streaming
        # voice channel checks its own token (?token=<bearer>) — same
        # OpenNVR-issued token, viewer tier suffices (voice = chat).
        if runtime.cfg.auth_mode == "opennvr":
            _tok = websocket.query_params.get("token", "")
            _user = await runtime.auth.me(_tok) if (_tok and runtime.auth) else None
            if _user is None:
                await websocket.close(code=4401)   # 4401 = auth required
                return
        # Lazy-imported so the module loads without Pipecat installed.
        from pipecat.transports.network.fastapi_websocket import (
            FastAPIWebsocketParams,
            FastAPIWebsocketTransport,
        )
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams
        from serializer import RawPcmSerializer

        await websocket.accept()
        # RawPcmSerializer is camera-agent-local — it speaks raw int16
        # PCM on both directions of the WebSocket so the self-contained
        # /demo HTML page can use vanilla JS + AudioContext without
        # bundling the Pipecat JS client. Production deployments can
        # swap to ProtobufFrameSerializer + @pipecat-ai/client-js for
        # richer frame types (transcripts, control frames, etc.).
        transport = FastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_in_sample_rate=16000,
                audio_in_channels=1,
                audio_out_enabled=True,
                audio_out_sample_rate=22050,
                audio_out_channels=1,
                add_wav_header=False,
                vad_enabled=True,
                vad_analyzer=SileroVADAnalyzer(
                    sample_rate=16000,
                    params=VADParams(
                        confidence=0.55,
                        start_secs=0.15,
                        stop_secs=0.7,
                        min_volume=0.08,
                    ),
                ),
                vad_audio_passthrough=True,
                serializer=RawPcmSerializer(),
            ),
        )

        task = build_pipeline_task(runtime, transport)
        from pipecat.pipeline.runner import PipelineRunner
        runner = PipelineRunner(handle_sigint=False)
        try:
            await runner.run(task)
        except Exception:
            logger.exception("websocket conversation crashed")

    return app


# ── Request/response conversation turn (push-to-talk path) ─────────
#
# The /converse endpoint below is the reliable, demo-friendly path: the
# browser records ONE complete utterance with MediaRecorder and POSTs the
# whole blob. We transcode it to clean 16 kHz mono WAV with ffmpeg (so the
# input format never matters), transcribe it, run the tool-calling LLM loop,
# synthesise the reply, and return text + audio in one JSON response. No
# streaming, no custom resampler, no server-side VAD — none of the moving
# parts that made the live WebSocket path hallucinate.


def _transcode_to_wav16k(blob: bytes) -> bytes:
    """Any audio container (WebM/Opus, Ogg, MP4, …) → 16 kHz mono PCM WAV.

    MediaRecorder emits whatever the browser supports (usually
    ``audio/webm;codecs=opus``); ffmpeg normalises it to exactly what
    Whisper wants. Raises on failure so the caller can report it cleanly.
    """
    proc = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-i", "pipe:0",
            "-ar", "16000", "-ac", "1",
            "-f", "wav", "pipe:1",
        ],
        input=blob,
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]
        raise RuntimeError(f"ffmpeg transcode failed: {err or 'no output'}")
    return proc.stdout


async def _invoke_tool(runtime: "CameraAgentRuntime", call: dict[str, Any]) -> tuple[str, str]:
    """Run one tool call; return (name, result_string). Never raises."""
    func = call.get("function") or {}
    name = str(func.get("name") or "").strip()
    args_raw = func.get("arguments")
    try:
        if isinstance(args_raw, str):
            args = json.loads(args_raw) if args_raw.strip() else {}
        elif isinstance(args_raw, dict):
            args = dict(args_raw)
        else:
            args = {}
    except (json.JSONDecodeError, ValueError):
        return name or "<unknown>", f"ERROR: tool '{name}' received malformed arguments."
    handler = runtime.tool_handlers.get(name)
    if handler is None:
        return name, f"ERROR: tool '{name}' is not registered."
    try:
        result = await handler(args)
    except Exception:
        logger.exception("Tool %s raised", name)
        return name, f"ERROR: tool '{name}' failed unexpectedly."
    result = str(result)
    if len(result) > 1200:
        result = result[:1200] + " …(truncated)"
    return name, result


# Words that mean "this is a question about a camera / the scene". If the
# model answers an utterance containing any of these WITHOUT calling a tool,
# we force a grounding detection (see _run_conversation_turn). Positive
# matching (vs a chit-chat blocklist) avoids force-grounding closings like
# "thanks, that's all" while still catching "is anyone there?".
_CAMERA_WORDS: tuple[str, ...] = (
    "see", "look", "watch", "watching", "camera", "cam",
    "anyone", "anybody", "someone", "somebody", "nobody",
    "person", "people", "man", "woman", "kid", "child", "face",
    "door", "porch", "outside", "yard", "driveway", "garage", "street",
    # scene locations
    "gate", "window", "fence", "entrance", "hallway", "room", "kitchen",
    "lot", "lobby", "stairs", "balcony",
    "happening", "detect", "count", "package", "parcel", "delivery",
    "dog", "dogs", "cat", "cats", "animal", "car", "cars", "truck", "trucks",
    "vehicle", "bike", "people", "persons",
    # visual-attribute verbs ("what is he wearing/doing?") — these are why a
    # caption/VQA question still triggers grounding instead of a fabrication
    "wearing", "wear", "dressed", "holding", "carrying", "doing",
    "visible", "present", "moving", "movement", "motion",
)
_CAMERA_RE = re.compile(r"\b(" + "|".join(_CAMERA_WORDS) + r")\b", re.IGNORECASE)


def _looks_like_camera_question(text: str) -> bool:
    """True if the utterance is about a camera / the scene. Used only to
    decide whether to force a grounding detection when the model failed to
    call a tool itself — so a weak model can't fabricate "I see a dog"."""
    return bool(_CAMERA_RE.search(text or ""))


# Presence/count questions about concrete objects ("is anyone there?",
# "how many cars?", "any people?") must be answered by the object DETECTOR
# (yolov8), NOT a scene caption — BLIP describes the scene ("a table with a
# laptop") but can't reliably answer "is there a person?". Open "what's
# there / describe it" questions, by contrast, are best served by the BLIP
# caption. _pick_forced_tool routes the forced-grounding call accordingly.
_DETECTION_WORDS: tuple[str, ...] = (
    "person", "people", "anyone", "anybody", "someone", "somebody",
    "nobody", "man", "woman", "kid", "child", "face", "count", "many",
    "car", "cars", "truck", "vehicle", "bike", "bicycle", "motorcycle",
    "dog", "cat", "animal", "package", "parcel", "delivery",
)
_DETECTION_RE = re.compile(r"\b(" + "|".join(_DETECTION_WORDS) + r")\b", re.IGNORECASE)

# Attribute / activity / appearance questions ("what is he WEARING?", "what's
# he DOING?", "DESCRIBE the scene", "what's HAPPENING?") want a scene
# description (BLIP caption, or a VQA model), NOT the object detector — even
# though they usually also contain an object noun like "man" that would
# otherwise match _DETECTION_RE. These take precedence (test-report S-4/L-3/V-3).
_DESCRIBE_WORDS: tuple[str, ...] = (
    "describe", "description", "detail", "details", "wearing", "wear", "dressed",
    "doing", "holding", "carrying", "looks like", "look like", "looking",
    "appearance", "happening", "going on", "scene", "activity", "colour", "color",
)
_DESCRIBE_RE = re.compile(r"\b(" + "|".join(_DESCRIBE_WORDS) + r")\b", re.IGNORECASE)

# Presence / count phrasing ("how many…", "are there any…", "is there a…",
# "count…") → the detector, even when the object noun is a plural the detection
# vocab doesn't list ("dogs"), or absent entirely ("are there any?").
_COUNT_RE = re.compile(
    r"\b(how many|how much|are there|is there|number of|count|anyone|anybody|"
    r"\bany\b)\b", re.IGNORECASE)


def _pick_forced_tool(text: str) -> str:
    """Choose the forced-grounding tool by question type. Description/attribute/
    activity questions → ``describe_camera`` (BLIP caption / VQA, which falls
    back to the detector if no caption adapter is registered). Object presence/
    count questions → ``detect_objects`` (yolov8). Describe takes precedence so
    'what is the man wearing?' isn't routed to the detector just because it
    contains 'man'."""
    t = text or ""
    if _DESCRIBE_RE.search(t):
        return "describe_camera"
    if _COUNT_RE.search(t) or _DETECTION_RE.search(t):
        return "detect_objects"
    return "describe_camera"


# Questions about the camera ROSTER / system config ("how many cameras are
# configured?", "which cameras do you have?", "list the cameras") are about
# the SYSTEM, not what's visible — the model answers them correctly from its
# prompt context, so forced grounding must NOT override them with an
# (irrelevant) scene detection. Distinguished from scene questions by
# "camera/cam" being the noun being counted/listed, not an object inside a
# camera's view ("how many PEOPLE on the camera" stays a scene question).
_CONFIG_RE = re.compile(
    r"\b(how many|number of|which|what|list(?:\s+\w+){0,3})\s+(cameras?|cams?)\b"
    r"|\b(cameras?|cams?)\s+(are|do you|configured|connected|available|set up|online|exist)\b",
    re.IGNORECASE,
)


def _is_config_question(text: str) -> bool:
    """True for questions about the camera roster/config (how many/which
    cameras exist) rather than what's visible in one. Forced grounding skips
    these so a correct context answer isn't clobbered by a scene detection."""
    return bool(_CONFIG_RE.search(text or ""))


def _pick_camera(text: str, cameras: list[str], preferred: str | None = None) -> str:
    """Best-effort: which camera did the user mean? An explicit name in the
    utterance wins; otherwise fall back to the UI-selected ``preferred``
    camera, then to the first configured camera."""
    t = text.lower().replace("-", " ")
    compact = t.replace(" ", "")
    for cam in cameras:
        if cam.lower() in compact:  # "cam1", "camera1"
            return cam
    words = {"one": "1", "two": "2", "three": "3", "four": "4",
             "first": "1", "second": "2", "third": "3"}
    for word, n in words.items():
        if re.search(rf"\b{word}\b", t) and f"cam{n}" in cameras:
            return f"cam{n}"
    for n in ("1", "2", "3", "4"):
        if re.search(rf"\b{n}\b", t) and f"cam{n}" in cameras:
            return f"cam{n}"
    if preferred and preferred in cameras:
        return preferred
    return cameras[0]


async def _run_conversation_turn(
    runtime: "CameraAgentRuntime",
    history: list[dict[str, str]],
    user_text: str,
    *,
    max_iterations: int = 4,
    preferred_camera: str | None = None,
    tool_definitions: list[dict[str, Any]] | None = None,
) -> str:
    """Run the tool-calling LLM loop for one user utterance and return the
    final spoken reply. ``history`` holds prior user/assistant text turns
    (tool internals are kept turn-local, not persisted).

    Anti-fabrication guard: small CPU models sometimes answer a camera
    question straight from the prompt ("I see a dog") without calling a
    tool. If that happens we FORCE a real detection on the target camera
    and make the model answer from that result — so a reply about a camera
    is always grounded in an actual frame, never imagined.
    """
    # Roster/config questions ("how many cameras are configured?") are answered
    # deterministically from the config — the model can't reliably count them and
    # tends to narrate a phantom tool. Short-circuit BEFORE the LLM loop so these
    # are instant: previously they ran the full tool loop (tens of seconds on a
    # CPU model) only to have the roster answer override the result at the end.
    if _is_config_question(user_text):
        return _roster_answer(runtime.cfg.cameras)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": runtime.build_system_prompt()}
    ]
    tools = tool_definitions if tool_definitions is not None else runtime.tool_definitions
    cameras = [cam.camera_id for cam in runtime.cfg.cameras]
    # UI-selected camera: when the user doesn't name one, bias the model
    # toward the camera the operator picked in the dropdown.
    if preferred_camera and preferred_camera in cameras:
        messages.append({
            "role": "system",
            "content": (
                f"The user is currently viewing camera '{preferred_camera}'. "
                f"If they ask about a camera without naming one, assume they "
                f"mean '{preferred_camera}'."
            ),
        })
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    final = ""
    grounded = False   # did any tool actually run this turn?
    forced = False     # have we already injected a forced detection?
    tools_called = 0   # how many tool calls ran (for the turn-outcome log)
    last_tool_result = ""  # most recent tool output — used as a fallback when
    #                        the model returns empty content on the compose turn
    #                        (small/thinking models sometimes do), so the user
    #                        gets the real detection/caption instead of "Sorry".
    for iteration in range(max_iterations):
        response = await runtime.ollama.chat(
            messages=messages,
            tools=tools,
            temperature=runtime.cfg.llm_temperature,
            max_tokens=runtime.cfg.llm_max_tokens,
        )
        message = response.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        content = (message.get("content") or "").strip()
        logger.info(
            "converse: LLM iter %d content=%r tool_calls=%d",
            iteration, content[:120], len(tool_calls),
        )

        if tool_calls:
            grounded = True
            messages.append({
                "role": "assistant", "content": content, "tool_calls": tool_calls,
            })
            for call in tool_calls:
                name, result = await _invoke_tool(runtime, call)
                logger.info("converse: tool %s -> %s", name, result[:120])
                tools_called += 1
                last_tool_result = result or last_tool_result
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "name": name,
                    "content": result,
                })
            continue

        # No tool call. If the model tried to answer a camera question
        # without looking — judged from EITHER the user's question or the
        # model's own reply mentioning a camera/scene — force a grounding
        # detection and re-ask. Checking the reply too catches cases where
        # STT garbled the camera word (e.g. "what's on hammer 2") but the
        # model still fabricated "camera 2 is ...".
        if (
            not grounded and not forced and cameras
            and not _is_config_question(user_text)
            and (
                _looks_like_camera_question(user_text)
                or _looks_like_camera_question(content)
            )
        ):
            forced = True
            grounded = True
            cam = _pick_camera(user_text, cameras, preferred_camera)
            # Route to the detector for presence/count questions and to the
            # BLIP caption for open "what's there?" questions. Small models
            # often refuse or fabricate instead of calling a tool, so this
            # forced path is what most camera questions actually hit —
            # picking the RIGHT tool here is what makes "is there a person?"
            # get a detector answer ("no people") instead of an off-topic
            # scene caption.
            tool_name = _pick_forced_tool(user_text)
            call = {
                "id": "forced-0", "type": "function",
                "function": {"name": tool_name,
                             "arguments": {"camera_id": cam}},
            }
            name, result = await _invoke_tool(runtime, call)
            logger.info("converse: FORCED grounding (%s) on %s -> %s",
                        tool_name, cam, result[:120])
            tools_called += 1
            last_tool_result = result or last_tool_result
            messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
            messages.append({
                "role": "tool", "tool_call_id": "forced-0",
                "name": name, "content": result,
            })
            continue

        # Accept the reply (genuine chit-chat, or already grounded).
        final = content
        break
    else:
        logger.warning("converse: tool loop exhausted")

    # Prefer the model's composed reply (stripped of <think> reasoning + ids).
    # If it's empty after stripping (a thinking model burned its budget on
    # reasoning) but a tool ran, surface that result. For camera-roster/config
    # questions, answer deterministically — small models often just deflect
    # ("I'll check…") and there's no tool to ground them.
    cleaned = _clean_for_speech(final, runtime.cfg.cameras)
    if _is_config_question(user_text):
        # Roster/config questions ("how many cameras are configured?") are
        # answered deterministically and FIRST — the model can't reliably count
        # the configured cameras and often narrates a tool it never called
        # ("…calling detect_objects to check…"). The roster is authoritative, so
        # don't let that narration through as the answer.
        reply, source = _roster_answer(runtime.cfg.cameras), "roster"
    elif cleaned and not _is_deflection(cleaned):
        reply, source = cleaned, "llm"
    elif last_tool_result:
        reply, source = _humanize_for_speech(last_tool_result, runtime.cfg.cameras), "tool_fallback"
    else:
        reply, source = (cleaned or "Sorry, I'm having trouble answering that right now."), "none"

    # One structured line that explains WHY the reply was what it was — so a bad
    # answer is diagnosable from the logs (camera offline, adapter down, the LLM
    # not calling tools, a deflection, etc.) without re-running.
    degraded = _degradation_reasons(last_tool_result, final, cleaned,
                                    grounded, tools_called)
    log = logger.warning if degraded else logger.info
    log("converse: TURN reply_source=%s grounded=%s forced=%s tools=%d "
        "issues=%s reply=%r", source, grounded, forced, tools_called,
        ",".join(degraded) or "none", reply[:90])
    return reply


# Hollow "acknowledgement" replies a small model emits instead of answering
# ("I see… I'll check the camera.") — treat as no-answer so we ground/fallback.
_DEFLECTION_RE = re.compile(
    r"^(i see\b.*?)?(i'?ll|let me|i will|i am going to|i'?m going to|checking|"
    r"i'?m calling|calling|going to call|let me call|i need to|trying to|"
    r"one moment|hold on|give me a)\b", re.IGNORECASE)


def _is_deflection(text: str) -> bool:
    t = (text or "").strip().lower().lstrip(".… ")
    return bool(_DEFLECTION_RE.match(t)) and len(t) < 80


def _degradation_reasons(last_tool_result: str, final: str, cleaned: str,
                         grounded: bool, tools_called: int) -> list[str]:
    """Classify why a turn might have produced a poor answer, for the TURN log.
    Returns short tags (camera_offline, adapter_unavailable, llm_think_only, …)
    so a bad reply is diagnosable straight from the logs."""
    issues: list[str] = []
    lt = (last_tool_result or "").lower()
    if "appears to be offline" in lt:
        issues.append("camera_offline")
    if "is not configured" in lt:
        issues.append("camera_not_configured")
    if any(s in lt for s in ("unavailable", "isn't enabled",
                             "not yet", "couldn't", "can't be reached")):
        issues.append("adapter_unavailable")
    if (final or "").strip() and not (cleaned or "").strip():
        issues.append("llm_think_only")       # whole token budget went to <think>
    elif not (final or "").strip() and not grounded:
        issues.append("llm_empty")
    elif cleaned and _is_deflection(cleaned):
        issues.append("llm_deflection")
    return issues


def _roster_answer(cameras) -> str:
    """Deterministic answer for 'how many cameras / which cameras' — never
    relies on the model."""
    cams = list(cameras or [])
    if not cams:
        return "No cameras are configured yet."
    names = [(getattr(c, "role", "") or c.camera_id) for c in cams]
    names = [n for n in names if n and n != "(no role configured)"] or \
            [c.camera_id for c in cams]
    if len(cams) == 1:
        return f"There is one camera: {names[0]}."
    return f"There are {len(cams)} cameras: {', '.join(names)}."


def _load_demo_html() -> str:
    """Read the static demo page off disk. Kept as a separate file
    so designers can iterate on the HTML without restarting Python."""
    path = Path(__file__).parent / "demo" / "index.html"
    if not path.is_file():
        return "<h1>demo/index.html missing</h1>"
    return path.read_text(encoding="utf-8")


# ── CLI entry point ────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "OpenNVR camera-agent — voice agent grounded in live cameras."
        )
    )
    parser.add_argument("--config", required=True, help="Path to config.yml")
    parser.add_argument("--log-level", default="INFO", help="Python log level")
    return parser.parse_args(argv)


class _RedactTokensFilter(logging.Filter):
    """Blank credential-bearing query values in log lines.

    MediaMTX accepts its scoped streaming JWT as a ``?jwt=`` query param, so
    any URL that carries one (e.g. a WHEP request logged by uvicorn's access
    logger) would otherwise land in the logs verbatim — a live read token for
    whoever can read the logs. Same for our own bearer-ish params.
    """

    _SENSITIVE = re.compile(
        r"\b(jwt|token|access_token|refresh_token|api_key)=[^&\s\"']+",
        re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "=" in record.msg:
            record.msg = self._SENSITIVE.sub(r"\1=[redacted]", record.msg)
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(
                self._SENSITIVE.sub(r"\1=[redacted]", a)
                if isinstance(a, str) else a
                for a in record.args)
        return True


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    redact = _RedactTokensFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(redact)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging(args.log_level)
    cfg = load_config(args.config)
    runtime = CameraAgentRuntime(cfg)
    app = build_app(runtime)

    import uvicorn
    config = uvicorn.Config(
        app, host=cfg.host, port=cfg.port, log_level=args.log_level.lower()
    )
    server = uvicorn.Server(config)
    # uvicorn installs its own handlers (no propagation to root), so the
    # access log needs the redaction filter attached directly. After
    # uvicorn.Config so its dictConfig can't wipe the filter.
    redact = _RedactTokensFilter()
    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(redact)

    def _sig(signum: int, frame: Any) -> None:
        logger.info("received signal %d; stopping", signum)
        server.should_exit = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
