#!/usr/bin/env bash
# ============================================================
# Tests for the media-plane proxy paths (ISSUE-6 v8):
#   * nginx /webrtc/, /hls/, /playback/ proxy to mediamtx
#   * docker-compose tier0 emits HTTPS URLs through nginx
#   * recordings.py:get_playback_url uses the external URL chain
#     (regression test for the bug where it emitted the internal
#     Docker URL, breaking recording playback on LAN browsers)
#   * WebRTC ICE UDP port is published on NGINX_BIND_HOST
#
# Run with: bash tests/host-hardening/test_media_proxy.sh
# ============================================================

set -u

. "$(dirname "$0")/_lib.sh"
require_python_yaml

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running media-proxy tests"
echo ""

NGINX_CONF="${REPO_ROOT}/nginx/opennvr.conf"
COMPOSE_TIER0="${REPO_ROOT}/docker-compose.tier0.yml"
RECORDINGS_PY="${REPO_ROOT}/server/routers/recordings.py"

# ── 1-3. nginx config has the three media proxy locations ────
for kind in webrtc hls playback; do
    start_test "nginx config has location /${kind}/ block"
    if grep -qE "location[[:space:]]+/${kind}/[[:space:]]*\{" "$NGINX_CONF"; then
        pass
    else
        fail "missing 'location /${kind}/ {' block in $NGINX_CONF"
    fi
done

# ── 4-6. each proxy_passes to the right mediamtx port ────────
# Scheme-agnostic port check (the scheme-aware checks live below
# in tests 13-16 — they verify proxy_pass scheme matches mediamtx's
# own TLS posture per V-019).
#
# ISSUE-19: previously used `declare -A` (bash 4+ associative array)
# which doesn't exist on macOS's bundled /bin/bash 3.2 (Apple won't
# ship the GPLv3 bash 4+). Replaced with a case statement so this
# test runs on every operator's machine.
port_for() {
    case "$1" in
        webrtc)   echo 8889 ;;
        hls)      echo 8888 ;;
        playback) echo 9996 ;;
        *)        echo "" ;;
    esac
}
for kind in webrtc hls playback; do
    port=$(port_for "$kind")
    start_test "nginx /${kind}/ proxies to mediamtx:${port}"
    block=$(awk -v k="/${kind}/" '
        $0 ~ "location[[:space:]]+"k {flag=1}
        flag {print; if ($0 ~ /^[[:space:]]*\}/) {flag=0}}
    ' "$NGINX_CONF")
    if echo "$block" | grep -qE "proxy_pass[[:space:]]+https?://mediamtx:${port}/"; then
        pass
    else
        fail "expected proxy_pass http(s)://mediamtx:${port}/ in /${kind}/ block"
    fi
done

# ── 7. Tier 0 compose emits HTTPS URLs through nginx ─────────
start_test "Tier 0 MEDIAMTX_EXTERNAL_* URLs go through nginx (HTTPS, sub-paths)"
core_env=$(python3 - <<PY
import yaml
c = yaml.safe_load(open("${COMPOSE_TIER0}"))
env = c["services"]["opennvr-core"]["environment"]
for e in env:
    if "EXTERNAL_BASE_URL" in str(e) or "EXTERNAL_HLS_URL" in str(e) or "EXTERNAL_PLAYBACK_URL" in str(e):
        print(e)
PY
)
if echo "$core_env" | grep -q "MEDIAMTX_PUBLIC_URL.*\/webrtc" \
   && echo "$core_env" | grep -q "MEDIAMTX_PUBLIC_URL.*\/hls" \
   && echo "$core_env" | grep -q "MEDIAMTX_PUBLIC_URL.*\/playback"; then
    pass
else
    fail "Tier 0 compose external URLs must interpolate MEDIAMTX_PUBLIC_URL with sub-paths; got: ${core_env}"
fi

# ── 8. WebRTC UDP+TCP ports published on NGINX_BIND_HOST ────
start_test "WebRTC ICE port 8189 published on NGINX_BIND_HOST (uplink-side)"
result=$(python3 - <<PY
import yaml
c = yaml.safe_load(open("${COMPOSE_TIER0}"))
ports = c["services"]["mediamtx"]["ports"]
udp = any("8189:8189/udp" in str(p) and "NGINX_BIND_HOST" in str(p) for p in ports)
tcp = any("8189:8189/tcp" in str(p) and "NGINX_BIND_HOST" in str(p) for p in ports)
print(f"udp={udp} tcp={tcp}")
PY
)
if echo "$result" | grep -q "udp=True" && echo "$result" | grep -q "tcp=True"; then
    pass
