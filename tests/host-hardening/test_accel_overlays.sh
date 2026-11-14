#!/usr/bin/env bash
# Regression tests for the accelerator overlays' shape and the way start.sh
# assembles them.
#
# The field bug (Ventuno Q, 2026-11-12, a fresh plain install): the qualcomm
# overlay overrode caption-adapter and whisper-adapter, services that only
# docker-compose.camera-agent.yml defined then (the captioner has since
# moved to docker-compose.apps.yml, 2026-11-14). An install without the camera
# agent handed Compose two services with no image, and Compose 2.24 — the
# package on the board's Ubuntu — rejected the whole project ("service
# caption-adapter has neither an image nor a build context"). Newer Compose
# skips a service whose profile is off, so every test and every box with a
# recent Compose passed. In the same install the embedder fill found
# siglip-qnn-adapter:latest-qualcomm, said nothing, and the overlay asked
# for <ADAPTER_TAG>-qualcomm, which did not exist; one missing image
# cancelled every other pull.
#
# The rules these tests defend:
#   * an accelerator overlay overrides only services of the compose files
#     it is applied with: the main overlay docker-compose.yml, a *-apps
#     fragment also docker-compose.apps.yml, a *-agent fragment also
#     docker-compose.camera-agent.yml;
#   * compose_args appends a fragment only when its file is in the set,
#     after the overlay it belongs to;
#   * fill_qualcomm_embedder writes the tag it FOUND as EMBED_ADAPTER_TAG,
#     and leaving the flavour clears it.
set -u
. "$(dirname "$0")/_lib.sh"
require_python_yaml

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running accelerator overlay tests"
echo ""

# ── 1. Every overlay overrides only services its companion files define ──
start_test "each docker-compose.accel-*.yml overrides only services of the files it is applied with"
out=$(python3 - "$REPO_ROOT" <<'PY'
import glob, os, sys, yaml
repo = sys.argv[1]
def services(fn):
    return set(((yaml.safe_load(open(os.path.join(repo, fn))) or {}).get("services") or {}))
base, apps, agent = services("docker-compose.yml"), services("docker-compose.apps.yml"), services("docker-compose.camera-agent.yml")
bad = []
for fn in sorted(glob.glob(os.path.join(repo, "docker-compose.accel-*.yml"))):
    name = os.path.basename(fn); stem = name[:-len(".yml")]
    if stem.endswith("-agent"):
        allowed, companions = base | agent, "docker-compose.yml + docker-compose.camera-agent.yml"
    elif stem.endswith("-apps"):
        allowed, companions = base | apps, "docker-compose.yml + docker-compose.apps.yml"
    else:
        allowed, companions = base, "docker-compose.yml"
    for svc in services(name):
        if svc not in allowed:
            bad.append(f"{name}: '{svc}' is not a service of {companions} — move it to the matching fragment")
print("\n".join(bad))
sys.exit(1 if bad else 0)
PY
)
if [ $? -eq 0 ]; then pass; else fail "$out"; fi

# ── The REAL start.sh functions, pulled out so a drift fails here ──
extract() { awk "/^$1\\(\\) \\{/,/^\\}/" "$REPO_ROOT/start.sh"; }
FUNCS=""
for fn in get_env_var write_env_var set_env_var accel_args compose_args fill_qualcomm_embedder reset_stale_accel_fills; do
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
cp "$REPO_ROOT"/docker-compose*.yml "$WORK"/
cd "$WORK" || exit 1
YELLOW=''; NC=''; CYAN=''; GRAY=''; GREEN=''; BRIGHT_CYAN=''; RED=''
COMPOSE_FILE="docker-compose.yml"
eval "$FUNCS"

# compose_args asks the host which flavour it is; the tests decide.
detect_accel() { echo "${TEST_ACCEL:-qualcomm}"; }
# Host probes compose_args calls that are not under test here: no USB
# webcams, no multicast prober.
webcams_usable() { return 1; }
lan_discovery_core_ok() { return 1; }

