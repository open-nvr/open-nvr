#!/usr/bin/env bash
# Regression tests for what a FRESH install hands Compose and Docker.
#
# The field bugs (Ventuno Q, 2026-11-12, a fresh clone of main with the
# installer's defaults):
#   * docker-compose.lan-discovery.yml built the prober from an inline
#     Dockerfile that said `FROM ${CORE_IMAGE}`; Compose interpolated the
#     variable (to a blank, with a WARN nobody reads) before Docker saw the
#     file, and `./start.sh up` died at "FROM requires either one or three
#     arguments". The installer pulls the base set and never builds that
#     service, so nothing failed until the very last step.
#   * The prober copies the host-agent binary out of the core image, which
#     only 0.1.11 and newer carry; a checkout of main with the installer's
#     older pin failed the same way for a different reason.
#   * The qualcomm flavour switched visit descriptions on with no captioner
#     in the install (the captioner lived in docker-compose.camera-agent.yml
#     then; since 2026-11-14 it is in docker-compose.apps.yml, which every
#     stock install carries).
#
# The rules these tests defend:
#   * inside any dockerfile_inline, a variable Docker must expand is written
#     $${…}; a plain ${…} is Compose's to substitute and almost never meant;
#   * compose_args adds the discovery overlay only for a core pin that
#     carries the prober's binary (0.1.11+, or a non-release tag);
#   * fill_qualcomm_captioner turns the description flags on only when the
#     apps file (where the captioner is defined) is in the install: always,
#     unless OPENNVR_DEFAULT_APPS=off keeps it out and no pick names it.
set -u
. "$(dirname "$0")/_lib.sh"
require_python_yaml

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running fresh-install path tests"
echo ""

# ── 1. Inline Dockerfiles leave Docker's variables to Docker ──
start_test "no dockerfile_inline hands Compose a \${…} meant for Docker (write \$\${…})"
out=$(python3 - "$REPO_ROOT" <<'PY'
import glob, os, re, sys, yaml
repo = sys.argv[1]
# Compose's own tags (!override, !reset) are not PyYAML's business here.
class Loader(yaml.SafeLoader):
    pass
Loader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_mapping(node) if isinstance(node, yaml.MappingNode) else loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else loader.construct_scalar(node))
bad = []
for fn in sorted(glob.glob(os.path.join(repo, "docker-compose*.yml"))):
    doc = yaml.load(open(fn), Loader=Loader) or {}
    for name, svc in (doc.get("services") or {}).items():
        inline = ((svc or {}).get("build") or {}).get("dockerfile_inline") if isinstance((svc or {}).get("build"), dict) else None
        if not inline:
            continue
        for i, line in enumerate(inline.splitlines(), 1):
            # a ${…} not preceded by another $ is Compose interpolation
            for m in re.finditer(r"(?<!\$)\$\{[^}]*\}", line):
                bad.append(f"{os.path.basename(fn)}: service {name}, inline line {i}: {m.group(0)} — Compose will substitute this before Docker sees it; write $$ for a Docker ARG")
print("\n".join(bad))
sys.exit(1 if bad else 0)
PY
)
if [ $? -eq 0 ]; then pass; else fail "$out"; fi

start_test "the prober's FROM and COPY lines reach Docker as \${CORE_IMAGE} / \${TARGETARCH}"
rendered=$(python3 - "$REPO_ROOT" <<'PY'
import os, sys, yaml
repo = sys.argv[1]
doc = yaml.safe_load(open(os.path.join(repo, "docker-compose.lan-discovery.yml")))
print(doc["services"]["lan-probe"]["build"]["dockerfile_inline"])
PY
)
if grep -q 'FROM \$\${CORE_IMAGE} AS core' <<<"$rendered" && grep -q 'linux-\$\${TARGETARCH}/opennvr-host' <<<"$rendered"; then pass
else fail "$(grep -E 'FROM|COPY' <<<"$rendered")"; fi

