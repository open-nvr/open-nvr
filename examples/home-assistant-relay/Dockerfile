# syntax=docker/dockerfile:1.7
# home-assistant-relay example app
#
# Build (from the open-nvr repo root):
#   docker build -f examples/home-assistant-relay/Dockerfile -t opennvr/home-assistant-relay:1.0.0 .
#
# Run (talks to NATS + MQTT broker / HA REST on the host network):
#   docker run --rm \
#     --network host \
#     -v $(pwd)/config.yml:/app/config.yml:ro \
#     opennvr/home-assistant-relay:1.0.0 \
#     --config /app/config.yml

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The app SDK owns the runtime stack (nats-py, PyYAML, httpx); only
# paho-mqtt (the MQTT-discovery backend) stays app-side. Build from
# the repo root so the SDK folder is in the build context.
COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir \
        /opt/opennvr-app-sdk \
        "paho-mqtt>=2.0,<3.0"

COPY examples/home-assistant-relay/__init__.py            ./__init__.py
COPY examples/home-assistant-relay/ha_mapper.py           ./ha_mapper.py
COPY examples/home-assistant-relay/publishers.py          ./publishers.py
COPY examples/home-assistant-relay/home_assistant_relay.py ./home_assistant_relay.py
COPY examples/home-assistant-relay/config.example.yml     ./config.example.yml
COPY examples/home-assistant-relay/config.docker.yml  ./config.docker.yml

ENV PYTHONUNBUFFERED=1

# No HEALTHCHECK — the relay is a NATS subscriber, no HTTP surface.

ENTRYPOINT ["python", "home_assistant_relay.py"]
CMD ["--config", "config.yml"]