files_of() { # the -f files of an args string, in order, one per line
    local prev="" tok
    for tok in $1; do [[ "$prev" == -f ]] && echo "$tok"; prev="$tok"; done
}
profiles_of() { # the --profile values of an args string, one per line
    local prev="" tok
    for tok in $1; do [[ "$prev" == --profile ]] && echo "$tok"; prev="$tok"; done
}

# ── 2. A plain install: overlay + apps fragment, no agent fragment ──
start_test "plain install (no camera agent): qualcomm overlay and its -apps fragment, no -agent fragment"
printf 'OPENNVR_EXAMPLE_COMPOSE=\nOPENNVR_EXAMPLE_PROFILE=\n' > .env
got=$(files_of "$(compose_args)")
if printf '%s\n' "$got" | grep -qx 'docker-compose.accel-qualcomm.yml' \
   && printf '%s\n' "$got" | grep -qx 'docker-compose.accel-qualcomm-apps.yml' \
   && ! printf '%s\n' "$got" | grep -q 'accel-qualcomm-agent'; then pass
else fail "files: $(echo "$got" | tr '\n' ' ')"; fi

# ── 3. With the camera agent: no -agent fragment exists any more (the
# captioner and speech to text ride the -apps fragment), and compose_args
# never names a file that is not there ──
start_test "with docker-compose.camera-agent.yml: every -f names an existing file; the -apps fragment follows the overlay"
printf 'OPENNVR_EXAMPLE_COMPOSE=docker-compose.camera-agent.yml\nOPENNVR_EXAMPLE_PROFILE=camera-agent\n' > .env
got=$(files_of "$(compose_args)")
missing=""; for f in $got; do [[ -f "$f" ]] || missing="$missing $f"; done
ov=$(printf '%s\n' "$got" | grep -nx 'docker-compose.accel-qualcomm.yml' | cut -d: -f1)
fr=$(printf '%s\n' "$got" | grep -nx 'docker-compose.accel-qualcomm-apps.yml' | cut -d: -f1)
if [[ -z "$missing" && -n "$ov" && -n "$fr" && "$fr" -gt "$ov" ]]; then pass
else fail "missing:$missing files: $(echo "$got" | tr '\n' ' ')"; fi

# ── 4. No apps file at all: no -apps fragment ──
start_test "OPENNVR_DEFAULT_APPS=off and no apps pick: the overlay alone, no -apps fragment"
printf 'OPENNVR_EXAMPLE_COMPOSE=\nOPENNVR_EXAMPLE_PROFILE=\nOPENNVR_DEFAULT_APPS=off\n' > .env
got=$(files_of "$(compose_args)")
if printf '%s\n' "$got" | grep -qx 'docker-compose.accel-qualcomm.yml' \
   && ! printf '%s\n' "$got" | grep -q 'accel-qualcomm-apps' \
   && ! printf '%s\n' "$got" | grep -q 'docker-compose.apps.yml'; then pass
else fail "files: $(echo "$got" | tr '\n' ' ')"; fi

# ── 4b. The agent brings the apps file: the captioner is defined there ──
start_test "camera agent with OPENNVR_DEFAULT_APPS=off: docker-compose.apps.yml and its -apps fragment ride along (the captioner lives there)"
printf 'OPENNVR_EXAMPLE_COMPOSE=docker-compose.camera-agent.yml\nOPENNVR_EXAMPLE_PROFILE=camera-agent-chat\nOPENNVR_DEFAULT_APPS=off\n' > .env
got=$(files_of "$(compose_args)")
ap=$(printf '%s\n' "$got" | grep -nx 'docker-compose.apps.yml' | cut -d: -f1)
fr=$(printf '%s\n' "$got" | grep -nx 'docker-compose.accel-qualcomm-apps.yml' | cut -d: -f1)
ov=$(printf '%s\n' "$got" | grep -nx 'docker-compose.accel-qualcomm.yml' | cut -d: -f1)
if [[ -n "$ap" && -n "$fr" && -n "$ov" && "$ap" -lt "$ov" && "$fr" -gt "$ov" ]] \
   && ! printf '%s\n' "$(profiles_of "$(compose_args)")" | grep -qx 'default-apps'; then pass
