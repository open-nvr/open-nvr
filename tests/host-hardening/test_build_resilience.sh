#!/usr/bin/env bash
# ============================================================
# Tests for ISSUE-7 — standard stack build must not depend on any
# external package repository (Alpine apk, Debian apt, PyPI pip)
# during image build. Some operator networks (reported from
# IN/IR/CN) block dl-cdn.alpinelinux.org and its mirrors. The
# fix is to source binaries via multi-stage COPY from another
# Docker Hub image, never `RUN apk add` etc.
#
# This test walks every docker-compose*.yml and asserts that no
# `dockerfile_inline:` block contains:
#   * `RUN apk add ...`
#   * `RUN apt-get install ...`
#   * `RUN pip install ...`
#
# ISSUE-7 v6 expanded scope: runtime container `command:` blocks
# are now checked too, because we hit the same dl-cdn.alpinelinux.
# org filter from cert-init and camera-agent-config-init's
# ``apk add openssl`` / ``apk add gettext`` lines. The fix pattern
# for those is different from multi-stage COPY (swap to a base
# image with the tool pre-baked, or replace the tool with one
# already in busybox), but the regression contract is identical:
# the container's runtime startup must not depend on an external
# package repository being reachable.
#
# Run with: bash tests/host-hardening/test_build_resilience.sh
# ============================================================

set -u

# ISSUE-19: bail early with a clear install command if pyyaml is
# missing on the python3 these tests run against. Otherwise the
# python heredocs below fail with an opaque ModuleNotFoundError.
. "$(dirname "$0")/_lib.sh"
require_python_yaml

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running build-resilience tests"
echo ""

# Extract every dockerfile_inline block from every compose file and
# grep each for forbidden RUN patterns. Returns the violations on
# stdout, one per line.
find_violations() {
    local compose_file="$1"
    python3 - "$compose_file" <<'PY'
import sys, yaml
fn = sys.argv[1]
try:
    c = yaml.safe_load(open(fn))
except Exception as e:
    print(f"YAML_ERROR: {e}")
    sys.exit(0)
for name, svc in (c.get("services") or {}).items():
    build = svc.get("build")
    if not isinstance(build, dict):
        continue
    inline = build.get("dockerfile_inline")
    if not inline:
        continue
    for lineno, line in enumerate(inline.splitlines(), 1):
        stripped = line.strip()
        # The forbidden patterns. RUN apk/apt-get/pip mean the build
        # depends on an external package repository being reachable.
        if stripped.startswith("RUN apk add") \
           or stripped.startswith("RUN apk update") \
           or "apt-get install" in stripped and stripped.startswith("RUN") \
           or stripped.startswith("RUN pip install") \
           or stripped.startswith("RUN pip3 install"):
            print(f"{name}:{lineno}: {stripped}")
PY
}

# ── 1. standard compose: no forbidden RUN in any dockerfile_inline ──
start_test "docker-compose.yml has no RUN apk/apt/pip in dockerfile_inline"
violations=$(find_violations "${REPO_ROOT}/docker-compose.yml")
if [ -z "$violations" ]; then
    pass
else
    fail "found forbidden RUN in build:"$'\n'"${violations}"
fi

# ── 3. canonical compose is self-contained ──────────────────
start_test "docker-compose.yml directly defines the standard stack"
compose_shape=$(python3 - "${REPO_ROOT}" <<'PY'
import sys, yaml
from pathlib import Path
root = Path(sys.argv[1])
c = yaml.safe_load((root / "docker-compose.yml").read_text())
has_services = bool(c.get("services"))
has_include = bool(c.get("include"))
legacy_exists = (root / "docker-compose.tier0.yml").exists()
print(f"has_services={has_services} has_include={has_include} legacy_exists={legacy_exists}")
PY
)
if echo "$compose_shape" | grep -q "has_services=True" \
   && echo "$compose_shape" | grep -q "has_include=False" \
   && echo "$compose_shape" | grep -q "legacy_exists=False"; then
    pass
