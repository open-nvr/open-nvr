# syntax=docker/dockerfile:1.7
# alert-notifier example app
#
# Build (from the open-nvr repo root — the context must include
# sdk/opennvr-app-sdk, which this app depends on):
#   docker build -f examples/alert-notifier/Dockerfile -t opennvr/alert-notifier:2.0.0 .
#
# Run (consumes opennvr.alerts.> from the compose bus):
#   docker run --rm \
#     --network opennvr_internal \
#     -v $(pwd)/config.yml:/app/config.yml:ro \
#     opennvr/alert-notifier:2.0.0 \
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

# The SDK owns the whole runtime stack (nats-py subscription, httpx,
# PyYAML config, the §03 contract server). Every one of the ten
# channels is HTTP through that httpx or stdlib smtplib: a notifier
# that cannot start is a notifier that does not notify, so no channel
# is allowed to add a dependency. No inference, no pixels.
COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

# All three modules, or the image starts and dies on `import channels`.
COPY examples/alert-notifier/alert_notifier.py   ./alert_notifier.py
COPY examples/alert-notifier/channels.py         ./channels.py
COPY examples/alert-notifier/routing.py          ./routing.py
COPY examples/alert-notifier/config.example.yml  ./config.example.yml

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "alert_notifier.py"]
CMD ["--config", "config.yml"]