else fail "files: $(echo "$got" | tr '\n' ' ')"; fi

# ── 4c. The description flags start the captioner on a plain install ──
start_test "EVENTS_CAPTION_ENRICHMENT=true and no agent: --profile descriptions is added"
printf 'OPENNVR_EXAMPLE_COMPOSE=\nOPENNVR_EXAMPLE_PROFILE=\nEVENTS_CAPTION_ENRICHMENT=true\n' > .env
got=$(profiles_of "$(compose_args)")
if printf '%s\n' "$got" | grep -qx 'descriptions'; then pass
else fail "profiles: $(echo "$got" | tr '\n' ' ')"; fi

start_test "the agent profile already starts the captioner: no --profile descriptions on top"
printf 'OPENNVR_EXAMPLE_COMPOSE=docker-compose.camera-agent.yml\nOPENNVR_EXAMPLE_PROFILE=camera-agent-chat\nEVENTS_CAPTION_ENRICHMENT=true\n' > .env
got=$(profiles_of "$(compose_args)")
if ! printf '%s\n' "$got" | grep -qx 'descriptions'; then pass
else fail "profiles: $(echo "$got" | tr '\n' ' ')"; fi

start_test "flags off: no --profile descriptions"
printf 'OPENNVR_EXAMPLE_COMPOSE=\nOPENNVR_EXAMPLE_PROFILE=\nEVENTS_CAPTION_ENRICHMENT=false\nEVENTS_DESCRIPTOR_ENRICHMENT=false\n' > .env
got=$(profiles_of "$(compose_args)")
if ! printf '%s\n' "$got" | grep -qx 'descriptions'; then pass
else fail "profiles: $(echo "$got" | tr '\n' ' ')"; fi

# ── 4d. The embeddings flag starts the embedders ──
start_test "EVENTS_EMBED_ENRICHMENT=true: --profile embeddings is added"
printf 'OPENNVR_EXAMPLE_COMPOSE=\nOPENNVR_EXAMPLE_PROFILE=\nEVENTS_EMBED_ENRICHMENT=true\n' > .env
got=$(profiles_of "$(compose_args)")
if printf '%s\n' "$got" | grep -qx 'embeddings'; then pass
else fail "profiles: $(echo "$got" | tr '\n' ' ')"; fi

start_test "embeddings already picked (the qualcomm fill's way): no second --profile embeddings"
printf 'OPENNVR_EXAMPLE_COMPOSE=\nOPENNVR_EXAMPLE_PROFILE=embeddings\nEVENTS_EMBED_ENRICHMENT=true\n' > .env
got=$(profiles_of "$(compose_args)")
if [[ "$(printf '%s\n' "$got" | grep -cx 'embeddings')" == 1 ]]; then pass
else fail "profiles: $(echo "$got" | tr '\n' ' ')"; fi

start_test "EVENTS_EMBED_ENRICHMENT=false: no --profile embeddings"
printf 'OPENNVR_EXAMPLE_COMPOSE=\nOPENNVR_EXAMPLE_PROFILE=\nEVENTS_EMBED_ENRICHMENT=false\n' > .env
got=$(profiles_of "$(compose_args)")
if ! printf '%s\n' "$got" | grep -qx 'embeddings'; then pass
else fail "profiles: $(echo "$got" | tr '\n' ' ')"; fi

# ── 4e. Voice search starts the speech adapter ──
start_test "VOICE_SEARCH=on and no voice agent: --profile voice is added"
printf 'OPENNVR_EXAMPLE_COMPOSE=\nOPENNVR_EXAMPLE_PROFILE=\nVOICE_SEARCH=on\n' > .env
got=$(profiles_of "$(compose_args)")
if printf '%s\n' "$got" | grep -qx 'voice'; then pass
else fail "profiles: $(echo "$got" | tr '\n' ' ')"; fi