else
    fail "docker-compose.yml must define services directly, without an include or legacy file. Got: $compose_shape"
fi

# ── 4. camera-agent compose: same check ─────────────────────
start_test "docker-compose.camera-agent.yml has no RUN apk/apt/pip in dockerfile_inline"
violations=$(find_violations "${REPO_ROOT}/docker-compose.camera-agent.yml")
if [ -z "$violations" ]; then
    pass
else
    fail "found forbidden RUN in build:"$'\n'"${violations}"
fi

# ── 4a. docker-compose.yml yolov8-weights-init: image+build pattern, cp command ──
# After ISSUE-7 v3 the standard stack default uses the pre-baked weights
# image directly. The service must:
#   * declare BOTH `image:` (so Compose pulls when available) AND
#     `build:` (so Compose falls back to building locally if the
#     registry image isn't reachable);
#   * have a command that's a simple cp from the image's baked-in
#     /yolov8n.onnx into the /weights volume — NOT apt/pip/export;
#   * make the image tag operator-overridable via YOLOV8_WEIGHTS_IMAGE
#     so operators on networks that block ghcr.io can point at a
#     private registry.
start_test "standard yolov8-weights-init has image+build pattern (Compose pull-or-build)"
yolov_shape=$(python3 - "${REPO_ROOT}" <<'PY'
import sys, yaml
c = yaml.safe_load(open(sys.argv[1] + "/docker-compose.yml"))
svc = c["services"]["yolov8-weights-init"]
print("HAS_IMAGE:", "image" in svc)
print("HAS_BUILD:", "build" in svc)
cmd = str(svc.get("command", ""))
print("CMD_HAS_CP:",       "cp " in cmd)
print("CMD_HAS_APT:",      "apt-get" in cmd or "apt install" in cmd)
print("CMD_HAS_PIP:",      "pip install" in cmd or "pip3 install" in cmd)
print("CMD_HAS_YOLO_EXP:", "yolo export" in cmd)
PY
)
if echo "$yolov_shape" | grep -q "HAS_IMAGE: True" \
   && echo "$yolov_shape" | grep -q "HAS_BUILD: True" \
   && echo "$yolov_shape" | grep -q "CMD_HAS_CP: True" \
   && echo "$yolov_shape" | grep -q "CMD_HAS_APT: False" \
   && echo "$yolov_shape" | grep -q "CMD_HAS_PIP: False" \
   && echo "$yolov_shape" | grep -q "CMD_HAS_YOLO_EXP: False"; then
    pass
else
    fail "standard yolov8-weights-init must use image+build with a cp command. Got: $yolov_shape"
fi

start_test "standard yolov8-weights-init has explicit pull_policy: missing (pull-then-build)"
# Self-review M-1: with image+build both defined, Compose's default
# pull semantics aren't fully documented. We pin pull_policy: missing
# so Compose tries to pull from GHCR first and falls back to building
# locally only if the pull fails. Operators on networks that can
# reach GHCR get the 5-sec pull; operators behind filters get the
# ~10-min local build. Either way the single command works.
pp=$(python3 - "${REPO_ROOT}" <<'PY'
import sys, yaml
c = yaml.safe_load(open(sys.argv[1] + "/docker-compose.yml"))
print(c["services"]["yolov8-weights-init"].get("pull_policy", "(unset)"))
PY
)
if [ "$pp" = "missing" ]; then
    pass
else
    fail "yolov8-weights-init must declare pull_policy: missing; got: '$pp'"
fi

start_test "standard yolov8-weights-init image is YOLOV8_WEIGHTS_IMAGE-overridable"
img=$(python3 - "${REPO_ROOT}" <<'PY'
import sys, yaml
c = yaml.safe_load(open(sys.argv[1] + "/docker-compose.yml"))
print(c["services"]["yolov8-weights-init"]["image"])
PY
)
if echo "$img" | grep -q '${YOLOV8_WEIGHTS_IMAGE'; then
    pass
