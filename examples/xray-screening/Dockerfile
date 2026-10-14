# syntax=docker/dockerfile:1.7
# X-ray screening app.
#
# Build:
#   docker build -f examples/xray-screening/Dockerfile \
#       -t opennvr/xray-screening:local-build .
#
# It watches the scanner's screen as a live stream, so it needs ffmpeg to
# decode it and OpenCV to read it. No model: the scanner has already done
# the physics, and colour is what it reports.

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
        "httpx>=0.27,<1.0" \
        "PyYAML>=6.0,<7.0" \
        "numpy>=1.26,<3.0" \
        "opencv-python-headless>=4.9,<5.0"

COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

COPY examples/xray-screening/xray_screening.py  ./xray_screening.py
COPY examples/xray-screening/xray               ./xray
COPY examples/xray-screening/config.example.yml ./config.example.yml
ENV PYTHONPATH=/app

ENTRYPOINT ["python", "xray_screening.py"]
CMD ["--config", "config.yml"]
