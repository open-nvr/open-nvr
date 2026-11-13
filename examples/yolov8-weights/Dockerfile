# ============================================================
# OpenNVR — pre-baked YOLOv8n ONNX weights image (ISSUE-7 v2)
# ============================================================
#
# This image solves the "operator's ISP blocks deb.debian.org / PyPI"
# problem reported in IN/CN/IR networks. The default standard stack
# yolov8-weights-init container does an apt-get + pip install +
# ultralytics export on first boot — three external repositories
# that any of which can be filtered. Operators on those networks
# pull THIS image instead via the offline overlay:
#
#     docker compose -f docker-compose.yml \
#
# The image is built and published by .github/workflows/
# build-yolov8-weights.yml on every release tag. Operators with
# no internet can also build it locally on a network that works
# and `docker save | scp | docker load` it onto the deploy box,
# or push to their own private registry and point
# YOLOV8_WEIGHTS_IMAGE at it.
#
# Tag convention: ghcr.io/open-nvr/yolov8-weights:v<ultralytics-tag>
# e.g. ghcr.io/open-nvr/yolov8-weights:v8.3.0
#
# Image size: ~15 MB (single ONNX file on scratch base).

# ── Stage 1: build the ONNX from the upstream .pt checkpoint ──
# We use the official ultralytics image — it has torch +
# torchvision + the yolo CLI pre-installed, ~5 GB but used only
# during build, not in the final image. The export takes ~30 sec
# on x86 CI runners.
FROM ultralytics/ultralytics:8.4.177 AS exporter

# Pin the .pt URL so the build is reproducible. v8.3.0 matches what
# the default standard stack init container uses; if you change this, also
# update the adapter conformance kit (docs/AI_ADAPTER_CONTRACT.md).
ARG YOLOV8_PT_URL=https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26n.pt
WORKDIR /build
# Use Python's urllib instead of curl/wget — ultralytics base image
# has Python pre-installed, but we can't assume curl is present and
# we don't want to `apt-get install curl` (that would require
# deb.debian.org to be reachable from the build environment, which
# is exactly the dependency we're trying to eliminate). urllib is
# in the Python stdlib; no install needed.
RUN python -c "import urllib.request, sys; \
    urllib.request.urlretrieve(sys.argv[1], 'yolo26n.pt'); \
    print('Downloaded yolo26n.pt:', __import__('os').path.getsize('yolo26n.pt'), 'bytes')" \
    "${YOLOV8_PT_URL}" && \
    yolo export model=yolo26n.pt format=onnx opset=12 imgsz=640 simplify=False && \
    ls -la yolo26n.onnx && \
    # Sanity-check the file is a real ONNX model, not a 404 page
    python -c "import onnx; m = onnx.load('yolo26n.onnx'); print(f'ONNX op count: {len(m.graph.node)}, input: {m.graph.input[0].name}')"

# ── Stage 2: tiny alpine image with the .onnx ──
# Why alpine:3.20 instead of `FROM scratch`? standard stack's
# yolov8-weights-init service (in docker-compose.yml) runs
# `sh -c "cp /yolo26n.onnx /weights/yolo26n.onnx"` inside this
# image, which needs a shell. Alpine adds ~5 MB to the final
# image (final size: ~20 MB). Worth it — operators on filtered
# networks would otherwise need to add a shell themselves.
FROM alpine:3.20
COPY --from=exporter /build/yolo26n.onnx /yolo26n.onnx
# ENTRYPOINT intentionally not set — standard stack's service definition
# supplies its own `entrypoint: ["sh", "-c"]` + command.

# Labels for ghcr.io UI and `docker image inspect`.
LABEL org.opencontainers.image.title="OpenNVR YOLO26n weights"
LABEL org.opencontainers.image.description="Pre-built yolo26n.onnx (opset=12, imgsz=640, simplify=False; same (1, 84, 8400) tensor as yolov8n) for OpenNVR standard stack. Used by docker-compose.yml's yolov8-weights-init service instead of runtime apt+pip+export."
LABEL org.opencontainers.image.source="https://github.com/open-nvr/open-nvr"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
LABEL org.opencontainers.image.vendor="OpenNVR"
LABEL com.opennvr.yolov8.tag="v8.4.0"
LABEL com.opennvr.yolov8.model="yolo26n"
LABEL com.opennvr.yolov8.ultralytics="8.4.177"
LABEL com.opennvr.yolov8.opset="12"
LABEL com.opennvr.yolov8.imgsz="640"
LABEL com.opennvr.yolov8.simplify="false"
