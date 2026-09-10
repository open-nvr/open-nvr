# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""``opennvr-app new <app-id>`` — scaffold a runnable Detector app.

Ships inside the SDK wheel so a developer needs nothing but
``pip install opennvr-app-sdk``::

    opennvr-app new gate-watch --task object_detection
    cd gate-watch && uv sync && uv run pytest -q

The template (``templates/app``) is rendered with token substitution
in file names and contents, then finished for one of two SDK modes:

* ``pypi`` — the app pins the published ``opennvr-app-sdk`` and its
  Dockerfile builds from PyPI alone. The default everywhere except
  inside the OpenNVR repository.
* ``path`` — an editable dependency on a checkout's
  ``sdk/opennvr-app-sdk`` (the repository's own ``examples/``, which
  must track the SDK on main). Needs ``repo_root``.

``scripts/create_opennvr_app.py`` in the OpenNVR repository is a thin
wrapper over :func:`generate` that supplies ``repo_root``.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

try:
    from ._version import __version__
except ImportError:                      # loaded by file path, no package
    __version__ = re.search(              # (the repo's stdlib-only wrapper)
        r'__version__ = "([^"]+)"',
        Path(__file__).with_name("_version.py").read_text(encoding="utf-8")).group(1)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "app"
#: ``--repo``: the files that turn the app folder into a repository under
#: the org — CI, the publish workflow that builds + signs through
#: open-nvr/open-nvr's build-catalog-app.yml, the listing entry.
REPO_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "repo"
#: Dotfiles ship in the wheel under safe names and are renamed on output.
_RENAME = {"_github": ".github", "_gitignore": ".gitignore", "_dockerignore": ".dockerignore"}
DOCS_URL = "https://github.com/open-nvr/open-nvr/blob/main/docs/"
_KEBAB_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_SKIP = {"__pycache__", ".venv", ".pytest_cache", "uv.lock"}


def kebab_to_snake(app_id: str) -> str:
    return app_id.replace("-", "_")


def kebab_to_pascal(app_id: str) -> str:
    return "".join(part.capitalize() for part in app_id.split("-"))


def kebab_to_title(app_id: str) -> str:
    return " ".join(part.capitalize() for part in app_id.split("-"))


def sdk_requirement(version: str = __version__) -> str:
    """The floor a PyPI-mode app pins: this SDK's version, below 1.0."""
    return f"opennvr-app-sdk>={version},<1.0"


def _in_tree(app_dir: Path, repo_root: Path | None) -> bool:
    if repo_root is None:
        return False
    try:
        app_dir.resolve().relative_to(repo_root.resolve())
        return True
    except ValueError:
        return False


