# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""The model-facing half of the read tools: names, descriptions, schemas.

One definition, two hosts. The camera agent and core's workflow engine
(RFC #620) each implement these tools against what they own — the agent
over its adapters, core over the event store and KAI-C — but what the
MODEL sees has to be the same text in both, or a description fixed for
one silently stays broken in the other. So it lives here, and each host
has a parity test against it.

Moved verbatim from the camera agent (``examples/camera-agent/tools.py``),
where small models were measured against it (MODELS_AND_LATENCY.md);
wording changes are measured with the eval harness before they land.

Only READ tools are here. Nothing that moves things — gates, locks,
sirens, site mode — is ever a tool an AI step can call.
"""
from __future__ import annotations

from typing import Any

#: The ten read tools, in the order they are advertised. A host's parity
#: test checks it serves exactly these names with these schemas.
READ_TOOL_NAMES: tuple[str, ...] = (
    "describe_camera", "camera_snapshot", "detect_objects", "recognize_faces",
    "search_footage", "search_history", "describe_event", "describe_window",
    "recent_events", "recent_plates",
)


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
                                "Minutes back to search. Omit to search the "
                                "last week first, widening to all history "
                                "automatically if nothing matched."
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
                            "description": (
                                "For person searches: face-match the kept photos. Set true when "
                                "the question asks WHO came or names someone ('was Priya here', "
                                "'who came to the door'); leave it out otherwise — a recognition "
                                "call per photo is the wrong price for 'did anyone come'. Left "
                                "out, the agent decides from the question itself."),
                        },
                        "plate": {
                            "type": "string",
                            "description": "Vehicle searches: find visits whose read plate contains this text, e.g. 'KA01' or '1234'.",
                        },
                        "attr": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "What a skill SAID about the visit, as bare "
                                "words: ['blue'], ['blue','van'], ['hi-vis']. "
                                "Repeatable and ANDed. Use this for colour and "
                                "vehicle type — 'did you see a BLUE car' is "
                                "label=car with attr=['blue'], not label=car "
                                "alone.\n"
                                "ALSO A PERSON'S NAME. Where face "
                                "recognition is installed, a recognised "
                                "visit carries the name as a claim, so "
                                "'was Varun here yesterday' is "
                                "label=person with attr=['varun singh'] "
                                "and a time window — NOT a question this "
                                "tool cannot answer. Call list_people "
                                "first if unsure of the spelling; a name "
                                "nobody enrolled matches nothing."),
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
