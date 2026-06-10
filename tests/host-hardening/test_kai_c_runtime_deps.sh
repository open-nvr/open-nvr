#!/usr/bin/env bash
# ============================================================
# Static contract test for kai-c runtime dependencies (ISSUE-24).
#
# CONTEXT
# -------
# kai-c crashed in production with ``ModuleNotFoundError: No module
# named 'httpx'`` because httpx was misplaced in
# kai-c/pyproject.toml's ``[dependency-groups].dev`` group. The
# Dockerfile correctly does ``uv sync --no-dev`` which skips dev
# deps — and then kai_c/registry.py crashes at import time because
# the module top-level does ``import httpx``.
#
# The comment next to httpx in pyproject.toml said "no httpx in the
# live path" — that was aspirational, not accurate. Multiple
# production-code paths in registry.py use ``httpx.AsyncClient``.
#
# WHAT THIS TEST DOES
# -------------------
# Walks every Python file under kai-c/kai_c/ (production code, NOT
# tests). For each one, parses the AST and collects every
# top-level ``import X`` and ``from X import Y`` where X is a
# third-party module (not stdlib). Asserts each such X has a
# corresponding entry in kai-c/pyproject.toml's
# ``[project].dependencies`` array.
#
# Catches the class of bug structurally: any future PR that adds a
# top-level import of a non-stdlib module without also adding it to
# runtime deps fails this test before reaching main.
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

echo "Running kai-c runtime-deps contract tests"
echo ""