else
    fail "image must use \${YOLOV8_WEIGHTS_IMAGE:-...} for operator override; got: $img"
fi

# ── 4b. weights image Dockerfile final stage is COPY-only ──
# The weights Dockerfile's first stage uses ultralytics + Python
# (allowed — that's IMAGE BUILD time on a dev machine or GHA
# runner, not operator-deploy time). But the *final stage* — the
# one operators pull and run — must not have any RUN apk/apt/pip.
# It's just an alpine base + COPY of the .onnx.
start_test "GHA workflow publishes the tag docker-compose expects (v8.4.0-yolo26n)"
# Regression for ISSUE-7 v3 fix: the first GHA build pushed
# `:v8.3.40` (Python package version) while docker-compose.yml
# expected `:v8.3.0` (weights release version). Operators got
# "manifest unknown" on every pull. Lock the alignment so the two
# values can never drift again.
result=$(python3 - "${REPO_ROOT}" <<'PY'
import re, sys, yaml
root = sys.argv[1]

# Extract `:vX.Y.Z` from the compose image default.
compose_text = open(root + "/docker-compose.yml").read()
# The tag is <assets release>-<model> since YOLO26n (v8.4.0-yolo26n).
m = re.search(r"ghcr\.io/open-nvr/yolov8-weights:v([0-9A-Za-z.-]+)\}", compose_text)
compose_tag = m.group(1) if m else None

# Parse the GHA workflow YAML and dig into workflow_dispatch.inputs.
wf = yaml.safe_load(open(root + "/.github/workflows/build-yolov8-weights.yml"))
# The "on" key gets parsed as True (YAML boolean!) — work around.
on = wf.get("on", wf.get(True, {}))
inputs = on.get("workflow_dispatch", {}).get("inputs", {}) or {}
gha_input_default = inputs.get("weights_tag", {}).get("default")

# Also extract the fallback used in the run script:
#   WEIGHTS_TAG="${{ github.event.inputs.weights_tag || '8.3.0' }}"
wf_text = open(root + "/.github/workflows/build-yolov8-weights.yml").read()
m2 = re.search(r"weights_tag\s*\|\|\s*'([^']+)'", wf_text)
gha_run_fallback = m2.group(1) if m2 else None

print(f"compose_tag={compose_tag}")
print(f"gha_input_default={gha_input_default}")
print(f"gha_run_fallback={gha_run_fallback}")
PY
)
compose_tag=$(echo "$result" | grep "^compose_tag=" | cut -d= -f2)
gha_input_default=$(echo "$result" | grep "^gha_input_default=" | cut -d= -f2)
gha_run_fallback=$(echo "$result" | grep "^gha_run_fallback=" | cut -d= -f2)
if [ "$compose_tag" = "$gha_input_default" ] && [ "$compose_tag" = "$gha_run_fallback" ]; then
    pass
else
    fail "tag mismatch — compose v${compose_tag}, GHA input '${gha_input_default}', run fallback '${gha_run_fallback}'"
fi

start_test "GHA workflow includes runner disk cleanup before multi-arch build"
# Regression: the first build attempt hit "no space left on device"
# during multi-arch extract of ultralytics/ultralytics:8.3.40 (~7 GB
# of base images on a 14 GB runner). Free-disk-space action MUST
# come before the build step so the extract has room.
if grep -q "jlumbroso/free-disk-space" \
        "${REPO_ROOT}/.github/workflows/build-yolov8-weights.yml"; then
    pass
else
    fail "build workflow must use jlumbroso/free-disk-space (multi-arch builds OOM otherwise)"
fi

start_test "yolov8-weights Dockerfile final stage is COPY-only (no install)"
final_stage=$(awk '/^FROM alpine:3.20/,EOF' \
    "${REPO_ROOT}/examples/yolov8-weights/Dockerfile")
