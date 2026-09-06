# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""``opennvr-app validate [path]`` — check an app before it meets a stack.

Everything the catalog, the registry and a listing reviewer would
trip over, found on the developer's machine in a second:

* the **manifest** — id, version, category, params (names, types,
  defaults), alert types, scopes, actions, state views, commerce
  fields, the licence hook when ``entitlement`` says so;
* the **example config** (``config.example.yml``) loads through the
  app's own ``AppConfig`` — the operator-facing error text is what a
  developer sees first;
* the **listing** (``apps-index-entry.yml``, if present) mirrors the
  manifest — id, name, version, ``requires_tasks``, ``emits`` — and
  carries what the catalog policy requires (author, contact, source
  under the org, ``network_egress``, the org's image ref);
* the **repository shape** — Dockerfile, tests, a licence.

Errors fail the command (exit 1); warnings do not. The manifest is
found by importing the app module named in ``pyproject.toml``'s
``[project.scripts]`` (or the one ``*.py`` that builds an
``AppManifest``), so run it in the app's own environment.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .manifest import ENTITLEMENT_MODES, PRICING_MODELS, AppManifest, Param

_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_SCOPE_RE = re.compile(r"^events:[a-z0-9_]+(?:\.[a-z0-9_]+)*$")
_SCHEMA_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+\.v\d+$")

#: Categories the catalog groups by (the manifest's own comment + what
#: the shipped apps use). Anything else is a warning, not an error.
KNOWN_CATEGORIES = frozenset({
    "perimeter", "analytics", "vehicle", "doorstep", "forensics", "integration",
    "automation", "notifications", "assistant", "observability", "people", "retail", "safety",
})
#: The canonical task registry (server/config/tasks.yml) — an unknown
#: task greys the app out in every catalog, so it is worth a warning.
KNOWN_TASKS = frozenset({
    "object_detection", "face_recognition", "image_captioning", "vqa", "face_detection",
    "person_detection", "license_plate_recognition", "multi_object_tracking",
    "speech_to_text", "text_to_speech",
})
KNOWN_PARAM_TYPES = frozenset({"str", "int", "float", "bool", "list", "dict", "json",
                               "geometry.polygon", "geometry.tripwire", "image", "time_range"})
SEVERITIES = frozenset({"low", "medium", "high", "critical"})
_PY_TYPES = {"str": str, "int": int, "float": (int, float), "bool": bool, "list": list, "dict": dict}


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    manifest: AppManifest | None = None
    module_name: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, text: str) -> None:
        self.errors.append(text)

    def warn(self, text: str) -> None:
        self.warnings.append(text)

    def note(self, text: str) -> None:
        self.notes.append(text)


# ── finding the app ─────────────────────────────────────────────────


def find_app_module(app_dir: Path) -> str | None:
    """The module that builds the manifest: the ``[project.scripts]``
    target in pyproject.toml, else the one top-level ``*.py`` that
    mentions ``AppManifest(``."""
    pyproject = app_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            data = {}
        for target in (data.get("project", {}).get("scripts") or {}).values():
            module = str(target).split(":", 1)[0].strip()
            if module and (app_dir / f"{module}.py").exists():
                return module
    candidates = [p.stem for p in sorted(app_dir.glob("*.py"))
                  if "AppManifest(" in p.read_text(encoding="utf-8", errors="ignore")]
    return candidates[0] if len(candidates) == 1 else (None if not candidates else candidates[0])


def load_manifest(app_dir: Path, module_name: str) -> tuple[AppManifest | None, Any]:
    """Import the module from ``app_dir`` and return ``(manifest, module)``:
    a module-level ``AppManifest`` or the ``manifest`` of an archetype
    subclass defined there."""
    sys.path.insert(0, str(app_dir))
    try:
        sys.modules.pop(module_name, None)
        module = importlib.import_module(module_name)
    finally:
        try:
            sys.path.remove(str(app_dir))
        except ValueError:
            pass
    for name in ("manifest", "MANIFEST"):
        m = getattr(module, name, None)
        if isinstance(m, AppManifest):
            return m, module
    for _name, obj in inspect.getmembers(module, inspect.isclass):
        if obj.__module__ == module.__name__ and isinstance(getattr(obj, "manifest", None), AppManifest):
            return obj.manifest, module
    for _name, obj in inspect.getmembers(module):
        if isinstance(obj, AppManifest):
            return obj, module
    return None, module


