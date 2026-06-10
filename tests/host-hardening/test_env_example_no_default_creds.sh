#!/usr/bin/env bash
# ============================================================
# Static contract test for .env.example (ISSUE-26).
#
# CONTEXT
# -------
# .env.example previously shipped a hardcoded
# ``DEFAULT_ADMIN_PASSWORD=SecurePass123!`` line. ``cp .env.example
# .env`` (operator's first manual step) or the install wizard
# silently inherited that value, and ``init_db.py`` then seeded the
# admin user with ``password_set=True`` and that exact credential.
#
# Result: every fresh deploy used the literal string from a
# checked-in template as the admin password. The operator never saw
# the setup-token UI flow they expected, the install wizard didn't
# mention the credential at all, and the admin was a globally-known
# default — the exact V-001 anti-pattern OpenNVR positions itself
# against (Zenodo paper §3.1, ETSI EN 303 645 unique-credential).
#
# CONTRACT
# --------
# DEFAULT_ADMIN_PASSWORD in .env.example MUST be empty. The
# secure-by-default install path is the one-time setup token —
# operators who want a provisioned bootstrap can set the value
# explicitly themselves, sourced from a secrets manager.
#
# Same contract applies to ``DEFAULT_ADMIN_PASSWORD`` if it ever
# appears in a docker-compose file's ``environment:`` block as a
# hardcoded literal — that would re-introduce the bug at a
# different layer.
#
# Run with: bash tests/host-hardening/test_env_example_no_default_creds.sh
# ============================================================

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running .env.example default-credentials tests"
echo ""

# ── 1. .env.example's DEFAULT_ADMIN_PASSWORD is empty ────────
# Match exactly ``DEFAULT_ADMIN_PASSWORD=`` followed by end-of-line
# or whitespace only. Anything after the ``=`` triggers a fail.
# Lines starting with ``#`` (comments) are skipped — we only care
# about the actual env-var assignment.
start_test ".env.example ships DEFAULT_ADMIN_PASSWORD empty (no shipped credential)"
bad_line=$(grep -nE '^DEFAULT_ADMIN_PASSWORD=' "${REPO_ROOT}/.env.example" \
           | grep -vE '^[0-9]+:DEFAULT_ADMIN_PASSWORD=$' || true)
if [ -z "$bad_line" ]; then
    pass
else
    fail ".env.example ships a non-empty DEFAULT_ADMIN_PASSWORD:
${bad_line}

This re-introduces the ISSUE-26 bug. Operators who ``cp .env.example
.env`` will inherit the value, init_db.py will seed admin with that
exact password + password_set=True, and the secure-by-default setup-
token flow gets silently bypassed. Reset the value to:
    DEFAULT_ADMIN_PASSWORD=
(with nothing after the ``='') and let the setup-token path be the
default path. Operators who genuinely need a provisioned bootstrap
will set it themselves from a secrets manager."
fi

# ── 2. no compose file pins DEFAULT_ADMIN_PASSWORD to a literal ──
# Catches the same bug at a different layer: if anyone ever puts
# ``- DEFAULT_ADMIN_PASSWORD=something`` in a compose service's
# environment block, that overrides the .env value at runtime and
# the contract above is bypassed.
start_test "no compose file pins DEFAULT_ADMIN_PASSWORD to a literal value"
hits=$(grep -nE 'DEFAULT_ADMIN_PASSWORD=[^$ ]' \
       "${REPO_ROOT}"/docker-compose*.yml 2>/dev/null \
       | grep -v 'DEFAULT_ADMIN_PASSWORD=\${' \
       || true)
if [ -z "$hits" ]; then
    pass
else
    fail "compose file(s) pin DEFAULT_ADMIN_PASSWORD to a literal:
${hits}

If you need to pass the value through to a container, use
``DEFAULT_ADMIN_PASSWORD=\${DEFAULT_ADMIN_PASSWORD}`` so the actual
value comes from the operator's .env (or stays empty for the
setup-token flow)."
fi

