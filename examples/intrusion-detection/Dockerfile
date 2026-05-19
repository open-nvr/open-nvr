# syntax=docker/dockerfile:1.7
# Intrusion-detection example app.
#
# Build:
#   docker build -f examples/intrusion-detection/Dockerfile -t opennvr/example-intrusion-detection:1.0.0 .
#
# Run (sidecar to KAI-C and OpenNVR backend on the same host):
#   docker run --rm \
#     -v $(pwd)/examples/intrusion-detection/config.yml:/app/config.yml:ro \
#     --network host \
#     opennvr/example-intrusion-detection:1.0.0

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Minimal runtime deps — httpx for KAI-C calls and snapshot fetches,
# PyYAML for the config file. No cv2/PIL — frames go through KAI-C
# as base64 JSON without any local image processing in this example.
RUN pip install --no-cache-dir \
        "httpx>=0.27,<1.0" \
        "PyYAML>=6.0,<7.0"

COPY examples/intrusion-detection/intrusion_detection.py ./intrusion_detection.py
COPY examples/intrusion-detection/zone.py                ./zone.py
COPY examples/intrusion-detection/alerts.py              ./alerts.py
COPY examples/intrusion-detection/frame_sources.py       ./frame_sources.py

ENV PYTHONPATH=/app

ENTRYPOINT ["python", "intrusion_detection.py", "--config", "/app/config.yml"]
