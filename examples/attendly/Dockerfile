# syntax=docker/dockerfile:1.7
# Attendly on OpenNVR — the attendance app, same packaging as the Camera
# Agent: one image, its own port, listed in the App Catalog with an
# "Open app" link.
#
# Build (from the open-nvr repo root):
#   docker build -f examples/attendly/Dockerfile -t opennvr/attendly:1.0.0 .
#
# Run next to a running OpenNVR (the compose overlay does this for you):
#   docker run --rm --network opennvr_internal -p 9300:9300 \
#     -e OPENNVR_URL=http://opennvr-core:8000 -e KAIC_URL=http://opennvr-core:8100 \
#     -e OPENNVR_INTERNAL_API_KEY=... -v opennvr_attendly_data:/data \
#     opennvr/attendly:1.0.0
#
# Then open http://localhost:9300/ (or the catalog's "Open app").

# ── Stage 1: the React UI ────────────────────────────────────────
FROM node:20-alpine AS web
WORKDIR /web
COPY examples/attendly/web/package.json examples/attendly/web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY examples/attendly/web/ ./
RUN npm run typecheck && npm run build

# ── Stage 2: the Python app ──────────────────────────────────────
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        # opencv-python-headless needs libgomp / libglib on the slim base
        libgomp1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The image installs EXACTLY what uv.lock says — the dependency set the
# tests run against (see examples/camera-agent/Dockerfile for why). The App
# SDK is left out of the export and installed from the in-repo tree below.
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /bin/uv
COPY examples/attendly/pyproject.toml examples/attendly/uv.lock /src/examples/attendly/
COPY sdk/opennvr-app-sdk/pyproject.toml /src/sdk/opennvr-app-sdk/pyproject.toml
RUN cd /src/examples/attendly \
    && uv export --locked --no-dev --no-emit-project \
        --no-emit-package opennvr-app-sdk \
        -o /src/requirements.txt \
    && grep -c "==" /src/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    for i in 1 2 3 4 5; do \
        echo "pip install attempt $i/5..."; \
        pip install --timeout 300 --retries 10 --require-hashes -r /src/requirements.txt \
        && break; \
        if [ "$i" = 5 ]; then echo "pip install failed after 5 attempts" >&2; exit 1; fi; \
        echo "attempt $i failed; retrying in 15s..."; sleep 15; \
    done

COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir /opt/opennvr-app-sdk

COPY examples/attendly/attendly_app.py ./attendly_app.py
COPY examples/attendly/attendly      ./attendly
COPY --from=web /web/dist             ./web/dist

# Face crops and snapshots are not written unless ATTENDLY_SAVE_DEBUG_FRAMES
# is set; the SQLite database lives on /data.
ENV ATTENDLY_DATABASE_URL=sqlite:////data/attendly.db \
    ATTENDLY_DATA_DIR=/data \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]

EXPOSE 9300

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=10 \
    CMD python -c "import httpx, sys; sys.exit(0 if httpx.get('http://127.0.0.1:9300/health', timeout=3).status_code == 200 else 1)"

ENTRYPOINT ["python", "attendly_app.py"]
