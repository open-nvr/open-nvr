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
import logging
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

from adapter_clients import (
    KaicAdapterClient,
    OllamaClient,
    PiperClient,
    WhisperClient,
)
from context import CameraContext, CameraSpec, run_event_subscriber
from frame_sources import build_frame_source
from tools import CameraTools, build_tool_definitions

logger = logging.getLogger("camera-agent")


# ── Config ──────────────────────────────────────────────────────────


@dataclass
class AppConfig:
    """Operator-tunable settings. Validated in ``load_config``."""

    # KAI-C (for the vision tool calls — auditable).
    kaic_url: str
    kaic_api_key: str
    detection_adapter: str = "yolov8"
    recognition_adapter: str = "insightface"
    caption_adapter: str = "blip"

    # Direct adapter URLs (streaming voice path — bypasses KAI-C in v0.1).
    whisper_url: str = "http://127.0.0.1:9003"
    whisper_token: str = ""
    ollama_url: str = "http://127.0.0.1:9004"
    ollama_token: str = ""
    piper_url: str = "http://127.0.0.1:9001"
    piper_token: str = ""

    # LLM tuning.
    llm_model: str = "llama3.2:3b"
    llm_temperature: float = 0.4
    llm_max_tokens: int = 256

    # Caching / event ring.
    frame_cache_ttl_seconds: float = 2.0
    event_ring_size: int = 256

    # Optional NATS for the recent_events tool.
    nats_inference_url: str | None = None
    nats_inference_token: str | None = None

    # HTTP listen address.
    host: str = "127.0.0.1"
    port: int = 9100

    # System prompt + cameras.
    system_prompt: str = ""
    cameras: list[CameraSpec] = None  # type: ignore[assignment]


_DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful, concise voice assistant for a home with security "
    "cameras. The user asks questions about what's happening on those "
    "cameras. Always use your tools to ground answers in live camera data "
    "— don't guess. Respond in 1-2 short sentences because your reply is "
    "spoken aloud."
)


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise SystemExit(f"config file {path} did not parse to a dict")

    for required in ("kaic_url", "kaic_api_key"):
        if not raw.get(required):
            raise SystemExit(f"config: {required} is required")

    cameras_raw = raw.get("cameras") or []
    cameras: list[CameraSpec] = []
    for entry in cameras_raw:
        if not isinstance(entry, dict):
            raise SystemExit("config: each camera must be a mapping")
        cam_id = entry.get("camera_id")
        url = entry.get("frame_url")
        if not cam_id or not url:
            raise SystemExit("config: camera entries need camera_id + frame_url")
        cameras.append(CameraSpec(
            camera_id=str(cam_id),
            frame_url=str(url),
            role=str(entry.get("role") or "(no role configured)"),
        ))
    if not cameras:
        raise SystemExit("config: at least one camera is required")

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

    return AppConfig(
        kaic_url=str(raw["kaic_url"]),
        kaic_api_key=str(raw["kaic_api_key"]),
        detection_adapter=_str("detection_adapter", "yolov8"),
        recognition_adapter=_str("recognition_adapter", "insightface"),
        caption_adapter=_str("caption_adapter", "blip"),
        whisper_url=_str("whisper_url", "http://127.0.0.1:9003"),
        whisper_token=_str("whisper_token", ""),
        ollama_url=_str("ollama_url", "http://127.0.0.1:9004"),
        ollama_token=_str("ollama_token", ""),
        piper_url=_str("piper_url", "http://127.0.0.1:9001"),
        piper_token=_str("piper_token", ""),
        llm_model=_str("llm_model", "llama3.2:3b"),
        llm_temperature=_float("llm_temperature", 0.4),
        llm_max_tokens=_int("llm_max_tokens", 256),
        frame_cache_ttl_seconds=_float("frame_cache_ttl_seconds", 2.0),
        event_ring_size=_int("event_ring_size", 256),
        nats_inference_url=raw.get("nats_inference_url"),
        nats_inference_token=raw.get("nats_inference_token"),
        host=_str("host", "127.0.0.1"),
        port=_int("port", 9100),
        system_prompt=str(raw.get("system_prompt") or _DEFAULT_SYSTEM_PROMPT),
        cameras=cameras,
    )


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
        self.ollama = OllamaClient(
            url=cfg.ollama_url, token=cfg.ollama_token, model=cfg.llm_model,
        )
        self.piper = PiperClient(url=cfg.piper_url, token=cfg.piper_token)

        self.caption_client = KaicAdapterClient(
            kaic_url=cfg.kaic_url,
            api_key=cfg.kaic_api_key,
            adapter_name=cfg.caption_adapter,
        )
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

        self.tools = CameraTools(
            context=self.context,
            caption_client=self.caption_client,
            detection_client=self.detection_client,
            recognition_client=self.recognition_client,
        )
        self.tool_definitions = build_tool_definitions(
            [cam.camera_id for cam in cfg.cameras]
        )
        self.tool_handlers = {
            "describe_camera": self.tools.describe_camera,
            "detect_objects": self.tools.detect_objects,
            "recognize_faces": self.tools.recognize_faces,
            "recent_events": self.tools.recent_events,
        }

        self._stop_event = asyncio.Event()
        self._subscriber_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────

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
        else:
            logger.info(
                "camera-agent: NATS not configured; recent_events tool "
                "will always report 'no events'"
            )

    async def shutdown(self) -> None:
        self._stop_event.set()
        if self._subscriber_task is not None:
            try:
                await asyncio.wait_for(self._subscriber_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._subscriber_task.cancel()
            except Exception:
                logger.exception("subscriber shutdown raised")
        # Close all reusable HTTP clients so pytest / uvicorn don't
        # log warnings about unclosed AsyncClient instances on exit.
        await asyncio.gather(
            self.whisper.aclose(),
            self.ollama.aclose(),
            self.piper.aclose(),
            self.caption_client.aclose(),
            self.detection_client.aclose(),
            self.recognition_client.aclose(),
            return_exceptions=True,
        )

    # ── System prompt construction ────────────────────────────────

    def build_system_prompt(self) -> str:
        """Compose the system prompt the LLM sees: operator's base
        prompt + a per-camera roster so the model can pick the right
        ``camera_id`` from natural-language references."""
        roster = "\n".join(
            f"- {cam.camera_id}: {cam.role}" for cam in self.cfg.cameras
        )
        return (
            f"{self.cfg.system_prompt.strip()}\n\n"
            f"Cameras available to you:\n{roster}\n\n"
            f"Always pass one of the camera_id values exactly as listed "
            f"when calling a tool."
        )


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
            allow_interruptions=True,
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

    @app.get("/health")
    async def _health() -> dict[str, Any]:
        return {
            "status": "ok",
            "cameras": [cam.camera_id for cam in runtime.cfg.cameras],
            "tools": list(runtime.tool_handlers.keys()),
            "llm_model": runtime.cfg.llm_model,
        }

    @app.get("/demo", response_class=HTMLResponse)
    async def _demo() -> HTMLResponse:
        return HTMLResponse(_load_demo_html())

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket) -> None:  # FastAPI injects the WebSocket by type
        # Lazy-imported so the module loads without Pipecat installed.
        from pipecat.transports.network.fastapi_websocket import (
            FastAPIWebsocketParams,
            FastAPIWebsocketTransport,
        )
        from pipecat.audio.vad.silero import SileroVADAnalyzer
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
                audio_out_enabled=True,
                add_wav_header=False,
                vad_enabled=True,
                vad_analyzer=SileroVADAnalyzer(),
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


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )


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

    def _sig(signum: int, frame: Any) -> None:
        logger.info("received signal %d; stopping", signum)
        server.should_exit = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
