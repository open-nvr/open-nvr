# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""``opennvr-app spec`` — print an app's OpenAPI / AsyncAPI document.

The document is generated from the app's manifest by
:mod:`~.openapi`, so this is the same JSON a running app serves at
``GET /openapi.json``. Emitting it without running the app is what lets
it go into CI, into a client generator, or into the published API
reference::

    opennvr-app spec                            # OpenAPI 3.1, JSON, stdout
    opennvr-app spec --format asyncapi --yaml   # AsyncAPI 3.0, YAML
    opennvr-app spec -o openapi.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def run_spec(app_dir: Path, *, fmt: str = "openapi", as_yaml: bool = False,
             output: str | None = None) -> int:
    """Render the spec for the app in ``app_dir``. Returns an exit code."""
    from .openapi import contract_asyncapi, contract_openapi, prune
    from .validate import find_app_module, load_manifest

    app_dir = app_dir.expanduser().resolve()
    module_name = find_app_module(app_dir)
    if module_name is None:
        print(f"error: no app module found in {app_dir}", file=sys.stderr)
        return 2
    try:
        manifest, _module = load_manifest(app_dir, module_name)
    except Exception as exc:  # noqa: BLE001 — any import error is the user's
        print(f"error: importing {module_name!r} failed: "
              f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2
    if manifest is None:
        print(f"error: {module_name!r} defines no app", file=sys.stderr)
        return 2

    builder = contract_asyncapi if fmt == "asyncapi" else contract_openapi
    document = prune(builder(manifest))

    if as_yaml:
        import yaml

        text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    else:
        text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"

    if output:
        path = Path(output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {fmt} ({len(text)} bytes) to {path}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


__all__ = ["run_spec"]
