# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# The E2E runner: pytest + Playwright + ffmpeg + the docker CLI.
#
# Built and used only by docker-compose.e2e.yml. It joins the stack's internal
# network and drives it from the inside, which is what lets the suite reach
# services that publish no host port at all — every app's contract surface,
# the MediaMTX admin API, NATS — and what keeps it immune to the Windows
# port-binding lottery (#298).
#
# Based on Microsoft's Playwright image because it ships the browsers together
# with the exact system libraries they need. Installing Chromium correctly on
# a plain Debian base is a moving target that is not worth owning here.

FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

# ffmpeg    generates the synthetic test clips (harness/clips.py)
# docker-cli reads container logs for the evidence bundle and scrapes the
#           one-time setup token, which the server prints to stdout and
#           exposes through no API
# The docker CLI comes from Docker's own apt repo: the Ubuntu-packaged
# `docker.io` pulls in the daemon as well, which is dead weight in a container
# that only ever talks to a mounted socket.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        gnupg; \
    install -m 0755 -d /etc/apt/keyrings; \
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg; \
    chmod a+r /etc/apt/keyrings/docker.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends docker-ce-cli; \
    apt-get purge -y --auto-remove gnupg; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /work/tests/e2e

# Dependencies are listed here rather than COPYing pyproject.toml, because the
# COPY sits above this layer: touching the manifest to add a pytest marker
# would then invalidate it and re-download ~50MB of wheels for nothing. The
# suite itself — pyproject.toml included — is bind-mounted at run time, so the
# image never needs its own copy and editing a test never rebuilds anything.
#
# Keep this list in step with tests/e2e/pyproject.toml [project.dependencies].
RUN pip install --no-cache-dir \
        "pytest>=8.0" \
        # PINNED to the base image tag above. The image ships browser
        # binaries for exactly its own Playwright version, and the Python
        # package finds them by a version-stamped path -- so an unpinned
        # install silently pulls a newer playwright whose browsers are not
        # in the image, and every UI test dies with "Executable doesn't
        # exist at /ms-playwright/...". Bump this and FROM together.
        "playwright==1.55.0" \
        "pytest-playwright>=0.5" \
        "pytest-randomly>=3.15" \
        "httpx>=0.27" \
        "pyotp>=2.9" \
        "nats-py>=2.6" \
        "websockets>=12.0"

# Google Chrome, for H.264. Not a preference -- a requirement of the product
# under test.
#
# The base image's bundled `chromium` is built from open-source Chromium with
# proprietary codecs left out, so it does not advertise H.264 in a WebRTC SDP
# and cannot decode it in a <video> element either. Every camera in this
# system publishes H.264, so with that browser MediaMTX rejects the WHEP offer
# ("codecs not supported by client"), the tile shows *"WHEP connection failed:
# 400"*, and the HLS fallback is no better off. The video tests then fail on a
# product that is working perfectly.
#
# This is not fixable with launch flags -- `--headless=new` was tried and the
# codec is simply absent from the binary. Chrome stable carries the licensed
# codecs, so `channel="chrome"` in conftest.py selects it. Installed through
# Playwright's own installer so it lands where Playwright expects to find it.
RUN set -eux; \
    python -m playwright install --with-deps chrome; \
    rm -rf /var/lib/apt/lists/*

# Playwright's browsers are already present in the base image; this only
# verifies the Python bindings agree with them, failing the build rather than
# the first UI test if they ever drift apart. The Chrome check is the same
# idea: an image that built without it must not reach a test run, because the
# failure it produces looks exactly like a broken video pipeline.
RUN set -eux; \
    python -c "import playwright; print('playwright bindings ok')"; \
    command -v google-chrome

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    E2E_ARTIFACTS=/work/tests/e2e/.artifacts

ENTRYPOINT ["pytest"]
CMD ["-m", "smoke"]