def app_class(module: Any, manifest: AppManifest) -> type | None:
    for _name, obj in inspect.getmembers(module, inspect.isclass):
        if getattr(obj, "manifest", None) is manifest and obj.__module__ == module.__name__:
            return obj
    return None


# ── checks ──────────────────────────────────────────────────────────


def check_manifest(m: AppManifest, report: Report, cls: type | None = None) -> None:
    if not _KEBAB_RE.match(m.id or ""):
        report.error(f"manifest.id {m.id!r} is not kebab-case (lowercase letters, digits, single hyphens)")
    if not (m.name or "").strip():
        report.error("manifest.name is empty")
    if not _SEMVER_RE.match(m.version or ""):
        report.error(f"manifest.version {m.version!r} is not semantic (MAJOR.MINOR.PATCH)")
    if m.category not in KNOWN_CATEGORIES:
        report.warn(f"manifest.category {m.category!r} is not one the catalog groups by "
                    f"({', '.join(sorted(KNOWN_CATEGORIES))}) — it will show under its own heading")
    if not (m.summary or "").strip():
        report.warn("manifest.summary is empty — the catalog card shows 'No summary provided.'")
    for task in m.requires_tasks:
        if task not in KNOWN_TASKS:
            report.warn(f"requires_tasks {task!r} is not a canonical task — no adapter advertises it, "
                        "so the catalog will grey the app out (server/config/tasks.yml)")
    for scope in m.requires_scopes:
        if not _SCOPE_RE.match(scope):
            report.error(f"requires_scopes {scope!r} must look like 'events:<domain>.<event>'")
    if m.subscribes is not None and not re.match(r"^[A-Za-z0-9_.*>-]+$", m.subscribes):
        report.error(f"subscribes {m.subscribes!r} is not a NATS subject pattern")

    seen: set[str] = set()
    for p in m.params:
        _check_param(p, seen, report)
    names = [a.name for a in m.emits]
    for name in names:
        if names.count(name) > 1:
            report.error(f"emits: alert type {name!r} declared twice")
        if not re.match(r"^[a-z0-9_-]+$", name or ""):
            report.error(f"emits: alert type name {name!r} — use lowercase letters, digits, _ or -")
    for a in m.emits:
        if a.severity not in SEVERITIES:
            report.error(f"emits[{a.name}].severity {a.severity!r} must be one of {sorted(SEVERITIES)}")
    action_names = [a.name for a in m.actions]
    for name in action_names:
        if action_names.count(name) > 1:
            report.error(f"actions: {name!r} declared twice")
    view_names = [getattr(v, "name", None) or getattr(v, "title", "") for v in m.state_schema]
    for name in view_names:
        if name and view_names.count(name) > 1:
            report.warn(f"state_schema: two views named {name!r}")

    if m.pricing not in PRICING_MODELS:
        report.error(f"pricing {m.pricing!r} must be one of {sorted(PRICING_MODELS)}")
    if m.entitlement not in ENTITLEMENT_MODES:
        report.error(f"entitlement {m.entitlement!r} must be one of {sorted(ENTITLEMENT_MODES)}")
    if m.pricing != "free" and not (m.price_note or "").strip():
        report.warn("pricing is not 'free' but price_note is empty — say what it costs")
    if m.pricing != "free" and m.entitlement == "none":
        report.warn("a paid app with entitlement='none' can be enabled without a licence — "
                    "declare entitlement='license_key' and implement verify_license if you gate it")
    if m.entitlement == "license_key" and cls is not None:
        impl = getattr(cls, "verify_license", None)
        base = None
        for parent in cls.__mro__[1:]:
            if hasattr(parent, "verify_license"):
                base = getattr(parent, "verify_license")
                break
        if impl is None or impl is base:
            report.error("entitlement='license_key' but the app does not override verify_license() — "
                         "core will never accept a key")
    if m.ui_mode == "external" and not (m.ui_url or "").startswith(("http://", "https://")):
        report.error("ui_mode='external' needs an http(s) ui_url ('{host}' is substituted)")
    if m.has_ui and cls is not None and m.ui_mode == "internal":
        if not any(hasattr(parent, "render_ui") or hasattr(parent, "ui_html") for parent in cls.__mro__):
            report.note("has_ui=True: make sure the contract server serves GET /ui")


