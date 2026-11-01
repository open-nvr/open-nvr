#!/usr/bin/env bash
# Regression tests for start.sh writing what it resolved into .env.
#
# The field bug (Ventuno Q, 2026-11-01): start.sh resolved OPENNVR_ACCEL=auto
# into the qualcomm overlay and three DETECT_* exports that lived only in its
# own process. The test team then ran `docker compose -f docker-compose.yml
# -f docker-compose.fakecams.yml up -d fakecams` and `... up -d <app>` by
# hand; Compose rebuilt every service whose config differed from the running
# one — the detect-pipeline and the yolov8 adapter — as the plain cpu image,
# with no NPU device. Sixteen cameras that ran on the Hexagon went to the
# CPU, the host sat at 99 %, and the health checks still said "accelerator
# in use".
#
# The rules these tests defend:
#   * persist_compose_set writes the -f files and --profile names of the
#     resolved command line as COMPOSE_FILE / COMPOSE_PROFILES, the keys
#     Compose reads from .env on every invocation in the checkout.
#   * apply_accel_env PERSISTS what it fills (set_env_var), and only values
#     the operator left at the default.
#   * reset_stale_accel_fills puts the CPU default back in a value the
#     PREVIOUS flavour filled when the flavour changes, and leaves a value
#     the operator typed alone.
#   * run_prune keeps the release in .env (every flavour), the weights
#     images and anything a container references; everything else of ours goes.
set -u
. "$(dirname "$0")/_lib.sh"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running start.sh persistence tests"
echo ""

# The REAL functions, pulled out of start.sh so a drift fails here.
extract() { awk "/^$1\\(\\) \\{/,/^\\}/" start.sh; }
FUNCS=""
for fn in get_env_var write_env_var set_env_var persist_compose_set reset_stale_accel_fills apply_accel_env run_prune; do
    body=$(extract "$fn")
    if [[ -z "$body" ]]; then
        echo "✗ could not extract $fn from start.sh" >&2
        exit 2
    fi
    FUNCS="$FUNCS
$body"
done

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK" || exit 1
YELLOW=''; NC=''; CYAN=''; GRAY=''; GREEN=''; BRIGHT_CYAN=''
eval "$FUNCS"

# ── persist_compose_set ──────────────────────────────────────────────
start_test "persist_compose_set writes COMPOSE_FILE and COMPOSE_PROFILES from the resolved args"
: > .env
persist_compose_set "-f docker-compose.yml -f docker-compose.camera-agent.yml --profile camera-agent -f docker-compose.accel-qualcomm.yml -f docker-compose.apps.yml --profile default-apps"
if [[ "$(get_env_var COMPOSE_FILE)" == "docker-compose.yml:docker-compose.camera-agent.yml:docker-compose.accel-qualcomm.yml:docker-compose.apps.yml" \
   && "$(get_env_var COMPOSE_PROFILES)" == "camera-agent,default-apps" \
   && "$(get_env_var COMPOSE_PATH_SEPARATOR)" == ":" ]]; then pass
else fail "got COMPOSE_FILE=$(get_env_var COMPOSE_FILE) COMPOSE_PROFILES=$(get_env_var COMPOSE_PROFILES)"; fi

start_test "persist_compose_set replaces a previous set rather than appending a duplicate"
persist_compose_set "-f docker-compose.yml"
if [[ "$(grep -c '^COMPOSE_FILE=' .env)" == "1" && "$(get_env_var COMPOSE_FILE)" == "docker-compose.yml" \
   && "$(get_env_var COMPOSE_PROFILES)" == "" ]]; then pass
else fail "$(grep '^COMPOSE_' .env | tr '\n' ' ')"; fi

# ── apply_accel_env persists its fills ───────────────────────────────
# detect_accel is start.sh's host probe; the tests decide the flavour.
start_test "qualcomm fills DETECT_ONNX_BACKEND / DETECT_ONNX_PROVIDERS into .env, not just the environment"
printf 'DETECT_ONNX_BACKEND=auto\nDETECT_ONNX_PROVIDERS=\nDETECT_HWACCEL=cpu\n' > .env
detect_accel() { echo qualcomm; }
getent() { echo "fastrpc:x:125:arduino"; }
apply_accel_env >/dev/null 2>&1
if [[ "$(get_env_var DETECT_ONNX_BACKEND)" == "ort" && "$(get_env_var DETECT_ONNX_PROVIDERS)" == "QNNExecutionProvider:htp" \
   && "$(get_env_var OPENNVR_ACCEL_RESOLVED)" == "qualcomm" && "$(get_env_var FASTRPC_GID)" == "125" ]]; then pass
else fail "$(cat .env | tr '\n' ' ')"; fi

