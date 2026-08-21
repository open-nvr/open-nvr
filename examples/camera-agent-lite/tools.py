# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The fixed tool registry: specs, validation, execution, and handlers.

Only registered tools can ever run; unknown names are rejected before any
handler is consulted. Model-generated text is NEVER forwarded to a shell,
SQL, eval, or the filesystem — only to these validators.

`look_at_camera` is the on-demand vision tool: it fetches a frame through
:mod:`context` and runs the VLM adapter, returning a text answer — this is
how the text model "sees" without ever handling pixels itself.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

from context import CameraContext, CameraContextError, CameraState

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

import logging

logger = logging.getLogger(__name__)


# ── Specs ──────────────────────────────────────────────────────────


class Permission(str, Enum):
    READ = "read"          # observe only (status, list, look)
    SYSTEM = "system"      # system info


@dataclass
class ToolResult:
    tool: str
    ok: bool
    data: dict = field(default_factory=dict)
    error: Optional[str] = None

    def to_model_json(self) -> dict:
        if self.ok:
            return {"ok": True, **self.data}
        return {"ok": False, "error": self.error}


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    permission: Permission = Permission.READ
    timeout_s: float = 8.0

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def obj(properties: dict, required: Optional[list[str]] = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


CAMERA_ID = {"type": "string",
             "description": "Camera id or name, e.g. 'camera_2' or 'porch'."}

# The fixed tool catalogue. The agent is strictly conversational: it can look,
# list, and describe — recording is always on 24/7 in OpenNVR and there are no
# control tools.
SPECS: dict[str, ToolSpec] = {
    "look_at_camera": ToolSpec(
        "look_at_camera",
        "Look at a camera's current view and answer a visual question about it "
        "(what is visible, who/what is there, is the room empty, what someone is "
        "holding/wearing, etc.). Use this whenever answering needs actually seeing "
        "the camera. Set temporal=true for motion/direction/enter/exit questions.",
        obj(
            {
                "camera_id": CAMERA_ID,
                "question": {"type": "string", "maxLength": 300,
                             "description": "The user's visual question."},
                "temporal": {"type": "boolean", "default": False,
                             "description": "true to compare several recent frames."},
            },
            ["camera_id", "question"],
        ),
        Permission.READ,
        timeout_s=95.0,
    ),
    "list_cameras": ToolSpec(
        "list_cameras", "List all cameras and their online/offline status.",
        obj({}), Permission.READ,
    ),
    "get_camera_status": ToolSpec(
        "get_camera_status", "Get one camera's status (online/offline, recording).",
        obj({"camera_id": CAMERA_ID}, ["camera_id"]), Permission.READ,
    ),
    "current_time": ToolSpec(
        "current_time", "Get the current local date and time.", obj({}), Permission.SYSTEM,
    ),
    "system_status": ToolSpec(
        "system_status", "Get agent/system health (CPU, RAM).", obj({}),
        Permission.SYSTEM,
    ),
}

Handler = Callable[..., Any]


# ── Validation ─────────────────────────────────────────────────────

_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


class ValidationError(ValueError):
    pass


def _check_one(name: str, value: Any, schema: dict) -> Any:
    jtype = schema.get("type")
    if jtype:
        expected = _TYPE_MAP.get(jtype)
        # bool is a subclass of int; reject bools where int/number expected
        if jtype in ("integer", "number") and isinstance(value, bool):
            raise ValidationError(f"'{name}' must be {jtype}, got boolean")
        if expected and not isinstance(value, expected):
            raise ValidationError(f"'{name}' must be {jtype}, got {type(value).__name__}")

    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"'{name}' must be one of {schema['enum']}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(f"'{name}' must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(f"'{name}' must be <= {schema['maximum']}")

    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        raise ValidationError(f"'{name}' exceeds maxLength {schema['maxLength']}")

    return value


def validate_args(spec: ToolSpec, args: dict | None) -> dict:
    """Return a cleaned args dict (with defaults applied) or raise ValidationError."""
    args = args or {}
    if not isinstance(args, dict):
        raise ValidationError("arguments must be an object")

    schema = spec.parameters
    props: dict = schema.get("properties", {})
    required: list = schema.get("required", [])

    if not schema.get("additionalProperties", True):
        extra = set(args) - set(props)
        if extra:
            raise ValidationError(f"unknown argument(s): {sorted(extra)}")

    for req in required:
        if req not in args:
            raise ValidationError(f"missing required argument '{req}'")

    cleaned: dict = {}
    for key, sub in props.items():
        if key in args and args[key] is not None:
            cleaned[key] = _check_one(key, args[key], sub)
        elif "default" in sub:
            cleaned[key] = sub["default"]
    return cleaned


# ── Registry + executor ────────────────────────────────────────────


@dataclass
class RegisteredTool:
    spec: ToolSpec
    handler: Handler       # sync or async callable(**cleaned_args) -> ToolResult


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: Handler) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool '{spec.name}' already registered")
        self._tools[spec.name] = RegisteredTool(spec=spec, handler=handler)

    def get(self, name: str) -> Optional[RegisteredTool]:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def openai_tools(self) -> list[dict]:
        return [t.spec.openai_schema() for t in self._tools.values()]


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, default_timeout_s: float = 5.0) -> None:
        self.registry = registry
        self.default_timeout_s = default_timeout_s

    async def execute(self, name: str, args: dict | None) -> ToolResult:
        tool = self.registry.get(name)
        if tool is None:
            return ToolResult(tool=name, ok=False, error=f"unknown tool '{name}'")

        try:
            cleaned = validate_args(tool.spec, args)
        except ValidationError as exc:
            return ToolResult(tool=name, ok=False, error=f"invalid arguments: {exc}")

        timeout = tool.spec.timeout_s or self.default_timeout_s
        try:
            result = await asyncio.wait_for(self._call(tool.handler, cleaned), timeout)
        except asyncio.TimeoutError:
            logger.warning("tool '%s' timed out after %.1fs", name, timeout)
            return ToolResult(tool=name, ok=False, error="tool timed out")
        except Exception as exc:  # never surface a stack trace upstream
            logger.exception("tool '%s' failed", name)
            return ToolResult(tool=name, ok=False, error=str(exc))

        if isinstance(result, ToolResult):
            return result
        return ToolResult(tool=name, ok=True, data=result or {})

    @staticmethod
    async def _call(handler, cleaned: dict):
        if inspect.iscoroutinefunction(handler):
            return await handler(**cleaned)
        # run sync handlers off the event loop to avoid blocking
        return await asyncio.to_thread(lambda: handler(**cleaned))