def _mode_tokens(mode: str, app_id: str, app_dir: Path,
                 repo_root: Path | None) -> dict[str, str]:
    module = kebab_to_snake(app_id)
    if mode == "pypi":
        return {
            "__SDK_REQUIREMENT__": sdk_requirement(),
            "__UV_SOURCES__": "",
            "__DOCKER_BUILD_HINT__": f"docker build -t {app_id}:0.1.0 .   # no OpenNVR checkout needed",
            "__DOCKER_SDK_INSTALL__": (
                "# The SDK owns the runtime stack; the pin matches pyproject.toml.\n"
                f'RUN pip install --no-cache-dir "{sdk_requirement()}"'),
            "__DOCKER_COPY__": f"COPY {module}.py config.example.yml ./",
            "__DOCS__": DOCS_URL,
            "__SYNC_HINT__": "installs opennvr-app-sdk from PyPI + pytest",
            "__PYPROJECT_HINT__": "Pins the published SDK",
            "__DOCKERFILE_HINT__": "Builds from PyPI alone",
            "__LICENSE__": "MIT",
        }
    assert repo_root is not None
    sdk_dir = repo_root / "sdk" / "opennvr-app-sdk"
    try:
        rel = Path(os.path.relpath(sdk_dir, app_dir)).as_posix()
    except ValueError:                       # different drive on Windows
        rel = sdk_dir.as_posix()
    try:
        rel_app = app_dir.resolve().relative_to(repo_root.resolve()).as_posix()
        docs_rel = "../" * len(Path(rel_app).parts) + "docs/"
    except ValueError:
        # --sdk path for an app OUTSIDE the checkout (a developer hacking
        # on the SDK itself): the editable dep is absolute, the docs
        # links go to GitHub, and the Dockerfile has no in-tree layout.
        rel_app = app_dir.name
        docs_rel = DOCS_URL
    return {
        "__SDK_REQUIREMENT__": "opennvr-app-sdk",
        "__UV_SOURCES__": (
            "\n# Editable dependency on this checkout's SDK — the examples track\n"
            "# the SDK on main. An app in its own repository pins the published\n"
            "# package instead (opennvr-app new … --sdk pypi).\n"
            "[tool.uv.sources]\n"
            f'opennvr-app-sdk = {{ path = "{rel}", editable = true }}\n'),
        "__DOCKER_BUILD_HINT__": (
            f"docker build -f {rel_app}/Dockerfile -t opennvr/{app_id}:0.1.0 .   "
            "# from the repo root"),
        "__DOCKER_SDK_INSTALL__": (
            "# The SDK from the checkout, so the image matches the in-tree contract.\n"
            "COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk\n"
            "RUN pip install --no-cache-dir /opt/opennvr-app-sdk"),
        "__DOCKER_COPY__": (
            f"COPY {rel_app}/{module}.py      ./{module}.py\n"
            f"COPY {rel_app}/config.example.yml ./config.example.yml"),
        "__DOCS__": docs_rel,
        "__SYNC_HINT__": "installs the SDK (editable) + pytest",
        "__PYPROJECT_HINT__": "Editable SDK dep + dev group",
        "__DOCKERFILE_HINT__": "SDK-install image (build from the repo root)",
        "__LICENSE__": "AGPL-3.0",
    }


def build_tokens(app_id: str, task: str, app_dir: Path, *, mode: str,
                 repo_root: Path | None) -> dict[str, str]:
    tokens = {
        "__APP_ID__": app_id,
        "__APP_MODULE__": kebab_to_snake(app_id),
        "__APP_ENV__": kebab_to_snake(app_id).upper(),
        "__APP_CLASS__": kebab_to_pascal(app_id),
        "__APP_NAME__": kebab_to_title(app_id),
        "__TASK__": task,
    }
    tokens.update(_mode_tokens(mode, app_id, app_dir, repo_root))
    return tokens


def substitute(text: str, tokens: dict[str, str]) -> str:
    # Longest tokens first so __APP_MODULE__ never loses to __APP__-like prefixes.
    for token in sorted(tokens, key=len, reverse=True):
        text = text.replace(token, tokens[token])
    return text