if echo "$final_stage" | grep -qE "^\s*RUN .*apk add|^\s*RUN .*apt-get|^\s*RUN .*pip install"; then
    fail "final stage of yolov8-weights Dockerfile must not RUN apk/apt/pip"
else
    pass
fi

# ── 4c. weights image Dockerfile doesn't depend on apt for curl ──
# ISSUE-7 v3 specifically dropped `apt-get install curl` from the
# Dockerfile build stage (replaced with Python urllib). If a future
# contributor adds it back, every operator behind a Debian-repo
# filter regresses.
start_test "yolov8-weights Dockerfile does not apt-get install curl at build time"
# Match only actual RUN lines, not Dockerfile comments that
# explain why we don't do this. `^[[:space:]]*RUN ` is the gate.
if grep -E "^[[:space:]]*RUN " "${REPO_ROOT}/examples/yolov8-weights/Dockerfile" \
        | grep -qE "apt-get .*install.*curl|apt install.*curl"; then
    fail "weights Dockerfile must not RUN apt-get install curl — use Python urllib"
else
    pass
fi

# ── 5. positive contract: mediamtx's final base is curlimages/curl ──
# ISSUE-7 v4 fix: the v1 pattern (alpine final base + COPY of just
# the curl binary) was broken because curl needs libcurl.so.4 and
# other dynamic libraries at runtime. Using curlimages/curl as the
# FINAL base means curl AND its libs are correctly wired. We then
# COPY only the mediamtx Go binary in (statically linked, no deps).
# The contract being pinned: the LAST FROM in mediamtx's
# dockerfile_inline must be `curlimages/curl:*`.
start_test "mediamtx dockerfile_inline final base is curlimages/curl (libs wired)"
standard_inline=$(python3 - "${REPO_ROOT}" <<'PY'
import sys, yaml
root = sys.argv[1]
c = yaml.safe_load(open(root + "/docker-compose.yml"))
print(c["services"]["mediamtx"]["build"]["dockerfile_inline"])
PY
)
# Extract the LAST FROM line — that's the final stage's base image.
final_base=$(echo "$standard_inline" | grep -E "^[[:space:]]*FROM " | tail -1 | awk '{print $2}')
if echo "$final_base" | grep -qE "^curlimages/curl:"; then
    pass
else
    fail "mediamtx final stage base must be curlimages/curl:<tag> (got: '$final_base'). The alpine + COPY-curl-binary pattern is BROKEN because curl needs its dynamic libs."
fi

# ── 5a. negative contract: final stage is NOT plain alpine ──
# Lock the inverse — anyone reverting to `FROM alpine:3.20` as the
# final base regresses to the libcurl.so.4 missing bug.
start_test "mediamtx dockerfile_inline final base is NOT plain alpine (libcurl.so.4 missing)"
if echo "$final_base" | grep -qE "^alpine:"; then
    fail "mediamtx final stage must not be plain alpine — curl needs its libs"
else
    pass
fi

# ── 5b. WORKDIR / is reset after the base switch ──
# curlimages/curl sets WORKDIR /home/curl_user. mediamtx looks for
# its config in CWD, so without resetting WORKDIR it loads from
# /home/curl_user/mediamtx.yml — silent fallback to built-in
# defaults, hardened YAML config ignored (RTSPS doesn't bind,
# RTMP/SRT come up enabled, etc.). ISSUE-7 v5.
start_test "mediamtx dockerfile_inline resets WORKDIR / after base switch"
if echo "$standard_inline" | grep -qE "^[[:space:]]*WORKDIR[[:space:]]+/[[:space:]]*$"; then
    pass
else
    fail "mediamtx Dockerfile must include 'WORKDIR /' so the hardened config at /mediamtx.yml is found"
fi

# ── 5c. USER root reset (mediamtx needs to bind listener ports) ──
start_test "mediamtx dockerfile_inline resets USER root after base switch"
if echo "$standard_inline" | grep -qE "^[[:space:]]*USER[[:space:]]+root[[:space:]]*$"; then
    pass
