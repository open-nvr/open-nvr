# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""
App manifests — the declarative block every app ships (App SDK spec §03/§05).

The manifest is load-bearing three ways:

* the future ``GET /manifest`` contract endpoint returns
  ``manifest.to_dict()`` so the catalog can render a card + config
  form without app-specific code;
* ``PUT /apps/{id}/config`` validates operator config against
  ``params`` without app-specific code;
* ``requires_tasks`` is checked against ``GET /api/v1/adapters`` so
  the catalog can grey out apps whose model prerequisites aren't met.

Param ``type`` accepts either a Python type (``float``, ``list``, …)
or a string for UI-schema types the catalog renders specially
(``"geometry.polygon"`` becomes a zone editor on a camera still).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _type_name(t: Any) -> str:
    """Render a Param type for the wire: Python types by name
    (``float`` → ``"float"``), strings pass through
    (``"geometry.polygon"``)."""
    if isinstance(t, type):
        return t.__name__
    return str(t)


@dataclass
class Param:
    """One typed, declarative config knob.

    ``per_camera=True`` marks params the catalog collects per camera
    (zones, tripwires) rather than once per app."""

    name: str
    type: Any
    default: Any = None
    per_camera: bool = False
    description: str = ""
    # Required params have no usable default; the future PUT /config
    # validator and the catalog form both need the distinction.
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "type": _type_name(self.type),
            "default": self.default,
            "per_camera": self.per_camera,
            "description": self.description,
        }


@dataclass
class AlertType:
    """One alert kind the app can emit — drives catalog documentation
    and downstream routing defaults."""

    name: str
    severity: str = "medium"  # low / medium / high / critical
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StateView:
    """One declarative view over the app's ``GET /state`` payload.

    The catalog renders these with ZERO app-specific UI code — the same
    bet as ``params`` → config form. An app that exposes richer live
    state (occupancy per zone, plates deduped, tracks active) declares
    how to show it instead of shipping a frontend:

    ``kind="metric"``
        A single scalar at ``path`` rendered as a stat chip
        (e.g. ``path="denylist_size"`` → "Denylist · 4").
    ``kind="table"``
        A list at ``path``; ``columns`` names the keys to show when the
        rows are dicts. A list of scalars renders as one column.
    ``kind="gauge"``
        A numeric ``path`` rendered as a horizontal bar between ``min``
        and ``max``, coloured amber past ``warn`` and red past
        ``danger`` (e.g. zone occupancy). A dict-of-numbers renders one
        gauge per key (per camera / per zone).
    ``kind="log"``
        A recent-events feed: ``path`` is a list of strings or dicts
        ``{message, time, level}``; newest ``limit`` shown first.
    ``kind="gallery"``
        A thumbnail wall: ``path`` is a list of dicts
        ``{image|url, label, time}`` — for plate crops, package or
        doorbell snapshots. ``image`` may be a ``data:`` URI.

    ``path`` is a dot-path into the ``/state`` dict (``"zones"``,
    ``"counters.in"``). A missing path renders as an em-dash, never an
    error — ``/state`` is live data and may not have filled in yet.
    """

    name: str
    label: str
    kind: str = "metric"  # metric | table | gauge | log | gallery
    path: str = ""
    columns: list[str] = field(default_factory=list)
    description: str = ""
    # gauge bounds/thresholds (ignored by other kinds)
    min: float | None = None
    max: float | None = None
    unit: str = ""
    warn: float | None = None
    danger: float | None = None
    # log / gallery: how many recent entries to show
    limit: int | None = None

    def to_dict(self) -> dict[str, Any]:
        # Drop unset optionals so manifests stay lean and the frontend's
        # `?? default` fallbacks apply cleanly.
        d = asdict(self)
        for k in ("min", "max", "warn", "danger", "limit"):
            if d.get(k) is None:
                d.pop(k, None)
        if not d.get("unit"):
            d.pop("unit", None)
        return d


@dataclass
class Action:
    """One operator-invokable action on the app's contract surface.

    Declared like params, rendered like params: the catalog builds a
    generic form from ``params`` and POSTs it to
    ``/actions/{name}`` on the app — proxied through the server's
    ``POST /api/v1/apps/{id}/actions/{name}``, which is **user-JWT
    only**. The governance boundary is deliberate: actions are operator
    verbs (search footage, enroll a face); the OpenNVR Agent's service
    key can read state but can NEVER invoke an action.

    ``confirm=True`` makes the catalog ask before invoking (for actions
    with side effects). The app implements the verb by overriding
    :meth:`ContractMixin.on_action`.
    """

    name: str
    label: str
    params: list[Param] = field(default_factory=list)
    description: str = ""
    confirm: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "params": [p.to_dict() for p in self.params],
            "description": self.description,
            "confirm": self.confirm,
        }


