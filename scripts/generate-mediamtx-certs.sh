#!/usr/bin/env bash
# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
#
# OpenNVR — M1b-fixup H-3: generate a self-signed TLS cert pair for
# MediaMTX's RTSPS / HLS-over-TLS / WebRTC-signaling listeners.
#
# Why this exists
# ---------------
# The hardened MediaMTX templates (mediamtx.docker.yml, mediamtx.yml) ship
# with rtspEncryption="strict", hlsEncryption=yes, webrtcEncryption=yes.
# MediaMTX refuses to start without the matching server.key + server.crt
# files. For a fresh OpenNVR install — especially the `docker compose up`
# quickstart — there is no pre-existing PKI. This script generates a
# locally-trusted, 10-year self-signed cert pair the first time it runs.
#
# Aligned with Zenodo 17261761 §4.2 Tier 2 (TLS/SRTP re-streaming) and
# §4.3 ("certificate-based authentication"). For production deployments
# with a real PKI, replace the generated cert with one issued by your
# internal or public CA.
#
# Usage
# -----
#   scripts/generate-mediamtx-certs.sh           # generate if missing
#   scripts/generate-mediamtx-certs.sh --force   # regenerate, overwriting
#   scripts/generate-mediamtx-certs.sh --out DIR # write to a different dir
#
# Default output directory is `./mediamtx-certs/` at the repo root, and
# the docker-compose volume mount bind-mounts that into MediaMTX's
# working directory.

set -euo pipefail

OUT_DIR="./mediamtx-certs"
FORCE="false"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)
            FORCE="true"
            shift
            ;;
        --out)
            OUT_DIR="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '2,33p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Run with --help for usage." >&2
            exit 64
            ;;
    esac
done

# Resolve OUT_DIR to an absolute path so the success message is unambiguous.
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

CRT="$OUT_DIR/server.crt"
KEY="$OUT_DIR/server.key"

if [[ -f "$CRT" && -f "$KEY" && "$FORCE" != "true" ]]; then
    echo "MediaMTX certs already exist at $OUT_DIR (server.crt + server.key)."
    echo "Run with --force to regenerate."
    exit 0
fi

if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required but not installed." >&2
    echo "Install with: brew install openssl  |  apt-get install openssl" >&2
    exit 69
fi

# Subject Alternative Names: cover loopback, the docker-compose service
# name, and 'localhost'. Operators with a routable hostname should add it
# via the EXTRA_SAN env var (e.g. EXTRA_SAN="DNS:opennvr.lan,IP:10.0.0.5").
SAN="DNS:localhost,DNS:mediamtx,IP:127.0.0.1,IP:::1"
if [[ -n "${EXTRA_SAN:-}" ]]; then
    SAN="$SAN,$EXTRA_SAN"
fi

# Use a tmp openssl config so we can embed the SAN cleanly. This is the
# only portable way that works across openssl 1.1.x and 3.x without
# needing the -addext flag (which is missing on Ubuntu 20.04 LTS).
CONFIG_FILE="$(mktemp)"
trap 'rm -f "$CONFIG_FILE"' EXIT

cat >"$CONFIG_FILE" <<EOF
[req]
default_bits       = 2048
prompt             = no
default_md         = sha256
req_extensions     = req_ext
distinguished_name = dn

[dn]
CN = opennvr-mediamtx

[req_ext]
subjectAltName = $SAN

[v3_ca]
subjectAltName     = $SAN
basicConstraints   = critical, CA:FALSE
keyUsage           = critical, digitalSignature, keyEncipherment
extendedKeyUsage   = serverAuth
EOF

# 10-year validity — internal/private use only, no public revocation.
openssl req -x509 -newkey rsa:2048 -nodes \
    -days 3650 \
    -keyout "$KEY" \
    -out    "$CRT" \
    -config "$CONFIG_FILE" \
    -extensions v3_ca \
    >/dev/null 2>&1

chmod 0600 "$KEY"
chmod 0644 "$CRT"

echo "Generated MediaMTX self-signed cert pair:"
echo "  $CRT"
echo "  $KEY"
echo
echo "SAN: $SAN"
echo
echo "Next steps:"
echo "  - For docker-compose deployments, mount this directory into the"
echo "    mediamtx container at the MediaMTX working directory (typically"
echo "    via a volume in docker-compose.yml under services.mediamtx)."
echo "  - For bare-metal deployments, copy server.crt and server.key next"
echo "    to your mediamtx.yml file (or wherever MediaMTX's working dir is)."
echo "  - Browser clients will see an untrusted-cert warning the first"
echo "    time they connect; for production, replace this cert with one"
echo "    issued by your internal CA."