else
    fail "mediamtx Dockerfile must include 'USER root' so it can bind listener ports"
fi

# ── 5d. CMD [] defensively clears inherited args ──
# Self-review post-v5: if a future curlimages/curl tag adds a CMD
# directive (e.g. ["--help"]), it would be passed as args to our
# ENTRYPOINT ["/mediamtx"], breaking the boot silently. We clear
# it explicitly.
start_test "mediamtx dockerfile_inline clears inherited CMD with CMD []"
if echo "$standard_inline" | grep -qE "^[[:space:]]*CMD[[:space:]]+\[\][[:space:]]*$"; then
    pass
else
    fail "mediamtx Dockerfile must include 'CMD []' so inherited base-image CMD doesn't leak as args to mediamtx"
fi

# ── 6. base images come from Docker Hub only (no external regs) ──
# Operators behind ISP filters typically still have Docker Hub reachable
# (Docker.io is the universal baseline; if it's blocked nothing works at
# all). We assert every FROM in every dockerfile_inline either:
#   * has no registry prefix (= Docker Hub default), OR
#   * uses ghcr.io/open-nvr/* (= our own registry, optional but allowed).
# Anything else (quay.io, registry.gitlab.com, a private registry) is a
# new dependency operators may not be able to reach.
start_test "every FROM in build inline uses Docker Hub or ghcr.io/open-nvr"
disallowed=$(python3 - <<PY
import yaml, re
bad = []
for fn in ("docker-compose.yml", "docker-compose.camera-agent.yml"):
    c = yaml.safe_load(open("${REPO_ROOT}/" + fn))
    for name, svc in (c.get("services") or {}).items():
        build = svc.get("build")
        if not isinstance(build, dict): continue
        inline = build.get("dockerfile_inline") or ""
        for line in inline.splitlines():
            m = re.match(r'^\s*FROM\s+(\S+)', line)
            if not m: continue
            img = m.group(1)
            host = img.split("/")[0] if "/" in img else ""
            # Docker Hub: no host or host has no dot (e.g. "alpine", "library/alpine")
            if not host or "." not in host:
                continue
            # Our own registry is allowed.
            if img.startswith("ghcr.io/open-nvr/"):
                continue
            bad.append(f"{fn}:{name}: FROM {img}")
print("\n".join(bad))
PY
)
if [ -z "$disallowed" ]; then
    pass
else
    fail "non-Hub registry referenced in build:"$'\n'"${disallowed}"
fi

# ════════════════════════════════════════════════════════════
# ISSUE-7 v6: runtime ``command:`` block contract
# ════════════════════════════════════════════════════════════
# Reuses the same forbidden-pattern set as the build-time checks
# above, but applied to every service's ``command:`` block across
# every compose file. Comment lines (starting with ``#`` after
# optional whitespace) are skipped — explanatory references like
# ``# was apk add gettext, now sed`` must not trigger a fail.
find_command_violations() {
    python3 - "$REPO_ROOT" <<'PY'
import sys, os, yaml, glob

root = sys.argv[1]
# Forbidden runtime package-install patterns. The regex equivalents
# would catch more variants but plain ``in`` is enough for our shapes.
forbidden = [
    "apk add",
    "apk update",
    "apt-get install",
    "apt install",
    "pip install",
    "pip3 install",
]

# Every compose file we ship.
for fn in sorted(glob.glob(os.path.join(root, "docker-compose*.yml"))):
    try:
        c = yaml.safe_load(open(fn))
    except Exception as e:
        print(f"{os.path.basename(fn)}:YAML_ERROR: {e}")
        continue
    for name, svc in (c.get("services") or {}).items():
        cmd = svc.get("command")
        if cmd is None:
            continue
        # ``command:`` can be a string or a list. When it's a list
        # with ``sh -c <script>`` the script is the last element.
        # When it's a plain string, treat the whole thing as the
        # script.
        if isinstance(cmd, list):
            scripts = [str(x) for x in cmd]
        else:
            scripts = [str(cmd)]
        for script in scripts:
            for lineno, line in enumerate(script.splitlines(), 1):
                s = line.lstrip()
                if not s or s.startswith("#"):
                    continue
                for pat in forbidden:
                    if pat in line:
                        print(f"{os.path.basename(fn)}:{name}:{lineno}: {line.rstrip()}")
                        break
PY
}

