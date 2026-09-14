# ============================================================
# OpenNVR — pre-baked YOLO11n-pose ONNX weights image
# ============================================================
#
# The pose adapter ships without weights, and unlike every other
# model in the stack there is nowhere to download this one from:
# Ultralytics publishes the .pt, never the ONNX, so the graph has
# to be exported by somebody. Until this image existed that
# somebody was the operator, and a fresh install got an adapter
# that started, answered /health, and failed every /infer with a
# missing-file error — the silent kind of broken.
#
# So: export once, here, and ship the graph. The init service in
# docker-compose.apps.yml copies it into the weights volume.
#
# It is the same shape as examples/yolov8-weights/ and for one of
# the same reasons: an operator on a filtered network (reported
# from IN/CN/IR) cannot reach PyPI or ultralytics.com at boot, so
# a first-boot pip+export is not a thing to rely on.
#
# Image size: ~17 MB (alpine + one ~12 MB ONNX).
#
# Build it yourself, if you would rather not pull:
#     docker build -t ghcr.io/open-nvr/yolo-pose-weights:v8.3.0 \
#       examples/yolo-pose-weights
# or build it where the network works and move it:
#     docker save … | ssh box docker load

# ── Stage 1: export the ONNX from the upstream .pt checkpoint ──
# The official ultralytics image already has torch + the yolo CLI.
# It is ~5 GB and used only here; nothing of it survives to stage 2.
FROM ultralytics/ultralytics:8.3.40 AS exporter

# Pinned so the build is reproducible. Same assets release the rest
# of the stack pins to.
ARG POSE_PT_URL=https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n-pose.pt
WORKDIR /build

# urllib rather than curl/wget: the ultralytics base has Python, and
# `apt-get install curl` would need deb.debian.org — the exact
# dependency this image exists to remove.
#
# dynamic=True is not optional. The adapter's `imgsz` is its CPU dial
# (OPENNVR_POSE_IMGSZ, 448 by default), and a fixed-size export
# rejects every size but the one it was exported at — so a fixed
# graph would turn that dial into a crash. opset 12 and imgsz 448
# match the adapter repo's own export recipe (download_models.py).
RUN python -c "import urllib.request, sys, os; \
    urllib.request.urlretrieve(sys.argv[1], 'yolo11n-pose.pt'); \
    print('Downloaded yolo11n-pose.pt:', os.path.getsize('yolo11n-pose.pt'), 'bytes')" \
    "${POSE_PT_URL}" && \
    yolo export model=yolo11n-pose.pt format=onnx opset=12 imgsz=448 dynamic=True && \
    ls -la yolo11n-pose.onnx && \
    # Prove it is a model and not a 404 page, and that the graph really
    # is dynamic — a silently-static export passes every check but the
    # one that matters, on the operator's machine, weeks later.
    python -c "import onnx; m = onnx.load('yolo11n-pose.onnx'); \
    dims = [d.dim_param or d.dim_value for d in m.graph.input[0].type.tensor_type.shape.dim]; \
    print('ONNX ops:', len(m.graph.node), 'input:', m.graph.input[0].name, dims); \
    assert any(isinstance(d, str) and d for d in dims), \
        f'export is not dynamic: {dims}'"

# ── Stage 2: the graph, and a shell to copy it with ──
# alpine rather than scratch because the init service runs `sh` inside
# this image to do the copy (see docker-compose.apps.yml).
FROM alpine:3.20
COPY --from=exporter /build/yolo11n-pose.onnx /yolo11n-pose.onnx
# ENTRYPOINT deliberately unset — the compose service supplies its own.

LABEL org.opencontainers.image.title="OpenNVR YOLO11n-pose weights"
LABEL org.opencontainers.image.description="Pre-built yolo11n-pose.onnx (opset=12, imgsz=448, dynamic) for the OpenNVR pose adapter. Ultralytics publishes only the .pt, so this image carries the exported graph."
LABEL org.opencontainers.image.source="https://github.com/open-nvr/open-nvr"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
LABEL org.opencontainers.image.vendor="OpenNVR"
LABEL com.opennvr.yolopose.tag="v8.3.0"
LABEL com.opennvr.yolopose.opset="12"
LABEL com.opennvr.yolopose.imgsz="448"
LABEL com.opennvr.yolopose.dynamic="true"
