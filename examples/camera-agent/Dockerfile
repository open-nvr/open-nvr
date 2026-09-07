# syntax=docker/dockerfile:1.7
# camera-agent example app
#
# Build (from the open-nvr repo root):
#   docker build -f examples/camera-agent/Dockerfile -t opennvr/camera-agent:1.0.0 .
#
# Run (talks to KAI-C + all adapters on the host network):
#   docker run --rm \
#     --network host \
#     -v $(pwd)/config.yml:/app/config.yml:ro \
#     opennvr/camera-agent:1.0.0 \
#     --config /app/config.yml
#
# Then open http://localhost:9100/demo in your browser.

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        # Pipecat's silero extra runs Silero VAD on onnxruntime (no
        # torch); onnxruntime needs libgomp on the slim base.
        libgomp1 \
        # ffmpeg lets the agent grab a JPEG frame from an rtsp:// camera
        # (RtspFrameSource in frame_sources.py).
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pin Pipecat carefully — its API has shifted between minor versions
# (the 1.x line moved turn-taking into the user aggregator and the
# serializer/transport modules; camera_agent.py, services.py and
# serializer.py are written against 1.8). Keep this in lock-step with
# pyproject.toml. If you bump these, re-run the example's test suite
# (tests/test_pipeline_e2e.py drives the real pipeline) and the demo page
# locally before committing.
# Resilient install for slow / flaky PyPI links (pipecat's silero extra pulls
# torch — a large download). Three layers of resilience:
#   * --mount=type=cache keeps the pip wheel cache across build attempts, so a
#     re-run after a network stall only re-fetches what's still missing (this is
#     why --no-cache-dir is intentionally dropped).
#   * a retry loop re-runs the whole install up to 5× so a transient read
#     timeout self-recovers instead of failing the build.
#   * --timeout 300 tolerates slow reads before giving up.
RUN --mount=type=cache,target=/root/.cache/pip \
    for i in 1 2 3 4 5; do \
        echo "pip install attempt $i/5..."; \
        pip install --timeout 300 --retries 10 \
            "pipecat-ai[silero,websocket]>=1.8.1,<2.0" \
            "httpx>=0.27,<1.0" \
            "fastapi>=0.110,<1.0" \
            "uvicorn>=0.27,<1.0" \
            "PyYAML>=6.0,<7.0" \
            "nats-py>=2.6" \
            "Pillow>=10.0" \
            "opencv-python-headless>=4.8" \
        && break; \
        if [ "$i" = 5 ]; then echo "pip install failed after 5 attempts" >&2; exit 1; fi; \
        echo "attempt $i failed; retrying in 15s..."; sleep 15; \
    done

# Pipecat's TTS sentence aggregation uses NLTK's punkt_tab tokenizer and
# downloads it on first use — which a sovereign, offline site cannot do,
# and which would otherwise delay the first spoken reply. Bundle it at
# build time; NLTK_DATA points the runtime at it. Smart Turn v3 and the
# Silero VAD ship inside the pipecat wheel already (no download).
ENV NLTK_DATA=/opt/nltk_data
RUN python -m nltk.downloader -d /opt/nltk_data punkt_tab

# CPU fit. The only heavy math in THIS container is Smart Turn v3's
# end-of-turn verdict (once per pause: an 8 s log-mel in numpy + a small
# ONNX model) and Silero VAD (~0.7% of a core). Left uncapped, numpy's
# BLAS/OpenMP fans the log-mel across every core, which measured SLOWER
# on a 4-core box (90–130 ms vs ~60 ms single-threaded) and competes with
# Whisper/Piper/Ollama next door. camera_agent.py sets the same defaults
# when these are absent; here so `docker run -e` overrides are explicit.
# onnxruntime's own pool is sized by `turn_cpu_threads` in config.yml
# (auto: 1 thread, 2 from 8 cores) — see MODELS_AND_LATENCY.md.
ENV OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1

# The OpenNVR App SDK — monitor_host.py (the create_monitor convergence)
# imports opennvr_app_sdk, and the count/crossing rule libraries below are
# built on it. Installed from the in-repo package like the detector examples.
COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

COPY examples/camera-agent/__init__.py        ./__init__.py
COPY examples/camera-agent/adapter_clients.py ./adapter_clients.py
COPY examples/camera-agent/camera_agent.py    ./camera_agent.py
COPY examples/camera-agent/context.py         ./context.py
COPY examples/camera-agent/frame_sources.py   ./frame_sources.py
COPY examples/camera-agent/serializer.py      ./serializer.py
COPY examples/camera-agent/services.py        ./services.py
COPY examples/camera-agent/tools.py           ./tools.py
COPY examples/camera-agent/footage_index.py   ./footage_index.py
COPY examples/camera-agent/monitor_host.py    ./monitor_host.py
COPY examples/camera-agent/system_check.py    ./system_check.py
# The count/crossing conversational monitors instantiate the SAME SDK rule
# classes the standalone catalog apps use — one rule library, two front doors
# (app-sdk-spec §07). monitor_host loads them by file path from OPENNVR_RULES_DIR
# (set below). The rule modules import only opennvr_app_sdk + stdlib, so just
# the module files are needed — not their standalone alerts/zone shims.
COPY examples/occupancy-counting/occupancy_counting.py ./rules/occupancy-counting/occupancy_counting.py
COPY examples/line-crossing/line_crossing.py           ./rules/line-crossing/line_crossing.py
# Bundled copy of the canonical task registry (server/config/tasks.yml).
# The agent derives its skill→backing-tasks map from this at runtime, so a
# new task with an agent_skill becomes a skill backing with no code change.
COPY examples/camera-agent/tasks.yml          ./tasks.yml
COPY examples/camera-agent/config.example.yml ./config.example.yml
COPY examples/camera-agent/demo               ./demo

# Point monitor_host at the bundled rule libraries (the /app layout flattens
# the agent modules, so the dev-tree parent.parent default doesn't apply).
ENV OPENNVR_RULES_DIR=/app/rules
ENV PYTHONUNBUFFERED=1

EXPOSE 9100

ENTRYPOINT ["python", "camera_agent.py"]
CMD ["--config", "config.yml"]
