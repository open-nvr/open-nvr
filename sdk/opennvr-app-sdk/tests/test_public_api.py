# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""The public API has a front door, and the front door is enforced.

Ninety-odd exports with no ordering is a wall, not an API: a reader
cannot tell what to learn first from what exists for the one app that
needs it. `API_TIERS` is that ordering, and because `__all__` is
assembled from it, the tiers cannot fall out of step with the exports —
and neither can the documentation site, which builds its navigation
from the same tuples.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import opennvr_app_sdk as sdk

#: Reachable from the package but deliberately not exported: the
#: submodules themselves, dunders, and names the SDK re-exports for its
#: own internal use.
NOT_PUBLIC = {"annotations", *(t.upper().replace("-", "_") for t in sdk.API_TIERS)}


def all_tiered() -> list[str]:
    return [name for tier in sdk.API_TIERS.values() for name in tier]


def test_every_tiered_name_exists():
    missing = [name for name in all_tiered() if not hasattr(sdk, name)]
    assert missing == [], f"tiered but not importable: {missing}"


def test_no_name_is_in_two_tiers():
    names = all_tiered()
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert duplicates == [], f"exported from more than one tier: {duplicates}"


def test_all_is_assembled_from_the_tiers():
    assert sdk.__all__ == ["API_TIERS", *all_tiered()]


def test_the_front_door_stays_small():
    """The first tier is what a developer reads before writing anything.
    If it grows past a handful of names it has stopped being a front
    door — move the newcomer to the tier it belongs in."""
    assert len(sdk.API_TIERS["front-door"]) <= 8


def test_every_public_name_is_tiered():
    """Nothing reachable and undocumented: a name the package exposes
    but no tier claims is a name nobody can find."""
    exported = set(sdk.__all__)
    submodules = {m.name for m in pkgutil.iter_modules(sdk.__path__)}
    stray = sorted(
        name for name in vars(sdk)
        if not name.startswith("_")
        and name not in exported
        and name not in submodules
        and name not in NOT_PUBLIC
    )
    assert stray == [], f"reachable but in no tier: {stray}"


@pytest.mark.parametrize("name", all_tiered())
def test_every_export_is_documented(name):
    """A docstring is not optional on the public surface — the
    documentation site is generated from these, so a missing one is a
    blank page."""
    obj = getattr(sdk, name)
    if not (inspect.isclass(obj) or inspect.isfunction(obj)):
        return                       # constants document themselves
    assert (obj.__doc__ or "").strip(), f"{name} has no docstring"


def test_every_submodule_has_a_module_docstring():
    for module in pkgutil.iter_modules(sdk.__path__):
        if module.name.startswith("_") or module.name == "templates":
            continue
        imported = importlib.import_module(f"opennvr_app_sdk.{module.name}")
        assert (imported.__doc__ or "").strip(), \
            f"opennvr_app_sdk.{module.name} has no module docstring"