start_test "no runtime package install (apk/apt/pip) in any compose command: block"
# ISSUE-7 v6: the cert-init containers used ``apk add openssl`` at
# startup and camera-agent-config-init used ``apk add gettext``.
# Both reached dl-cdn.alpinelinux.org which several operator ISPs
# filter. Fix: cert-init now uses alpine/openssl base (openssl
# pre-baked), camera-agent-config-init uses sed instead of envsubst
# (sed is in busybox). This regression test locks the class of bug.
runtime_violations=$(find_command_violations)
if [ -z "$runtime_violations" ]; then
    pass
else
    fail "runtime package install found in compose command:"$'\n'"${runtime_violations}"
fi

# ── 7. cert-init services use alpine/openssl, not plain alpine ──
# Positive contract: lock the fix shape so a future contributor
# can't revert to alpine:3.20 + apk add openssl.
start_test "mediamtx-certs-init uses alpine/openssl base"
cert_init_bases=$(python3 - "$REPO_ROOT" <<'PY'
import sys, os, yaml
root = sys.argv[1]
out = []
for fn in ("docker-compose.yml",):
    c = yaml.safe_load(open(os.path.join(root, fn)))
    svc = c["services"]["mediamtx-certs-init"]
    out.append(f"{fn}:{svc.get('image','(unset)')}")
print("\n".join(out))
PY
)
if echo "$cert_init_bases" | grep -vq "alpine/openssl:"; then
    fail "every mediamtx-certs-init must use alpine/openssl:* (no runtime apk needed). Got: $cert_init_bases"
else
    pass
fi

start_test "nginx-certs-init (standard) uses alpine/openssl base"
nginx_cert_base=$(python3 - "$REPO_ROOT" <<'PY'
import sys, os, yaml
c = yaml.safe_load(open(os.path.join(sys.argv[1], "docker-compose.yml")))
print(c["services"]["nginx-certs-init"].get("image", "(unset)"))
PY
)
if echo "$nginx_cert_base" | grep -q "^alpine/openssl:"; then
    pass
else
    fail "nginx-certs-init must use alpine/openssl:* base, got: $nginx_cert_base"
fi

# ── 7a. cert-init services explicitly clear ENTRYPOINT ──
# ``alpine/openssl`` sets ENTRYPOINT ["openssl"]. Without an explicit
# ``entrypoint: []`` override, our ``command: [sh, -c, ...]`` would
# be passed as ARGS to openssl, not run as the container's argv.
# That would silently break the cert-init container.
start_test "cert-init services clear the inherited ENTRYPOINT (entrypoint: [])"
ep_check=$(python3 - "$REPO_ROOT" <<'PY'
import sys, os, yaml
root = sys.argv[1]
bad = []
specs = [
    ("docker-compose.yml", "mediamtx-certs-init"),
    ("docker-compose.yml", "nginx-certs-init"),
]
for fn, name in specs:
    c = yaml.safe_load(open(os.path.join(root, fn)))
    ep = c["services"][name].get("entrypoint", "<unset>")
    if ep != []:
        bad.append(f"{fn}:{name} entrypoint={ep!r} (must be [])")
print("\n".join(bad))
PY
)
if [ -z "$ep_check" ]; then
    pass
else
    fail "$ep_check"
fi

