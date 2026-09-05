# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Suite-wide isolation for the ``core.*`` module objects.

Every test module is imported at collection time, so a module-level
``from core.config import settings`` binds the object that exists
*then*. A few suites (``test_m1b_mediamtx_hardening``,
``test_m1c_transport_probe``) later purge ``core`` and ``core.*`` from
``sys.modules`` to re-validate ``Settings`` against a fresh
environment. From that point on, any code that resolves the module
lazily — ``routers.apps._internal_api_key`` doing
``from core.config import settings`` at call time — sees a NEW
``settings`` object, while the test module's monkeypatch landed on
the OLD one. The symptom is a test that passes alone and fails in the
full run with a 401 (``test_registry_contract`` found it; several
suites carry private work-arounds for the same thing, e.g.
``test_camera_settings._stable_core_env``).

This fixture snapshots the ``core`` namespace of ``sys.modules``
before each test module runs and restores it afterwards, so a purge
is confined to the module that asked for it. Per-module scope, not
per-test: a module that purges expects its own later tests to keep
the fresh objects.
"""
from __future__ import annotations

import sys

import pytest

_NAMESPACE = ("core",)


def _is_core(name: str) -> bool:
    return any(name == ns or name.startswith(ns + ".") for ns in _NAMESPACE)


@pytest.fixture(autouse=True, scope="module")
def _restore_core_modules():
    before = {k: v for k, v in sys.modules.items() if _is_core(k)}
    yield
    for k in [k for k in sys.modules if _is_core(k)]:
        if k not in before:
            del sys.modules[k]
    sys.modules.update(before)
