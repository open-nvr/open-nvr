#!/usr/bin/env bash
# Regression tests for the installer's app menu and the multi-app
# OPENNVR_EXAMPLE_* wiring it persists.
#
# The field bug: the menu walked examples/*/ and stamped
# "[no Compose manifest]" on anything without its own compose file. That
# was every catalog app (they share docker-compose.apps.yml), two
# weight-baking helper images and two SDK tutorials that are not apps —
# eighteen rows, one installable. An operator who wanted License Plate
# Recognition on first start was told it could not be installed.
#
# The rules these tests defend:
#   * the menu IS the App Catalog: exactly the apps_index.yml entries whose
#     id is a service in docker-compose.apps.yml, plus the Camera Agent.
#   * helper images and SDK tutorials never appear.
#   * every catalog app carries its own id as a compose profile, on every
#     service it depends on, so picking one starts one.
#   * several picks persist as comma lists and start.sh replays each one.
set -u

. "$(dirname "$0")/_lib.sh"
require_python_yaml

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running installer apps-menu tests"
echo ""

eval "$(awk '/^find_example_compose\(\)/,/^}/' scripts/install.sh)"
eval "$(awk '/^catalog_apps\(\)/,/^}/' scripts/install.sh)"
if ! declare -F catalog_apps >/dev/null; then
    echo "✗ could not extract catalog_apps from scripts/install.sh" >&2
    exit 1
fi

# ── 1. the catalog reader agrees with the catalog ──
start_test "catalog_apps lists exactly the installable apps_index.yml entries"
expected=$(python3 - <<'PY'
import yaml, re
idx = yaml.safe_load(open("server/config/apps_index.yml"))
svcs = yaml.safe_load(open("docker-compose.apps.yml"))["services"]
print("\n".join(e["id"] for e in idx if e["id"] in svcs))
PY
)
actual=$(catalog_apps | cut -f1)
if [ "$expected" = "$actual" ] && [ -n "$actual" ]; then pass; else
    fail "expected:\n$expected\ngot:\n$actual"; fi

# ── 2. non-apps under examples/ never reach the menu ──
start_test "weight helpers and SDK tutorials are not offered"
bad=""
for d in yolo-pose-weights yolov8-weights alerts-subscriber inference-listener; do
    catalog_apps | cut -f1 | grep -qx "$d" && bad="$bad $d"
done
if [ -z "$bad" ]; then pass; else fail "offered:$bad"; fi

# ── 3. every row has a name and a summary ──
start_test "every row carries a name and a summary"
short=$(catalog_apps | awk -F'\t' 'NF != 3 || $2 == "" || $3 == ""')
if [ -z "$short" ]; then pass; else fail "$short"; fi

# ── 4. one profile per app, on every service it needs ──
start_test "each catalog app's profile covers the app, its init, adapters and egress-proxy"
missing=$(python3 - <<'PY'
import yaml
d = yaml.safe_load(open("docker-compose.apps.yml"))["services"]
idx = yaml.safe_load(open("server/config/apps_index.yml"))
out = []
for e in idx:
    app = e["id"]
    if app not in d: continue
    need = {app}
    stack = [app]
    while stack:
        s = stack.pop()
        for dep in (d[s].get("depends_on") or {}):
            if dep in d and dep not in need:
                need.add(dep); stack.append(dep)
    for s in sorted(need):
        if app not in (d[s].get("profiles") or []):
            out.append(f"{s} lacks profile {app}")
print("\n".join(out))
PY
)
if [ -z "$missing" ]; then pass; else fail "$missing"; fi

