# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenNVR camera-agent-lite — entry point.

Ask your cameras, on a shoestring: llama.cpp + whisper.cpp + Piper + SmolVLM
adapter containers instead of Ollama, one small FastAPI app instead of a
streaming pipeline. Run it, open http://localhost:9101/demo, type or tap the
mic and talk.

    python camera_agent.py --config config.yml

Configuration is a single YAML file (copy config.example.yml). There is no
.env, no login token, no password: camera discovery and frames come from
OpenNVR's internal camera-agent endpoint using the stack's INTERNAL_API_KEY,
same as the camera-agent example.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
import sys
from contextlib import asynccontextmanager

import numpy as np
import uvicorn
import yaml
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from adapter_clients import SttClient, TtsClient
from services import AgentBrain

logger = logging.getLogger("camera_agent_lite")

HERE = pathlib.Path(__file__).resolve().parent


# ── Configuration ──────────────────────────────────────────────────


class Config(BaseModel):
    # Server
    agent_name: str = "Camera Agent Lite"
    listen_host: str = "127.0.0.1"
    listen_port: int = 9101

    # The four AI adapters (llamacpp / whispercpp / pipertts / smolvlm).
    # adapter_token = the OPENNVR_ADAPTER_TOKEN the adapters were started
    # with (the compose overlay passes INTERNAL_API_KEY on both sides).
    adapter_token: str = ""
    llm_url: str = "http://127.0.0.1:9014"
    stt_url: str = "http://127.0.0.1:9013"
    tts_url: str = "http://127.0.0.1:9012"
    vlm_url: str = "http://127.0.0.1:9016"
    adapter_timeout_seconds: float = 120.0

    # LLM sampling (short spoken answers).
    llm_temperature: float = 0.3
    llm_max_tokens: int = 200
    stt_language: str = "en"

    # Cameras: auto-discover from OpenNVR (internal API key), or a static list
    # of {camera_id, name, frame_url, role} entries (file/http/rtsp URLs).
    opennvr_cameras_url: str = ""
    opennvr_api_key: str = ""
    cameras: list[dict] = Field(default_factory=list)
    frame_cache_ttl_seconds: float = 2.0
    multi_frame_count: int = 3
    multi_frame_interval_ms: int = 1000

    # Routing / tools
    routing_min_confidence: float = 0.6
    tool_timeout_seconds: float = 5.0

    log_level: str = "INFO"