@dataclass
class AppManifest:
    """The static identity + schema of one app.

    ``subscribes`` is the NATS subject pattern for InferenceSubscriber
    apps (``None`` for FrameApps that drive inference themselves).
    ``requires_tasks`` names adapter task types the app depends on,
    e.g. ``["object_detection"]``. ``requires_adapters`` (RFC-0002
    Phase 3, decision 7) names the specific KAI-C adapters that must be
    PROVISIONED with the app — the installer ups them alongside the app
    and refcounts them across apps on uninstall. Adapters the standard
    stack already ships (yolov8) are reused, not listed.
    """

    id: str
    name: str
    version: str
    category: str
    summary: str = ""
    requires_tasks: list[str] = field(default_factory=list)
    requires_adapters: list[str] = field(default_factory=list)
    # RFC-0002 Phase 5: event scopes this app requests, e.g.
    # ["events:plate.recognized"]. Scopes name DOMAIN events (the
    # opennvr.events.* tree, docs/EVENT_CONTRACTS.md) — plate reads are
    # PII (decision 6), so consuming them is a declared, granted,
    # audited capability, visible in the App Catalog. Registration
    # auto-grants and audits each scope today; bus-side enforcement
    # (per-app NATS users whose subscribe permissions ARE the grants)
    # is the staged follow-up recorded in the RFC.
    requires_scopes: list[str] = field(default_factory=list)
    subscribes: str | None = None
    params: list[Param] = field(default_factory=list)
    emits: list[AlertType] = field(default_factory=list)
    # Declarative live-state views (optional) — how the catalog renders
    # this app's GET /state payload. Empty ⇒ the catalog shows raw
    # state JSON as before.
    state_schema: list[StateView] = field(default_factory=list)
    # Declarative operator actions (optional) — verbs the catalog can
    # invoke on the app's contract surface via the server's JWT-only
    # proxy. Empty ⇒ no Actions section renders.
    actions: list[Action] = field(default_factory=list)
    # RFC-0002 Phase 4 app-surface convention: True ⇒ the app serves an
    # HTML dashboard at GET /ui on its contract port, and core proxies
    # it at /api/v1/apps/{id}/ui (the catalog renders it sandboxed).
    has_ui: bool = False
    # How the app's UI reaches the operator:
    #   "internal" (default) — the sandboxed GET /ui dashboard above is
    #       the whole surface, embedded in the catalog's app page.
    #   "external" — the app is a full application with its own web UI
    #       (multi-page, scripted, its own auth); the catalog renders an
    #       "Open app" button instead of embedding. External UIs are NOT
    #       proxied by core: the operator's browser talks to the app
    #       directly, so ``ui_url`` must be reachable from the browser,
    #       not just the compose network.
    ui_mode: str = "internal"
    # Where "Open app" points when ``ui_mode="external"``. Apps rarely
    # know their deployment hostname at build time, so the literal
    # placeholder ``{host}`` is replaced by the catalog with the hostname
    # the operator is browsing from: ``"http://{host}:8090/"``.
    ui_url: str = ""

    # ── Store listing (the catalog's Details section) ───────────────
    # The app's storefront: the catalog app page renders these so an
    # operator can understand the app completely before enabling it.
    # ``description`` is long-form text — blank-line-separated blocks
    # render as paragraphs. ``use_cases`` is a short list of the
    # concrete jobs the app solves ("Alarm on unregistered vehicles").
    description: str = ""
    author: str = ""
    website: str = ""
    license: str = ""
    use_cases: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.ui_mode not in ("internal", "external"):
            raise ValueError(
                f"ui_mode must be 'internal' or 'external', got {self.ui_mode!r}"
            )
        if self.ui_mode == "external" and not self.ui_url:
            raise ValueError(
                "ui_mode='external' requires ui_url (the browser-reachable "
                "URL of the app's UI; '{host}' is substituted by the catalog)"
            )

    def to_dict(self) -> dict[str, Any]:
        """The ``GET /manifest`` payload (and the ``manifest_json``
        snapshot the app registry stores)."""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "category": self.category,
            "summary": self.summary,
            "requires_tasks": list(self.requires_tasks),
            "requires_adapters": list(self.requires_adapters),
            "requires_scopes": list(self.requires_scopes),
            "subscribes": self.subscribes,
            "params": [p.to_dict() for p in self.params],
            "emits": [a.to_dict() for a in self.emits],
            "state_schema": [v.to_dict() for v in self.state_schema],
            "actions": [a.to_dict() for a in self.actions],
            "has_ui": bool(self.has_ui),
            "ui_mode": self.ui_mode,
            "ui_url": self.ui_url,
            "description": self.description,
            "author": self.author,
            "website": self.website,
            "license": self.license,
            "use_cases": list(self.use_cases),
        }
