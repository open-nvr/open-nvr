# ppe-check — Tier-0 subscriber: a person missing required safety kit, judged from the best crop by open-vocabulary detection through KAI-C.
# Build context is the repository root (the SDK is copied from the checkout).
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
        "httpx>=0.27,<1.0" \
        "PyYAML>=6.0,<7.0" \
        "nats-py>=2.6"

COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

COPY examples/ppe-check/ppe_check.py          ./
COPY examples/ppe-check/config.example.yml     ./config.example.yml
COPY examples/ppe-check/config.docker.yml  ./config.docker.yml

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "ppe_check.py"]
CMD ["--config", "config.yml"]
