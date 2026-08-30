# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""
opennvr-app-sdk — the shared base for OpenNVR monitoring apps.

Per the App SDK spec, the SDK folds config loading, §11.5 alert
dispatch, zone geometry, keyed TTL state, the NATS subscribe loop, the
CLI, and signal handling behind ``app(Detector).run()`` — what's left
in an app is the rule plus a declarative :class:`AppManifest`.

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
from .config import load_yaml, require
from .contract import ContractServer
from .detector import AppRunner, Detector, app
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
from .manifest import Action, AlertType, AppManifest, Param, StateView
from .state import KeyedState, StateRecord, keyed_state
from .domain_events import DomainEventPublisher, domain_envelope, domain_subject
from .events import EventsClient, StoredEvent
from .tier0 import (
    BestFrameClient,
    Tier0Snapshot,
    describe_counts,
    is_tier0_subject,
    tier0_to_detections,
    make_best_frame_fetch,
    snapshot_from_event,
)

__version__ = "0.2.0"

__all__ = [
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