# ── 7b. camera-agent-config-init: sed (busybox) instead of envsubst ──
# Positive contract: the substitution toolchain is sed-based so it
# doesn't need ``apk add gettext``. If someone reverts to envsubst,
# this catches it.
start_test "camera-agent-config-init uses sed (busybox), not envsubst"
config_init_cmd=$(python3 - "$REPO_ROOT" <<'PY'
import sys, os, yaml
c = yaml.safe_load(open(os.path.join(sys.argv[1], "docker-compose.camera-agent.yml")))
cmd = c["services"]["camera-agent-config-init"]["command"]
print("\n".join(cmd) if isinstance(cmd, list) else str(cmd))
PY
)
# Want at least one ``sed -e`` on a non-comment line, and no envsubst.
has_sed=$(echo "$config_init_cmd" | grep -vE '^\s*#' | grep -cE '\bsed\s+-e')
has_envsubst=$(echo "$config_init_cmd" | grep -vE '^\s*#' | grep -c 'envsubst')
if [ "$has_sed" -gt 0 ] && [ "$has_envsubst" -eq 0 ]; then
    pass
else
    fail "camera-agent-config-init must use sed (busybox), not envsubst. has_sed=$has_sed has_envsubst=$has_envsubst"
fi

# ════════════════════════════════════════════════════════════
# ISSUE-11: external-Dockerfile pull-or-build contract
# ════════════════════════════════════════════════════════════
# The ISSUE-7 → v6 work only audited ``dockerfile_inline:`` blocks
# in compose files. It missed an entire category: external
# Dockerfiles referenced via ``build: dockerfile: <path>`` — most
# notably the root ``./Dockerfile`` used by opennvr-core. That
# Dockerfile does ``RUN apt-get install`` at both the python-builder
# stage AND the runtime stage (supervisor, gosu, libpq5, libgl1,
# libglib2.0-0, ...) for legitimate reasons — the GHA publish-images
# workflow runs it on an unfiltered ubuntu-latest runner and pushes
# the result to ghcr.io/open-nvr/core. The Dockerfile itself is
# fine; what's NOT fine is forcing operators on filtered networks
# to BUILD it locally. They can't reach deb.debian.org.
#
# The contract this section locks: if a compose service references
# an external Dockerfile that does runtime package installs, the
# service MUST also declare ``image:`` + ``pull_policy: missing``
# so Compose pulls the pre-built image first and only falls back
# to local build when the registry is unreachable.
start_test "services with apt/apk/pip in external Dockerfiles declare image+pull_policy fallback"
external_violations=$(python3 - "$REPO_ROOT" <<'PY'
import sys, os, yaml, re
from pathlib import Path

REPO = Path(sys.argv[1])
COMPOSES = ("docker-compose.yml", "docker-compose.camera-agent.yml")

# Lines that pin the bug class.
RUNTIME_INSTALL_RE = re.compile(
    r"^\s*RUN\s+.*(apt-get\s+install|apt\s+install|apk\s+add|apk\s+update|pip\s+install|pip3\s+install)"
)

def dockerfile_does_runtime_install(df_path):
    """True if the file at df_path has any RUN <pkg-manager> install line."""
    if not df_path.exists():
        return False
    for line in df_path.read_text().splitlines():
        if RUNTIME_INSTALL_RE.search(line):
            return True
    return False

violations = []
for compose_fn in COMPOSES:
    fn = REPO / compose_fn
    if not fn.exists():
        continue
    c = yaml.safe_load(fn.read_text())
    for name, svc in (c.get("services") or {}).items():
        build = svc.get("build")
        if not isinstance(build, dict):
            continue
        # External Dockerfile = build.dockerfile is a path string,
        # NOT a heredoc'd dockerfile_inline.
        df_ref = build.get("dockerfile")
        if not df_ref or build.get("dockerfile_inline"):
            continue
        # Skip profile-gated services. The contract this test
        # enforces is "default operator deploys must not require
        # access to filtered package mirrors". Services hidden
        # behind ``profiles: [...]`` are explicitly opt-in — the
        # operator who runs ``--profile ai`` or
        # ``--profile camera-agent`` is making a deliberate choice
        # and accepts the build-side burden. Tracked as
        # known-gap follow-ups (ai-adapter and camera-agent both
        # need published images before they can be folded into
        # the default-active path).
        if svc.get("profiles"):
            continue
        # Resolve the path relative to the build context.
        ctx = build.get("context", ".")
        df_path = (REPO / ctx / df_ref).resolve()
        if not dockerfile_does_runtime_install(df_path):
            continue
        # Service's Dockerfile installs packages at build time. The
        # service MUST provide a pull fallback: ``image:`` set AND
        # ``pull_policy: missing`` (so Compose pulls first, falls
        # back to local build only if pull fails).
        has_image = bool(svc.get("image"))
        pp = svc.get("pull_policy")
        if not has_image:
            violations.append(
                f"{compose_fn}::{name}: external Dockerfile {df_ref} does runtime "
                f"package install, but service has no `image:` — operators on "
                f"filtered networks (deb.debian.org / pypi.org / dl-cdn.alpinelinux.org) "
                f"can't build it locally and have no fallback to pull from."
            )
        elif pp != "missing":
            violations.append(
                f"{compose_fn}::{name}: has image+build but pull_policy is "
                f"{pp!r} (expected 'missing'). Without 'missing', Compose's "
                f"pull-then-build semantics aren't deterministic — some "
                f"operators will still hit the local-build apt-get path."
            )

for v in violations:
    print(v)
sys.exit(len(violations))
PY
)
ev_exit=$?
if [ "$ev_exit" -eq 0 ]; then
    pass
