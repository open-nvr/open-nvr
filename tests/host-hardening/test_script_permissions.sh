#!/usr/bin/env bash
# ============================================================
# Tests that every operator-facing shell script in the repo is
# marked executable in git's index (100755, not 100644).
#
# Why this matters: the README's quickstart instructs operators
# to run scripts via `./start.sh up`, `./scripts/generate-secrets.sh
# --write`, etc. If a script was committed without `git update-index
# --chmod=+x`, the operator hits "Permission denied" and has to
# `chmod +x` it manually on every fresh checkout. This test catches
# the moment any maintainer adds a new script without the bit set,
# instead of every operator hitting the same trap.
#
# Run with: bash tests/host-hardening/test_script_permissions.sh
# ============================================================

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running script-permission tests"
echo ""

# ── 1. git is available + we're in a repo ────────────────────
start_test "git is available + this is a repo"
if git rev-parse --git-dir >/dev/null 2>&1; then
    pass
else
    fail "not a git repo (or git not in PATH) — test cannot run"
    echo ""
    echo "Tests run    : ${TESTS_RUN}"
    echo "Tests failed : ${TESTS_FAILED}"
    exit 1
fi

# ── 2. every operator-runnable .sh has mode 100755 in git index ──
# "Operator-runnable" = .sh at repo top level, OR under scripts/,
# OR under tests/host-hardening/ (we run these by hand during
# review). Other places (vendored / generated / submodule) are
# excluded so we don't flag things outside our control.
start_test "every top-level + scripts/ + tests/ .sh is mode 100755 in git"
non_exec=$(git ls-files --stage \
    '*.sh' 'scripts/*.sh' 'tests/host-hardening/*.sh' 2>/dev/null \
  | awk '$1 != "100755" {print $1 " " $4}')
if [ -z "$non_exec" ]; then
    pass
else
    fail "the following .sh files lack the executable bit in git:
${non_exec}

Fix with:
    git update-index --chmod=+x <file>
    git add <file>      # (commit the index update)
    git commit -m 'mark <file> executable'"
fi

# ── 2a. ISSUE-9 regression: the host-hardening test files are tracked ──
# Discovered 2026-06-09: ``.gitignore`` had an un-anchored
# ``host-hardening/`` pattern that excluded ``./tests/host-hardening/``
# along with the operator-artifact dir ``./host-hardening/``. The
# entire test suite (ISSUE-6 → ISSUE-8) was never committed —
# every "X tests green" claim in CHANGELOG was working-tree-only.
# Tests 2 and 3 below took a globbed file list as input, so an empty
# list passed vacuously — they couldn't detect their own absence.
#
# This test specifically asserts each suite's source file is
# tracked. It can't pass vacuously: if the list is empty (i.e. no
# *.sh files exist where we expect them), it fails loudly.
start_test "host-hardening test suite files are tracked in git (not gitignored)"
expected_suites="
_lib.sh
test_build_resilience.sh
test_cert_san.sh
test_compose_file_selection.sh
test_docker_subnets.sh
test_env_example_no_default_creds.sh
test_kai_c_runtime_deps.sh
test_media_proxy.sh
test_nftables_template.sh
test_script_permissions.sh
test_security_posture.sh
test_setup_token_banner.sh
test_url_fallback_chain.sh
test_v022_sovereignty_docker_bridge.sh
"
problems=""
for f in $expected_suites; do
    path="tests/host-hardening/$f"
    # 1. The file must exist on disk.
    if [ ! -f "$path" ]; then
        problems+="$path: file does not exist on disk"$'\n'
        continue
    fi
    # 2. The file must NOT be gitignored (an ignore rule would
    #    silently keep it out of commits even if it's on disk).
    if git check-ignore -q "$path" 2>/dev/null; then
        ignored_by=$(git check-ignore -v "$path" 2>/dev/null)
        problems+="$path: gitignored by $ignored_by"$'\n'
        continue
    fi
    # 3. The file must be tracked in git's index. ``not-ignored``
    #    alone isn't enough — an untracked file is also not
    #    ignored, but won't ship to other contributors. ``ls-files``
    #    returns the path iff it's tracked.
    if [ -z "$(git ls-files --error-unmatch "$path" 2>/dev/null)" ]; then
        problems+="$path: not tracked in git (run: git add $path)"$'\n'
        continue
    fi
done
if [ -z "$problems" ]; then
    pass
else
    fail "host-hardening test suite tracking problems:
${problems}
This is the ISSUE-9 class of bug: regression tests claimed
'green' in CHANGELOG but live only in local working trees. Run
``git add tests/host-hardening/`` to track them, and audit
.gitignore for any un-anchored ``host-hardening/`` line —
change it to ``/host-hardening/`` (root-anchored) so it only
matches the operator-artifact dir at repo root."
fi

# ── 3. every .sh that's executable in git is ALSO executable on disk ──
# Catches the inverse failure mode — someone fixed the git bit but
# the working tree's filesystem permissions didn't propagate (rare,
# but happens on Windows checkouts and some FUSE filesystems).
start_test "every git-executable .sh is also executable on the filesystem"
mismatches=""
while IFS= read -r line; do
    mode=$(echo "$line" | awk '{print $1}')
    path=$(echo "$line" | awk '{print $4}')
    if [ "$mode" = "100755" ] && [ -f "$path" ] && [ ! -x "$path" ]; then
        mismatches+="$path"$'\n'
    fi
done < <(git ls-files --stage '*.sh' 'scripts/*.sh' 'tests/host-hardening/*.sh' 2>/dev/null)
if [ -z "$mismatches" ]; then
    pass
else
    fail "the following .sh files are 100755 in git but lack +x on disk:
${mismatches}
This usually means the working tree was extracted by a tool that
doesn't preserve the executable bit (e.g. some Windows zip tools,
or core.fileMode=false in .gitconfig). Run:
    chmod +x ${mismatches}
to fix the working tree."
fi

echo ""
echo "────────────────────────────────────────────"
echo "Tests run    : ${TESTS_RUN}"
echo "Tests failed : ${TESTS_FAILED}"
if [ "$TESTS_FAILED" -eq 0 ]; then echo "Result       : all green"; exit 0
else echo "Result       : failures"; exit 1; fi
