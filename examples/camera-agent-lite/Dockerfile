# syntax=docker/dockerfile:1.7
# camera-agent-lite example app
#
# Build (from the open-nvr repo root):
#   docker build -f examples/camera-agent-lite/Dockerfile -t opennvr/camera-agent-lite:local-build .
#
# Run (talks to the four AI adapters + OpenNVR core over the network):
#   docker run --rm -p 9101:9101 \
#     -v $(pwd)/config.yml:/app/config.yml:ro \
#     opennvr/camera-agent-lite:local-build --config /app/config.yml
#
# Then open http://localhost:9101/demo in your browser.
#
# This image is the deliberately SMALL half of the lite stack: no Pipecat, no
# torch, no model weights — just the FastAPI app + tool brain. The models run
# in the four adapter containers (llamacpp / whispercpp / pipertts / smolvlm).

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        # ffmpeg lets the agent grab a JPEG frame from an rtsp:// source
        # (RtspFrameSource in frame_sources.py — the MediaMTX tap URLs the
        # OpenNVR roster endpoint returns are rtsp://).
        ffmpeg \
        # onnxruntime (Pipecat's Silero VAD backend) needs libgomp on slim.
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# pipecat-ai[silero] = Silero VAD on onnxruntime (NO torch) — ~70 MB, keeps
# this image in the hundreds of MB. Pin matches pyproject.toml / camera-agent.
RUN pip install --no-cache-dir \
        "fastapi>=0.110,<1.0" \
        "uvicorn>=0.27,<1.0" \
        "httpx>=0.27,<1.0" \
        "pydantic>=2.6,<3.0" \
        "PyYAML>=6.0,<7.0" \
        "numpy>=1.26" \
        "psutil>=5.9" \
        "pipecat-ai[silero,websocket]>=0.0.55,<0.0.60"

COPY examples/camera-agent-lite/__init__.py         ./__init__.py
COPY examples/camera-agent-lite/camera_agent.py     ./camera_agent.py
COPY examples/camera-agent-lite/adapter_clients.py  ./adapter_clients.py
COPY examples/camera-agent-lite/context.py          ./context.py
COPY examples/camera-agent-lite/frame_sources.py    ./frame_sources.py
COPY examples/camera-agent-lite/routing.py          ./routing.py
COPY examples/camera-agent-lite/serializer.py       ./serializer.py
COPY examples/camera-agent-lite/services.py         ./services.py
COPY examples/camera-agent-lite/tools.py            ./tools.py
COPY examples/camera-agent-lite/voice.py            ./voice.py
COPY examples/camera-agent-lite/config.example.yml  ./config.example.yml
COPY examples/camera-agent-lite/demo                ./demo

ENV PYTHONUNBUFFERED=1

EXPOSE 9101

# /health is the open, content-free liveness endpoint (/api/status is auth-
# gated). uvicorn serves exactly one protocol, so probe https first (the
# compose deployment serves TLS) and fall back to plain http (bare runs).
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=60s \
    CMD ["python", "-c", "import httpx, sys\ndef g(u):\n    return httpx.get(u, timeout=2, verify=False).status_code == 200\nok = False\ntry:\n    ok = g('https://127.0.0.1:9101/health')\nexcept Exception:\n    try:\n        ok = g('http://127.0.0.1:9101/health')\n    except Exception:\n        pass\nsys.exit(0 if ok else 1)"]

ENTRYPOINT ["python", "camera_agent.py"]
CMD ["--config", "config.yml"]
