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
# auto (default), cpu or qualcomm. onnxruntime-qnn lets the segmentation
# model run on a Qualcomm Hexagon NPU (~14 ms a frame where the CPU takes
# ~850 ms). auto adds it to every arm64 build: the image CI publishes is
# one multi-arch image, and a site that installs it from the registry (the
# App Catalog pins it) never builds with ACCEL=qualcomm, so an arm64 image
# without it left Qualcomm boards on the CPU. On other arm64 boards it is
# inert: the app tries the NPU only when GOLD_BAR_DESK_ONNX_PROVIDERS asks
# for it and /dev/fastrpc-cdsp is there. amd64 builds never carry it.
# cpu leaves it out everywhere; qualcomm insists on it.
ARG ACCEL=auto
# Set by buildx for each platform it builds (amd64, arm64).
ARG TARGETARCH

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

# onnxruntime-qnn >= 2.0 is a PLUGIN provider that sits next to plain
# onnxruntime (it needs >= 1.24.2) and bundles the QAIRT user-space; the
# host lends the fastrpc device and two libraries. Pinned like
# detect-pipeline's: the plugin tracks a QAIRT release.
RUN arch="${TARGETARCH:-$(uname -m)}"; \
    case "${ACCEL}" in \
      cpu) want=no ;; \
      qualcomm) want=yes ;; \
      auto) case "$arch" in arm64|aarch64) want=yes ;; *) want=no ;; esac ;; \
      *) echo "unknown ACCEL=${ACCEL} (auto|cpu|qualcomm)" >&2; exit 1 ;; \
    esac; \
    if [ "$want" = yes ]; then \
      pip install --no-cache-dir "onnxruntime>=1.24.2,<2" onnxruntime-qnn==2.7.0; \
    fi

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
# numpy's OpenBLAS starts one worker per core at import and lets them spin
# waiting for work. The app's only matrix work is one small multiply a
# frame (the bar outlines), so on a Ventuno Q seven idle workers burned
# ~2.5 of 8 cores: 340% CPU at 4 analysed frames a second, 95% with one.
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1

ENTRYPOINT ["python", "gold_bar_desk.py"]
CMD ["--config", "config.yml"]
