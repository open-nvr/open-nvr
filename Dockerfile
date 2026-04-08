# ==========================================
# STAGE 1: Frontend Build (Node.js)
# ==========================================
FROM node:23-alpine AS frontend-builder

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

# Copy project files for dependency resolution
COPY server/pyproject.toml /build/server/pyproject.toml
COPY kai-c/pyproject.toml /build/kai-c/pyproject.toml

# Create virtual environments and sync dependencies (--no-install-project skips building the project itself)
# Server dependencies
RUN cd /build/server && uv venv /build/server-venv && \
    uv sync --frozen --no-dev --no-install-project --directory /build/server || \
    uv sync --no-dev --no-install-project --directory /build/server

# Kai-C dependencies  
RUN cd /build/kai-c && uv venv /build/kai-c-venv && \
    uv sync --frozen --no-dev --no-install-project --directory /build/kai-c || \
    uv sync --no-dev --no-install-project --directory /build/kai-c

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
RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor \
    curl \
    gosu \
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