else
    fail "WebRTC port 8189 must be published on NGINX_BIND_HOST for UDP and TCP; got: ${result}"
fi

# ── 9. mediamtx advertises ICE candidates via MTX_WEBRTCADDITIONALHOSTS ──
start_test "mediamtx env has MTX_WEBRTCADDITIONALHOSTS interpolated from MEDIAMTX_WEBRTC_HOSTS"
mtx_env=$(python3 - <<PY
import yaml
c = yaml.safe_load(open("${COMPOSE_TIER0}"))
env = c["services"]["mediamtx"]["environment"]
for e in env:
    if "WEBRTC" in str(e):
        print(e)
PY
)
if echo "$mtx_env" | grep -q "MTX_WEBRTCADDITIONALHOSTS" \
   && echo "$mtx_env" | grep -q "MEDIAMTX_WEBRTC_HOSTS"; then
    pass
else
    fail "mediamtx env must wire MTX_WEBRTCADDITIONALHOSTS from MEDIAMTX_WEBRTC_HOSTS; got: ${mtx_env}"
fi

# ── 10. opennvr-core's 127.0.0.1:8000 binding is unchanged (no regression) ──
start_test "opennvr-core still binds host port 127.0.0.1:8000 (V-015 trust zone intact)"
result=$(python3 - <<PY
import yaml
c = yaml.safe_load(open("${COMPOSE_TIER0}"))
print("127.0.0.1:8000:8000" in c["services"]["opennvr-core"]["ports"])
PY
)
if echo "$result" | grep -q "True"; then
    pass
else
    fail "opennvr-core host port should still be loopback-only; got: ${result}"
fi

# ── 11. recordings.py uses the external playback URL chain (bug fix) ──
start_test "recordings.py:get_playback_url uses mediamtx_external_playback_url fallback"
# The bug was that recordings.py used settings.mediamtx_playback_url
# directly (Docker-internal), which the browser cannot reach. The
# fix must use the same external-first fallback chain as streams.py.
if grep -q "settings.mediamtx_external_playback_url" "$RECORDINGS_PY"; then
    # And the get_playback_url function specifically must use the
    # fallback variable, not the bare internal URL. awk range
    # patterns: skip the opening async-def line so the closing
    # pattern doesn't match the same line.
    if awk '/^async def get_playback_url/{f=1; next} f && /^async def /{f=0} f' \
            "$RECORDINGS_PY" | grep -q "playback_base"; then
        pass
    else
        fail "get_playback_url body must use playback_base from the fallback chain"
    fi
else
    fail "recordings.py must reference mediamtx_external_playback_url (regression: was using internal URL)"
fi

start_test "recordings.py:get_playback_url no longer emits bare mediamtx_playback_url to browser"
# Ensure the bare reference inside get_playback_url is gone (the
# bug). The setting is still allowed for server-side internal calls
# (list/health/etc.) — we only care about the playback-URL path
# the browser consumes.
bad=$(awk '/^async def get_playback_url/,/^async def [a-z_]+/' "$RECORDINGS_PY" \
       | grep -E "\{settings\.mediamtx_playback_url\}/get" || true)
if [ -z "$bad" ]; then
    pass
else
    fail "get_playback_url still emits the internal URL directly: ${bad}"
fi

# ── Self-review M-1: scheme of proxy_pass matches upstream's TLS posture ──
# mediamtx.docker.yml sets hlsEncryption=yes and webrtcEncryption=yes
# (V-019), so the HLS/WebRTC ports serve HTTPS, not HTTP. Playback
# stays plaintext (playbackEncryption=no). nginx's proxy_pass scheme
# MUST match — otherwise nginx tries plain HTTP against a TLS port
# (or vice versa) and the connection fails at runtime. This caught a
# real bug in v8 that the static "location-block-present" test would
# have missed.
start_test "nginx /webrtc/ uses HTTPS upstream (matches mediamtx webrtcEncryption=yes)"
block=$(awk -v k="/webrtc/" '
    $0 ~ "location[[:space:]]+"k {flag=1}
    flag {print; if ($0 ~ /^[[:space:]]*\}/) {flag=0}}
' "$NGINX_CONF")
if echo "$block" | grep -qE "proxy_pass[[:space:]]+https://mediamtx:8889/"; then
    pass
else
    fail "/webrtc/ proxy_pass must be https:// because mediamtx serves TLS on :8889"
fi