start_test "VOICE_SEARCH=on with the chat agent: --profile voice is still added (chat carries no speech adapter)"
printf 'OPENNVR_EXAMPLE_COMPOSE=docker-compose.camera-agent.yml\nOPENNVR_EXAMPLE_PROFILE=camera-agent-chat\nVOICE_SEARCH=on\n' > .env
got=$(profiles_of "$(compose_args)")
if printf '%s\n' "$got" | grep -qx 'voice'; then pass
else fail "profiles: $(echo "$got" | tr '\n' ' ')"; fi

start_test "VOICE_SEARCH=on with the voice agent: no --profile voice on top"
printf 'OPENNVR_EXAMPLE_COMPOSE=docker-compose.camera-agent.yml\nOPENNVR_EXAMPLE_PROFILE=camera-agent\nVOICE_SEARCH=on\n' > .env
got=$(profiles_of "$(compose_args)")
if ! printf '%s\n' "$got" | grep -qx 'voice'; then pass
else fail "profiles: $(echo "$got" | tr '\n' ' ')"; fi

start_test "VOICE_SEARCH unset: no --profile voice"
printf 'OPENNVR_EXAMPLE_COMPOSE=\nOPENNVR_EXAMPLE_PROFILE=\n' > .env
got=$(profiles_of "$(compose_args)")
if ! printf '%s\n' "$got" | grep -qx 'voice'; then pass
else fail "profiles: $(echo "$got" | tr '\n' ' ')"; fi

# ── 5. The intel flavour follows the same rule ──
start_test "intel: the -apps fragment follows docker-compose.apps.yml"
printf 'OPENNVR_EXAMPLE_COMPOSE=\nOPENNVR_EXAMPLE_PROFILE=\n' > .env
got=$(TEST_ACCEL=intel files_of "$(TEST_ACCEL=intel compose_args)")
if printf '%s\n' "$got" | grep -qx 'docker-compose.accel-intel.yml' \
   && printf '%s\n' "$got" | grep -qx 'docker-compose.accel-intel-apps.yml' \
   && ! printf '%s\n' "$got" | grep -q 'qualcomm'; then pass
else fail "files: $(echo "$got" | tr '\n' ' ')"; fi

# ── 6. The embedder fill writes the tag it found ──
start_test "fill_qualcomm_embedder writes EMBED_ADAPTER_TAG=latest-qualcomm when the pinned release has no image"
printf 'ADAPTER_TAG=0.1.10\nEVENTS_EMBED_ENRICHMENT=false\nOPENNVR_EXAMPLE_PROFILE=\n' > .env
accel_image_available() { [[ "$1" == *:latest-qualcomm ]]; }
fill_qualcomm_embedder >/dev/null 2>&1
if [[ "$(get_env_var EMBED_ADAPTER_TAG)" == "latest-qualcomm" && "$(get_env_var EVENTS_EMBED_ENRICHMENT)" == "true" \
   && "$(get_env_var OPENNVR_EXAMPLE_PROFILE)" == "embeddings" ]]; then pass
else fail "$(grep -E '^(EMBED_ADAPTER_TAG|EVENTS_EMBED_ENRICHMENT|OPENNVR_EXAMPLE_PROFILE)=' .env | tr '\n' ' ')"; fi

start_test "fill_qualcomm_embedder prefers the pinned release's image when it exists"
printf 'ADAPTER_TAG=0.1.11\nEVENTS_EMBED_ENRICHMENT=false\nOPENNVR_EXAMPLE_PROFILE=\n' > .env
accel_image_available() { return 0; }
fill_qualcomm_embedder >/dev/null 2>&1
if [[ "$(get_env_var EMBED_ADAPTER_TAG)" == "0.1.11-qualcomm" ]]; then pass
else fail "EMBED_ADAPTER_TAG=$(get_env_var EMBED_ADAPTER_TAG)"; fi