# ── The REAL start.sh functions ──
extract() { awk "/^$1\\(\\) \\{/,/^\\}/" "$REPO_ROOT/start.sh"; }
FUNCS=""
for fn in get_env_var write_env_var set_env_var version_ge lan_discovery_core_ok accel_args compose_args captioner_file_in_set fill_qualcomm_captioner prompt_nic_topology; do
    body=$(extract "$fn")
    if [[ -z "$body" ]]; then
        echo "✗ could not extract $fn from start.sh" >&2
        exit 2
    fi
    FUNCS="$FUNCS
$body"
done
LAN_DISCOVERY_MIN_CORE=$(grep -oE '^LAN_DISCOVERY_MIN_CORE="[0-9.]+"' "$REPO_ROOT/start.sh" | cut -d'"' -f2)
[[ -n "$LAN_DISCOVERY_MIN_CORE" ]] || { echo "✗ LAN_DISCOVERY_MIN_CORE not found in start.sh" >&2; exit 2; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cp "$REPO_ROOT"/docker-compose*.yml "$WORK"/
cd "$WORK" || exit 1
YELLOW=''; NC=''; CYAN=''; GRAY=''; GREEN=''; BRIGHT_CYAN=''; RED=''
COMPOSE_FILE="docker-compose.yml"
eval "$FUNCS"
detect_accel() { echo cpu; }
uname() { echo Linux; }

files_of() { local prev="" tok; for tok in $1; do [[ "$prev" == -f ]] && echo "$tok"; prev="$tok"; done; }

# ── 2. The discovery overlay follows the core pin ──
start_test "LAN_DISCOVERY=on with CORE_TAG=0.1.10: the overlay is left out (with a notice)"
printf 'LAN_DISCOVERY=on\nCORE_TAG=0.1.10\nOPENNVR_EXAMPLE_COMPOSE=\n' > .env
got=$(files_of "$(compose_args 2>/dev/null)")
if ! grep -q 'lan-discovery' <<<"$got"; then pass; else fail "files: $(tr '\n' ' ' <<<"$got")"; fi

start_test "LAN_DISCOVERY=on with CORE_TAG=$LAN_DISCOVERY_MIN_CORE: the overlay is in"
printf 'LAN_DISCOVERY=on\nCORE_TAG=%s\nOPENNVR_EXAMPLE_COMPOSE=\n' "$LAN_DISCOVERY_MIN_CORE" > .env
got=$(files_of "$(compose_args 2>/dev/null)")
if grep -qx 'docker-compose.lan-discovery.yml' <<<"$got"; then pass; else fail "files: $(tr '\n' ' ' <<<"$got")"; fi

start_test "LAN_DISCOVERY=on with CORE_TAG=main (not a release number): the overlay is in"
printf 'LAN_DISCOVERY=on\nCORE_TAG=main\nOPENNVR_EXAMPLE_COMPOSE=\n' > .env
got=$(files_of "$(compose_args 2>/dev/null)")
if grep -qx 'docker-compose.lan-discovery.yml' <<<"$got"; then pass; else fail "files: $(tr '\n' ' ' <<<"$got")"; fi

start_test "LAN_DISCOVERY=off: no overlay whatever the pin"
printf 'LAN_DISCOVERY=off\nCORE_TAG=main\nOPENNVR_EXAMPLE_COMPOSE=\n' > .env
got=$(files_of "$(compose_args 2>/dev/null)")
if ! grep -q 'lan-discovery' <<<"$got"; then pass; else fail "files: $(tr '\n' ' ' <<<"$got")"; fi

# ── 3. Descriptions only where a captioner runs ──
accel_image_available() { return 0; }
start_test "qualcomm captioner fill: OPENNVR_DEFAULT_APPS=off and no pick (no apps file) → adapter and tag filled, description flags left off"
printf 'CAPTION_ADAPTER=ollamavlm\nCAPTION_ADAPTER_TAG=\nADAPTER_TAG=0.1.10\nEVENTS_CAPTION_ENRICHMENT=false\nEVENTS_DESCRIPTOR_ENRICHMENT=false\nEVENTS_DESCRIPTOR_PEOPLE=false\nOPENNVR_EXAMPLE_COMPOSE=\nOPENNVR_DEFAULT_APPS=off\n' > .env
fill_qualcomm_captioner >/dev/null 2>&1
if [[ "$(get_env_var CAPTION_ADAPTER)" == "litertlm" && "$(get_env_var CAPTION_ADAPTER_TAG)" == "0.1.10-qualcomm" \
   && "$(get_env_var EVENTS_CAPTION_ENRICHMENT)" == "false" && "$(get_env_var EVENTS_DESCRIPTOR_ENRICHMENT)" == "false" ]]; then pass
else fail "$(grep -E '^(CAPTION_ADAPTER|CAPTION_ADAPTER_TAG|EVENTS_CAPTION_ENRICHMENT|EVENTS_DESCRIPTOR_ENRICHMENT)=' .env | tr '\n' ' ')"; fi

start_test "qualcomm captioner fill: a plain install (no pick, default apps on) → description flags on: the captioner is in the apps file"
printf 'CAPTION_ADAPTER=ollamavlm\nCAPTION_ADAPTER_TAG=\nADAPTER_TAG=0.1.10\nEVENTS_CAPTION_ENRICHMENT=false\nEVENTS_DESCRIPTOR_ENRICHMENT=false\nEVENTS_DESCRIPTOR_PEOPLE=false\nOPENNVR_EXAMPLE_COMPOSE=\n' > .env
fill_qualcomm_captioner >/dev/null 2>&1
if [[ "$(get_env_var CAPTION_ADAPTER)" == "litertlm" && "$(get_env_var CAPTION_ADAPTER_TAG)" == "0.1.10-qualcomm" \
   && "$(get_env_var EVENTS_CAPTION_ENRICHMENT)" == "true" && "$(get_env_var EVENTS_DESCRIPTOR_ENRICHMENT)" == "true" ]]; then pass
else fail "$(grep -E '^(CAPTION_ADAPTER|CAPTION_ADAPTER_TAG|EVENTS_CAPTION_ENRICHMENT|EVENTS_DESCRIPTOR_ENRICHMENT)=' .env | tr '\n' ' ')"; fi

start_test "qualcomm captioner fill: with the camera-agent file → description flags on"
printf 'CAPTION_ADAPTER=ollamavlm\nCAPTION_ADAPTER_TAG=\nADAPTER_TAG=0.1.10\nEVENTS_CAPTION_ENRICHMENT=false\nEVENTS_DESCRIPTOR_ENRICHMENT=false\nEVENTS_DESCRIPTOR_PEOPLE=false\nOPENNVR_EXAMPLE_COMPOSE=docker-compose.camera-agent.yml,docker-compose.apps.yml\n' > .env
fill_qualcomm_captioner >/dev/null 2>&1
if [[ "$(get_env_var CAPTION_ADAPTER)" == "litertlm" && "$(get_env_var EVENTS_CAPTION_ENRICHMENT)" == "true" \
   && "$(get_env_var EVENTS_DESCRIPTOR_ENRICHMENT)" == "true" && "$(get_env_var EVENTS_DESCRIPTOR_PEOPLE)" == "true" ]]; then pass
else fail "$(grep -E '^(CAPTION_ADAPTER|EVENTS_CAPTION_ENRICHMENT|EVENTS_DESCRIPTOR_ENRICHMENT|EVENTS_DESCRIPTOR_PEOPLE)=' .env | tr '\n' ' ')"; fi

start_test "qualcomm captioner fill: litertlm already chosen with no tag → the -qualcomm tag is filled (the installer's case)"
printf 'CAPTION_ADAPTER=litertlm\nCAPTION_ADAPTER_TAG=\nADAPTER_TAG=0.1.11\nEVENTS_CAPTION_ENRICHMENT=true\nEVENTS_DESCRIPTOR_ENRICHMENT=true\nEVENTS_DESCRIPTOR_PEOPLE=false\nOPENNVR_EXAMPLE_COMPOSE=docker-compose.camera-agent.yml\n' > .env
fill_qualcomm_captioner >/dev/null 2>&1
if [[ "$(get_env_var CAPTION_ADAPTER)" == "litertlm" && "$(get_env_var CAPTION_ADAPTER_TAG)" == "0.1.11-qualcomm" ]]; then pass
else fail "$(grep -E '^(CAPTION_ADAPTER|CAPTION_ADAPTER_TAG)=' .env | tr '\n' ' ')"; fi

start_test "qualcomm captioner fill: an operator's own tag is never touched"
printf 'CAPTION_ADAPTER=litertlm\nCAPTION_ADAPTER_TAG=main-qualcomm\nADAPTER_TAG=0.1.11\nOPENNVR_EXAMPLE_COMPOSE=docker-compose.camera-agent.yml\n' > .env
fill_qualcomm_captioner >/dev/null 2>&1
if [[ "$(get_env_var CAPTION_ADAPTER_TAG)" == "main-qualcomm" ]]; then pass
else fail "tag became $(get_env_var CAPTION_ADAPTER_TAG)"; fi

start_test "qualcomm captioner fill: a CPU captioner the operator chose on purpose (blip) is left alone"
printf 'CAPTION_ADAPTER=blip\nCAPTION_ADAPTER_TAG=\nADAPTER_TAG=0.1.11\nOPENNVR_EXAMPLE_COMPOSE=docker-compose.camera-agent.yml\n' > .env
fill_qualcomm_captioner >/dev/null 2>&1
if [[ "$(get_env_var CAPTION_ADAPTER)" == "blip" && -z "$(get_env_var CAPTION_ADAPTER_TAG)" ]]; then pass
else fail "$(grep -E '^(CAPTION_ADAPTER|CAPTION_ADAPTER_TAG)=' .env | tr '\n' ' ')"; fi

# ── 4. The network menu: Enter is the safe default, not a hidden skip ──
start_test "network menu: Enter takes Simple and writes the stream address and WebRTC hosts"
printf 'OPENNVR_HOST_IP=\n' > .env
detect_lan_ip() { echo 192.168.1.5; }
WHITE=''
rc=0; printf '\n' | prompt_nic_topology $'eth0:192.168.1.5\nwlan0:10.0.0.5' >/dev/null 2>&1 || rc=$?
if [[ "$rc" == 0 && "$(get_env_var NGINX_BIND_HOST)" == "0.0.0.0" \
   && "$(get_env_var MEDIAMTX_PUBLIC_URL)" == "https://192.168.1.5" \
   && "$(get_env_var MEDIAMTX_WEBRTC_HOSTS)" == "192.168.1.5,10.0.0.5" ]]; then pass
else fail "rc=$rc $(grep -E '^(NGINX_BIND_HOST|MEDIAMTX_PUBLIC_URL|MEDIAMTX_WEBRTC_HOSTS)=' .env | tr '\n' ' ')"; fi

start_test "network menu: EOF on stdin (no terminal left) is the default too, not a dead launcher"
printf 'OPENNVR_HOST_IP=\n' > .env
rc=0; prompt_nic_topology $'eth0:192.168.1.5\nwlan0:10.0.0.5' </dev/null >/dev/null 2>&1 || rc=$?
if [[ "$rc" == 0 && "$(get_env_var NGINX_BIND_HOST)" == "0.0.0.0" ]]; then pass
else fail "rc=$rc $(grep -E '^NGINX_BIND_HOST=' .env)"; fi

start_test "network menu: an answer nobody offered is refused"
rc=0; printf '7\n' | prompt_nic_topology $'eth0:192.168.1.5\nwlan0:10.0.0.5' >/dev/null 2>&1 || rc=$?
if [[ "$rc" != 0 ]]; then pass; else fail "accepted 7"; fi

echo ""
echo "Tests run: $TESTS_RUN, failed: $TESTS_FAILED"
[ "$TESTS_FAILED" -eq 0 ]
