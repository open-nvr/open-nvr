#!/bin/sh
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Cross-compiles the host agent for every host OS into OUT (default /out),
# stamped with the build core reports: "<version>+<commit>", or "<version>".
#   sh build.sh <version> [commit]
# Run by the core Dockerfile; a script rather than an inline RUN so BuildKit
# never rewrites the shell's own ${...} expansions.
set -eu
# No whitespace of any kind: a checkout with CRLF line endings (Windows)
# hands over "0.1.10\r", and a \r inside -ldflags splits the -X value, so the
# linker sees a stray argument and fails with nothing but its usage text.
VERSION=$(printf '%s' "$1" | tr -d '\r\n\t ')
COMMIT=$(printf '%s' "${2:-}" | tr -d '\r\n\t ')
OUT="${OUT:-/out}"
BUILD="$VERSION"
if [ -n "$COMMIT" ]; then
    BUILD="$VERSION+$COMMIT"
else
    # No commit (a local `start.sh build`): stamp a hash of the sources, so
    # an installed agent still sees a NEW build after a code change and
    # replaces itself — the bare version alone made every dev build equal.
    SRC_HASH=$(cd "$(dirname "$0")" && find . -name '*.go' -o -name 'go.mod' -o -name 'go.sum' | sort | xargs cat | sha256sum | cut -c1-8)
    BUILD="$VERSION+src$SRC_HASH"
fi
PKG=github.com/open-nvr/open-nvr/host-agent/internal/buildinfo

for t in windows/amd64 windows/arm64 linux/amd64 linux/arm64 darwin/amd64 darwin/arm64; do
    os=${t%/*}
    arch=${t#*/}
    ext=""
    flags="-s -w"
    if [ "$os" = windows ]; then
        ext=.exe
        # GUI subsystem: no console window when the logon task starts it.
        flags="$flags -H windowsgui"
    fi
    echo "building $t ($BUILD)"
    CGO_ENABLED=0 GOOS=$os GOARCH=$arch go build -trimpath \
        -ldflags "$flags -X $PKG.Build=$BUILD" \
        -o "$OUT/$os-$arch/opennvr-host$ext" ./cmd/opennvr-host
done
printf '%s\n' "$BUILD" > "$OUT/BUILD"