def _check_param(p: Param, seen: set[str], report: Report) -> None:
    if not re.match(r"^[a-z][a-z0-9_]*$", p.name or ""):
        report.error(f"params: name {p.name!r} — use snake_case")
    if p.name in seen:
        report.error(f"params: {p.name!r} declared twice")
    seen.add(p.name)
    type_name = p.type.__name__ if isinstance(p.type, type) else str(p.type)
    if type_name not in KNOWN_PARAM_TYPES:
        report.warn(f"params[{p.name}].type {type_name!r} is not one the catalog form renders "
                    f"({', '.join(sorted(KNOWN_PARAM_TYPES))}) — it falls back to a JSON box")
    if p.default is not None and type_name in _PY_TYPES:
        expected = _PY_TYPES[type_name]
        bad = (type_name in ("int", "float") and isinstance(p.default, bool)) \
            or not isinstance(p.default, expected)
        if bad:
            report.error(f"params[{p.name}].default {p.default!r} is not a {type_name}")
    if type_name == "geometry.polygon" and p.default not in (None, []) and not isinstance(p.default, list):
        report.error(f"params[{p.name}] geometry.polygon default must be a list of points or []")
    if p.required and p.default is not None:
        report.warn(f"params[{p.name}] is required but has a default — one of the two is redundant")
    if p.suggestions and not all(isinstance(s, str) for s in p.suggestions):
        report.error(f"params[{p.name}].suggestions must be strings")
    if p.per_camera and type_name not in ("geometry.polygon", "geometry.tripwire", "list", "dict",
                                          "time_range", "json", "str", "int", "float", "bool"):
        report.warn(f"params[{p.name}] per_camera with type {type_name!r} — the per-camera editor may not render it")


def check_config(app_dir: Path, module: Any, report: Report) -> None:
    example = app_dir / "config.example.yml"
    if not example.exists():
        report.warn("no config.example.yml — operators and the Docker template need one")
        return
    from .config import BaseAppConfig, load_app_config

    cfg_cls = getattr(module, "AppConfig", None)
    if not (isinstance(cfg_cls, type) and issubclass(cfg_cls, BaseAppConfig)):
        loader = getattr(module, "load_config", None)
        if callable(loader):
            try:
                loader(example)
                report.note("config.example.yml loads through load_config()")
            except Exception as exc:  # noqa: BLE001
                report.error(f"config.example.yml does not load through load_config(): {exc}")
            return
        report.note("no AppConfig(BaseAppConfig) in the module — config.example.yml checked for base keys only")
        cfg_cls = BaseAppConfig
    try:
        load_app_config(example, cfg_cls)
        report.note(f"config.example.yml loads as {cfg_cls.__name__}")
    except Exception as exc:  # noqa: BLE001
        report.error(f"config.example.yml does not load as {cfg_cls.__name__}: {exc}")


