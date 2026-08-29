#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""Validate ``server/config/apps_index.yml`` — the App Store submission gate.

This is the CI / pre-PR check a community app submission has to pass before
it lands in the curated index (see ``docs/CONTRIBUTING_APPS.md``). It is
deliberately **stdlib + PyYAML only** — no server import required — so a
contributor can run it in a clean checkout without booting Postgres or
``uv sync``-ing the backend:

    python3 scripts/validate_apps_index.py            # shipped index
    python3 scripts/validate_apps_index.py path/to/index.yml
    make validate-apps-index

What it enforces (one clear message per offending entry, non-zero exit on
any hard failure):

* every entry has the required fields the ``IndexEntry`` pydantic model
  needs — ``id, name, summary, category, version, image, requires_tasks,
  docs_url, install`` — with the right TYPES (a ``version: 1.0`` YAML
  float would 500 the whole store page through pydantic), and
  ``install`` carries a non-empty ``compose`` and ``command``;
* ``id`` is unique across the file, kebab-case (``[a-z0-9-]``), and —
  the install contract — **exists as a service in
  docker-compose.apps.yml**. Both install paths run ``docker compose …
  up -d <id>`` against that overlay, so a listing without a service
  block fails on install for everyone (review finding: 8 of 10 original
  entries were uninstallable this way);
* ``install.command`` is EXACTLY the canonical compose-up command for
  the entry's own id — the store UI renders it with a Copy button, so
  this field is operator-executed content and free-form shell here is a
  supply-chain hole (``curl … | sh`` must never ride a merged entry);
* ``install.compose`` parses as YAML and contains none of the dangerous
  compose directives (``privileged``, ``cap_add``, ``security_opt``,
  ``network_mode``, ``pid``, ``ipc``, ``devices``, published ``ports``),
  no docker.sock or absolute host-path bind mounts, and every service
  ``image`` matches the entry's declared image (or the overlay's
  ``${<ID>_IMAGE:-opennvr/<id>:local-build}`` pin slot) — the snippet
  is what an operator reviews, so it must not diverge from the fields
  the gate validated;
* ``image`` is a well-formed ref (``ghcr.io/...`` or ``opennvr/...``);
* ``image_digest``, when present, is ``sha256:`` + 64 lowercase hex;
* ``docs_url`` is an ``https://`` URL (repo-relative paths 404 in the
  UI, and anything else is an unconstrained href sink);
* NO plaintext secrets — in ``KEY=value`` **or** ``KEY: value`` form,
  in the compose block **and** the command — ``${VAR}`` placeholders
  only.

What it only WARNS about (free-text is allowed by the adapter contract §4,
so an unknown task is a nudge, not a rejection):

* a ``requires_tasks`` entry that is not a canonical task name or alias
  from ``server/config/tasks.yml`` (nor a capability named in
  ``server/config/use_case_map.yml``).

Warnings go to stderr and do NOT change the exit code; hard failures do.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INDEX = REPO_ROOT / "server" / "config" / "apps_index.yml"
USE_CASE_MAP = REPO_ROOT / "server" / "config" / "use_case_map.yml"
TASKS_REGISTRY = REPO_ROOT / "server" / "config" / "tasks.yml"
APPS_OVERLAY = REPO_ROOT / "docker-compose.apps.yml"

# Required top-level fields, mirroring the IndexEntry pydantic model in
# server/routers/apps.py (build_context / emits / image_digest are optional).
REQUIRED_FIELDS = (
    "id",
    "name",
    "summary",
    "category",
    "version",
    "image",
    "requires_tasks",
    "docs_url",
    "install",
)

# id must be kebab-case: lowercase alnum words joined by single hyphens.
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# image ref: a ghcr.io/... path or an opennvr/... path, optionally :tagged.
# (The digest lives in image_digest, not here.)
_IMAGE_RE = re.compile(r"^(?:ghcr\.io/[a-z0-9._/-]+|opennvr/[a-z0-9._/-]+)(?::[a-zA-Z0-9._-]+)?$")

# A published-image digest is sha256 + exactly 64 lowercase hex chars.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# A ${VAR} / $VAR placeholder — allowed anywhere a value could hold a secret.
_PLACEHOLDER_RE = re.compile(r"\$\{?[A-Z_][A-Z0-9_]*\}?")

