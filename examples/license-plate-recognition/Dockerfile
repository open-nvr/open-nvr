# syntax=docker/dockerfile:1.7
# license-plate-recognition example app
#
# Build (from the open-nvr repo root — the context must include
# sdk/opennvr-app-sdk, which this app depends on):
#   docker build -f examples/license-plate-recognition/Dockerfile -t opennvr/license-plate-recognition:2.0.0 .
#
# Run (consumes plate.recognized.v1 from the compose bus):
#   docker run --rm \
#     --network opennvr_internal \
#     -v $(pwd)/config.yml:/app/config.yml:ro \
#     opennvr/license-plate-recognition:2.0.0 \
#     --config /app/config.yml
#
# Or use the repo-root docker-compose.apps.yml overlay, which builds
# this image, generates the config from config.docker.yml, and wires
# KAI-C + registry self-registration automatically.

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The SDK owns the whole runtime stack (nats-py subscription + alert
# fan-out, httpx webhooks, PyYAML config, the Detector loop and §03
# contract server) — install it from the repo checkout so the image
# always matches the in-tree contract surface. v2 (RFC-0002 Phase 4):
# no Pillow, no pipeline — the app never sees a pixel.
COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

# The entry module's full import graph: alerts.py is the SDK
# compatibility shim. A missed module here is a startup crash.
COPY examples/license-plate-recognition/license_plate_recognition.py ./license_plate_recognition.py
COPY examples/license-plate-recognition/alerts.py                    ./alerts.py
COPY examples/license-plate-recognition/config.example.yml           ./config.example.yml

ENV PYTHONUNBUFFERED=1

# No HEALTHCHECK — liveness is Docker's "process is alive"; readiness
# is the SDK contract server (/health on contract_port when enabled).

ENTRYPOINT ["python", "license_plate_recognition.py"]
CMD ["--config", "config.yml"]
