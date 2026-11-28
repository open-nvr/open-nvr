#!/bin/sh
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Cross-compiles the host agent for every host OS into OUT (default /out),
# stamped with the build core reports: "<version>+<commit>", or "<version>".
#   sh build.sh <version> [commit]
# Run by the core Dockerfile; a script rather than an inline RUN so BuildKit
# never rewrites the shell's own ${...} expansions.
#
# With a signing key (SIGNING_KEY_FILE, default /run/secrets/agent_signing_key,
# a BuildKit secret CI passes), it also writes OUT/manifest.json and
# OUT/manifest.sig: what site agents check before they update themselves
# (internal/update). Without one (a fork, a local build) the builds are
# unsigned, and site agents never update themselves to them.
set -eu
# No whitespace of any kind: a checkout with CRLF line endings (Windows)
# hands over "0.1.10\r", and a \r inside -ldflags splits the -X value, so the
# linker sees a stray argument and fails with nothing but its usage text.
VERSION=$(printf '%s' "$1" | tr -d '\r\n\t ')
COMMIT=$(printf '%s' "${2:-}" | tr -d '\r\n\t ')
OUT="${OUT:-/out}"
SIGNING_KEY_FILE="${SIGNING_KEY_FILE:-/run/secrets/agent_signing_key}"
# A hash of the agent's own sources: a site agent updates only to a build
# whose agent code differs from its own, so a core commit that did not
# touch the agent does not make every site download it again.
SRC_HASH=$(cd "$(dirname "$0")" && find . -name '*.go' -o -name 'go.mod' -o -name 'go.sum' | sort | xargs cat | sha256sum | cut -c1-16)
BUILT_AT="${SOURCE_DATE_EPOCH:-$(date +%s)}"
BUILD="$VERSION"
if [ -n "$COMMIT" ]; then
    BUILD="$VERSION+$COMMIT"
else
    # No commit (a local `start.sh build`): stamp a hash of the sources, so
    # an installed agent still sees a NEW build after a code change and
    # replaces itself — the bare version alone made every dev build equal.
    BUILD="$VERSION+src$(printf '%s' "$SRC_HASH" | cut -c1-8)"
fi
PKG=github.com/open-nvr/open-nvr/host-agent/internal/buildinfo
STAMP="-X $PKG.Build=$BUILD -X $PKG.Source=$SRC_HASH -X $PKG.BuiltAt=$BUILT_AT"

# linux/arm is 32-bit Raspberry Pi OS (ARMv7: Pi 2 and later); GOARM=7 below.
for t in windows/amd64 windows/arm64 linux/amd64 linux/arm64 linux/arm darwin/amd64 darwin/arm64; do
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
    CGO_ENABLED=0 GOOS=$os GOARCH=$arch GOARM=7 go build -trimpath \
        -ldflags "$flags $STAMP" \
        -o "$OUT/$os-$arch/opennvr-host$ext" ./cmd/opennvr-host
    if [ "$os" = windows ]; then
        # The same program as a console one, for site agents (docs/SITE_AGENT.md):
        # run from a terminal, a GUI-subsystem program prints nothing and
        # PowerShell does not wait for it, so `enrol` seemed to do nothing.
        CGO_ENABLED=0 GOOS=$os GOARCH=$arch GOARM=7 go build -trimpath \
            -ldflags "-s -w $STAMP" \
            -o "$OUT/$os-$arch/console/opennvr-host$ext" ./cmd/opennvr-host
    fi
done
printf '%s\n' "$BUILD" > "$OUT/BUILD"

if [ "${AGENT_SIGNING:-off}" = on ] && [ ! -s "$SIGNING_KEY_FILE" ]; then
    echo "AGENT_SIGNING=on but no signing key at $SIGNING_KEY_FILE: refusing to ship unsigned agent builds" >&2
    exit 1
fi
if [ -s "$SIGNING_KEY_FILE" ]; then
    echo "signing the agent builds"
    go run ./cmd/opennvr-sign sign -key "$SIGNING_KEY_FILE" -dir "$OUT" \
        -build "$BUILD" -version "$VERSION" -source "$SRC_HASH" -built-at "$BUILT_AT"
else
    rm -f "$OUT/manifest.json" "$OUT/manifest.sig"
    echo "no signing key: these agent builds are unsigned (site agents do not update themselves to them)"
fi
