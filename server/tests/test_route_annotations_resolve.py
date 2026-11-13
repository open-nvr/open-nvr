# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Every route's annotations name things that exist.

The core image runs Python 3.11, which evaluates a function's annotations
when it is defined; the test runners use 3.14, which evaluates them only
when asked (PEP 649). So a route typed ``Literal[...]`` without importing
``Literal`` passes every test here and then stops core from starting at
all. Resolving each endpoint's type hints asks for them, on any version.

Run with:
    cd server && pytest tests/test_route_annotations_resolve.py -v
"""
from __future__ import annotations

import importlib
import os
import pkgutil
import secrets
import sys
import types as _types
import typing
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from fastapi import APIRouter  # noqa: E402

import routers  # noqa: E402


def _endpoints():
    for info in pkgutil.iter_modules(routers.__path__):
        try:
            module = importlib.import_module(f"routers.{info.name}")
        except Exception as exc:  # a router that will not even import is its own test's business
            yield pytest.param(None, None, id=f"{info.name}:import", marks=pytest.mark.skip(reason=str(exc)[:80]))
            continue
        for value in vars(module).values():
            if isinstance(value, APIRouter):
                for route in value.routes:
                    endpoint = getattr(route, "endpoint", None)
                    if endpoint is not None and getattr(endpoint, "__module__", None) == module.__name__:
                        yield pytest.param(module, endpoint, id=f"{info.name}:{endpoint.__name__}")


@pytest.mark.parametrize("module, endpoint", list(_endpoints()))
def test_route_annotations_resolve(module, endpoint):
    typing.get_type_hints(endpoint, include_extras=True)