def check_listing(app_dir: Path, m: AppManifest, report: Report) -> None:
    path = app_dir / "apps-index-entry.yml"
    if not path.exists():
        report.note("no apps-index-entry.yml — the listing is checked only when it lives with the app")
        return
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        report.error(f"apps-index-entry.yml is not valid YAML: {exc}")
        return
    entry = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(entry, dict):
        report.error("apps-index-entry.yml must hold one mapping (or a one-item list)")
        return
    for key, want in (("id", m.id), ("name", m.name), ("version", str(m.version))):
        if str(entry.get(key, "")) != str(want):
            report.error(f"listing.{key} {entry.get(key)!r} != manifest.{key} {want!r}")
    if sorted(entry.get("requires_tasks") or []) != sorted(m.requires_tasks):
        report.error(f"listing.requires_tasks {entry.get('requires_tasks')!r} != manifest {m.requires_tasks!r}")
    emits = sorted(a.name for a in m.emits)
    if sorted(entry.get("emits") or []) != emits:
        report.error(f"listing.emits {entry.get('emits')!r} != manifest alert types {emits!r}")
    if entry.get("kind", "installable") == "installable":
        for key in ("author", "contact"):
            if not str(entry.get(key) or "").strip() or "example.com" in str(entry.get(key)) \
                    or str(entry.get(key)).startswith("Your "):
                report.error(f"listing.{key} is missing or still the placeholder")
        source = str(entry.get("source") or "")
        if not source.startswith("https://github.com/open-nvr/"):
            report.warn(f"listing.source {source!r} is not a repository under the open-nvr organisation — "
                        "catalog apps live there (docs/CONTRIBUTING_APPS.md)")
        if entry.get("network_egress") is None:
            report.error("listing.network_egress is required ([] if the app never leaves the site)")
        image = str(entry.get("image") or "")
        if image and not image.startswith(f"ghcr.io/open-nvr/{m.id}"):
            report.warn(f"listing.image {image!r} is not ghcr.io/open-nvr/{m.id} — the org's CI publishes there")
        if not entry.get("image_digest"):
            report.note("listing.image_digest not set yet — take it from the publish workflow's summary")
        if str(entry.get("summary") or "").startswith("What "):
            report.warn("listing.summary is still the scaffold placeholder")
    report.note("apps-index-entry.yml mirrors the manifest")


def check_repository(app_dir: Path, report: Report) -> None:
    if not (app_dir / "Dockerfile").exists():
        report.warn("no Dockerfile — the catalog builds one from source")
    if not any((app_dir / "tests").glob("test_*.py")) if (app_dir / "tests").is_dir() else True:
        report.warn("no tests/test_*.py — a listing review expects a green test suite")
    if (app_dir / ".github" / "workflows").is_dir() and not any(
            p.name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING") for p in app_dir.iterdir()):
        report.warn("no LICENSE file — add AGPL-3.0 or Apache-2.0 (your copyright) before the first tag")


# ── entry point ─────────────────────────────────────────────────────


def validate_app(app_dir: Path) -> Report:
    report = Report()
    app_dir = app_dir.resolve()
    if not app_dir.is_dir():
        report.error(f"{app_dir} is not a directory")
        return report
    module_name = find_app_module(app_dir)
    if module_name is None:
        report.error("no app module found — expected a [project.scripts] entry in pyproject.toml "
                     "or a top-level *.py that builds an AppManifest")
        return report
    report.module_name = module_name
    try:
        manifest, module = load_manifest(app_dir, module_name)
    except Exception as exc:  # noqa: BLE001
        report.error(f"importing {module_name!r} failed: {exc.__class__.__name__}: {exc}")
        return report
    if manifest is None:
        report.error(f"module {module_name!r} defines no AppManifest (module-level or on the app class)")
        return report
    report.manifest = manifest
    cls = app_class(module, manifest)
    check_manifest(manifest, report, cls)
    check_config(app_dir, module, report)
    check_listing(app_dir, manifest, report)
    check_repository(app_dir, report)
    return report


def print_report(report: Report, app_dir: Path) -> None:
    m = report.manifest
    head = f"{m.id} {m.version} ({m.category})" if m else str(app_dir)
    print(f"opennvr-app validate — {head}")
    for line in report.notes:
        print(f"  · {line}")
    for line in report.warnings:
        print(f"  ! {line}")
    for line in report.errors:
        print(f"  ✗ {line}")
    if report.ok:
        print(f"OK — {len(report.warnings)} warning(s)")
    else:
        print(f"FAILED — {len(report.errors)} error(s), {len(report.warnings)} warning(s)")