start_test "fill_qualcomm_embedder writes nothing when no image exists"
printf 'ADAPTER_TAG=0.1.10\nEVENTS_EMBED_ENRICHMENT=false\n' > .env
accel_image_available() { return 1; }
fill_qualcomm_embedder >/dev/null 2>&1
if [[ -z "$(get_env_var EMBED_ADAPTER_TAG)" && "$(get_env_var EVENTS_EMBED_ENRICHMENT)" == "false" ]]; then pass
else fail "$(grep -E '^(EMBED_ADAPTER_TAG|EVENTS_EMBED_ENRICHMENT)=' .env | tr '\n' ' ')"; fi

# ── 7. Leaving the flavour clears the tag ──
start_test "reset_stale_accel_fills clears EMBED_ADAPTER_TAG the qualcomm fill wrote"
printf 'OPENNVR_ACCEL_RESOLVED=qualcomm\nEMBED_ADAPTER_TAG=latest-qualcomm\nEVENTS_EMBED_ENRICHMENT=true\nOPENNVR_EXAMPLE_PROFILE=embeddings\n' > .env
reset_stale_accel_fills cpu >/dev/null 2>&1
if [[ -z "$(get_env_var EMBED_ADAPTER_TAG)" && "$(get_env_var EVENTS_EMBED_ENRICHMENT)" == "false" ]]; then pass
else fail "$(grep -E '^(EMBED_ADAPTER_TAG|EVENTS_EMBED_ENRICHMENT)=' .env | tr '\n' ' ')"; fi

# ── 8. The detector's crop budget follows the flavour (Ventuno Q, 2026-11-13:
#    8 crops × 5 fps × 2 busy cameras pinned the Hexagon; 3 × 4 fps fitted) ──
start_test "qualcomm overlay defaults DETECT_MAX_REGIONS to 3 and DETECT_FPS to 4; base compose to 2 fps and 4 crops; .env.example leaves both empty"
out=$(python3 - "$REPO_ROOT" <<'PY'
import os, re, sys, yaml
repo = sys.argv[1]
def env(fn):
    svc = (yaml.safe_load(open(os.path.join(repo, fn))) or {})["services"]["detect-pipeline"]
    return {e.split("=", 1)[0]: e.split("=", 1)[1] for e in svc.get("environment") or [] if isinstance(e, str)}
qc, base = env("docker-compose.accel-qualcomm.yml"), env("docker-compose.yml")
bad = []
if qc.get("DETECT_MAX_REGIONS") != "${DETECT_MAX_REGIONS:-3}": bad.append(f"qualcomm DETECT_MAX_REGIONS={qc.get('DETECT_MAX_REGIONS')!r}")
if qc.get("DETECT_FPS") != "${DETECT_FPS:-4}": bad.append(f"qualcomm DETECT_FPS={qc.get('DETECT_FPS')!r}")
if base.get("DETECT_MAX_REGIONS") != "${DETECT_MAX_REGIONS:-4}": bad.append(f"base DETECT_MAX_REGIONS={base.get('DETECT_MAX_REGIONS')!r}")
if base.get("DETECT_FPS") != "${DETECT_FPS:-2}": bad.append(f"base DETECT_FPS={base.get('DETECT_FPS')!r} (2 is what a CPU-only host sustains per busy camera, measured)")
ex = open(os.path.join(repo, ".env.example")).read()
for k in ("DETECT_MAX_REGIONS", "DETECT_FPS"):
    m = re.search(rf"^{k}=(.*)$", ex, re.M)
    if not m or m.group(1).strip(): bad.append(f".env.example {k}={m.group(1) if m else '<missing>'!r} (must be empty: a value there outranks the flavour)")
print("\n".join(bad))
PY
)
if [[ -z "$out" ]]; then pass; else fail "$out"; fi

echo ""
echo "Tests run: $TESTS_RUN, failed: $TESTS_FAILED"
[ "$TESTS_FAILED" -eq 0 ]
