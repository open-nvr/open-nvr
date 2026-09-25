# ==========================================
# STAGE 1: Frontend Build (Node.js)
# ==========================================
# --platform=$BUILDPLATFORM pins this stage to the ARCHITECTURE OF THE
# BUILDER, not the target. Vite emits static JS/CSS/HTML that is
# architecture-independent, so there is nothing to gain from running
# `npm ci` + `npm run build` under QEMU for the linux/arm64 leg of a
# multi-arch build — and plenty to lose: an emulated Node build is
# roughly an order of magnitude slower and is the single step that
# would make arm64 publishing impractical. The dist/ output is copied
# into the target-arch runtime stage below.
FROM --platform=$BUILDPLATFORM node:23-alpine AS frontend-builder

WORKDIR /app-src

# Copy package files first for better caching
COPY app/package.json app/package-lock.json ./

# Install dependencies (including dev deps for build)
RUN npm ci

# Copy the rest of the frontend source code
COPY app/ ./

# Build the production assets (Vite React)
RUN npm run build
# The output will be in /app-src/dist


# ==========================================
# STAGE 2: Python Dependencies Builder
# ==========================================
FROM python:3.11-slim AS python-builder

WORKDIR /build

# Install build dependencies ONLY in this stage
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy uv binary
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy project files for dependency resolution.
#
# THE LOCKFILES ARE NOT OPTIONAL. Without them `uv sync --frozen` fails
# with "Unable to find lockfile", and the `|| uv sync` that used to
# follow quietly resolved the newest of everything instead — so CI
# tested one dependency set and the shipped image ran another, with
# nothing reporting the difference.
#
# That is not hypothetical twice over. FastAPI drifted to 0.141.1
# against a lock pinning 0.135.3 and every API token started getting
# 403s; the note in docs/design/home-assistant-integration-
# implementation-plan.md calls it a "Trap found live" and works around
# the symptom. Then SQLAlchemy 2.1.0 changed the default DBAPI for a
# bare `postgresql://` URL from psycopg2 to psycopg 3, which is not
# installed, and the backend could not import at all.
COPY server/pyproject.toml server/uv.lock /build/server/
COPY kai-c/pyproject.toml kai-c/uv.lock /build/kai-c/

# Create virtual environments and sync dependencies (--no-install-project skips building the project itself)
#
# No `|| uv sync` fallback. A lockfile out of step with pyproject.toml
# is a thing to fix in one command, and failing here says so; silently
# building something else does not.
# Server dependencies
RUN cd /build/server && uv venv /build/server-venv && \
    VIRTUAL_ENV=/build/server-venv uv sync --frozen --no-dev --no-install-project --directory /build/server --active

# Kai-C dependencies
RUN cd /build/kai-c && uv venv /build/kai-c-venv && \
    VIRTUAL_ENV=/build/kai-c-venv uv sync --frozen --no-dev --no-install-project --directory /build/kai-c --active

# Install opencv-python-headless separately (not in pyproject.toml)
RUN uv pip install --python /build/server-venv/bin/python --no-cache-dir opencv-python-headless


# ==========================================
# STAGE 3: Final Runtime Image (OPTIMIZED)
# ==========================================
FROM python:3.11-slim

WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/server

# Install ONLY runtime dependencies (NO build-essential, NO libpq-dev)
# These are the minimal libraries needed to RUN the compiled packages
# ffmpeg: /recordings/frame (and the camera-agent twin) extract a still from a
# recorded segment by shelling out to ffmpeg; without it they 502 on every install.
RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor \
    curl \
    gosu \
    ffmpeg \
    libpq5 \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean \
    && rm -rf /tmp/* /var/tmp/*

# Copy pre-built virtual environments from builder stage
COPY --from=python-builder /build/server-venv /app/server-venv
COPY --from=python-builder /build/kai-c-venv /app/kai-c-venv

# Set up Python path to use virtual environments
ENV PATH="/app/server-venv/bin:$PATH"
ENV VIRTUAL_ENV="/app/server-venv"

# ==========================================
# Copy Application Code
# ==========================================

# Copy backend code
COPY server/ ./server/

# Copy Kai-C code
COPY kai-c/ ./kai-c/

# Copy built frontend assets from Stage 1
COPY --from=frontend-builder /app-src/dist ./app/dist

# Copy Supervisor Configuration
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Copy entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# SECURITY: Remove any .env files and .git directories
RUN find /app -name ".env" -type f -delete 2>/dev/null || true && \
    find /app -name ".env.*" -type f -delete 2>/dev/null || true && \
    find /app -name "*.env" -type f -delete 2>/dev/null || true && \
    find /app -name ".git" -type d -exec rm -rf {} + 2>/dev/null || true && \
    find /app -name "env.example" -type f -delete 2>/dev/null || true && \
    find /app -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true && \
    find /app -type f -name "*.pyc" -delete 2>/dev/null || true && \
    echo "✓ Cleaned sensitive and unnecessary files from image"

# Create non-root user for better security (optional but recommended)
RUN useradd -m -u 1000 opennvr && \
    mkdir -p /app/logs && \
    mkdir -p /app/keys && \
    mkdir -p /app/AI-adapters/AIAdapters/frames && \
    chown -R opennvr:opennvr /app
# Note: Do NOT switch to USER opennvr here - entrypoint needs root to fix permissions

# Expose ports
# 8000: Core API
# 8100: Kai-C Internal API
EXPOSE 8000 8100

# Health check (optional)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Use entrypoint script to fix permissions before starting services
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]


