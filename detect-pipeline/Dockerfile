# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# OpenNVR Tier-0 detect-pipeline service.
#
# One Dockerfile, several hardware flavours (OpenNVR Accelerate). ACCEL picks
# the inference runtime wheel and the vendor user-space that goes with it;
# the code is identical in every flavour and the hardware is chosen at run
# time (DETECT_HWACCEL for decode, DETECT_ONNX_PROVIDERS for the detector).
#   cpu   (default)  plain onnxruntime; amd64 + arm64
#   intel            onnxruntime-openvino + Intel OpenCL/NPU runtime
#                    (scripts/install-intel-runtime.sh); amd64 only
#   qualcomm         + onnxruntime-qnn, the Qualcomm AI Runtime plugin provider
#                    (Hexagon NPU on Dragonwing / Snapdragon); arm64 only
#   docker build --build-arg ACCEL=intel -t detect-pipeline:intel .
ARG BASE=python:3.11-slim
FROM ${BASE}
ARG ACCEL=cpu

# ffmpeg/ffprobe: hardware decode + resolution probe of the camera substream.
# intel-media-va-driver is the VA-API (iHD) driver for every Intel iGPU
# since Broadwell, from Debian main. Debian's ffmpeg pulls in Mesa's AMD
# driver by itself but NOT Intel's, so without it DETECT_HWACCEL=vaapi on an
# Intel box fails libva init and silently falls back to software decode —
# while core, reading the same setting, has already promoted the camera to
# the main stream. Verified on Arrow Lake (8086:7d67): H264/HEVC decode,
# no LIBVA_DRIVER_NAME needed. amd64 only — the package does not exist for
# arm64 (no Intel iGPUs there), and the multi-arch build must not fail on it.
# libglib2.0-0: opencv-python-headless runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
         apt-get install -y --no-install-recommends intel-media-va-driver libva-drm2; \
       fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir ".[service]"

# Flavour step. The accelerator wheels CONFLICT with plain onnxruntime (same
# import name), so swap rather than add. Pinned: the OpenVINO EP changes
# device semantics between releases.
# qualcomm is the exception: onnxruntime-qnn >= 2.0 is a PLUGIN provider
# (a library the service registers at startup, see onnx_detector
# .register_plugin_providers), so it sits NEXT TO plain onnxruntime rather
# than replacing it. The wheel bundles the whole QAIRT user-space (libQnnHtp,
# the HTP v68–v81 skels and stubs); the host only lends the fastrpc device
# and its two libraries, see docker-compose.accel-qualcomm.yml. Pinned: the
# plugin tracks a QAIRT release and ORT's plugin API is young.
RUN case "${ACCEL}" in \
      cpu) ;; \
      intel) sh scripts/install-intel-runtime.sh \
             && pip uninstall -y onnxruntime \
             && pip install --no-cache-dir onnxruntime-openvino==1.24.1 ;; \
      qualcomm) pip install --no-cache-dir "onnxruntime>=1.24.2,<2" onnxruntime-qnn==2.7.0 ;; \
      *) echo "unknown ACCEL=${ACCEL} (cpu|intel|qualcomm)" >&2; exit 1 ;; \
    esac
ENV OPENNVR_ACCEL_FLAVOUR=${ACCEL}

# On by default; set DETECT_PIPELINE_ENABLED=false to disable without redeploy.
ENV DETECT_PIPELINE_ENABLED=true
# glibc gives every thread that mallocs its own arena (up to 8 x cores)
# and arenas never shrink: the evidence path decodes 1080p main-stream
# clips in worker threads, and each fetch left the process ~1 MiB larger
# for good — 256 MiB to 932 MiB over 200 fetches on the Ventuno Q
# (2026-11-10) with every buffer in the code bounded and no tracks live.
# Two arenas keep the fragmentation to a plateau; numpy's big buffers go
# through mmap anyway, so throughput is unaffected.
ENV MALLOC_ARENA_MAX=2

# The service holds no ports open — it consumes MediaMTX and publishes to NATS.
CMD ["opennvr-tier0"]
