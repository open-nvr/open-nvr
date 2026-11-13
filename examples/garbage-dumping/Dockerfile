# garbage-dumping — frame-polling app: garbage dumped in a watched zone, found by open-vocabulary detection through KAI-C.
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
        "Pillow>=10,<12"

COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

COPY examples/garbage-dumping/garbage_dumping.py          ./
COPY examples/garbage-dumping/config.example.yml     ./config.example.yml
COPY examples/garbage-dumping/config.docker.yml  ./config.docker.yml

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "garbage_dumping.py"]
CMD ["--config", "config.yml"]
