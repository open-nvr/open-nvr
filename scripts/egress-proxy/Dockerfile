# syntax=docker/dockerfile:1.7
# OpenNVR egress proxy — the only door out of the apps network.
#
# Sits on the internal ``opennvr_apps`` network and the platform network;
# every app container gets it as HTTP_PROXY / HTTPS_PROXY. It asks core
# per destination whether the calling app may open it (see proxy.py and
# docs/APP_NETWORK.md). Standard library only; no policy of its own.
#
# Build (from the repo root):
#   docker build -f scripts/egress-proxy/Dockerfile -t opennvr/egress-proxy:local .

FROM python:3.12-alpine

RUN adduser -D -H -u 10001 egress
WORKDIR /app
COPY scripts/egress-proxy/proxy.py ./proxy.py
USER egress
ENV PYTHONUNBUFFERED=1
EXPOSE 3128

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3128/healthz', timeout=3).status == 200 else 1)"

ENTRYPOINT ["python", "proxy.py"]
