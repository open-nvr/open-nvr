#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
create_opennvr_app — scaffold a new OpenNVR Detector app.

Copies ``templates/opennvr-app`` into ``<dest>/<app-id>/``, substitutes
the placeholder tokens, renames the app module, and prints the next
steps. Stdlib only — no network, no side effects beyond writing the new
directory.

Usage::

    python3 scripts/create_opennvr_app.py <app-id> [--task object_detection] [--dest examples/]

Example::

    python3 scripts/create_opennvr_app.py package-watch --task object_detection

Tokens substituted in every template file (and in file names):

    __APP_ID__      kebab-case id            package-watch
    __APP_MODULE__  snake_case module name   package_watch
    __APP_CLASS__   PascalCase class name    PackageWatch
    __APP_NAME__    Title-cased human name   Package Watch
    __TASK__        adapter task (--task)    object_detection

The generated app's smoke test passes against the real SDK out of the
box (``cd <dest>/<app-id> && uv sync && uv run pytest -q``).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Repo root is the parent of scripts/. Templates + default dest are
# resolved relative to it so the generator works from any CWD.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE_DIR = _REPO_ROOT / "templates" / "opennvr-app"
_SDK_DIR = _REPO_ROOT / "sdk" / "opennvr-app-sdk"

_KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# The template module file carries the __APP_MODULE__ token in its name.
_MODULE_FILE_TOKEN = "__APP_MODULE__"


def kebab_to_snake(app_id: str) -> str:
    return app_id.replace("-", "_")


def kebab_to_pascal(app_id: str) -> str:
    return "".join(part.capitalize() for part in app_id.split("-"))


def kebab_to_title(app_id: str) -> str:
    return " ".join(part.capitalize() for part in app_id.split("-"))


def sdk_path_for(app_dir: Path) -> str:
    """The editable SDK path written into the generated pyproject.

    Relative when the app lives inside the repo (keeps in-tree apps
    portable — ``examples/<id>/`` → ``../../sdk/opennvr-app-sdk``);
    absolute for an out-of-tree ``--dest`` so ``uv sync`` still resolves
    it. Always forward-slashed so the TOML is platform-neutral."""
    try:
        rel = os.path.relpath(_SDK_DIR, app_dir)
        # Only prefer the relative form when it actually stays a tidy
        # ``../`` walk; a relpath that bounces through the filesystem
        # root (different drive on Windows) raises ValueError below.
        return Path(rel).as_posix()
    except ValueError:
        return _SDK_DIR.as_posix()


def sdk_version() -> str:
    """The SDK version in this checkout — the floor a PyPI-mode app pins."""
    try:
        text = (_SDK_DIR / "opennvr_app_sdk" / "_version.py").read_text(encoding="utf-8")
    except OSError:
        return "0.4.0"          # the first PyPI release — a safe floor
    match = re.search(r'__version__ = "([^"]+)"', text)
    return match.group(1) if match else "0.4.0"


def is_in_tree(app_dir: Path) -> bool:
    try:
        app_dir.resolve().relative_to(_REPO_ROOT.resolve())
        return True
    except ValueError:
        return False


def apply_pypi_mode(app_dir: Path, app_id: str, version: str) -> None:
    """Rewrite the rendered pyproject + Dockerfile for an app that lives
    OUTSIDE this repository and gets the SDK from PyPI — no editable
    path, no ``COPY sdk/...`` from a checkout the developer does not
    have. This is what a third-party developer needs; the in-tree form
    stays for the examples, which must track the SDK on main."""
    pyproject = app_dir / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    text = text.replace(
        '    "opennvr-app-sdk",\n',
        f'    "opennvr-app-sdk>={version},<1.0",\n')
    start = text.find("# Editable path dep on the in-repo SDK.")
    end = text.find("[project.optional-dependencies]")
    if start != -1 and end != -1:
        text = text[:start] + text[end:]
    pyproject.write_text(text, encoding="utf-8")

    module = kebab_to_snake(app_id)
    (app_dir / "Dockerfile").write_text(f"""# syntax=docker/dockerfile:1.7
# {kebab_to_title(app_id)} — OpenNVR Detector app, built on the published SDK.
#
# Build (from this directory — no OpenNVR checkout needed):
#   docker build -t {app_id}:0.1.0 .
#
# Run (on the stack's compose network):
#   docker run --rm --network opennvr_internal \\
#     -v $(pwd)/config.yml:/app/config.yml:ro {app_id}:0.1.0

FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The SDK owns the runtime stack (NATS loop, alert fan-out, the platform
# client). Pin the same floor as pyproject.toml.
RUN pip install --no-cache-dir "opennvr-app-sdk>={version},<1.0"

COPY {module}.py config.example.yml ./

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "{module}.py"]
CMD ["--config", "config.yml"]
""", encoding="utf-8")


def build_tokens(app_id: str, task: str, app_dir: Path) -> dict[str, str]:
    """The token → replacement map applied to file contents and names."""
    return {
        "__APP_ID__": app_id,
        "__APP_MODULE__": kebab_to_snake(app_id),
        "__APP_CLASS__": kebab_to_pascal(app_id),
        "__APP_NAME__": kebab_to_title(app_id),
        "__TASK__": task,
        "__SDK_PATH__": sdk_path_for(app_dir),
    }


