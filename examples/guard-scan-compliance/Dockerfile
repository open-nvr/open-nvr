# syntax=docker/dockerfile:1.7
# Guard-scan compliance app.
#
# Build:
#   docker build -f examples/guard-scan-compliance/Dockerfile \
#       -t opennvr/guard-scan-compliance:local-build .
#
# Unlike the snapshot-polling example apps, this one WATCHES: it decodes
# a live stream, so the image needs ffmpeg, and it reasons about pixels
# around the guard's hand, so it needs OpenCV. Both are the point of the
# app rather than incidental weight.

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# opencv-python-headless, not opencv-python: no GUI in a container, and
# the headless build drops the X11 stack the full one drags in.
RUN pip install --no-cache-dir \
        "httpx>=0.27,<1.0" \
        "PyYAML>=6.0,<7.0" \
        "websockets>=13.0" \
        "numpy>=1.26,<3.0" \
        "opencv-python-headless>=4.9,<5.0"

COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

COPY examples/guard-scan-compliance/guard_scan_compliance.py ./guard_scan_compliance.py
COPY examples/guard-scan-compliance/guard_scan               ./guard_scan
COPY examples/guard-scan-compliance/config.example.yml       ./config.example.yml

# Where the per-frame keypoint logs go — the training set this collects
# as it runs. A volume in compose, pruned by age.
RUN mkdir -p /data/sessions
ENV PYTHONPATH=/app

ENTRYPOINT ["python", "guard_scan_compliance.py"]
CMD ["--config", "config.yml"]
