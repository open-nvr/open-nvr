# ============================================================
# OpenNVR — pre-baked FastSAM-s ONNX weights image
# ============================================================
#
# The gold-bar-desk app segments bars with FastSAM-s, run in the app
# with onnxruntime. Like yolo11n-pose, Ultralytics publishes only the
# .pt, so the graph has to be exported by somebody, and it is done once,
# here, rather than by every operator (or at first boot, which a
# filtered network cannot do). Same shape as examples/yolo-pose-weights/.
#
# Image size: ~55 MB (alpine + one ~47 MB ONNX).
#
# Build it yourself, if you would rather not pull:
#     docker build -t ghcr.io/open-nvr/fastsam-weights:v8.3.0 \
#       examples/fastsam-weights

# ── Stage 1: export the ONNX from the upstream .pt checkpoint ──
FROM ultralytics/ultralytics:8.3.40 AS exporter

ARG FASTSAM_PT_URL=https://github.com/ultralytics/assets/releases/download/v8.3.0/FastSAM-s.pt
WORKDIR /build

# A FIXED 640 input, not dynamic: the app always letterboxes the tray
# crop to 640, and the fixed graph is what it was measured with.
# simplify=False keeps the export from reaching for onnxslim.
RUN python -c "import urllib.request, sys, os; \
    urllib.request.urlretrieve(sys.argv[1], 'FastSAM-s.pt'); \
    print('Downloaded FastSAM-s.pt:', os.path.getsize('FastSAM-s.pt'), 'bytes')" \
    "${FASTSAM_PT_URL}" && \
    python -c "from ultralytics import FastSAM; \
    FastSAM('FastSAM-s.pt').export(format='onnx', opset=12, imgsz=640, \
                                   dynamic=False, simplify=False)" && \
    ls -la FastSAM-s.onnx && \
    # Prove it is the graph the app decodes: two outputs, boxes + 32 mask
    # coefficients over 8400 anchors, and 32 prototypes at 160x160. A
    # different layout would load fine and segment nothing.
    python -c "import onnx; g = onnx.load('FastSAM-s.onnx').graph; \
    dims = lambda v: [d.dim_value for d in v.type.tensor_type.shape.dim]; \
    outs = [dims(o) for o in g.output]; print('outputs:', outs); \
    assert outs == [[1, 37, 8400], [1, 32, 160, 160]], outs"

# ── Stage 2: the graph, and a shell to copy it with ──
FROM alpine:3.20
COPY --from=exporter /build/FastSAM-s.onnx /FastSAM-s.onnx
# ENTRYPOINT deliberately unset — the compose service supplies its own.

LABEL org.opencontainers.image.title="OpenNVR FastSAM-s weights"
LABEL org.opencontainers.image.description="Pre-built FastSAM-s.onnx (opset=12, imgsz=640, static) for the OpenNVR gold-bar-desk app."
LABEL org.opencontainers.image.source="https://github.com/open-nvr/open-nvr"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
LABEL org.opencontainers.image.vendor="OpenNVR"
LABEL com.opennvr.fastsam.tag="v8.3.0"
LABEL com.opennvr.fastsam.opset="12"
LABEL com.opennvr.fastsam.imgsz="640"
