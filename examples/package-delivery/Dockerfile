# syntax=docker/dockerfile:1.7
# package-delivery example app
#
# Build (from the open-nvr repo root):
#   docker build -f examples/package-delivery/Dockerfile -t opennvr/package-delivery:1.1.0 .
#
# Run (the stock compose overlay does this for you):
#   docker run --rm --network host \
#     -v $(pwd)/config.yml:/app/config.yml:ro \
#     opennvr/package-delivery:1.1.0 --config /app/config.yml

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The OpenNVR App SDK owns the runtime stack: the NATS loop, the platform
# client (snapshots, evidence, KAI-C inference), alerts and the contract
# server. Installed from the in-repo package.
COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

COPY examples/package-delivery/package_delivery.py ./package_delivery.py
COPY examples/package-delivery/config.example.yml ./config.example.yml

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "package_delivery.py"]
CMD ["--config", "config.yml"]