# ── 5. the menu persists picks as comma lists ──
menu_run() {
    # Drive choose_example up to the point the Camera Agent's own prompts
    # would begin, with every prompt stubbed, and print what it persisted.
    printf '%s\n' "$1" | bash -c '
        set -u
        info() { :; }; ok() { :; }; warn() { :; }
        die() { printf "DIE:%s\n" "$*"; exit 1; }
        ask_yes_no() { return 0; }; env_set() { :; }
        eval "$(awk "/^find_example_compose\(\)/,/^}/" scripts/install.sh)"
        eval "$(awk "/^catalog_apps\(\)/,/^}/" scripts/install.sh)"
        body=$(awk "/^choose_example\(\) \{/{f=1} f{print} /name=\"camera-agent\"\$/{if(f) exit}" scripts/install.sh)
        eval "$body"$'"'"'\n printf "%s|%s|%s\\n" "$EXAMPLE_NAME" "$EXAMPLE_COMPOSE" "$EXAMPLE_PROFILE"; }'"'"'
        choose_example' 2>&1 | tail -1
}
lpr_n=$( { echo camera-agent; catalog_apps | cut -f1; } | grep -n -x license-plate-recognition | cut -d: -f1)
db_n=$( { echo camera-agent; catalog_apps | cut -f1; } | grep -n -x smart-doorbell | cut -d: -f1)

start_test "Enter picks the Camera Agent alone"
got=$(menu_run "")
if [ "$got" = "camera-agent|docker-compose.camera-agent.yml|camera-agent" ]; then pass; else fail "got: $got"; fi

start_test "two catalog apps share one overlay and get one profile each"
got=$(menu_run "$lpr_n, $db_n")
if [ "$got" = "license-plate-recognition,smart-doorbell|docker-compose.apps.yml|license-plate-recognition,smart-doorbell" ]; then pass; else fail "got: $got"; fi

start_test "agent plus a catalog app carries both overlays; repeats and 0 are ignored"
got=$(menu_run "1,$lpr_n,$lpr_n,0")
if [ "$got" = "camera-agent,license-plate-recognition|docker-compose.camera-agent.yml,docker-compose.apps.yml|camera-agent,license-plate-recognition" ]; then pass; else fail "got: $got"; fi

start_test "an out-of-range pick dies instead of installing something else"
got=$(menu_run "99")
case "$got" in DIE:*) pass ;; *) fail "got: $got" ;; esac

# ── 6. start.sh replays every file and every profile ──
compose_args_run() {
    bash -c '
        COMPOSE_FILE=docker-compose.yml
        get_env_var() { case "$1" in
            OPENNVR_EXAMPLE_COMPOSE) printf "%s" "'"$1"'" ;;
            OPENNVR_EXAMPLE_PROFILE) printf "%s" "'"$2"'" ;;
            OPENNVR_DEFAULT_APPS) printf "%s" "'"${3:-}"'" ;;
            *) printf "" ;; esac; }
        eval "$(awk "/^compose_args\(\)/,/^}/" start.sh)"
        compose_args'
}
start_test "start.sh emits one -f per compose file and one --profile per app"
got=$(compose_args_run "docker-compose.camera-agent.yml,docker-compose.apps.yml" "camera-agent-chat,license-plate-recognition")
if [[ "$got" == *"-f docker-compose.camera-agent.yml -f docker-compose.apps.yml"* && \
      "$got" == *"--profile camera-agent-chat --profile license-plate-recognition"* ]]; then pass; else fail "got: $got"; fi

start_test "start.sh does not add the apps overlay twice"
got=$(compose_args_run "docker-compose.apps.yml" "guard-scan-compliance")
n=$(printf '%s' "$got" | grep -o "docker-compose.apps.yml" | wc -l | tr -d ' ')
if [ "$n" = "1" ] && [[ "$got" == *"--profile guard-scan-compliance"* && "$got" == *"--profile default-apps"* ]]; then pass; else fail "got: $got"; fi

start_test "a single value still reads as a list of one"
got=$(compose_args_run "docker-compose.camera-agent.yml" "camera-agent")
if [[ "$got" == *"-f docker-compose.camera-agent.yml --profile camera-agent"* ]]; then pass; else fail "got: $got"; fi

echo ""
if [ "$TESTS_FAILED" -eq 0 ]; then
    echo "✓ all $TESTS_RUN tests passed"; exit 0
else
    echo "✗ $TESTS_FAILED of $TESTS_RUN tests failed"; exit 1
fi
