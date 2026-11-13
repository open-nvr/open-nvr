# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""`AppManifest`, `Param`, `AlertType`, `StateView`, `Action` — the
declaration that makes the App Catalog render your app.

Demonstrates: every manifest field the catalog reads, `StateView` of
each kind, `Action` with typed params, `ContractMixin.state_snapshot`,
`ContractMixin.on_action`, `ui_html`, `DETECTION_LABELS`.

The bet the SDK makes: an app that DECLARES its surfaces needs no
frontend. The catalog builds the config form from `params`, the
dashboard from `state_schema`, the buttons from `actions`, and the
store listing from `description` / `use_cases` / `pricing` — with zero
app-specific UI code. This file is the whole vocabulary in one place.
"""
from typing import Any

from opennvr_app_sdk import (
    Action, AlertType, AppManifest, DETECTION_LABELS, Detector, Param, StateView,
)

MANIFEST = AppManifest(
    # ── Identity ───────────────────────────────────────────────────
    id="footage-search",                 # kebab-case, unique, immutable
    name="Footage Search",
    version="1.2.0",                     # semantic; the catalog shows it
    category="forensics",                # perimeter | analytics | vehicle |
                                         # doorstep | forensics | integration
    summary="Find the clip where something happened, by describing it.",

    # ── Prerequisites — the catalog greys the app out without them ──
    requires_tasks=["object_detection"],          # any adapter advertising it
    requires_adapters=[],                         # a SPECIFIC adapter to provision
    requires_scopes=[],                           # PII-bearing domain events
    provides=["forensics"],                       # lights a first-class page
    subscribes="opennvr.inference.>",             # None for a FrameApp

    # ── Config form ────────────────────────────────────────────────
    params=[
        Param("watch_labels", list, default=["person", "car"],
              description="Labels worth indexing.",
              suggestions=list(DETECTION_LABELS[:8])),
        Param("retention_days", int, default=30, required=True,
              description="How long to keep the index."),
        Param("search_zone", "geometry.polygon", per_camera=True,
              description="Restrict the index to this area of each camera."),
    ],

    # ── What it fires ──────────────────────────────────────────────
    emits=[
        AlertType("index-stalled", severity="high",
                  description="No frames indexed for an hour."),
    ],

    # ── The dashboard, declared not coded ──────────────────────────
    state_schema=[
        StateView(name="indexed", label="Frames indexed", kind="metric",
                  path="indexed_total"),
        StateView(name="backlog", label="Index backlog", kind="gauge",
                  path="backlog", min=0, max=1000, warn=200, danger=600,
                  unit="frames"),
        StateView(name="recent", label="Recent hits", kind="table",
                  path="recent", columns=["when", "camera", "label"]),
        StateView(name="log", label="Activity", kind="log", path="log", limit=20),
        StateView(name="thumbs", label="Best frames", kind="gallery",
                  path="thumbs", limit=12),
    ],

    # ── Operator verbs, declared not coded ─────────────────────────
    actions=[
        Action(name="search", label="Search footage",
               description="Find clips matching a description.",
               params=[Param("query", str, required=True,
                             description="e.g. 'red car after 9pm'"),
                       Param("hours", int, default=24)]),
        Action(name="reindex", label="Rebuild the index", confirm=True,
               description="Discards and rebuilds. Takes minutes."),
    ],

    # ── Store listing ──────────────────────────────────────────────
    description=(
        "Footage Search indexes every detection the platform already "
        "produces and lets an operator find the moment by describing it.\n\n"
        "Nothing leaves the deployment: the index is local."
    ),
    use_cases=["Find the clip an insurer asked for",
               "Answer 'when did that van last come?' in seconds"],
    author="OpenNVR",
    website="https://opennvr.org",
    license="AGPL-3.0-or-later",
    contact="mailto:apps@opennvr.org",

    # ── Commerce ───────────────────────────────────────────────────
    pricing="free",                      # free | paid | subscription | contact
    price_note="",                       # "$29 / camera / year"
    entitlement="none",                  # none | license_key (see 10_selling.py)

    # ── UI ─────────────────────────────────────────────────────────
    has_ui=True,                         # serve GET /ui, embedded sandboxed
    ui_mode="internal",                  # "external" → an "Open app" button
)


class FootageSearch(Detector):
    """The implementation side of the declarations above."""

    manifest = MANIFEST

    def setup(self) -> None:
        self.indexed_total = 0
        self.recent: list[dict[str, Any]] = []
        self.log: list[str] = []

    def on_detections(self, camera_id, detections, event):
        self.indexed_total += len(detections)
        return []

    # `state_schema` paths above resolve into THIS dict.
    def state_snapshot(self) -> dict[str, Any]:
        return {
            "indexed_total": self.indexed_total,
            "backlog": 0,
            "recent": self.recent[-50:],
            "log": self.log[-50:],
            "thumbs": [],
        }

    # `actions` above dispatch into THIS method. Raise KeyError for a
    # name you don't handle (→ 404) and ValueError for bad params
    # (→ 400); anything else becomes a 500 without taking the app down.
    def on_action(self, name: str, params: dict[str, Any]) -> Any:
        if name == "search":
            query = str(params.get("query") or "").strip()
            if not query:
                raise ValueError("'query' is required")
            return {"hits": self.search(query, int(params.get("hours", 24)))}
        if name == "reindex":
            self.indexed_total = 0
            return {"status": "rebuilding"}
        raise KeyError(name)

    def search(self, query: str, hours: int) -> list[dict[str, Any]]:
        return []

    # `has_ui=True` means this is served at GET /ui and proxied by core
    # at /api/v1/apps/{id}/ui, rendered sandboxed in the catalog.
    def ui_html(self) -> str:
        return (
            "<!doctype html><meta charset='utf-8'>"
            f"<h3>Footage Search</h3><p>{self.indexed_total} frames indexed.</p>"
        )
