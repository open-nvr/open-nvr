# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""
opennvr-app-sdk — the shared base for OpenNVR monitoring apps.

Per the App SDK spec, the SDK folds config loading, §11.5 alert
dispatch, zone geometry, keyed TTL state, the NATS subscribe loop, the
CLI, and signal handling behind one runnable app — what's left to write
is the rule plus a declarative :class:`AppManifest`.

Start with :class:`App`
-----------------------

:class:`App` is the front door. It declares the manifest, the config
and the rules in one place, and compiles down to the :class:`Detector`
described below — same process, same manifest, same alerts::

    from opennvr_app_sdk import App

    app = App("driveway-watch", name="Driveway Watch", category="perimeter")
    app.param("dwell_s", float, default=30.0)

    @app.on_detection("person", zone="driveway", dwell="$dwell_s")
    def loitering(event):
        event.alert(f"Person loitering on {event.camera}", severity="high")

    if __name__ == "__main__":
        raise SystemExit(app.run())

Everything below the facade stays available and unchanged; drop to it
when a rule outgrows the decorators.

Archetypes (spec §02):

* :class:`Detector` — subscribes to ``opennvr.inference.*`` events
  another app is already driving (loitering, counting, dashboards).
* :class:`FrameApp` — drives inference itself by polling frames into
  KAI-C (intrusion, LPR, package delivery).
* :class:`AlertSubscriber` — consumes ``opennvr.alerts.*`` (the
  alerts-subscriber template, HA relay, SIEM bridges).