# Heuristic secret sniff: a KEY/TOKEN/PASSWORD/SECRET assignment whose value
# is NOT a ${VAR} placeholder. Catches a baked literal without flagging the
# legitimate ``- OPENNVR_INTERNAL_API_KEY=${INTERNAL_API_KEY}`` line.
_SECRETISH_KEY_RE = re.compile(
    r"(?i)(?:api[_-]?key|secret|password|passwd|token|access[_-]?key|private[_-]?key)"
)

# The canonical compose-up command every entry's install.command must be,
# verbatim, with the entry's own id. This field is rendered with a Copy
# button in the store UI — it is operator-EXECUTED content, so anything
# free-form here (a `curl … | sh`) is a supply-chain hole, not a style
# choice. One id-parameterised shape, nothing else.
_COMMAND_TEMPLATE = (
    "docker compose -f docker-compose.yml -f docker-compose.apps.yml "
    "--profile apps up -d {id}"
)

# Compose service keys that grant host-level or network-level power an
# App Store entry must never carry. `ports` publishes to the host (apps
# are internal-network + `expose` only); the rest are container-escape
# or lateral-movement levers.
_FORBIDDEN_SERVICE_KEYS = (
    "privileged",
    "cap_add",
    "security_opt",
    "network_mode",
    "pid",
    "ipc",
    "devices",
    "ports",
)

# The per-app image pin slot the overlay uses (see image_env_key in
# scripts/app-installer/reconciler.py): ${<ID>_IMAGE:-opennvr/<id>:local-build}
def _pin_slot_for(app_id: str) -> str:
    env_key = app_id.upper().replace("-", "_") + "_IMAGE"
    return "${" + env_key + ":-opennvr/" + app_id + ":local-build}"


def _load_canonical_tasks() -> set[str]:
    """The known-good task vocabulary.

    Primary source: ``server/config/tasks.yml`` — the platform's canonical
    task registry — folding in every canonical name AND its aliases.
    Secondary: every ``needs_capability`` + ``also_needs`` string from the
    product-owned use-case map. Missing / unreadable files ⇒ the check
    just warns more widely. Never raises — the task check is advisory.
    (The old hardcoded alias table is gone: it silently whitelisted
    ``ocr``/``object_tracking``, which exist in NEITHER registry — exactly
    the drift this warning exists to catch.)
    """
    tasks: set[str] = set()
    try:
        rows = yaml.safe_load(TASKS_REGISTRY.read_text()) or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if isinstance(row.get("task"), str):
                tasks.add(row["task"])
            for alias in row.get("aliases") or []:
                if isinstance(alias, str):
                    tasks.add(alias)
    except Exception:
        pass
    try:
        rows = yaml.safe_load(USE_CASE_MAP.read_text()) or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            cap = row.get("needs_capability")
            if isinstance(cap, str):
                tasks.add(cap)
            for extra in row.get("also_needs") or []:
                if isinstance(extra, str):
                    tasks.add(extra)
    except Exception:
        pass
    return tasks


def _load_overlay_services(overlay: Path = APPS_OVERLAY) -> set[str] | None:
    """Service names defined in docker-compose.apps.yml, or None when the
    overlay can't be read (validator degrades to a warning — a clean
    checkout always has it, but a caller validating a lone index file
    against a custom path shouldn't hard-fail on repo layout)."""
    try:
        doc = yaml.safe_load(overlay.read_text()) or {}
        services = doc.get("services")
        if isinstance(services, dict):
            return set(services)
    except Exception:
        pass
    return None


def _entry_label(entry: object, index: int) -> str:
    """Human-readable handle for an entry in error messages."""
    if isinstance(entry, dict) and isinstance(entry.get("id"), str):
        return f"entry '{entry['id']}'"
    return f"entry #{index} (no valid id)"