start_test "nginx /hls/ uses HTTPS upstream (matches mediamtx hlsEncryption=yes)"
block=$(awk -v k="/hls/" '
    $0 ~ "location[[:space:]]+"k {flag=1}
    flag {print; if ($0 ~ /^[[:space:]]*\}/) {flag=0}}
' "$NGINX_CONF")
if echo "$block" | grep -qE "proxy_pass[[:space:]]+https://mediamtx:8888/"; then
    pass
else
    fail "/hls/ proxy_pass must be https:// because mediamtx serves TLS on :8888"
fi

start_test "nginx /playback/ uses HTTP upstream (mediamtx playbackEncryption=no)"
block=$(awk -v k="/playback/" '
    $0 ~ "location[[:space:]]+"k {flag=1}
    flag {print; if ($0 ~ /^[[:space:]]*\}/) {flag=0}}
' "$NGINX_CONF")
if echo "$block" | grep -qE "proxy_pass[[:space:]]+http://mediamtx:9996/"; then
    pass
else
    fail "/playback/ proxy_pass must be http:// (no S) because mediamtx playback is plaintext on the bridge"
fi

start_test "TLS upstreams disable cert verification (self-signed bridge cert)"
# Both /webrtc/ and /hls/ point at mediamtx's self-signed cert.
# proxy_ssl_verify off is required for the connection to succeed.
for kind in webrtc hls; do
    block=$(awk -v k="/${kind}/" '
        $0 ~ "location[[:space:]]+"k {flag=1}
        flag {print; if ($0 ~ /^[[:space:]]*\}/) {flag=0}}
    ' "$NGINX_CONF")
    if ! echo "$block" | grep -q "proxy_ssl_verify off"; then
        fail "/${kind}/ must have proxy_ssl_verify off (self-signed bridge cert)"
        continue 2
    fi
done
pass

# ── 12. nginx proxy locations carry X-Forwarded-Proto https ─
start_test "all three media proxy blocks set X-Forwarded-Proto https"
for kind in webrtc hls playback; do
    block=$(awk -v k="/${kind}/" '
        $0 ~ "location[[:space:]]+"k {flag=1}
        flag {print; if ($0 ~ /^[[:space:]]*\}/) {flag=0}}
    ' "$NGINX_CONF")
    if ! echo "$block" | grep -q "X-Forwarded-Proto[[:space:]]\+https"; then
        fail "/${kind}/ block missing X-Forwarded-Proto https"
        continue 2
    fi
done
pass

# ── 18. nginx depends on mediamtx (ISSUE-21) ─────────────────
# nginx's opennvr.conf uses ``proxy_pass https://mediamtx:8889/`` (and
# :8888, :9996) which nginx resolves at CONFIG LOAD time, not at request
# time. Without an explicit ``depends_on: mediamtx``, nginx can race
# ahead of the bridge DNS being populated with mediamtx's hostname and
# crash-loop forever with ``host not found in upstream "mediamtx"``.
# On a clean first boot the opennvr-core dependency usually wins this
# race by accident (it waits on mediamtx via service_healthy too), but
# on recovery boots (post compose-file switch + stale Docker networks)
# the race surfaces. Lock the dependency explicitly.
start_test "tier0 nginx service depends_on: mediamtx with service_healthy"
dep_check=$(python3 - "${REPO_ROOT}" <<'PY'
import sys, yaml
from pathlib import Path
c = yaml.safe_load((Path(sys.argv[1]) / "docker-compose.tier0.yml").read_text())
nginx = c["services"]["nginx"]
deps = nginx.get("depends_on", {})
if not isinstance(deps, dict):
    print(f"depends_on is not a dict: {deps!r}")
    sys.exit(1)
if "mediamtx" not in deps:
    print("nginx.depends_on is missing mediamtx")
    sys.exit(1)
cond = deps["mediamtx"].get("condition")
if cond != "service_healthy":
    print(f"mediamtx condition is {cond!r} (expected service_healthy)")
    sys.exit(1)
print("ok")
PY
)
if echo "$dep_check" | grep -q "^ok"; then
    pass
else
    fail "nginx must depend on mediamtx (service_healthy): ${dep_check}
Without this, nginx can start before mediamtx is registered in the
Docker bridge DNS and crash-loop with ``host not found in upstream
\"mediamtx\"`` at config load time."
fi

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────"
echo "Tests run    : ${TESTS_RUN}"
echo "Tests failed : ${TESTS_FAILED}"
if [ "$TESTS_FAILED" -eq 0 ]; then echo "Result       : all green"; exit 0
else echo "Result       : failures"; exit 1; fi