def generate(app_id: str, task: str, dest_dir: Path, *, sdk: str = "auto",
             repo_root: Path | None = None, template_dir: Path = TEMPLATE_DIR,
             repo: bool = False) -> Path:
    """Render the template into ``dest_dir/<app-id>/``; return that path.
    ``sdk``: ``auto`` (pypi unless the app lands inside ``repo_root``),
    ``pypi`` or ``path`` (requires ``repo_root``). ``repo``: also lay
    down the repository files (CI, the publish workflow, the listing
    entry) — an app destined for ``open-nvr/app-<id>``; implies pypi."""
    if sdk not in ("auto", "path", "pypi"):
        raise ValueError(f"sdk must be auto, path or pypi (got {sdk!r})")
    if repo and sdk == "path":
        raise ValueError("--repo scaffolds a standalone repository; it pins the published SDK (--sdk pypi)")
    if repo:
        sdk = "pypi"
    if not _KEBAB_RE.match(app_id):
        raise ValueError(
            f"app-id {app_id!r} is not kebab-case — lowercase letters, digits "
            "and single hyphens (e.g. 'gate-watch')")
    if not template_dir.is_dir():
        raise FileNotFoundError(f"template directory not found: {template_dir}")
    app_dir = dest_dir / app_id
    if app_dir.exists():
        raise FileExistsError(f"destination already exists: {app_dir}")
    mode = sdk if sdk != "auto" else ("path" if _in_tree(app_dir, repo_root) else "pypi")
    if mode == "path" and repo_root is None:
        raise ValueError("sdk='path' needs repo_root (an OpenNVR checkout)")

    tokens = build_tokens(app_id, task, app_dir, mode=mode, repo_root=repo_root)
    tokens["__REPO_SECTION__"] = _REPO_README_SECTION if repo else ""
    sources = [template_dir] + ([REPO_TEMPLATE_DIR] if repo else [])
    for source_dir in sources:
        for src in sorted(source_dir.rglob("*")):
            rel_parts = src.relative_to(source_dir).parts
            if any(p in _SKIP for p in rel_parts):
                continue
            dst = app_dir.joinpath(*(substitute(_RENAME.get(p, p), tokens) for p in rel_parts))
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(substitute(src.read_text(encoding="utf-8"), tokens),
                           encoding="utf-8")
    return app_dir


_REPO_README_SECTION = """
## This repository

Scaffolded with `opennvr-app new --repo` for a home under the `open-nvr`
organisation (`open-nvr/app-__APP_ID__`):

* `.github/workflows/ci.yml` runs the tests on every push and PR.
* `.github/workflows/publish.yml` builds the image **from this source**,
  for amd64 + arm64, pushes `ghcr.io/open-nvr/__APP_ID__` and signs it
  (Sigstore keyless) through OpenNVR's `build-catalog-app` workflow on
  every push to `main` and every `v*` tag. The job summary prints the
  `image_digest:` for the listing.
* `apps-index-entry.yml` is the App Catalog listing, reviewed with the
  code; the listing PR copies it into `server/config/apps_index.yml`.

Pick a licence (AGPL-3.0 or Apache-2.0, your copyright) and add the
`LICENSE` file before the first tag. Then: open an issue titled
*App: __APP_ID__* in `open-nvr/open-nvr` with a link here, and a
maintainer creates the repository under the org and transfers or seeds
it from yours — you stay its maintainer.
"""


def print_next_steps(app_id: str, app_dir: Path, *, mode: str) -> None:
    module = kebab_to_snake(app_id)
    try:
        rel = app_dir.relative_to(Path.cwd())
    except ValueError:
        rel = app_dir
    docs = DOCS_URL if mode == "pypi" else "docs/"
    print(f"\nScaffolded {app_id!r} at {app_dir}\n")
    print("Next steps:")
    print(f"  cd {rel}")
    print("  uv sync                 # " + ("opennvr-app-sdk from PyPI + pytest"
                                            if mode == "pypi" else "the SDK (editable) + pytest"))
    print("  uv run pytest -q        # the smoke test — should be GREEN")
    print("  uv run opennvr-app dev  # watch the rule fire against a simulated camera")
    print("  uv run opennvr-app validate .   # what a reviewer would check")
    print("  uv run opennvr-app spec         # the app's OpenAPI 3.1 document")
    print(f"  # open {module}.py and edit the @app.on_detection function — that's the rule")
    print("  cp config.example.yml config.yml   # then edit it")
    print(f"  uv run python {module}.py --config config.yml --once")
    print("\nRun it against a stack, then list it in the App Catalog:")
    print(f"  {docs}FIRST_DETECTOR.md")
    print(f"  {docs}CONTRIBUTING_APPS.md")


