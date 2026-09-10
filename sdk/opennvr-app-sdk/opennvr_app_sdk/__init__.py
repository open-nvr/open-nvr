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

    @app.on_detection("person", zone="driveway", dwell=30)
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
from .facade import DEFAULT_MIN_CONFIDENCE, App, DetectionEvent
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

__all__ = [
    # ── The front door ────────────────────────────────────────
    # Start here: App + DetectionEvent are the whole of a first
    # app. Everything below is the layer they compile down to,
    # reachable when a rule outgrows the facade.
    "App",
    "DetectionEvent",
    "DEFAULT_MIN_CONFIDENCE",
    # The stack's egress proxy, for plain-TCP clients (docs/APP_NETWORK.md)
    "proxy_address",
    "connect_via_proxy",
    # Typed domain-event payloads (docs/EVENT_CONTRACTS.md as Python)
    "TypedPayload",
    "EVENT_TYPES",
    "typed_payload",
    "DetectionObserved",
    "VisitRecorded",
    "PlateRecognized",
    "AccessDecided",
    "OccupancyChanged",
    "OccupancyHeatmap",
    "OccupancyFootfall",
    # Domain events (producing side of docs/EVENT_CONTRACTS.md)
    "DomainEventPublisher",
    "domain_envelope",
    "domain_subject",
    # Archetype bases + runners
    "Detector",
    "FrameApp",
    "AlertSubscriber",
    "AppRunner",
    "AlertSubscriberRunner",
    "app",
    "alert_app",
    # Alerts (§11.5)
    "Alert",
    "AlertSource",
    "AlertChannel",
    "AlertDispatcher",
    "StdoutChannel",
    "WebhookChannel",
    "NatsAlertChannel",
    "alert_subject",
    "build_dispatcher",
    "set_default_source",
    "DEFAULT_ALERT_SUBJECT_PREFIX",
    # Manifest
    "AppManifest",
    "PRICING_MODELS",
    "ENTITLEMENT_MODES",
    "DETECTION_LABELS",
    "Entitlement",
    "Param",
    "AlertType",
    "StateView",
    "Action",
    # Keyed TTL state
    "keyed_state",
    "KeyedState",
    "StateRecord",
    # Geometry
    "Point",
    "Zone",
    "Tripwire",
    "bbox_center",
    # Config helpers
    "load_yaml",
    "require",
    # Cameras (roster + per-camera assignment) and the app's credential
    "discover_cameras",
    "cameras_for_skill",
    "filter_cameras_for_skill",
    "full_frame_polygon",
    "AppCredentials",
    "auth_headers",
    # The operator behind a /ui view or an action (X-OpenNVR-User)
    "UserContext",
    "verify_call_token",
    "current_user",
    # The platform client (everything an app reads from core / KAI-C)
    "OpenNVR",
    "BaseAppConfig",
    "load_app_config",
    "AsyncOpenNVR",
    "Camera",
    "Recording",
    "PlatformError",
    "InferStream",
    # Consuming contracted domain events
    "DomainEvent",
    "DomainEventSubscriber",
    "domain_event_app",
    "parse_domain_event",
    # Frame-app plumbing
    "FrameSource",
    "KaiCClient",
    "KaiCError",
    # Per-camera frame sources
    "CameraFrameSource",
    "FileFrameSource",
    "HttpSnapshotSource",
    "FrameSourceError",
    "build_frame_source",
    "DictFrameSource",
    "dict_frame_source",
    # Contract surface (§03)
    "ContractServer",
    # Tier-0 consumption (answer from the always-on detector; reuse its best frame)
    "Tier0Snapshot",
    "snapshot_from_event",
    "describe_counts",
    "EventsClient",
    "StoredEvent",
    "is_tier0_subject",
    "tier0_to_detections",
    "BestFrameClient",
    "make_best_frame_fetch",
]