# ── 3. install.sh never writes a literal DEFAULT_ADMIN_PASSWORD ──
# The interactive installer at scripts/install.sh should NEVER bake
# a literal password into the .env it writes. If it does, every
# fresh install picks up the same credential.
start_test "scripts/install.sh does not write a literal DEFAULT_ADMIN_PASSWORD"
if [ -f "${REPO_ROOT}/scripts/install.sh" ]; then
    bad=$(grep -nE 'DEFAULT_ADMIN_PASSWORD=[A-Za-z0-9!]+' \
          "${REPO_ROOT}/scripts/install.sh" \
          | grep -v '#' || true)
    if [ -z "$bad" ]; then
        pass
    else
        fail "scripts/install.sh writes a literal DEFAULT_ADMIN_PASSWORD value:
${bad}
The installer must either leave the value empty (preferred — setup
token flow) or read it from an operator prompt / a secrets source
that doesn't end up in the repo."
    fi
else
    pass   # no installer in this checkout
fi

# ── 4. init_db.py rejects historical .env.example defaults at runtime ──
# Defense in depth (ISSUE-27): even if a stray .env file ends up with
# DEFAULT_ADMIN_PASSWORD=SecurePass123! (from old tutorials, AI-
# generated examples, half-migrated installs), init_db.py must
# refuse to honor it and fall through to the setup-token flow.
# Parse init_db.py with ast and assert the KNOWN_BAD_PASSWORDS set
# contains at minimum the historical value plus common weak ones.
start_test "init_db.py rejects historical .env.example default (SecurePass123!) at runtime"
result=$(python3 - "${REPO_ROOT}" <<'PY'
import ast, sys
from pathlib import Path
src = (Path(sys.argv[1]) / "server/scripts/init_db.py").read_text()
tree = ast.parse(src)

# Find KNOWN_BAD_PASSWORDS = frozenset({...}) — recurse the whole tree
# because the assignment lives inside a function body, not at module
# top-level.
bad_set = None
for node in ast.walk(tree):
    if not isinstance(node, ast.Assign):
        continue
    for target in node.targets:
        if not (isinstance(target, ast.Name) and
                target.id == "KNOWN_BAD_PASSWORDS"):
            continue
        # Match frozenset({...}) — value is a Call with func.id frozenset
        v = node.value
        if (isinstance(v, ast.Call)
            and isinstance(v.func, ast.Name)
            and v.func.id == "frozenset"
            and v.args
            and isinstance(v.args[0], ast.Set)):
            bad_set = {
                e.value for e in v.args[0].elts
                if isinstance(e, ast.Constant)
            }
            break

if bad_set is None:
    print("KNOWN_BAD_PASSWORDS not found in init_db.py")
    sys.exit(1)

# Required entries — the historical .env.example default plus the
# weak passwords every threat-modeller's checklist mentions.
REQUIRED = {"securepass123!", "admin", "password", "changeme"}
missing = REQUIRED - bad_set
if missing:
    print(f"KNOWN_BAD_PASSWORDS missing required entries: {sorted(missing)}")
    print(f"Actual set: {sorted(bad_set)}")
    sys.exit(1)
print("ok")
PY
)
if echo "$result" | grep -q "^ok"; then
    pass
else
    fail "${result}
init_db.py's KNOWN_BAD_PASSWORDS reject list must include the
historical .env.example default ('securepass123!') plus common
weak values, so a stray DEFAULT_ADMIN_PASSWORD reaching the
runtime is refused and falls through to the setup-token flow."
fi

# ── Summary ────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────"
echo "Tests run    : ${TESTS_RUN}"
echo "Tests failed : ${TESTS_FAILED}"
if [ "$TESTS_FAILED" -eq 0 ]; then echo "Result       : all green"; exit 0
else echo "Result       : failures"; exit 1; fi
