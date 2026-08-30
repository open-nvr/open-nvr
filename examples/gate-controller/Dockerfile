# syntax=docker/dockerfile:1.7
# gate-controller example app
#
# Build (from the open-nvr repo root — the context must include
# sdk/opennvr-app-sdk, which this app depends on):
#   docker build -f examples/gate-controller/Dockerfile -t opennvr/gate-controller:1.0.0 .
#
# Run (consumes access.decided.v1 from the compose bus):
#   docker run --rm \
#     --network opennvr_internal \
#     -v $(pwd)/config.yml:/app/config.yml:ro \
#     opennvr/gate-controller:1.0.0 \
#     --config /app/config.yml
#
# Or use the repo-root docker-compose.apps.yml overlay, which builds
# this image, generates the config from config.docker.yml, and wires
# registry self-registration automatically.

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The SDK owns the whole runtime stack (nats-py subscription + alert
# fan-out, httpx relay calls, PyYAML config, the Detector loop and §03
# contract server). No inference, no pixels — bus in, relay out.
COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

COPY examples/gate-controller/gate_controller.py   ./gate_controller.py
COPY examples/gate-controller/config.example.yml   ./config.example.yml

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "gate_controller.py"]
CMD ["--config", "config.yml"]