def load_config(path: str | pathlib.Path) -> Config:
    p = pathlib.Path(path)
    if not p.is_file():
        raise SystemExit(
            f"config file not found: {p}\n"
            "Copy config.example.yml to config.yml and edit it first."
        )
    with open(p, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Config.model_validate(data)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    for noisy in ("httpx", "httpcore", "asyncio", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ── Web app ────────────────────────────────────────────────────────

_cfg: Config | None = None
_brain: AgentBrain | None = None
_stt: SttClient | None = None
_tts: TtsClient | None = None
_lock = asyncio.Lock()
_init_task: asyncio.Task | None = None


async def _initialize(cfg: Config) -> None:
    """Warm-up runs in the background so the page loads immediately and shows
    a 'starting…' state instead of refusing to connect."""
    global _brain, _stt, _tts
    brain = AgentBrain(cfg)
    _stt = SttClient(cfg.stt_url, cfg.adapter_token, language=cfg.stt_language,
                     timeout_s=cfg.adapter_timeout_seconds)
    _tts = TtsClient(cfg.tts_url, cfg.adapter_token,
                     timeout_s=cfg.adapter_timeout_seconds)
    await brain.setup()
    _brain = brain  # set LAST so /api/status flips ready only when fully warm
    logger.info("agent fully initialized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _init_task
    _init_task = asyncio.create_task(_initialize(_cfg))  # don't block serving
    yield
    if _init_task and not _init_task.done():
        _init_task.cancel()
    if _brain:
        await _brain.close()
    for client in (_stt, _tts):
        if client:
            await client.aclose()


app = FastAPI(lifespan=lifespan, title="OpenNVR Camera Agent Lite")


@app.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    return RedirectResponse("/demo")


@app.get("/demo", response_class=HTMLResponse)
async def demo() -> str:
    return (HERE / "demo" / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
async def status() -> dict:
    if _brain is None:
        return {"ready": False}
    info = await _brain.status()
    voice_ok = _stt is not None and _tts is not None
    if voice_ok:
        voice_ok = await _stt.health() and await _tts.health()
    return {"ready": True, "agent_name": _cfg.agent_name, "voice": voice_ok, **info}


@app.post("/ask")
async def ask(req: Request) -> JSONResponse:
    data = await req.json()
    question = (data.get("question") or "").strip()
    if not question:
        return JSONResponse({"answer": "Ask me something about your cameras."})
    if _brain is None:
        return JSONResponse({"answer": "Agent still starting, try again in a moment."})
    async with _lock:
        try:
            answer = await _brain.ask(question)
        except Exception as exc:
            logger.exception("ask failed")
            answer = f"Something went wrong ({type(exc).__name__})."
    return JSONResponse({"answer": answer})


@app.post("/voice")
async def voice(req: Request) -> JSONResponse:
    """Body = raw 16-bit little-endian PCM mono @ 16 kHz (from the browser)."""
    if _brain is None or _stt is None:
        return JSONResponse({"transcript": "", "answer": "Still starting.", "audio": None})
    body = await req.body()
    if not body:
        return JSONResponse({"transcript": "", "answer": "", "audio": None})
    pcm = np.frombuffer(body, dtype=np.int16).astype(np.float32) / 32768.0
    dur = len(pcm) / 16000.0
    rms = float(np.sqrt(np.mean(pcm ** 2))) if len(pcm) else 0.0
    # Energy gate: whisper hallucinates words ("Thank you.", "you") on silence
    # or ambient noise, so drop clips that are too short or too quiet.
    if dur < 0.4 or rms < 0.006:
        logger.info("voice: ignored (dur=%.2fs rms=%.4f)", dur, rms)
        msg = "I didn't catch that — try again."
        return JSONResponse({"transcript": "", "answer": msg, "audio": await _speak(msg)})
    try:
        res = await _stt.transcribe(pcm, 16000)
        transcript = res.text
    except Exception as exc:
        logger.warning("voice STT failed: %s", exc)
        return JSONResponse({"transcript": "",
                             "answer": f"Speech recognition failed ({type(exc).__name__}).",
                             "audio": None})
    logger.info("voice: dur=%.2fs rms=%.4f -> transcript=%r", dur, rms, transcript)
    if not transcript:
        msg = "I didn't catch that."
        return JSONResponse({"transcript": "", "answer": msg, "audio": await _speak(msg)})
    async with _lock:
        try:
            answer = await _brain.ask(transcript)  # logs Q/A centrally
        except Exception as exc:
            logger.exception("voice ask failed")
            answer = f"Something went wrong ({type(exc).__name__})."
    return JSONResponse({"transcript": transcript, "answer": answer,
                         "audio": await _speak(answer)})


async def _speak(text: str) -> str | None:
    if _tts is None:
        return None
    return await _tts.synthesize_b64(text)


@app.websocket("/ws")
async def ws_voice(websocket: WebSocket) -> None:
    """Streaming voice: raw 16 kHz int16 PCM in, TTS PCM (22050) out.

    A full Pipecat pipeline per connection — Silero VAD turn detection,
    whispercpp STT, the router/LLM tool loop, sentence-streamed pipertts —
    same architecture (and same raw-PCM wire protocol) as camera-agent's
    /ws. The demo page uses this and falls back to POST /voice when the
    socket can't be established.
    """
    # The ``websocket: WebSocket`` annotation is REQUIRED — without it
    # FastAPI treats it as a query parameter and rejects the handshake.
    if _brain is None or _stt is None or _tts is None:
        await websocket.close(code=1013)   # 1013 = try again later
        return
    # Lazy imports so the module stays importable without Pipecat installed.
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.transports.network.fastapi_websocket import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    from serializer import RawPcmSerializer
    from voice import build_pipeline_task

    await websocket.accept()
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
    task = build_pipeline_task(_brain, _cfg, transport,
                               stt_client=_stt, tts_client=_tts)
    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    except Exception:
        logger.exception("websocket voice conversation crashed")


# ── Main ───────────────────────────────────────────────────────────


def main() -> None:
    global _cfg
    parser = argparse.ArgumentParser(description="OpenNVR camera-agent-lite")
    parser.add_argument("--config", default="config.yml",
                        help="path to the YAML config (default: config.yml)")
    args = parser.parse_args()
    _cfg = load_config(args.config)
    setup_logging(_cfg.log_level)
    logger.info("starting %s on http://%s:%d/demo",
                _cfg.agent_name, _cfg.listen_host, _cfg.listen_port)
    uvicorn.run(app, host=_cfg.listen_host, port=_cfg.listen_port, log_level="warning")


if __name__ == "__main__":
    main()