# ── Handlers ───────────────────────────────────────────────────────


@dataclass
class ToolContext:
    context: CameraContext
    vlm: object                    # VlmClient (or a mock in tests)
    multi_frame_count: int = 3
    multi_frame_interval_ms: int = 1000
    default_camera_fn: Optional[Callable[[], Optional[str]]] = None


def _err(name: str, exc: Exception) -> ToolResult:
    return ToolResult(name, False, error=str(exc))


def register_tools(registry: ToolRegistry, ctx: ToolContext) -> None:
    cam_ctx = ctx.context
    vlm = ctx.vlm

    async def _resolve(camera_id: Optional[str]) -> str:
        if camera_id:
            # Normalise names / id variants ('cpplus', 'cam2') to the
            # canonical id; pass through unresolved so the frame path can
            # raise the informative unknown-camera error.
            await cam_ctx.refresh()
            return cam_ctx.resolve_id(camera_id) or camera_id
        if ctx.default_camera_fn:
            d = ctx.default_camera_fn()
            if d:
                return d
        cameras = await cam_ctx.list_cameras()
        online = [c.camera_id for c in cameras if c.state == CameraState.ONLINE]
        if len(online) == 1:
            return online[0]
        raise CameraContextError("please specify which camera", transient=False)

    # ---- vision (the on-demand VLM path) --------------------------------- #
    async def look_at_camera(camera_id: str, question: str, temporal: bool = False) -> ToolResult:
        try:
            camera_id = await _resolve(camera_id)
        except CameraContextError as exc:
            return _err("look_at_camera", exc)

        # A blank/again question yields a vague caption; give the small VLM a
        # concrete default that elicits presence — the "it didn't say someone
        # was there" symptom is worse with an empty prompt.
        question = (question or "").strip() or (
            "Describe what is happening in this camera view, and state plainly "
            "whether a person is present."
        )

        t0 = time.monotonic()
        try:
            if temporal:
                frames = await cam_ctx.get_frames(
                    camera_id, ctx.multi_frame_count, ctx.multi_frame_interval_ms
                )
            else:
                frames = [await cam_ctx.get_frame(camera_id)]
        except CameraContextError as exc:
            # short, spoken-friendly error
            msg = f"{camera_id.replace('_', ' ')} is currently unavailable."
            return ToolResult("look_at_camera", False, error=msg, data={"detail": str(exc)})
        capture_ms = (time.monotonic() - t0) * 1000.0

        if not frames or not frames[0].jpeg:
            return ToolResult("look_at_camera", False, error="I could not access a recent frame.")

        jpegs = [f.jpeg for f in frames]
        result = (
            await vlm.analyse_frames(jpegs, question)
            if len(jpegs) > 1 else
            await vlm.analyse_image(jpegs[0], question)
        )
        if result.error:
            return ToolResult("look_at_camera", False,
                              error="The vision model is temporarily unavailable.",
                              data={"detail": result.error})
        logger.debug("look_at_camera capture=%.0fms vlm=%.0fms", capture_ms, result.inference_ms)
        return ToolResult(
            "look_at_camera", True,
            data={"camera_id": camera_id, "answer": result.text,
                  "frames": len(jpegs),
                  "capture_ms": round(capture_ms, 1),
                  "vlm_ms": round(result.inference_ms, 1)},
        )

    # ---- camera reads ---------------------------------------------------- #
    async def list_cameras() -> ToolResult:
        try:
            cameras = await cam_ctx.list_cameras()
        except CameraContextError as exc:
            return _err("list_cameras", exc)
        return ToolResult("list_cameras", True, data={
            "cameras": [{"camera_id": c.camera_id, "name": c.name,
                         "state": c.state.value, "recording": c.recording}
                        for c in cameras]})

    async def get_camera_status(camera_id: str) -> ToolResult:
        try:
            info = await cam_ctx.get_status(camera_id)
        except CameraContextError as exc:
            return _err("get_camera_status", exc)
        return ToolResult("get_camera_status", True, data={
            "camera_id": info.camera_id, "state": info.state.value,
            "recording": info.recording})

    # ---- system ---------------------------------------------------------- #
    def current_time() -> ToolResult:
        now = datetime.now()
        return ToolResult("current_time", True, data={
            "spoken": now.strftime("%I:%M %p").lstrip("0"),
            "date": now.strftime("%A, %B %d, %Y"),
            "iso": now.isoformat(timespec="seconds")})

    def system_status() -> ToolResult:
        data: dict = {}
        if psutil is not None:
            vm = psutil.virtual_memory()
            data.update({
                "cpu_percent": psutil.cpu_percent(interval=0.0),
                "ram_used_gb": round(vm.used / 1e9, 2),
                "ram_total_gb": round(vm.total / 1e9, 2)})
        return ToolResult("system_status", True, data=data)

    registry.register(SPECS["look_at_camera"], look_at_camera)
    registry.register(SPECS["list_cameras"], list_cameras)
    registry.register(SPECS["get_camera_status"], get_camera_status)
    registry.register(SPECS["current_time"], current_time)
    registry.register(SPECS["system_status"], system_status)
