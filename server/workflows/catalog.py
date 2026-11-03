# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""What a workflow file is checked against, and the templates it can start from.

:func:`site_registry` is the site as the SDK's validator sees it
(``opennvr_app_sdk.workflows.Registry``): the AI step's read tools, and
the installed apps with their actions, where an action declared
``confirm=True`` moves things (RFC #620, section 4). Camera roles, webhook
destinations and site lists have no store yet, so they are not checked
at save; the engine refuses an unknown one when it runs.

:func:`templates` are the YAML files beside this module (RFC #620,
section 6), checked when they are read: a broken template fails its test,
not a user.
"""
from __future__ import annotations

import functools
import pathlib
from typing import Any

import yaml
from opennvr_app_sdk.agent import READ_TOOL_NAMES
from opennvr_app_sdk.workflows import AppInfo, Registry, Report, validate
from sqlalchemy.orm import Session

from models import InstalledApp

TEMPLATES = pathlib.Path(__file__).with_name("templates")


def site_registry(db: Session) -> Registry:
    apps = {}
    for app in db.query(InstalledApp).all():
        actions = (app.manifest_json or {}).get("actions") or []
        apps[app.id] = AppInfo(actions={a["name"]: bool(a.get("confirm"))
                                        for a in actions if isinstance(a, dict) and a.get("name")})
    return Registry(skills=dict.fromkeys(READ_TOOL_NAMES, "read"), apps=apps)


def check(text: str, db: Session) -> Report:
    """The SDK's validator, against this site."""
    return validate(text, registry=site_registry(db))


def needs_list(report: Report) -> list[str]:
    """``{"apps": ["gate-controller"]}`` -> ``["app:gate-controller"]``."""
    kinds = {"skills": "skill", "apps": "app", "cameras": "camera", "webhooks": "webhook",
             "lists": "list"}
    return [f"{kinds[k]}:{v}" for k, vs in report.needs.items() for v in vs]


@functools.lru_cache(maxsize=1)
def templates() -> tuple[dict[str, Any], ...]:
    out = []
    for path in sorted(TEMPLATES.glob("*.yml")):
        lines = path.read_text().splitlines()
        # The licence header is the repository's, not the user's workflow.
        while lines and (lines[0].startswith("#") or not lines[0].strip()):
            lines.pop(0)
        body = "\n".join(lines)
        report = validate(body, registry=Registry(skills=READ_TOOL_NAMES))
        if not report.ok:
            raise ValueError(f"template {path.name}: {[str(e) for e in report.errors]}")
        doc = yaml.safe_load(body)
        out.append({"id": path.stem, "name": doc["name"],
                    "description": " ".join(str(doc.get("description") or "").split()),
                    "needs": needs_list(report), "yaml": body + "\n"})
    return tuple(out)

