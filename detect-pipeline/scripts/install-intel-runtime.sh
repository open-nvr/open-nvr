#!/bin/sh
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Intel user-space runtime for the ``intel`` image flavour (ACCEL=intel):
#   - OpenCL compute runtime + graphics compiler  -> OpenVINO's GPU device (iGPU / Arc)
#   - Level Zero loader + NPU driver and compiler -> OpenVINO's NPU device (Core Ultra)
# Debian carries none of the compute runtime, so these come from Intel's own
# GitHub releases, pinned. amd64 only: there are no Intel iGPUs on arm64.
# The NPU *firmware* lives on the host (linux-firmware, loaded by the ivpu
# kernel driver), not here; the container only needs the user-space half.
set -eu
[ "$(dpkg --print-architecture)" = "amd64" ] || { echo "intel runtime: amd64 only, skipping"; exit 0; }

LEVEL_ZERO=1.34.0
GMM=22.10.0
# IGC is pinned BY the compute runtime (Depends: intel-igc-opencl-2 = this
# version); take the pair from the compute-runtime release notes, never latest.
IGC=2.41.5
IGC_BUILD=22716
NEO=26.35.39758.10
NPU=1.38.0.20260910-34487311128

GH=https://github.com
tmp=$(mktemp -d)
cd "$tmp"
# python:*-slim ships neither curl nor wget; urllib is already there.
get() {
  echo "  fetching $(basename "$2")"
  python3 - "$1" "$2" <<'PYEOF'
import sys, urllib.request
url, out = sys.argv[1], sys.argv[2]
for attempt in range(3):
    try:
        urllib.request.urlretrieve(url, out); break
    except Exception as exc:
        if attempt == 2: raise
        print(f"  retry {attempt + 1}: {exc}")
PYEOF
}

get "$GH/oneapi-src/level-zero/releases/download/v$LEVEL_ZERO/libze1_${LEVEL_ZERO}%2Bu24.04_amd64.deb"      libze1.deb
get "$GH/intel/compute-runtime/releases/download/$NEO/libigdgmm12_${GMM}_amd64.deb"                             gmm.deb
get "$GH/intel/intel-graphics-compiler/releases/download/v$IGC/intel-igc-core-2_${IGC}%2B${IGC_BUILD}_amd64.deb"   igc-core.deb
get "$GH/intel/intel-graphics-compiler/releases/download/v$IGC/intel-igc-opencl-2_${IGC}%2B${IGC_BUILD}_amd64.deb" igc-opencl.deb
get "$GH/intel/compute-runtime/releases/download/$NEO/intel-opencl-icd_${NEO}-0_amd64.deb"                     opencl-icd.deb
get "$GH/intel/linux-npu-driver/releases/download/v${NPU%%.2*}/linux-npu-driver-v${NPU}-ubuntu2404.tar.gz"     npu.tar.gz
mkdir npu && tar -xzf npu.tar.gz -C npu

export DEBIAN_FRONTEND=noninteractive
apt-get update
# One apt transaction for all the local debs: apt resolves the Debian deps the
# Ubuntu-built packages declare (libtbb12, hwloc ...) and FAILS on a conflict.
# (dpkg -i followed by apt-get -f install "fixes" a conflict by removing the
# package — which is how an image can end up with no OpenCL ICD and exit 0.)
apt-get install -y --no-install-recommends ocl-icd-libopencl1 clinfo \
    ./libze1.deb ./gmm.deb ./igc-core.deb ./igc-opencl.deb ./opencl-icd.deb \
    ./npu/intel-level-zero-npu_*.deb ./npu/intel-driver-compiler-npu_*.deb
# Prove the pieces OpenVINO needs are really there — or fail the build:
# the OpenCL ICD (GPU device), the graphics compiler it loads (lands in
# /usr/local/lib), and the Level Zero NPU driver + its compiler (NPU device).
test -f /etc/OpenCL/vendors/intel.icd
ls /usr/local/lib/libigc.so* >/dev/null
ls /usr/lib/x86_64-linux-gnu/libze_intel_npu.so* >/dev/null
ls /usr/lib/x86_64-linux-gnu/libopenvino_intel_npu_compiler.so >/dev/null
rm -rf /var/lib/apt/lists/* "$tmp"
echo "intel runtime installed: level-zero $LEVEL_ZERO, compute-runtime $NEO, igc $IGC, npu $NPU"