def substitute(text: str, tokens: dict[str, str]) -> str:
    for token, value in tokens.items():
        text = text.replace(token, value)
    return text


def rename_path_part(part: str, tokens: dict[str, str]) -> str:
    """Substitute tokens that appear in a path component (e.g. the app
    module file name ``__APP_MODULE__.py``)."""
    return substitute(part, tokens)


def generate(app_id: str, task: str, dest_dir: Path, *, sdk: str = "auto") -> Path:
    """Render the template into ``dest_dir/<app-id>/``. Returns the path
    to the created app directory. Raises on validation failures.

    ``sdk``: ``"path"`` keeps the editable dependency on this checkout's
    SDK (the examples); ``"pypi"`` pins the published package instead
    (a third-party app in its own repository); ``"auto"`` picks by where
    the app lands — in-tree → path, anywhere else → pypi."""
    if sdk not in ("auto", "path", "pypi"):
        raise ValueError(f"sdk must be auto, path or pypi (got {sdk!r})")
    if not _KEBAB_RE.match(app_id):
        raise ValueError(
            f"app-id {app_id!r} is not kebab-case — use lowercase letters, "
            f"digits, and single hyphens (e.g. 'package-watch')"
        )
    if not _TEMPLATE_DIR.is_dir():
        raise FileNotFoundError(
            f"template directory not found: {_TEMPLATE_DIR}"
        )

    app_dir = dest_dir / app_id
    if app_dir.exists():
        raise FileExistsError(f"destination already exists: {app_dir}")

    tokens = build_tokens(app_id, task, app_dir)

    # Walk the template tree, copying every file with tokens substituted
    # in both its path and its contents. Skip transient dirs.
    _SKIP = {"__pycache__", ".venv", ".pytest_cache", "uv.lock"}
    for src in sorted(_TEMPLATE_DIR.rglob("*")):
        rel_parts = src.relative_to(_TEMPLATE_DIR).parts
        if any(p in _SKIP for p in rel_parts):
            continue
        dst_parts = [rename_path_part(p, tokens) for p in rel_parts]
        dst = app_dir.joinpath(*dst_parts)
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        content = src.read_text(encoding="utf-8")
        dst.write_text(substitute(content, tokens), encoding="utf-8")

    mode = sdk if sdk != "auto" else ("path" if is_in_tree(app_dir) else "pypi")
    if mode == "pypi":
        apply_pypi_mode(app_dir, app_id, sdk_version())
    return app_dir


def _print_next_steps(app_id: str, app_dir: Path, *, pypi: bool = False) -> None:
    module = kebab_to_snake(app_id)
    try:
        rel = app_dir.relative_to(Path.cwd())
    except ValueError:
        rel = app_dir
    print(f"\nScaffolded {app_id!r} at {app_dir}\n")
    print("Next steps:")
    print(f"  cd {rel}")
    if pypi:
        print("  uv sync                 # install opennvr-app-sdk from PyPI + pytest")
    else:
        print("  uv sync                 # install the SDK (editable) + pytest")
    print("  uv run pytest -q        # the smoke test — should be GREEN")
    print(f"  # open {module}.py and fill in on_detections — that's the rule")
    print(f"  cp config.example.yml config.yml   # then edit it")
    print(f"  uv run python {module}.py --config config.yml --once")
    docs = ("https://github.com/open-nvr/open-nvr/blob/main/docs/" if pypi else "docs/")
    print("\nRun it against the stack + publish to the App Store:")
    print(f"  {docs}FIRST_DETECTOR.md      # the 15-minute walkthrough")
    print(f"  {docs}CONTRIBUTING_APPS.md   # list it in the catalog (installable or external)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="create_opennvr_app",
        description="Scaffold a new OpenNVR Detector app from the template.",
    )
    parser.add_argument(
        "app_id",
        help="kebab-case app id, e.g. 'package-watch' (matches AppManifest.id)",
    )
    parser.add_argument(
        "--task",
        default="object_detection",
        help="adapter task the app requires (requires_tasks). "
             "Default: object_detection.",
    )
    parser.add_argument(
        "--dest",
        default=str(_REPO_ROOT / "examples"),
        help="parent directory to create <app-id>/ under. Default: examples/.",
    )
    parser.add_argument(
        "--sdk",
        choices=("auto", "path", "pypi"),
        default="auto",
        help="where the app gets opennvr-app-sdk: 'path' = editable dep on "
             "this checkout (for examples/), 'pypi' = the published package "
             "(for an app in its own repository). Default: auto — pypi when "
             "--dest is outside this repository.",
    )
    args = parser.parse_args(argv)

    dest_dir = Path(args.dest).expanduser().resolve()
    try:
        app_dir = generate(args.app_id, args.task, dest_dir, sdk=args.sdk)
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    pypi = args.sdk == "pypi" or (args.sdk == "auto" and not is_in_tree(app_dir))
    _print_next_steps(args.app_id, app_dir, pypi=pypi)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