Apache-2.0, unlike the AGPL example apps — the SDK is meant to be
embedded in third-party apps the same way ``opennvr-adapter-sdk`` is.
"""
from .alerts import (
    DEFAULT_ALERT_SUBJECT_PREFIX,
    Alert,
    AlertChannel,
    AlertDispatcher,
    AlertSource,
    NatsAlertChannel,
    StdoutChannel,
    WebhookChannel,
    alert_subject,
    build_dispatcher,
    set_default_source,
)
from .alert_subscriber import AlertSubscriber, AlertSubscriberRunner, alert_app
from .config import BaseAppConfig, load_app_config, load_yaml, require
from .contract import ContractServer, Entitlement
from .detector import AppRunner, Detector, app
from .facade import (
    DEFAULT_ABSENCE_S, DEFAULT_MIN_CONFIDENCE, App, DetectionEvent, Setting,
    setting,
)
from .openapi import CONTRACT_API_VERSION, contract_asyncapi, contract_openapi
from .frame_app import FrameApp, FrameSource, KaiCClient, KaiCError
from .frame_sources import (
    CameraFrameSource,
    DictFrameSource,
    FileFrameSource,
    FrameSourceError,
    HttpSnapshotSource,
    build_frame_source,
    dict_frame_source,
)
from .geometry import Point, Tripwire, Zone, bbox_center
from .manifest import (
    DETECTION_LABELS, ENTITLEMENT_MODES, PRICING_MODELS, Action, AlertType, AppManifest,
    Param, StateView,
)
from .state import KeyedState, StateRecord, keyed_state
from .domain_events import DomainEventPublisher, domain_envelope, domain_subject
from .events import EventsClient, StoredEvent
from .cameras import (
    cameras_for_skill,
    discover_cameras,
    filter_cameras_for_skill,
    full_frame_polygon,
)
from .credentials import AppCredentials, auth_headers
from .usercontext import UserContext, current_user, verify_call_token
from .client import OpenNVR, Camera, Recording, PlatformError
from .aio import AsyncOpenNVR
from .infer_stream import InferStream
from .domain_subscriber import (
    DomainEvent, DomainEventSubscriber, domain_event_app, parse_domain_event,
)
from .egress import connect_via_proxy, proxy_address
from .event_types import (
    EVENT_TYPES, AccessDecided, DetectionObserved, OccupancyChanged, OccupancyFootfall,
    OccupancyHeatmap, PlateRecognized, TypedPayload, VisitRecorded, typed_payload,
)
from .tier0 import (
    BestFrameClient,
    Tier0Snapshot,
    describe_counts,
    is_tier0_subject,
    tier0_to_detections,
    make_best_frame_fetch,
    snapshot_from_event,
)

from ._version import __version__  # noqa: E402

# ── The public API, in tiers ────────────────────────────────────────
#
# ``__all__`` is assembled from these, so the tiers ARE the export
# list — there is no second place to update, and the documentation
# site builds its navigation from the same tuples. A name in two
# tiers, or in none, fails tests/test_public_api.py.

#: Start here — the whole of a first app.
FRONT_DOOR: tuple[str, ...] = (
    "App",
    "DetectionEvent",
    "Alert",
    "AppManifest",
    "Param",
    "setting",
)

#: The classes the facade compiles to, and their runners. Subclass one when a rule outgrows the decorators.
ARCHETYPES: tuple[str, ...] = (
    "Detector",
    "FrameApp",
    "AlertSubscriber",
    "DomainEventSubscriber",
    "app",
    "alert_app",
    "domain_event_app",
    "AppRunner",
    "AlertSubscriberRunner",
    "BaseAppConfig",
    "load_app_config",
)

#: The pieces a rule is built from: where, how long, and what to fire.
RULES: tuple[str, ...] = (
    "Zone",
    "Tripwire",
    "Point",
    "bbox_center",
    "full_frame_polygon",
    "keyed_state",
    "KeyedState",
    "StateRecord",
    "AlertType",
    "AlertSource",
    "AlertChannel",
    "AlertDispatcher",
    "StdoutChannel",
    "WebhookChannel",
    "NatsAlertChannel",
    "build_dispatcher",
    "alert_subject",
    "set_default_source",
    "DEFAULT_ALERT_SUBJECT_PREFIX",
    "DETECTION_LABELS",
    "Setting",
    "DEFAULT_ABSENCE_S",
    "DEFAULT_MIN_CONFIDENCE",
)

#: Everything an app reads from the running deployment.
PLATFORM: tuple[str, ...] = (
    "OpenNVR",
    "AsyncOpenNVR",
    "Camera",
    "Recording",
    "PlatformError",
    "EventsClient",
    "StoredEvent",
    "KaiCClient",
    "KaiCError",
    "InferStream",
    "discover_cameras",
    "cameras_for_skill",
    "filter_cameras_for_skill",
    "AppCredentials",
    "auth_headers",
    "FrameSource",
    "CameraFrameSource",
    "FileFrameSource",
    "HttpSnapshotSource",
    "DictFrameSource",
    "build_frame_source",
    "dict_frame_source",
    "FrameSourceError",
)

#: What the app exposes back: the catalog's config form, dashboard, actions, licence gate — and the generated specs.
SURFACES: tuple[str, ...] = (
    "StateView",
    "Action",
    "ContractServer",
    "Entitlement",
    "PRICING_MODELS",
    "ENTITLEMENT_MODES",
    "UserContext",
    "current_user",
    "verify_call_token",
    "contract_openapi",
    "contract_asyncapi",
    "CONTRACT_API_VERSION",
    "proxy_address",
    "connect_via_proxy",
)

#: The bus: contracted domain events, and Tier-0.
EVENTS: tuple[str, ...] = (
    "DomainEvent",
    "parse_domain_event",
    "DomainEventPublisher",
    "domain_envelope",
    "domain_subject",
    "TypedPayload",
    "typed_payload",
    "EVENT_TYPES",
    "DetectionObserved",
    "VisitRecorded",
    "PlateRecognized",
    "AccessDecided",
    "OccupancyChanged",
    "OccupancyHeatmap",
    "OccupancyFootfall",
    "Tier0Snapshot",
    "snapshot_from_event",
    "tier0_to_detections",
    "is_tier0_subject",
    "describe_counts",
    "BestFrameClient",
    "make_best_frame_fetch",
)

#: Low-level config helpers, for loaders that do their own parsing.
CONFIG: tuple[str, ...] = (
    "load_yaml",
    "require",
)

#: Tier name → the names it exports, in documentation order.
API_TIERS: dict[str, tuple[str, ...]] = {
    "front-door": FRONT_DOOR,
    "archetypes": ARCHETYPES,
    "rules": RULES,
    "platform": PLATFORM,
    "surfaces": SURFACES,
    "events": EVENTS,
    "config": CONFIG,
}

__all__ = ["API_TIERS", *(name for tier in API_TIERS.values() for name in tier)]
