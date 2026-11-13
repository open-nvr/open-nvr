# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""MediaMTX health must not answer anonymously.

Reported by Kamal Sentassi (S9S Security Research), coordinated
disclosure, 2026: ``GET /mediamtx/health`` was the only endpoint in
mediamtx_admin.py with no dependency, and no middleware covers the
router — so it answered unauthenticated, publishing
``mediamtx_admin_api`` (the internal admin URL) and raw exception text.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_SRC = os.path.join(os.path.dirname(__file__), "..", "routers", "mediamtx_admin.py")


def _tree():
    with open(_SRC, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _routes(tree):
    """Every decorated route handler in the module, name -> node."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                f = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(f, ast.Attribute) and f.attr in (
                    "get", "post", "put", "patch", "delete"
                ):
                    out[node.name] = node
    return out


def _dependency_names(fn):
    """Names of every Depends(...) default in the signature."""
    names = []
    for default in list(fn.args.defaults) + list(fn.args.kw_defaults):
        if isinstance(default, ast.Call) and getattr(default.func, "id", "") == "Depends":
            for arg in default.args:
                if isinstance(arg, ast.Name):
                    names.append(arg.id)
                elif isinstance(arg, ast.Attribute):
                    names.append(arg.attr)
    return names


def test_health_requires_authentication():
    fn = _routes(_tree())["mediamtx_health"]
    deps = _dependency_names(fn)
    assert any("get_current" in d for d in deps), (
        "GET /mediamtx/health has no auth dependency — it answers anonymously"
    )


def _verifies_in_body(fn):
    """Some routes authenticate inside the handler instead of via
    Depends — the MediaMTX runOn* hooks check a shared token on the
    request, because MediaMTX itself is the caller and carries no user
    session. That is a real guard and this test must recognise it, or it
    would push someone to "fix" a route that is already correct."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "id", None) or getattr(f, "attr", None) or ""
            if "verify" in name and "token" in name:
                return True
    return False


def test_every_route_in_the_router_is_authenticated():
    """The original bug was one endpoint missing what all its siblings
    had. Assert the property, not the single instance, so the next
    endpoint added here cannot repeat it."""
    unguarded = []
    for name, fn in _routes(_tree()).items():
        deps = _dependency_names(fn)
        by_dep = any("get_current" in d for d in deps)
        if not by_dep and not _verifies_in_body(fn):
            unguarded.append(name)
    assert not unguarded, f"unauthenticated mediamtx routes: {unguarded}"


def test_admin_url_is_not_returned_unconditionally():
    """The internal admin address is the sensitive part of the payload;
    it must sit behind the `detailed` (superuser) branch rather than
    being assembled into every response."""
    with open(_SRC, encoding="utf-8") as fh:
        src = fh.read()
    fn_src = src[src.index("async def mediamtx_health"):src.index("# ---- Control API passthrough")]
    for line in fn_src.splitlines():
        if "settings.mediamtx_admin_api" in line:
            indent = len(line) - len(line.lstrip())
            assert indent >= 12, (
                "admin_api is returned outside the superuser branch: " + line.strip()
            )
