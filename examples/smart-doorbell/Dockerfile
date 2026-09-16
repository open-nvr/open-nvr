# syntax=docker/dockerfile:1.7
# smart-doorbell example app
#
# Build (from the open-nvr repo root):
#   docker build -f examples/smart-doorbell/Dockerfile -t opennvr/smart-doorbell:1.0.0 .
#
# Run (talks to KAI-C + the InsightFace adapter on the host network):
#   docker run --rm \
#     --network host \
#     -v $(pwd)/config.yml:/app/config.yml:ro \
#     opennvr/smart-doorbell:1.0.0 \
#     --config /app/config.yml daemon
#
# NOTE the argument order: ``--config`` is a global option of the CLI
# and argparse requires it BEFORE the ``daemon`` subcommand.

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
        "httpx>=0.27,<1.0" \
        "PyYAML>=6.0,<7.0" \
        "nats-py>=2.6" \
        "Pillow>=10,<12"
# Pillow shrinks stranger snapshots to thumbnails for the dashboard's
# gallery; the app runs without it (raw JPEGs are embedded only when small).

# The OpenNVR App SDK — this app's modules import opennvr_app_sdk
# (alerts/frame_sources) at top level, so it must be installed into the
# image or the container crashes on startup with ModuleNotFoundError.
# Installed from the in-repo package.
COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

COPY examples/smart-doorbell/__init__.py        ./__init__.py
COPY examples/smart-doorbell/alerts.py          ./alerts.py
COPY examples/smart-doorbell/frame_sources.py   ./frame_sources.py
COPY examples/smart-doorbell/face_recognition_pipeline.py ./face_recognition_pipeline.py
COPY examples/smart-doorbell/smart_doorbell.py  ./smart_doorbell.py
COPY examples/smart-doorbell/config.example.yml ./config.example.yml

ENV PYTHONUNBUFFERED=1

# No HEALTHCHECK — the daemon loop never exposes an HTTP surface.
# Docker's "process is alive" liveness is what matters here.

# Global options first, subcommand last — ``daemon --config …`` is
# rejected by argparse (the subparser doesn't know --config), so the
# default CMD and any compose ``command`` override must spell it
# ``--config <path> daemon``.
ENTRYPOINT ["python", "smart_doorbell.py"]
CMD ["--config", "config.yml", "daemon"]