start_test "an operator's own provider list is never overwritten by the flavour"
printf 'DETECT_ONNX_BACKEND=ort\nDETECT_ONNX_PROVIDERS=QNNExecutionProvider:cpu\n' > .env
apply_accel_env >/dev/null 2>&1
if [[ "$(get_env_var DETECT_ONNX_PROVIDERS)" == "QNNExecutionProvider:cpu" ]]; then pass
else fail "got $(get_env_var DETECT_ONNX_PROVIDERS)"; fi

# ── reset_stale_accel_fills ──────────────────────────────────────────
start_test "a flavour change resets the values the previous flavour filled"
printf 'OPENNVR_ACCEL_RESOLVED=qualcomm\nDETECT_ONNX_BACKEND=ort\nDETECT_ONNX_PROVIDERS=QNNExecutionProvider:htp\nDETECT_HWACCEL=cpu\n' > .env
detect_accel() { echo cpu; }
apply_accel_env >/dev/null 2>&1
if [[ "$(get_env_var DETECT_ONNX_BACKEND)" == "auto" && "$(get_env_var DETECT_ONNX_PROVIDERS)" == "" \
   && "$(get_env_var OPENNVR_ACCEL_RESOLVED)" == "cpu" ]]; then pass
else fail "$(cat .env | tr '\n' ' ')"; fi

start_test "a flavour change leaves a value the operator typed alone"
printf 'OPENNVR_ACCEL_RESOLVED=intel\nDETECT_HWACCEL=qsv\nDETECT_ONNX_PROVIDERS=OpenVINOExecutionProvider:CPU\n' > .env
detect_accel() { echo cpu; }
apply_accel_env >/dev/null 2>&1
if [[ "$(get_env_var DETECT_HWACCEL)" == "qsv" && "$(get_env_var DETECT_ONNX_PROVIDERS)" == "OpenVINOExecutionProvider:CPU" ]]; then pass
else fail "$(cat .env | tr '\n' ' ')"; fi

start_test "intel → qualcomm: vaapi decode goes back to cpu, the provider is refilled for the HTP"
printf 'OPENNVR_ACCEL_RESOLVED=intel\nDETECT_HWACCEL=vaapi\nDETECT_ONNX_BACKEND=ort\nDETECT_ONNX_PROVIDERS=OpenVINOExecutionProvider:GPU\n' > .env
detect_accel() { echo qualcomm; }
apply_accel_env >/dev/null 2>&1
if [[ "$(get_env_var DETECT_HWACCEL)" == "cpu" && "$(get_env_var DETECT_ONNX_PROVIDERS)" == "QNNExecutionProvider:htp" \
   && "$(get_env_var DETECT_ONNX_BACKEND)" == "ort" ]]; then pass
else fail "$(cat .env | tr '\n' ' ')"; fi

# ── run_prune ────────────────────────────────────────────────────────
start_test "run_prune removes only our tags from other releases"
printf 'CORE_TAG=0.1.10\nADAPTER_TAG=0.1.10\n' > .env
REMOVED=""
docker() {
    case "$1 $2" in
        "images --format")
            printf '%s\n' \
                ghcr.io/open-nvr/core:0.1.10 ghcr.io/open-nvr/core:0.1.8 \
                ghcr.io/open-nvr/detect-pipeline:0.1.10-qualcomm ghcr.io/open-nvr/detect-pipeline:main-qualcomm \
                ghcr.io/open-nvr/yolov8-adapter:0.1.10-qualcomm ghcr.io/open-nvr/yolov8-adapter:latest-qualcomm \
                ghcr.io/open-nvr/yolov8-weights:v8.4.0-yolo26n ghcr.io/open-nvr/piper-adapter:0.1.8 \
                nginx:1.27-alpine postgres:15-alpine ;;
        "ps -a") printf '%s\n' ghcr.io/open-nvr/piper-adapter:0.1.8 ;;   # still referenced by a container
        "info --format") echo /var/lib/docker ;;
        "rmi "*) shift; REMOVED="$*" ;;
        "image prune") ;;
    esac
}
df() { printf 'Filesystem Size Used Avail Use%% Mounted\n/dev/x 55G 31G 22G 59%% /\n'; }
run_prune >/dev/null 2>&1
want="ghcr.io/open-nvr/core:0.1.8 ghcr.io/open-nvr/detect-pipeline:main-qualcomm ghcr.io/open-nvr/yolov8-adapter:latest-qualcomm"
if [[ "$REMOVED" == "$want" ]]; then pass
else fail "removed: '$REMOVED' wanted: '$want'"; fi

echo ""
if (( TESTS_FAILED > 0 )); then
    echo "✗ ${TESTS_FAILED} of ${TESTS_RUN} tests failed"
    exit 1
fi
echo "✓ all ${TESTS_RUN} tests passed"
