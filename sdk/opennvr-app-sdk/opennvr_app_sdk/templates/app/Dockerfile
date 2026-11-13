# syntax=docker/dockerfile:1.7
# __APP_NAME__ — OpenNVR Detector app
#
# Build:
#   __DOCKER_BUILD_HINT__
#
# Run (on the stack's compose network):
#   docker run --rm --network opennvr_internal \
#     -v $(pwd)/config.yml:/app/config.yml:ro __APP_ID__:0.1.0

FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

__DOCKER_SDK_INSTALL__

__DOCKER_COPY__

ENV PYTHONUNBUFFERED=1

# No HEALTHCHECK — readiness is the SDK contract server (/health on
# contract_port when enabled).
ENTRYPOINT ["python", "__APP_MODULE__.py"]
CMD ["--config", "config.yml"]