else
    fail "external Dockerfile pull-or-build contract violations:
${external_violations}

Fix: add to the affected service in the compose file:
    image: ghcr.io/open-nvr/<name>:\${TAG:-latest}
    pull_policy: missing
    build: ...          # keep the existing build block
The pre-built image is published by .github/workflows/publish-images.yml
(or equivalent). Compose pulls from GHCR first; if the pull fails
(registry blocked, fresh repo before first publish) it falls back
to building locally."
fi

# ── 11a. positive contract: opennvr-core uses pull-then-build across all composes ──
# Lock the specific service that hit the user. If a future refactor
# strips image+pull_policy from any compose file, this fails.
start_test "opennvr-core has image+build+pull_policy:missing in every compose where it builds"
core_shape=$(python3 - "$REPO_ROOT" <<'PY'
import sys, yaml
from pathlib import Path
REPO = Path(sys.argv[1])
bad = []
for fn in ("docker-compose.yml",):
    c = yaml.safe_load((REPO / fn).read_text())
    svc = c["services"].get("opennvr-core")
    if not svc:
        continue
    has_image = bool(svc.get("image"))
    has_build = bool(svc.get("build"))
    pp = svc.get("pull_policy")
    img = svc.get("image", "")
    # standard ships image-only (no build at all). That's fine — it's the
    # ultra-thin install-from-GHCR path. We only enforce the contract
    # when the service ALSO has a build block.
    if has_build:
        if not (has_image and pp == "missing"):
            bad.append(f"{fn}: image={has_image} pull_policy={pp!r}")
    # Whenever image: is set, it should reference our GHCR namespace.
    if has_image and not img.startswith("ghcr.io/open-nvr/core"):
        bad.append(f"{fn}: image={img!r} not under ghcr.io/open-nvr/core")
print("\n".join(bad))
PY
)
if [ -z "$core_shape" ]; then
    pass
else
    fail "opennvr-core compose shape violations:
${core_shape}"
fi

# ── Summary ────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────"
echo "Tests run    : ${TESTS_RUN}"
echo "Tests failed : ${TESTS_FAILED}"
if [ "$TESTS_FAILED" -eq 0 ]; then echo "Result       : all green"; exit 0
else echo "Result       : failures"; exit 1; fi
