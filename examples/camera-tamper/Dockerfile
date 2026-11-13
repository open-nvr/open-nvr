# camera-tamper — frame-polling app: covered, defocused, moved or frozen camera.
# Build context is the repository root (the SDK is copied from the checkout).
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
        "httpx>=0.27,<1.0" \
        "PyYAML>=6.0,<7.0" \
        "nats-py>=2.6" \
        "numpy>=1.26,<3" \
        "Pillow>=10,<12"

COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

COPY examples/camera-tamper/camera_tamper.py      ./
COPY examples/camera-tamper/config.example.yml    ./config.example.yml
COPY examples/camera-tamper/config.docker.yml  ./config.docker.yml

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "camera_tamper.py"]
CMD ["--config", "config.yml"]
