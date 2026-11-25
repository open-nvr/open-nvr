# syntax=docker/dockerfile:1.7
# Gold bar desk app.
#
# Build:
#   docker build -f examples/gold-bar-desk/Dockerfile \
#       -t opennvr/gold-bar-desk:local-build .
#
# It watches the desk as a live stream (ffmpeg), reasons about pixels
# (OpenCV), and segments bars itself with FastSAM-s on onnxruntime. The
# model is NOT in this image: fastsam-weights-init copies it into a
# volume mounted read-only at /weights.

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
        "opencv-python-headless>=4.9,<5.0" \
        "onnxruntime>=1.18,<2.0"

COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

COPY examples/gold-bar-desk/gold_bar_desk.py    ./gold_bar_desk.py
COPY examples/gold-bar-desk/bar_desk            ./bar_desk
COPY examples/gold-bar-desk/config.example.yml  ./config.example.yml
ENV PYTHONPATH=/app
# onnxruntime phones home to Microsoft on import unless this is set (see
# bar_desk/segmenter.py). The egress proxy would refuse it anyway, but
# would raise an alert for every attempt.
ENV ORT_DISABLE_TELEMETRY=1

ENTRYPOINT ["python", "gold_bar_desk.py"]
CMD ["--config", "config.yml"]