def main(argv: list[str] | None = None, *, repo_root: Path | None = None,
         default_dest: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="opennvr-app",
        description=f"OpenNVR App SDK {__version__} — scaffold and validate vision apps.")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="scaffold a runnable Detector app")
    new.add_argument("app_id", help="kebab-case app id, e.g. 'gate-watch' (= AppManifest.id)")
    new.add_argument("--task", default="object_detection",
                     help="adapter task the app requires (requires_tasks). Default: object_detection.")
    new.add_argument("--dest", default=str(default_dest or Path.cwd()),
                     help="parent directory to create <app-id>/ under. Default: the current directory.")
    new.add_argument("--sdk", choices=("auto", "path", "pypi"), default="auto",
                     help="pypi = pin the published opennvr-app-sdk (default outside an "
                          "OpenNVR checkout); path = editable dep on a checkout's SDK.")
    new.add_argument("--repo", action="store_true",
                     help="also lay down the repository files for open-nvr/app-<id>: CI, the "
                          "publish workflow (build + sign through the org's build-catalog-app), "
                          "the App Catalog listing entry. Implies --sdk pypi.")
    val = sub.add_parser("validate", help="check an app's manifest, example config, listing and repository shape")
    val.add_argument("path", nargs="?", default=".", help="the app directory (default: current directory)")
    spec = sub.add_parser("spec", help="print the app's OpenAPI 3.1 / AsyncAPI 3.0 document")
    spec.add_argument("path", nargs="?", default=".", help="the app directory (default: current directory)")
    spec.add_argument("--format", choices=("openapi", "asyncapi"), default="openapi",
                      help="openapi = the app's HTTP contract surface (default); "
                           "asyncapi = the NATS subjects it consumes and publishes.")
    spec.add_argument("--yaml", action="store_true", help="emit YAML instead of JSON.")
    spec.add_argument("-o", "--output", default=None, help="write to this file instead of stdout.")
    dev = sub.add_parser("dev", help="run the app against a simulated camera — no broker, no Docker")
    dev.add_argument("path", nargs="?", default=".", help="the app directory (default: current directory)")
    dev.add_argument("--config", default=None,
                     help="config file to run with. Default: config.example.yml beside the app.")
    dev.add_argument("--label", default="person", help="label of the simulated object. Default: person.")
    dev.add_argument("--camera", default="cam-1", help="camera id to simulate. Default: cam-1.")
    dev.add_argument("--confidence", type=float, default=0.8,
                     help="confidence of the simulated detection. Default: 0.8.")
    dev.add_argument("--rate", type=float, default=1.0,
                     help="simulated events per second. Default: 1.")
    dev.add_argument("--count", type=int, default=60,
                     help="how many events to feed before stopping. Default: 60.")
    dev.add_argument("--still", action="store_true",
                     help="park the object in the centre of the frame instead of walking it across.")
    dev.add_argument("--fast", action="store_true",
                     help="feed every event immediately; timestamps still advance at --rate.")
    args = parser.parse_args(argv)

    if args.command == "spec":
        from .speccmd import run_spec

        return run_spec(Path(args.path), fmt=args.format, as_yaml=args.yaml,
                        output=args.output)

    if args.command == "dev":
        from .dev import run_dev

        return run_dev(
            Path(args.path), config=args.config, label=args.label, camera=args.camera,
            confidence=args.confidence, rate=args.rate, count=args.count,
            still=args.still, fast=args.fast,
        )

    if args.command == "validate":
        from .validate import print_report, validate_app

        report = validate_app(Path(args.path).expanduser())
        print_report(report, Path(args.path))
        return 0 if report.ok else 1

    dest_dir = Path(args.dest).expanduser().resolve()
    try:
        app_dir = generate(args.app_id, args.task, dest_dir, sdk=args.sdk, repo_root=repo_root,
                           repo=args.repo)
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    mode = "pypi" if args.repo else (
        args.sdk if args.sdk != "auto" else ("path" if _in_tree(app_dir, repo_root) else "pypi"))
    print_next_steps(args.app_id, app_dir, mode=mode)
    if args.repo:
        print("\nRepository files: .github/workflows/{ci,publish}.yml, apps-index-entry.yml")
        print("  git init && git add -A && git commit -m 'scaffold' — then ask for open-nvr/app-"
              f"{args.app_id} (issue 'App: {args.app_id}')")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
