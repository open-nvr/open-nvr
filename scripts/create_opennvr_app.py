#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Scaffold an OpenNVR app from inside this repository.

A thin wrapper over the generator that ships in the SDK wheel
(``opennvr_app_sdk.scaffold`` — ``opennvr-app new`` once installed):
this one knows the repository root, so an app scaffolded under
``examples/`` gets the editable path to ``sdk/opennvr-app-sdk`` and
tracks the SDK on main, while any ``--dest`` outside the checkout pins
the published package, exactly as a third-party developer would.

    python3 scripts/create_opennvr_app.py gate-watch --task object_detection
    python3 scripts/create_opennvr_app.py gate-watch --dest ~/my-apps      # PyPI mode
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCAFFOLD = _REPO_ROOT / "sdk" / "opennvr-app-sdk" / "opennvr_app_sdk" / "scaffold.py"


def _load_scaffold():
    """Load the generator by file path — stdlib only, so this works in a
    clean checkout before anything is installed (the package import
    would pull in httpx/nats/yaml)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("opennvr_app_scaffold", _SCAFFOLD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] != "new":
        argv = ["new", *argv]          # keep the old positional form working
    return _load_scaffold().main(argv, repo_root=_REPO_ROOT,
                                 default_dest=_REPO_ROOT / "examples")


if __name__ == "__main__":
    raise SystemExit(main())