def _looks_like_baked_secret(text: str) -> list[str]:
    """Return offending lines where a secret-ish key has a literal value.

    A value that is exactly a ``${VAR}`` / ``$VAR`` placeholder (optionally
    quoted / empty) is fine. Anything else assigned to a secret-ish key is a
    hard failure — no plaintext secret ever ships in the curated index.

    Covers BOTH assignment forms compose accepts (review finding: the
    original ``=``-only scan let ``API_KEY: hunter2`` mapping-style env
    ship clean), and is run over ``install.command`` too, not just the
    compose block.
    """
    offenders: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-").strip()
        # `KEY=value` (compose list-style env / shell) first; fall back to
        # `KEY: value` (compose mapping-style env). For the mapping form,
        # require a single-token key so prose lines don't false-positive.
        if "=" in line:
            key, _, value = line.partition("=")
        elif ":" in line:
            key, _, value = line.partition(":")
            if " " in key.strip() or "\t" in key.strip():
                continue
        else:
            continue
        if not _SECRETISH_KEY_RE.search(key):
            continue
        # Drop a trailing inline `# comment` (compose keeps it as literal
        # text in a `|` block, but it isn't part of the value) before the
        # placeholder check, so a documented `${VAR}  # note` line is fine.
        value = value.split("#", 1)[0].strip().strip('"').strip("'")
        if value == "" or _PLACEHOLDER_RE.fullmatch(value):
            continue
        offenders.append(raw.strip())
    return offenders


def _check_compose_snippet(
    label: str, app_id: str | None, image: str | None, compose: str
) -> list[str]:
    """Hard checks on the install.compose snippet — the operator-reviewed
    (and potentially operator-pasted) content the submission gate exists
    to police. Returns error strings."""
    errors: list[str] = []
    # Must parse as YAML at all (a snippet that doesn't parse can hide
    # anything from a reviewer's eyes and breaks the checks below).
    try:
        doc = yaml.safe_load(compose)
    except yaml.YAMLError as exc:
        return [f"{label}: install.compose is not valid YAML: {exc}"]
    if not isinstance(doc, dict):
        return [f"{label}: install.compose must be a YAML mapping (services: …)"]

    services = doc.get("services")
    if not isinstance(services, dict) or not services:
        return [f"{label}: install.compose must define at least one service"]

    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            errors.append(
                f"{label}: install.compose service '{svc_name}' is not a mapping"
            )
            continue
        for key in _FORBIDDEN_SERVICE_KEYS:
            if key in svc:
                errors.append(
                    f"{label}: install.compose service '{svc_name}' uses "
                    f"forbidden directive '{key}' — App Store apps run "
                    "internal-network, unprivileged, expose-only"
                )
        for vol in svc.get("volumes") or []:
            vol_str = vol if isinstance(vol, str) else str(vol)
            src = vol_str.split(":", 1)[0].strip()
            if "docker.sock" in vol_str:
                errors.append(
                    f"{label}: install.compose service '{svc_name}' mounts "
                    "the Docker socket — never allowed for a store app"
                )
            elif src.startswith(("/", "~")):
                errors.append(
                    f"{label}: install.compose service '{svc_name}' bind-"
                    f"mounts host path '{src}' — only named volumes and "
                    "./ repo-relative template mounts are allowed"
                )
        # The snippet's image must be the entry's validated image (or the
        # overlay's pin slot) — otherwise the reviewed `image` field and
        # what the copy-paste actually runs diverge.
        svc_image = svc.get("image")
        if isinstance(svc_image, str) and app_id and image:
            allowed = {image, _pin_slot_for(app_id)}
            if svc_image not in allowed:
                errors.append(
                    f"{label}: install.compose service '{svc_name}' image "
                    f"'{svc_image}' does not match the entry's image "
                    f"'{image}' (or the pin slot {_pin_slot_for(app_id)!r})"
                )
    return errors


