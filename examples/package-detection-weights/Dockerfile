# ============================================================
# OpenNVR — pre-baked package-detection ONNX weights image
# ============================================================
#
# Package Delivery grades its own counts, and a detector it cannot
# load makes that grade read "fair" — a correct answer that looks
# like a broken one. An operator who has done nothing wrong sees the
# app apparently rating its own model poorly, when what it is
# actually reporting is that no model is there.
#
# So the weights ship with the app, the way yolov8-weights and
# yolo-pose-weights ship with theirs, and for the same reason those
# two exist: an operator on a filtered network (reported from
# IN/CN/IR) cannot reach a download URL at first boot, so fetching
# the graph at runtime is not something to rely on. The init service
# in docker-compose.apps.yml copies it into the weights volume.
#
# Unlike those two there is no upstream to export from. This model is
# ours — YOLOv8n fine-tuned on two openly licensed doorstep sets —
# so the graph comes from a release asset on the adapter repo, pinned
# below. Re-training publishes a new asset and bumps the tag; nothing
# here re-trains, because a build that trains is a build whose output
# nobody can reproduce.
#
#   package-detection v1.0.0
#   Data     package at front door v2 (1,293 img, MIT)
#            + Roboflow Packages v6 raw (26 img, CC0)
#   Training 40 epochs, imgsz 416, CPU, seed 0
#   val      P 0.959  R 0.831  mAP50 0.933  mAP50-95 0.763
#   test     P 0.974  R 0.804  mAP50 0.914  mAP50-95 0.631
#   Export   ONNX opset 12, dynamic axes, simplified — 11.7 MB
#   Speed    23-34 ms/frame, 2 x86 cores, onnxruntime CPU EP
#
# Image size: ~19 MB (alpine + one ~12 MB ONNX).
#
# Build it yourself, if you would rather not pull:
#     docker build -t ghcr.io/open-nvr/package-detection-weights:v1.0.0 \
#       examples/package-detection-weights
# or build it where the network works and move it:
#     docker save … | ssh box docker load
#
# To ship a fine-tune of your OWN doorsteps instead, point
# PACKAGE_ONNX_URL at your export — or skip this image entirely and
# put the file straight into the volume (README → "Getting the
# counter to good").

FROM alpine:3.20

# Pinned to a release asset, not to "latest": a weights image whose
# contents change under a fixed tag is the thing that makes a
# detection regression impossible to bisect.
ARG PACKAGE_ONNX_URL=https://github.com/open-nvr/ai-adapter/releases/download/package-detection-v1.0.0/yolov8n-package.onnx
ARG EXPECTED_MIN_BYTES=8000000

RUN apk add --no-cache wget \
 # Say why the download failed, in words. Every check below is chained
 # off this one with &&, so a wget that dies outright short-circuits the
 # lot and the build's whole output is "exit code: 8" — wget for "the
 # server answered with an error status", which on a release asset means
 # a 404. An asset that was never published, or that moved, is the
 # single likeliest reason this image fails to build, so it is the one
 # failure that must not be a bare exit code.
 && if ! wget -q -O /yolov8n-package.onnx "${PACKAGE_ONNX_URL}"; then \
      echo "" >&2; \
      echo "Could not download the weights:" >&2; \
      echo "  ${PACKAGE_ONNX_URL}" >&2; \
      echo "" >&2; \
      echo "wget failed, so nothing was fetched and none of the checks" >&2; \
      echo "below ever ran. Exit 8 is an error status from the server;" >&2; \
      echo "on a release asset that is a 404 — no release or no asset" >&2; \
      echo "under that exact tag and filename." >&2; \
      echo "" >&2; \
      echo "Publish the asset, or point PACKAGE_ONNX_URL at one that" >&2; \
      echo "exists. Tag and filename must both match exactly. A" >&2; \
      echo "re-trained model ships as a NEW tag rather than replacing" >&2; \
      echo "an existing one, so a given tag always carries one graph." >&2; \
      exit 1; \
    fi \
 # Prove it is a model and not a 404 page. A GitHub "Not Found" body
 # is ~9 bytes of HTML that copies into the volume perfectly happily
 # and then fails at the adapter, on the operator's machine, as a
 # missing-file error weeks later — the silent kind of broken these
 # images exist to stop.
 && SIZE=$(wc -c < /yolov8n-package.onnx) \
 && if [ "$SIZE" -lt "${EXPECTED_MIN_BYTES}" ]; then \
      echo "Downloaded ${SIZE} bytes from ${PACKAGE_ONNX_URL} — that is not the ONNX." >&2; \
      head -c 200 /yolov8n-package.onnx >&2; exit 1; \
    fi \
 # Structure, not a brand string. An ONNX file is a serialized
 # ModelProto whose first field is ir_version, so byte 0 is the
 # protobuf tag 0x08 — true of every export regardless of who made
 # it. Grepping for "onnx" in the header looks equivalent and is not:
 # this model's producer string is "pytorch", so that check rejects
 # the genuine weights and passes anything with the word in it.
 && if ! head -c 1 /yolov8n-package.onnx | od -An -tx1 | grep -q '08'; then \
      echo "${PACKAGE_ONNX_URL} is ${SIZE} bytes but does not start with" >&2; \
      echo "the protobuf tag 0x08, so it is not a serialized ONNX" >&2; \
      echo "ModelProto — a big file of the wrong kind (a .pt checkpoint," >&2; \
      echo "an archive) rather than a truncated or missing one." >&2; \
      exit 1; \
    fi \
 # And the graph really is the detector we think: YOLO exports name
 # their input tensor "images".
 && if ! head -c 256 /yolov8n-package.onnx | grep -qa "images"; then \
      echo "${PACKAGE_ONNX_URL} parses as ONNX but its input tensor is" >&2; \
      echo "not named \"images\", which every YOLO export uses. This is" >&2; \
      echo "some other model — check the tag points at the package" >&2; \
      echo "detector's graph and not another adapter's." >&2; \
      exit 1; \
    fi \
 && echo "Baked yolov8n-package.onnx (${SIZE} bytes)." \
 && apk del wget

LABEL org.opencontainers.image.title="OpenNVR package-detection weights"
LABEL org.opencontainers.image.description="Pre-built yolov8n-package.onnx (opset=12, dynamic, imgsz=416) for the OpenNVR package-detection adapter. YOLOv8n fine-tuned on openly licensed doorstep datasets; test mAP50 0.914."
LABEL org.opencontainers.image.source="https://github.com/open-nvr/open-nvr"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
LABEL org.opencontainers.image.vendor="OpenNVR"
LABEL com.opennvr.package-detection.version="1.0.0"
LABEL com.opennvr.package-detection.opset="12"
LABEL com.opennvr.package-detection.imgsz="416"
LABEL com.opennvr.package-detection.map50="0.914"
