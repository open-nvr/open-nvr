# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# OpenNVR Tier-0 detect-pipeline service.
FROM python:3.11-slim

# ffmpeg/ffprobe: hardware decode + resolution probe of the camera substream.
# libglib2.0-0: opencv-python-headless runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir ".[service]"

# On by default; set DETECT_PIPELINE_ENABLED=false to disable without redeploy.
ENV DETECT_PIPELINE_ENABLED=true

# The service holds no ports open — it consumes MediaMTX and publishes to NATS.
CMD ["opennvr-tier0"]