def validate_entry(
    entry: object,
    index: int,
    seen_ids: set[str],
    canonical_tasks: set[str],
    overlay_services: set[str] | None,
) -> tuple[list[str], list[str]]:
    """Validate one index entry.

    Returns ``(errors, warnings)`` — errors fail the build, warnings only
    print. Each string is prefixed with the entry label so the caller can
    emit them verbatim.
    """
    errors: list[str] = []
    warnings: list[str] = []
    label = _entry_label(entry, index)

    if not isinstance(entry, dict):
        return [f"{label}: not a mapping (got {type(entry).__name__})"], warnings

    # Required fields present + non-empty.
    for field in REQUIRED_FIELDS:
        if field not in entry:
            errors.append(f"{label}: missing required field '{field}'")
        elif field != "requires_tasks" and not entry[field]:
            errors.append(f"{label}: field '{field}' is empty")

    # Type checks mirroring the pydantic model. Pydantic v2 does NOT
    # coerce e.g. a YAML float ``version: 1.0`` to str — one such entry
    # would 500 the whole store page AND every install endpoint through
    # IndexEntry(**entry), so the gate refuses it here with a readable
    # message instead.
    for str_field in ("id", "name", "summary", "category", "version",
                      "image", "docs_url"):
        val = entry.get(str_field)
        if val is not None and not isinstance(val, str):
            errors.append(
                f"{label}: field '{str_field}' must be a string, got "
                f"{type(val).__name__} ({val!r}) — quote it in the YAML"
            )
    emits = entry.get("emits")
    if emits is not None and (
        not isinstance(emits, list)
        or any(not isinstance(e, str) for e in emits)
    ):
        errors.append(f"{label}: emits must be a list of strings")

    # id: kebab-case + unique + INSTALLABLE (a compose service exists).
    app_id = entry.get("id")
    if isinstance(app_id, str):
        if not _KEBAB_RE.match(app_id):
            errors.append(
                f"{label}: id '{app_id}' is not kebab-case "
                "(lowercase letters, digits, single hyphens)"
            )
        if app_id in seen_ids:
            errors.append(f"{label}: duplicate id '{app_id}'")
        seen_ids.add(app_id)
        # The install contract: both the one-click reconciler and the
        # copy-paste command run `docker compose … up -d <id>` against
        # docker-compose.apps.yml — an entry without a service block
        # there fails on install for EVERY user (review finding: 8 of 10
        # original entries were uninstallable exactly this way).
        if overlay_services is not None and app_id not in overlay_services:
            errors.append(
                f"{label}: id '{app_id}' has no service block in "
                "docker-compose.apps.yml — the entry is uninstallable. "
                "Add the service (+ config-init) to the overlay first; "
                "see docs/CONTRIBUTING_APPS.md."
            )
        elif overlay_services is None:
            warnings.append(
                f"{label}: could not read docker-compose.apps.yml to "
                f"verify a service block exists for '{app_id}'"
            )

    # image: well-formed ghcr.io/... or opennvr/... ref.
    image = entry.get("image")
    if isinstance(image, str) and image and not _IMAGE_RE.match(image):
        errors.append(
            f"{label}: image '{image}' is not a well-formed ref "
            "(expected ghcr.io/... or opennvr/...)"
        )

    # image_digest: optional, but must be sha256:<64 hex> when present.
    digest = entry.get("image_digest")
    if digest is not None:
        if not (isinstance(digest, str) and _DIGEST_RE.match(digest)):
            errors.append(
                f"{label}: image_digest '{digest}' must match "
                "sha256:<64 lowercase hex chars>"
            )

    # requires_adapters (RFC-0002 Phase 3, decision 7): adapters that must
    # be provisioned WITH the app. Names are snake_case KAI-C registry
    # names; each maps to the overlay service <name-with-dashes>-adapter.
    # A requirement without a service block is a WARNING, not an error —
    # it machine-checks the "cannot be provisioned" state (the historical
    # smart-doorbell NOTE): the store card already greys the app via its
    # unavailable task, and an install proceeds degraded exactly as the
    # app documents. Shipping the adapter service upgrades it silently.
    adapters = entry.get("requires_adapters", [])
    if not isinstance(adapters, list):
        errors.append(f"{label}: requires_adapters must be a list")
    else:
        for adapter in adapters:
            if not isinstance(adapter, str) or not re.fullmatch(
                    r"[a-z0-9]+(_[a-z0-9]+)*", adapter):
                errors.append(
                    f"{label}: requires_adapters entry {adapter!r} must be "
                    "a snake_case KAI-C adapter name")
                continue
            service = adapter.replace("_", "-") + "-adapter"
            if overlay_services is not None and service not in overlay_services:
                warnings.append(
                    f"{label}: required adapter '{adapter}' has no "
                    f"'{service}' service in docker-compose.apps.yml — the "
                    "app installs but that capability stays degraded until "
                    "an operator registers the adapter with KAI-C "
                    "themselves (RFC-0002 Phase 3)."
                )

    # requires_tasks: must be a list; unknown tasks warn (free-text allowed).
    tasks = entry.get("requires_tasks", [])
    if not isinstance(tasks, list):
        errors.append(f"{label}: requires_tasks must be a list")
    else:
        for task in tasks:
            if not isinstance(task, str):
                errors.append(f"{label}: requires_tasks entry {task!r} is not a string")
            elif task not in canonical_tasks:
                warnings.append(
                    f"{label}: requires_tasks '{task}' is not a known canonical "
                    "task (see server/config/use_case_map.yml). Free-text is "
                    "allowed, but prefer a canonical name if one fits."
                )

    # docs_url: must be https (repo-relative paths 404 in the SPA, and a
    # javascript:/data: href is a click-to-execute sink in the Docs link).
    docs_url = entry.get("docs_url")
    if isinstance(docs_url, str) and docs_url and not docs_url.startswith(
        "https://"
    ):
        errors.append(
            f"{label}: docs_url must be an https:// URL "
            f"(got '{docs_url}') — link the GitHub README, e.g. "
            "https://github.com/open-nvr/open-nvr/blob/main/examples/<id>/README.md"
        )

    # install: compose + command both present, non-empty, and SAFE.
    install = entry.get("install")
    if not isinstance(install, dict):
        errors.append(f"{label}: install must be a mapping with compose + command")
    else:
        compose = install.get("compose")
        command = install.get("command")
        if not (isinstance(compose, str) and compose.strip()):
            errors.append(f"{label}: install.compose is missing or empty")
        if not (isinstance(command, str) and command.strip()):
            errors.append(f"{label}: install.command is missing or empty")
        # The command is operator-EXECUTED content (rendered with a Copy
        # button) — it must be exactly the canonical compose-up for this
        # entry's id, nothing free-form.
        if isinstance(command, str) and isinstance(app_id, str):
            expected = _COMMAND_TEMPLATE.format(id=app_id)
            if command.strip() != expected:
                errors.append(
                    f"{label}: install.command must be exactly "
                    f"'{expected}' (got {command.strip()!r})"
                )
        # Snippet safety: parses, no dangerous directives, image matches.
        if isinstance(compose, str) and compose.strip():
            errors.extend(
                _check_compose_snippet(
                    label,
                    app_id if isinstance(app_id, str) else None,
                    image if isinstance(image, str) else None,
                    compose,
                )
            )
            # No plaintext secrets anywhere in the compose block…
            for offender in _looks_like_baked_secret(compose):
                errors.append(
                    f"{label}: install.compose contains a plaintext secret "
                    f"(use a ${{VAR}} placeholder): {offender!r}"
                )
        # …or in the command.
        if isinstance(command, str):
            for offender in _looks_like_baked_secret(command):
                errors.append(
                    f"{label}: install.command contains a plaintext secret "
                    f"(use a ${{VAR}} placeholder): {offender!r}"
                )

    return errors, warnings


