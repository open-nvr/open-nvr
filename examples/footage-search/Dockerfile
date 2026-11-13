# syntax=docker/dockerfile:1.7
# footage-search example app
#
# Build (from the open-nvr repo root — the context must include
# sdk/opennvr-app-sdk, which this app depends on):
#   docker build -f examples/footage-search/Dockerfile -t opennvr/footage-search:2.0.0 .
#
# Serve the app contract (manifest, live state, the "Search footage"
# action) on the compose network. No data volume: as of 2.0.0 this app
# keeps no index and reads everything from core when asked.
#   docker run --rm \
#     --network opennvr_internal \
#     -v $(pwd)/config.yml:/app/config.yml:ro \
#     opennvr/footage-search:2.0.0 \
#     --config /app/config.yml serve
#
# Search from a running container:
#   docker compose -f docker-compose.yml -f docker-compose.apps.yml \
#     exec footage-search \
#     python footage_search.py --config /app/config/config.yml \
#     search "red truck at the dock yesterday"
#
# Or use the repo-root docker-compose.apps.yml overlay, which builds
# this image and generates the config from config.docker.yml.

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The SDK owns the entire runtime stack: the contract server, the
# OpenNVR client this app searches through, PyYAML config, and httpx
# (which the optional Ollama query parser also rides). Installed from
# the repo checkout so the image always matches the in-tree contract
# surface.
COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

# The entry module's full import graph: query.py (NL → structured
# filter parsers). store.py is gone with the index it owned. A missed
# module here is a startup crash.
COPY examples/footage-search/footage_search.py ./footage_search.py
COPY examples/footage-search/query.py          ./query.py
COPY examples/footage-search/config.example.yml ./config.example.yml

ENV PYTHONUNBUFFERED=1

# No HEALTHCHECK — liveness is Docker's "process is alive". The app
# does serve /health on contract_port, and the App Catalog reads it;
# duplicating that as a container healthcheck would report a restart
# loop for an app whose only fault is that core is briefly down.

ENTRYPOINT ["python", "footage_search.py"]
CMD ["--config", "config.yml", "serve"]