# ── 1. every top-level import in production code is a runtime dep ──
start_test "every top-level import in kai-c/kai_c/ is in pyproject.toml [project].dependencies"
violations=$(python3 - "${REPO_ROOT}" <<'PY'
import ast
import sys
from pathlib import Path

REPO = Path(sys.argv[1])
KAI_C_PROD = REPO / "kai-c" / "kai_c"
PYPROJECT = REPO / "kai-c" / "pyproject.toml"

# Parse pyproject's [project].dependencies. tomllib ships in
# Python 3.11+. For 3.10 / 3.9 (sandbox / older CI runners) we
# fall back to a line-scanner that strips comments BEFORE looking
# for the closing ``]`` — a naive regex breaks when comments inside
# the list contain literal ``]`` characters (e.g. when our own
# explanatory text references TOML section headers like
# ``[dependency-groups]`` inside a comment).
import re
def parse_deps_fallback(text):
    in_proj = False
    in_deps = False
    items = []
    for raw_line in text.splitlines():
        # Track which TOML section we're in.
        m = re.match(r"^\[([^\]]+)\]\s*$", raw_line)
        if m:
            in_proj = (m.group(1) == "project")
            continue
        if not in_proj:
            continue
        # Strip line comments before searching for tokens. ``#`` not
        # inside quotes starts a comment.
        line = re.sub(r'(?<!["\'])#.*$', "", raw_line).rstrip()
        if not in_deps:
            if re.match(r'^\s*dependencies\s*=\s*\[', line):
                in_deps = True
            continue
        # Inside dependencies block — look for terminator ``]`` on a
        # line by itself, OR collect quoted entries.
        if re.match(r'^\s*\]\s*$', line):
            in_deps = False
            continue
        items += re.findall(r'"([^"]+)"', line)
    return items

try:
    import tomllib
    with open(PYPROJECT, "rb") as f:
        data = tomllib.load(f)
    deps_raw = data.get("project", {}).get("dependencies", []) or []
except ImportError:
    deps_raw = parse_deps_fallback(PYPROJECT.read_text())

# Strip version specifiers — we only care about the package name.
# Match the part before any of: >=, <=, ==, ~=, >, <, ;, [
def dep_name(s):
    return re.split(r'[<>=!~;\[ ]', s, 1)[0].strip().lower()
declared = {dep_name(d) for d in deps_raw}

# Some packages have an import name that differs from the PyPI name.
# Hardcode the common ones — extend as needed.
IMPORT_TO_PYPI = {
    "yaml": "pyyaml",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "bs4": "beautifulsoup4",
    # nats-py exposes the ``nats`` import name
    "nats": "nats-py",
}

# Standard-library modules a top-level import is allowed to reference
# without needing a pyproject entry. (Python 3.11+ ships sys.stdlib_module_names
# but a fixed subset is enough for this codebase.)
STDLIB = {
    "__future__",  # compiler directive, not a real package
    "abc", "argparse", "ast", "asyncio", "base64", "collections",
    "concurrent", "contextlib", "copy", "csv", "ctypes",
    "dataclasses", "datetime", "decimal", "difflib", "enum",
    "errno", "fcntl", "functools", "gc", "glob", "gzip", "hashlib",
    "hmac", "html", "http", "importlib", "inspect", "io",
    "ipaddress", "itertools", "json", "logging", "math", "multiprocessing",
    "numbers", "operator", "os", "pathlib", "pickle", "platform",
    "pprint", "queue", "random", "re", "secrets", "select",
    "shlex", "shutil", "signal", "socket", "ssl", "stat", "string",
    "struct", "subprocess", "sys", "tempfile", "textwrap", "threading",
    "time", "tomllib", "traceback", "types", "typing", "unicodedata",
    "urllib", "uuid", "warnings", "weakref", "xml", "zipfile",
    # third-party-looking but in stdlib in 3.11+
    "zoneinfo",
}

# Transitive imports allowed because they come bundled with a
# declared runtime dep. Adding ``starlette`` separately to runtime
# deps is best practice (PEP 508) but the FastAPI install pulls it
# in by definition, so any kai-c install with fastapi has starlette
# available. Keep this list short and only when the bundling is
# unambiguous in the upstream package.
BUNDLED_VIA = {
    "starlette": "fastapi",
}

violations = []
for py in sorted(KAI_C_PROD.rglob("*.py")):
    rel = py.relative_to(REPO)
    try:
        tree = ast.parse(py.read_text(), filename=str(py))
    except SyntaxError as e:
        violations.append(f"{rel}: parse error: {e}")
        continue
    # Only top-level imports — skip imports inside functions / classes /
    # try blocks. Module-load-time imports are what crash the container.
    for node in tree.body:
        def ok(root):
            if root in STDLIB or root == "kai_c":
                return True
            pypi = IMPORT_TO_PYPI.get(root, root).lower()
            if pypi in declared:
                return True
            carrier = BUNDLED_VIA.get(root)
            if carrier and carrier.lower() in declared:
                return True
            return False
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if not ok(root):
                    pypi = IMPORT_TO_PYPI.get(root, root).lower()
                    violations.append(
                        f"{rel}:{node.lineno}: import {root} — "
                        f"package {pypi!r} not in [project].dependencies"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                root = node.module.split(".")[0]
                if not ok(root):
                    pypi = IMPORT_TO_PYPI.get(root, root).lower()
                    violations.append(
                        f"{rel}:{node.lineno}: from {root} import ... — "
                        f"package {pypi!r} not in [project].dependencies"
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
    fail "found ${ev_exit} top-level import(s) of packages not declared as runtime deps:
${violations}

Each violation is a kai-c crash waiting to happen — the Dockerfile
runs ``uv sync --no-dev`` which only installs runtime deps. Move the
package from ``[dependency-groups].dev`` (or wherever it's currently
declared) to ``[project].dependencies`` in kai-c/pyproject.toml, then
regenerate the lock: ``cd kai-c && uv lock``."
fi

# ── 2. positive contract: httpx specifically (the bug this test was built for) ──
# Without this explicit pin, a regression that strips httpx from the
# runtime deps + ALSO removes the registry.py import line would pass
# test 1 above (no orphan import) but break the production paths
# that need httpx. This is an explicit "we use this at runtime" pin.
start_test "httpx is in kai-c [project].dependencies (registry.py needs it at runtime)"
result=$(python3 - "${REPO_ROOT}" <<'PY'
import sys
from pathlib import Path
try:
    import tomllib
    data = tomllib.loads((Path(sys.argv[1]) / "kai-c/pyproject.toml").read_text())
    deps = data.get("project", {}).get("dependencies", []) or []
except ImportError:
    # Same line-scanner fallback as test 1 — handles comments that
    # contain literal ``]`` characters (e.g. ``[dependency-groups]``).
    import re
    text = (Path(sys.argv[1]) / "kai-c/pyproject.toml").read_text()
    in_proj = False
    in_deps = False
    deps = []
    for raw in text.splitlines():
        m = re.match(r"^\[([^\]]+)\]\s*$", raw)
        if m:
            in_proj = (m.group(1) == "project")
            continue
        if not in_proj:
            continue
        line = re.sub(r'(?<!["\'])#.*$', "", raw).rstrip()
        if not in_deps:
            if re.match(r'^\s*dependencies\s*=\s*\[', line):
                in_deps = True
            continue
        if re.match(r'^\s*\]\s*$', line):
            in_deps = False
            continue
        deps += re.findall(r'"([^"]+)"', line)
has_httpx = any(d.lower().startswith("httpx") for d in deps)
print("ok" if has_httpx else f"missing — declared: {deps}")
PY
)
if echo "$result" | grep -q "^ok"; then
    pass
else
    fail "httpx is not in kai-c/pyproject.toml [project].dependencies: ${result}
ISSUE-24 reproduces: kai-c/kai_c/registry.py imports httpx at module
top-level AND creates ``httpx.AsyncClient(trust_env=False)`` at runtime."
fi

# ── Summary ────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────"
echo "Tests run    : ${TESTS_RUN}"
echo "Tests failed : ${TESTS_FAILED}"
if [ "$TESTS_FAILED" -eq 0 ]; then echo "Result       : all green"; exit 0
else echo "Result       : failures"; exit 1; fi