def validate_index(path: Path) -> tuple[list[str], list[str]]:
    """Validate an entire apps index file. Returns ``(errors, warnings)``."""
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        return [f"index file not found: {path}"], []
    except yaml.YAMLError as exc:
        return [f"{path}: YAML parse error: {exc}"], []

    if raw is None:
        return [f"{path}: index is empty"], []
    if not isinstance(raw, list):
        return [f"{path}: top level must be a list of entries, got {type(raw).__name__}"], []

    canonical_tasks = _load_canonical_tasks()
    overlay_services = _load_overlay_services()
    seen_ids: set[str] = set()
    errors: list[str] = []
    warnings: list[str] = []
    for i, entry in enumerate(raw):
        entry_errors, entry_warnings = validate_entry(
            entry, i, seen_ids, canonical_tasks, overlay_services
        )
        errors.extend(entry_errors)
        warnings.extend(entry_warnings)
    return errors, warnings


def main(argv: list[str]) -> int:
    path = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_INDEX
    errors, warnings = validate_index(path)

    for warning in warnings:
        print(f"WARN  {warning}", file=sys.stderr)

    if errors:
        print(f"\napps_index validation FAILED ({len(errors)} error(s)) — {path}", file=sys.stderr)
        for error in errors:
            print(f"  ERROR {error}", file=sys.stderr)
        return 1

    n = 0
    try:
        n = len(yaml.safe_load(path.read_text()) or [])
    except Exception:
        pass
    warn_note = f" ({len(warnings)} warning(s))" if warnings else ""
    print(f"apps_index OK — {n} entr{'y' if n == 1 else 'ies'} validated{warn_note}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
