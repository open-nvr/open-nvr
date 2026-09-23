# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
smart-doorbell — poll a doorbell camera, recognise faces via the
InsightFace adapter through KAI-C, fire alerts with severity that
depends on whether the face is registered.

Now built on the ``opennvr-app-sdk``. The SDK's
:class:`~opennvr_app_sdk.FrameApp` base owns the poll loop, per-camera
fetch/rule failure isolation, and the §03 contract endpoints. The
frame sources and the §11.5 alert stack moved into the SDK (thin shims
remain at ``frame_sources.py`` / ``alerts.py`` for import
compatibility).

What stays here — deliberately:

* the **face-DB enrollment flow** — operators register family members
  ahead of time via the ``enroll`` CLI subcommand below, which talks
  directly to the InsightFace adapter's ``/faces/register`` route
  (KAI-C does not proxy that surface, so neither does the SDK);
* ``KaicRecognitionClient`` — the SDK's ``KaiCClient`` behind this
  app's ``recognize`` spelling (``task="face_recognition"`` with the
  match ``threshold`` as a contract param);
* the recognised/unknown severity routing, the snapshot-for-strangers
  policy, and the dedup ledger (a plain dict keyed by
  ``(camera, person-or-unknown-bucket)`` — its shape is pinned by this
  app's tests).

Run as a daemon:
    python smart_doorbell.py daemon --config config.yml

Enroll Alice via REST (no shared volume needed):
    python smart_doorbell.py enroll \\
        --config config.yml \\
        --person-id alice \\
        --name "Alice Smith" \\
        --image ~/photos/alice.jpg \\
        --category family

List enrolled faces:
    python smart_doorbell.py list-faces --config config.yml
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import datetime as dt
import html as _html
import io
import json
import logging
import re
import signal
import sys
import time
import uuid
from types import SimpleNamespace
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx
import yaml

from alerts import (
    Alert,
    AlertDispatcher,
    AlertSource,
    DEFAULT_ALERT_SUBJECT_PREFIX,
    build_dispatcher,
)
from face_recognition_pipeline import (
    DEFAULT_RECOGNITION_THRESHOLD,
    FaceRead,
    FaceRecognitionPipeline,
    FaceRecognitionPipelineConfig,
    RecognitionClient,
)
from chime import ChimePolicy
from frame_sources import FrameSource, FrameSourceError, build_frame_source
from visit_log import VisitLog
from opennvr_app_sdk import (
    Action, AlertType, AppManifest, FrameApp, KaiCClient, Param, StateView,
)
from opennvr_app_sdk.frame_sources import CoreSnapshotSource, DictFrameSource

logger = logging.getLogger("smart-doorbell")

CORRELATION_ID_HEADER = "X-Correlation-Id"

# Cap on the raw JPEG snapshot we'll embed in an alert envelope.
# Base64 inflates by ~33%, so a 700 KB JPEG becomes ~933 KB on the
# wire — still under the NATS default 1 MB max_payload. Operators
# with NATS configured for larger payloads can override via
# ``snapshot_max_bytes`` in config. A snapshot above the cap is
# dropped from the envelope (the alert still fires) and a WARN log
# line names the camera so the operator can shrink the source.
_DEFAULT_SNAPSHOT_MAX_BYTES: int = 700 * 1024

# Who a face can be. One vocabulary for homes (family, friend), premises
# (resident, staff, contractor) and organisations (staff, contractor,
# visitor, watchlist), because the People page, the alert severity and
# the adapter's ``category`` field all read the same word.
PERSON_CATEGORIES: tuple[str, ...] = (
    "family", "resident", "friend", "staff", "contractor", "visitor", "watchlist",
)
# Alert severity per category. A watchlist match is the one recognised
# face that must alarm louder than a stranger.
_CATEGORY_SEVERITY: dict[str, str] = {
    "family": "low", "resident": "low", "friend": "low",
    "staff": "info", "contractor": "info", "visitor": "info",
    "watchlist": "high",
}

# The SDK FrameApp rejects a non-positive poll interval (its sleep is
# the shutdown-interruptible kind). This app historically accepted 0
# ("poll as fast as the cameras answer"); map that to a near-zero
# interval so old configs — and the test fixtures — keep working.
_MIN_POLL_INTERVAL_SECONDS = 0.001


MANIFEST = AppManifest(
    id="smart-doorbell",
    name="Smart Doorbell",
    version="1.0.0",
    category="doorstep",
    summary=(
        "Recognises faces at the door and keeps the People directory — family, "
        "residents, staff, contractors with expiry dates, a watchlist; strangers "
        "alert with a snapshot you can enrol from."
    ),
    requires_tasks=["face_recognition"],  # checked vs GET /api/v1/adapters
    # Lights the first-class People page (app/src/lib/appVerticals.ts):
    # the face directory, enrolment from door snapshots, visit history.
    provides=["people"],
    subscribes=None,  # FrameApp: drives inference itself via KAI-C
    params=[
        Param("poll_interval_seconds", float, default=1.0),
        Param("recognition_threshold", float, default=DEFAULT_RECOGNITION_THRESHOLD),
        Param("dedup_window_seconds", float, default=60.0,
              description="Per-(camera, person) re-fire suppression; 0 fires every read."),
        Param("attach_snapshot_for_unknowns", bool, default=True,
              description="Embed a base64 JPEG in unknown-face alerts only."),
        Param("snapshot_max_bytes", int, default=_DEFAULT_SNAPSHOT_MAX_BYTES,
              description="Pre-base64 snapshot cap; 0 disables the limit."),
        Param("history_days", int, default=30,
              description="How long a visit stays in the door's history."),
        Param("history_max", int, default=200,
              description="Hard cap on remembered visits, whatever the age."),
        Param("chime_enabled", bool, default=True,
              description="Ring for callers. Off still alerts and records."),
        Param("quiet_hours", str, default="",
              description="HH:MM-HH:MM in which only an alarm tone rings."),
        Param("rechime_seconds", int, default=300,
              description="Least gap between rings for the same caller."),
    ],
    emits=[
        AlertType("known_visitor", severity="low"),
        AlertType("unknown_visitor", severity="high",
                  description="Unrecognised face; carries a snapshot when enabled."),
        AlertType("watchlist_visitor", severity="high",
                  description="A face enrolled in the watchlist category was recognised."),
        AlertType("expired_pass", severity="high",
                  description="A recognised visitor or contractor whose valid_until date has passed."),
    ],
    has_ui=True,   # GET /ui dashboard, proxied at /api/v1/apps/{id}/ui
    state_schema=[
        StateView(name="enrolled", label="Enrolled faces", kind="metric",
                  path="enrolled_faces",
                  description="Faces in the adapter's DB (refreshed every minute)."),
        StateView(name="known", label="Known visitors", kind="metric",
                  path="visits.known", description="Since the app started."),
        StateView(name="strangers", label="Strangers", kind="metric",
                  path="visits.unknown", description="Since the app started."),
        StateView(name="deduped", label="Visitors tracked", kind="metric",
                  path="deduped_visitors_tracked"),
        StateView(name="cameras", label="Cameras", kind="table",
                  path="camera_health",
                  columns=["camera_id", "status", "last_frame_age_s", "error"],
                  description="Whether each door camera is answering."),
        StateView(name="strangers_wall", label="Latest strangers", kind="gallery",
                  path="stranger_gallery", limit=8,
                  description="Snapshots of unrecognised faces, newest first."),
        StateView(name="recent", label="Recent visitors", kind="log",
                  path="recent", limit=12,
                  description="Latest faces at the door; strangers show red."),
    ],
    # Operator actions (user-JWT-only): the face-enrollment UI that was
    # previously CLI-only. Talks to the InsightFace adapter's /faces/*
    # routes via the same _FaceAdminClient the CLI uses.
    actions=[
        Action(
            "enroll_face", "Enroll a face",
            params=[
                Param("name", str, required=True,
                      description="Display name (e.g. 'Alex Rivera')."),
                Param("image", "image", required=True,
                      description="A clear, front-facing photo: good light, no sunglasses or hat, "
                                  "face at least a third of the frame."),
                Param("category", str, default="family", choices=list(PERSON_CATEGORIES),
                      description="Who they are; sets the alert level (watchlist alarms high)."),
                Param("notes", str, default="",
                      description="Flat / unit, department, vehicle — anything the guard should see."),
                Param("valid_until", str, default="",
                      description="YYYY-MM-DD. After this date a contractor or visitor pass "
                                  "raises an expired-pass alert instead of a greeting."),
                Param("person_id", str, default="",
                      description="Leave blank to derive from the name; set to re-enrol an existing person."),
                Param("append", bool, default=False,
                      description="Add this photo to the person's existing samples instead of replacing "
                                  "them. More angles and lighting = fewer false strangers."),
            ],
            description="Register a known face so the doorbell greets them "
                        "instead of flagging a stranger.",
        ),
        Action(
            "enroll_stranger", "Enroll from a door snapshot",
            params=[
                Param("stranger_id", str, required=True,
                      description="The id of a snapshot on the strangers wall."),
                Param("name", str, default="",
                      description="For a new person. Leave blank when adding to an existing one."),
                Param("category", str, default="visitor", choices=list(PERSON_CATEGORIES)),
                Param("notes", str, default=""),
                Param("valid_until", str, default=""),
                Param("person_id", str, default="",
                      description="An enrolled person this face belongs to — the capture is added "
                                  "to their samples (the door learns its own angle and light)."),
            ],
            description="Turn a face the camera already saw into an enrolled person, or "
                        "add it to someone already enrolled — no photo to find, the door "
                        "just took it.",
        ),
        Action(
            "update_face", "Edit a person",
            params=[
                Param("person_id", str, required=True),
                Param("name", str, default=""),
                Param("category", str, default="", choices=[""] + list(PERSON_CATEGORIES)),
                Param("notes", str, default=""),
                Param("valid_until", str, default=""),
            ],
            description="Change name, category, notes or expiry without a new photo. "
                        "Blank fields are left as they are.",
        ),
        Action(
            "list_faces", "Enrolled faces", params=[],
            description="Everyone currently enrolled, with when they were last seen.",
        ),
        Action(
            "stranger_image", "Door snapshot",
            params=[Param("stranger_id", str, required=True)],
            description="The face crop behind a strangers-wall thumbnail, for review or enrolment.",
        ),
        Action(
            "delete_face", "Remove a face",
            params=[
                Param("person_id", str, required=True,
                      description="The id shown in 'Enrolled faces'."),
            ],
            description="Un-enroll a face.", confirm=True,
        ),
    ],
)


# ── Config ──────────────────────────────────────────────────────────


@dataclass
class CameraConfig:
    camera_id: str
    frame_url: str


@dataclass
class AppConfig:
    """Operator-tunable settings. Validated in ``load_config``."""

    # KAI-C is used for the recognition call (auditable).
    kaic_url: str
    kaic_api_key: str
    recognition_adapter: str = "insightface"
    #: How far outside a visit's span the frame's instant may fall and
    #: still attach the name (RFC-0003). Small on purpose: a doorbell
    #: frame is taken within a second or two of the person being there,
    #: and a window wide enough to feel safe is wide enough to attach a
    #: name to the wrong visitor. 0 refuses to guess at all — only a
    #: frame taken DURING a visit produces a claim.
    bind_tolerance_seconds: float = 5.0

    # The InsightFace adapter's direct URL for face-DB CRUD. KAI-C
    # does not proxy the /faces/* routes, so the enroll subcommand
    # hits the adapter directly. Bearer-token auth.
    adapter_url: str = "http://127.0.0.1:9005"
    adapter_token: str = ""

    cameras: list[CameraConfig] = field(default_factory=list)
    poll_interval_seconds: float = 1.0
    request_timeout_seconds: float = 30.0
    recognition_threshold: float = DEFAULT_RECOGNITION_THRESHOLD

    # Dedup: don't refire the same (camera, person-or-unknown-bucket)
    # alert within this window. Set 0 to fire every read.
    dedup_window_seconds: float = 60.0

    # If True, embed a base64 JPEG snapshot in UNKNOWN-face alert
    # envelopes only. A small downstream relay (see alerts-subscriber/)
    # can then forward the photo to Telegram / ntfy / Discord without
    # a second HTTP round-trip to the NVR. Known-face alerts still
    # ride small (no snapshot) so the alert bus stays low-bandwidth
    # in the common case.
    attach_snapshot_for_unknowns: bool = True

    # Pre-base64 cap on the embedded snapshot. Default keeps the
    # post-base64 envelope under NATS's 1 MB default max_payload.
    # A snapshot larger than this is dropped from the envelope
    # (the alert still fires) with a WARN log line.
    snapshot_max_bytes: int = _DEFAULT_SNAPSHOT_MAX_BYTES

    # How long the door remembers. The whole point of keeping a history
    # is "who came three days ago", so the default is a month rather
    # than the handful of entries a deque held — and it is bounded by a
    # count as well, because the log is one JSON value rewritten per
    # visit and a busy porch must not turn that into a megabyte.
    history_days: int = 30
    history_max: int = 200

    # The bell. An alert fires for every visit whatever these say —
    # these decide only whether something RINGS, which is a different
    # question and the one every annoying doorbell gets wrong.
    chime_enabled: bool = True
    # HH:MM-HH:MM. Inside it only the alarm tone rings: a delivery at
    # 03:00 goes in the log, a stranger at 03:00 still wakes the house.
    quiet_hours: str = ""
    # Longer than the alert dedup on purpose. Being told again in a feed
    # costs nothing; being rung at again costs attention.
    rechime_seconds: int = 300
    # category -> tone, overriding chime.DEFAULT_TONES. YAML rather than
    # a catalog Param because it is a mapping, not a value.
    chime_tones: dict[str, str] = field(default_factory=dict)

    # Alert delivery channels.
    webhook_url: str | None = None
    nats_alerts_url: str | None = None
    nats_alerts_token: str | None = None
    nats_alerts_subject_prefix: str = DEFAULT_ALERT_SUBJECT_PREFIX

    # App contract (spec §03) — all optional; see the SDK's contract
    # module. ``contract_port`` serves /health /manifest /state;
    # ``opennvr_url`` triggers registry self-registration on boot.
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise SystemExit(f"config file {path} did not parse to a dict")

    kaic_url = raw.get("kaic_url")
    kaic_api_key = raw.get("kaic_api_key")
    if not kaic_url:
        raise SystemExit("config: kaic_url is required")
    if not kaic_api_key:
        raise SystemExit("config: kaic_api_key is required")

    cameras_raw = raw.get("cameras") or []
    cameras: list[CameraConfig] = []
    for entry in cameras_raw:
        if not isinstance(entry, dict):
            raise SystemExit("config: each camera must be a mapping")
        cam_id = entry.get("camera_id")
        url = entry.get("frame_url")
        if not cam_id or not url:
            raise SystemExit("config: camera entries need camera_id + frame_url")
        cameras.append(CameraConfig(camera_id=cam_id, frame_url=url))
    if not cameras:
        # The enroll / list-faces subcommands DON'T need cameras
        # configured; the daemon does. We accept zero cameras at
        # parse time and check again at daemon-start.
        pass

    subject_prefix = str(
        raw.get("nats_alerts_subject_prefix", DEFAULT_ALERT_SUBJECT_PREFIX)
    ).strip() or DEFAULT_ALERT_SUBJECT_PREFIX

    return AppConfig(
        kaic_url=str(kaic_url),
        kaic_api_key=str(kaic_api_key),
        recognition_adapter=str(raw.get("recognition_adapter", "insightface")),
        bind_tolerance_seconds=max(
            0.0, float(raw.get("bind_tolerance_seconds", 5.0))),
        adapter_url=str(raw.get("adapter_url", "http://127.0.0.1:9005")),
        adapter_token=str(raw.get("adapter_token", "") or ""),
        cameras=cameras,
        poll_interval_seconds=float(raw.get("poll_interval_seconds", 1.0)),
        request_timeout_seconds=float(raw.get("request_timeout_seconds", 30.0)),
        recognition_threshold=float(
            raw.get("recognition_threshold", DEFAULT_RECOGNITION_THRESHOLD)
        ),
        dedup_window_seconds=float(raw.get("dedup_window_seconds", 60.0)),
        attach_snapshot_for_unknowns=bool(
            raw.get("attach_snapshot_for_unknowns", True)
        ),
        snapshot_max_bytes=int(
            raw.get("snapshot_max_bytes", _DEFAULT_SNAPSHOT_MAX_BYTES)
        ),
        history_days=int(raw.get("history_days", 30)),
        history_max=int(raw.get("history_max", 200)),
        chime_enabled=bool(raw.get("chime_enabled", True)),
        quiet_hours=str(raw.get("quiet_hours") or ""),
        rechime_seconds=int(raw.get("rechime_seconds", 300)),
        chime_tones={str(k): str(v) for k, v
                     in (raw.get("chime_tones") or {}).items()},
        webhook_url=raw.get("webhook_url"),
        nats_alerts_url=raw.get("nats_alerts_url"),
        nats_alerts_token=raw.get("nats_alerts_token"),
        nats_alerts_subject_prefix=subject_prefix,
        contract_port=(
            int(raw["contract_port"]) if raw.get("contract_port") is not None else None
        ),
        contract_bind_host=raw.get("contract_bind_host"),
        contract_host=raw.get("contract_host"),
        opennvr_url=raw.get("opennvr_url"),
        opennvr_token=raw.get("opennvr_token"),
    )


# ── KAI-C recognition client ───────────────────────────────────────


class KaicRecognitionClient(KaiCClient):
    """The SDK's KAI-C client behind this app's ``recognize`` spelling:
    ``task="face_recognition"`` with the match ``threshold`` as a
    contract-v1 param the adapter reads."""

    def __init__(
        self,
        kaic_url: str,
        api_key: str,
        adapter_name: str,
        timeout_seconds: float,
    ) -> None:
        super().__init__(kaic_url, adapter_name, api_key=api_key,
                         timeout_seconds=timeout_seconds)

    def recognize(
        self,
        frame_jpeg: bytes,
        *,
        threshold: float,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        return self.infer(frame_jpeg, task="face_recognition",
                          params={"threshold": threshold},
                          correlation_id=correlation_id)


# ── The orchestrator ───────────────────────────────────────────────


_THUMB_MAX_PX = 192
# Without Pillow (optional) the raw JPEG is embedded as-is, but only
# when it is small enough that eight of them in /state stay cheap to
# poll every few seconds; anything bigger is skipped, never shrunk badly.
_THUMB_RAW_CAP_BYTES = 24 * 1024


def _thumbnail_data_uri(frame: bytes) -> str | None:
    """A small ``data:image/jpeg`` URI for the dashboard's stranger wall
    and the directory avatars. Pillow shrinks the frame to ~190 px when
    it is installed (the image ships it); otherwise the raw JPEG is used
    only if it is already small."""
    data = _shrink_jpeg(frame, None, _THUMB_MAX_PX, quality=70)
    if data is None:
        if len(frame) > _THUMB_RAW_CAP_BYTES:
            return None
        data = frame
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def _shrink_jpeg(frame: bytes, bbox: tuple[int, int, int, int] | None,
                 max_px: int, *, quality: int) -> bytes | None:
    """Decode once, optionally crop to the face (with half a face of
    margin each side: hair, chin and ears are what a person recognises
    in a review), bound the longer side to ``max_px``, re-encode. None
    without Pillow or on a frame that does not decode."""
    try:
        from PIL import Image  # optional dependency
    except ImportError:  # pragma: no cover — depends on the environment
        return None
    try:
        with Image.open(io.BytesIO(frame)) as im:
            im = im.convert("RGB")
            if bbox:
                x1, y1, x2, y2 = (int(v) for v in bbox)
                w, h = max(1, x2 - x1), max(1, y2 - y1)
                mx, my = int(w * 0.5), int(h * 0.5)
                im = im.crop((max(0, x1 - mx), max(0, y1 - my),
                              min(im.width, x2 + mx), min(im.height, y2 + my)))
            im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality, optimize=True)
            return buf.getvalue()
    except Exception:
        return None


_CROP_MAX_PX = 320


def _face_crop_jpeg(frame: bytes, bbox: tuple[int, int, int, int] | None) -> bytes | None:
    """The face with generous margin, at most 320 px, as JPEG bytes — what
    the People page shows behind a stranger thumbnail and what enrolment
    from the wall sends to the adapter. Without Pillow the raw frame is
    kept when small, else nothing (never a bad enrolment)."""
    data = _shrink_jpeg(frame, bbox, _CROP_MAX_PX, quality=82)
    if data is not None:
        return data
    return frame if len(frame) <= 6 * _THUMB_RAW_CAP_BYTES else None


def _parse_date(value: str | None) -> str:
    """Validate a YYYY-MM-DD string; blank stays blank."""
    v = (value or "").strip()
    if not v:
        return ""
    try:
        dt.date.fromisoformat(v)
    except ValueError:
        raise ValueError("'valid_until' must be YYYY-MM-DD") from None
    return v


def _pass_expired(valid_until: str | None, today: "dt.date | None" = None) -> bool:
    v = (valid_until or "").strip()
    if not v:
        return False
    try:
        return dt.date.fromisoformat(v) < (today or dt.date.today())
    except ValueError:
        return False


class _TrackedFrameSource(DictFrameSource):
    """The by-reference camera map, plus a per-camera health record the
    dashboard reads. The SDK loop isolates fetch failures per camera and
    logs them, which is right for the loop and invisible to an operator;
    this is the one place a dead doorbell camera becomes a red row."""

    def __init__(self, sources, health: dict[str, dict[str, Any]]) -> None:
        super().__init__(sources)
        self._health = health
        # A camera picked in the catalog has no YAML frame_url: its frames
        # come from core's snapshot route, which serves only this app's picks.
        self._core = CoreSnapshotSource()

    def get_frame(self, camera_id: str) -> bytes | None:
        rec = self._health.setdefault(camera_id, {
            "last_frame": None, "last_error": None, "error": None, "frames": 0,
        })
        try:
            if camera_id in self._sources:
                frame = super().get_frame(camera_id)
            else:
                frame = self._core.get_frame(camera_id)
        except Exception as exc:
            rec["last_error"] = time.time()
            rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
            raise
        if frame:
            rec["last_frame"] = time.time()
            rec["frames"] += 1
            rec["error"] = None
        else:
            rec["last_error"] = time.time()
            rec["error"] = "camera returned no frame"
        return frame


def _slug(name: str) -> str:
    """A stable person_id from a display name: lowercase, non-alnum runs
    collapsed to single hyphens. Re-enrolling the same name upserts the
    face (the adapter keys on person_id)."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "face"


class _LazyNvr:
    """What :class:`VisitLog` needs, resolved at CALL time.

    The log is built in the constructor and the platform client is not
    available until after registration, so handing it ``app.nvr`` there
    would capture ``None`` — or worse, force the client to be built
    early, before core has minted this app's key (see the ``nvr``
    property). This forwards each attribute when it is actually used.
    """

    __slots__ = ("_app",)

    def __init__(self, app: Any) -> None:
        self._app = app

    def __getattr__(self, name: str) -> Any:
        return getattr(self._app.nvr, name)


class SmartDoorbell(FrameApp):
    """Polls all configured cameras (via the SDK FrameApp loop), runs
    recognition, dispatches.

    Alerts are dispatched *inside* the rule rather than returned to the
    base, because the dedup ledger gates them — a suppressed repeat
    visitor must not reach the dispatcher at all.
    """

    manifest = MANIFEST

    # Sentinel object used to bucket unknown-person dedup keys. We use
    # an object() rather than a string so a hostile / unlikely
    # ``person_id`` value (e.g. someone enrols a face with id
    # ``"__unknown__"``) can never collide with the stranger bucket.
    # Mixed-type tuple keys are fine in dict.
    _UNKNOWN_BUCKET = object()

    def __init__(
        self,
        config: AppConfig,
        pipeline: FaceRecognitionPipeline,
        dispatcher: AlertDispatcher,
    ) -> None:
        self.config = config
        self.pipeline = pipeline
        self.dispatcher = dispatcher
        self._frame_sources: dict[str, FrameSource] = {}
        for cam in config.cameras:
            self._frame_sources[cam.camera_id] = build_frame_source(
                camera_id=cam.camera_id, url=cam.frame_url,
            )
        # Per-camera fetch health, filled by _TrackedFrameSource.
        self._camera_health: dict[str, dict[str, Any]] = {}

        super().__init__(
            config,
            dispatcher,
            # By-reference bridge: swapping an entry in
            # ``self._frame_sources`` (test stubs, camera reconfig) is
            # picked up on the next tick.
            frame_source=_TrackedFrameSource(self._frame_sources, self._camera_health),
            cameras=[cam.camera_id for cam in config.cameras],
            poll_interval_seconds=(
                config.poll_interval_seconds
                if config.poll_interval_seconds > 0
                else _MIN_POLL_INTERVAL_SECONDS
            ),
        )

    def setup(self) -> None:
        self._cameras_by_id: dict[str, CameraConfig] = {
            cam.camera_id: cam for cam in self.config.cameras
        }
        # Key is (camera_id, person_id_or_sentinel). When the face is
        # recognised we use the person_id (a str); when it isn't we
        # use the ``_UNKNOWN_BUCKET`` sentinel object so a hostile
        # person_id can't collide with the stranger bucket. A plain
        # dict on purpose (not the SDK keyed_state): dedup reads
        # "last actually-fired", never refreshes on suppression, and
        # its shape is pinned by this app's test suite.
        self._last_fired: dict[tuple[str, Any], float] = {}
        # Rolling feed of the most recent visitors — powers the "Recent
        # visitors" log on the app's dashboard. Kept lightweight (no
        # embedded snapshots) since /state is polled frequently.
        self._recent: deque[dict[str, Any]] = deque(maxlen=25)
        # Visits since start, by outcome — the two headline numbers.
        self._visits: dict[str, int] = {"known": 0, "unknown": 0}
        self._started_at = time.time()
        # Thumbnails of the latest strangers for the gallery view — small
        # (see _thumbnail_data_uri) because /state is polled every few s.
        self._stranger_gallery: deque[dict[str, Any]] = deque(maxlen=8)
        # Full face crops behind the wall, by stranger id — served through
        # the stranger_image action, never through /state.
        self._stranger_crops: dict[str, bytes] = {}
        self._stranger_seq = 0
        # person_id -> last recognition wall time, merged into list_faces.
        self._last_seen: dict[str, float] = {}
        # Enrolled-face count from the adapter, refreshed at most once a
        # minute so /state never turns into an adapter round-trip per poll.
        self._enrolled_cache: tuple[float, int | None] = (0.0, None)
        # person_id -> metadata from the same refresh, so an expiry date
        # is known at recognition time without a second adapter call.
        self._people_meta: dict[str, dict[str, Any]] = {}
        #: The platform client, built on FIRST USE — core mints this
        #: app's key during registration, which the base class does in
        #: start(), after this constructor has run. A client built here
        #: would resolve its credential before the app key exists and
        #: fall back to the deployment's site key, which core reads as a
        #: platform component: unscoped, every camera in the building.
        self._nvr: Any = None
        #: Stranger TILES, kept across restarts — a thumbnail, a camera
        #: and a time. Presentation state for the wall, carrying no name:
        #: identities live in the platform's store now (RFC-0003), where
        #: they are scoped, auditable, and visible to the operator's own
        #: pages rather than only to this app.
        self._tiles = VisitLog(
            _LazyNvr(self),
            max_entries=self.config.history_max,
            max_days=self.config.history_days,
        )
        self._history_restored = False
        self._visit_seq = 0
        #: How the identities this app wrote were bound — on /state, so
        #: "is it guessing?" is answerable without reading a log. A
        #: deployment where `nearest` climbs is one where the doorbell
        #: sees faces between visits rather than during them.
        self._claims: dict[str, int] = {
            "window": 0, "nearest": 0, "ambiguous": 0,
            "no visit": 0, "unreachable": 0, "failed": 0,
        }
        #: Does anything ring, and with what. Separate from the alert
        #: path on purpose: every visit alerts, only some interrupt
        #: somebody.
        self._chime = ChimePolicy(
            tones=self.config.chime_tones,
            quiet_hours=self.config.quiet_hours,
            rechime_seconds=self.config.rechime_seconds,
            enabled=self.config.chime_enabled,
        )
        #: Rings since start, and the last decision — both on /state so
        #: "why didn't it ring?" is answerable without reading a log.
        self._rings = 0
        self._last_chime: dict[str, Any] | None = None

    @property
    def nvr(self):
        """The client this app talks to core with, built on first use."""
        if self._nvr is None:
            from opennvr_app_sdk import OpenNVR

            self._nvr = OpenNVR()
        return self._nvr

    @nvr.setter
    def nvr(self, client) -> None:
        self._nvr = client

    def _recent_visits_from_core(self) -> list[dict[str, Any]]:
        """The feed, read back from the platform's store.

        One query, not one per visit. Names come from the ``face_id``
        claims this app wrote, so a visit nobody recognised shows as
        "Unknown visitor" because there IS no claim — not because a
        field was blank.

        Returns ``[]`` on any failure. That is a display list; an empty
        feed on a doorbell that cannot reach core is a cosmetic gap,
        and the alerting path above does not depend on it.
        """
        try:
            answer = self.nvr.timeline.find(
                "", label=["person"],
                camera=[c.camera_id for c in self.config.cameras] or None,
                limit=self._recent.maxlen or 25)
        except Exception:  # noqa: BLE001
            logger.debug("feed could not be restored", exc_info=True)
            return []
        if answer is None:
            logger.info("feed not restored: core unreachable")
            return []
        out: list[dict[str, Any]] = []
        for row in answer.get("results") or []:
            name = next((str(c.get("value")) for c in (row.get("claims") or [])
                         if c.get("kind") == "face_id" and c.get("value")), None)
            out.append({
                "message": (f"{name} recognised" if name
                            else "Unknown visitor")
                           + f" at {row.get('camera_name') or row.get('camera_id')}",
                "time": row.get("started_at"),
                "level": "low" if name else "high",
                "camera": row.get("camera_name") or row.get("camera_id"),
                "name": name,
            })
        return out

    def _restore_history(self) -> None:
        """Put yesterday's visitors back on the feed and the wall, once.

        The FEED now comes from the platform: recent person visits on
        this app's cameras, with the name read from the ``face_id``
        claim this app wrote. That is the whole point of RFC-0003 —
        the answer to "who came to my door on Tuesday" is the same
        whether the doorbell, the operator's timeline or another app
        asks it.

        The WALL comes from the local tiles, because a thumbnail is
        presentation state and the store holds evidence paths rather
        than 190-pixel images.

        Lazily rather than in the constructor: reading either needs the
        platform client, which needs the app key core mints during
        registration — and a doorbell must come up and start watching
        the door whether or not its history can be read.
        """
        if self._history_restored:
            return
        self._history_restored = True
        for item in reversed(self._recent_visits_from_core()):
            self._recent.appendleft(item)
        try:
            entries = self._tiles.entries
        except Exception as exc:  # noqa: BLE001
            logger.warning("stranger tiles could not be restored (%s)", exc)
            return
        if not entries:
            return
        for item in reversed(self._tiles.strangers(
                self._stranger_gallery.maxlen or 8)):
            self._stranger_gallery.appendleft({
                "id": item.get("id"),
                "image": item.get("thumb"),
                # Two different absences, and the wall must not call them
                # the same thing. No inline thumbnail but the platform
                # still holds the crop means the face is there to enrol
                # from, just not drawn — the log keeps a thumbnail only
                # for the newest few. No crop either means the picture
                # really is gone.
                "photo_kept": bool(item.get("evidence_path")),
                "aged_out": not (item.get("thumb") or item.get("evidence_path")),
                "label": item.get("camera"),
                "time": item.get("at"),
            })
        logger.info("restored %d visits from history", len(entries))

    def request_stop(self) -> None:
        """Historical name — the SDK base spells it ``stop()``."""
        self.stop()

    def step(self) -> None:
        """Single pass over every camera. Used by --once and tests."""
        self.handle_tick()

    # ── The rule (one camera × one fetched frame) ──────────────────

    def on_frame(
        self, camera_id: str, frame_bytes: bytes
    ) -> Iterable[Alert] | None:
        # A YAML camera, or — connected to OpenNVR — a camera picked for
        # this app in the catalog, which needs no YAML entry at all.
        cam = self._cameras_by_id.get(camera_id) or CameraConfig(
            camera_id=camera_id, frame_url="opennvr:core")
        correlation_id = uuid.uuid4().hex
        # First frame after a start: put yesterday's visitors back on the
        # feed and the wall before this one joins them.
        self._restore_history()

        read = self.pipeline.process_frame(frame_bytes, correlation_id=correlation_id)
        if read is None or not read.face_detected:
            # No face → nothing to alert. (We could fire a "movement,
            # no recognisable face" event but that's a different
            # example app — keep this one focused on the doorbell.)
            return None

        bucket = read.person_id or self._UNKNOWN_BUCKET
        plate_key = (cam.camera_id, bucket)
        now = time.monotonic()
        if self.config.dedup_window_seconds > 0:
            last = self._last_fired.get(plate_key)
            if last is not None and (now - last) < self.config.dedup_window_seconds:
                return None
            self._last_fired[plate_key] = now

        # Decided BEFORE the alert is built, because the decision rides
        # in the envelope: whatever actually rings — a relay into ntfy,
        # a Home Assistant automation, a smart speaker — reads it from
        # there, and reads the reason when nothing rang.
        chime = self._chime.decide(
            key=f"{cam.camera_id}|{read.person_id or '?'}",
            category=read.category, recognized=read.recognized, now=now,
        )
        self._last_chime = {**chime.as_dict(), "at": time.time(),
                            "camera": cam.camera_id}
        if chime.ring:
            self._rings += 1

        attach_snapshot = (
            self.config.attach_snapshot_for_unknowns and not read.recognized
        )
        snapshot_bytes: bytes | None = None
        if attach_snapshot:
            cap = max(0, int(self.config.snapshot_max_bytes))
            if cap == 0 or len(frame_bytes) <= cap:
                snapshot_bytes = frame_bytes
            else:
                logger.warning(
                    "camera=%s: snapshot %d bytes exceeds snapshot_max_bytes=%d; "
                    "dropping from alert envelope correlation_id=%s",
                    cam.camera_id, len(frame_bytes), cap, read.correlation_id,
                )
        alert = self._build_alert(cam, read, snapshot_bytes, chime)
        self.dispatcher.dispatch(alert)
        now_wall = time.time()
        display = read.name or read.person_id or "?"
        self._recent.append({
            "message": (f"{display} recognised" if read.recognized
                        else "Unknown visitor")
                       + f" at {cam.camera_id}",
            "time": now_wall,
            "level": "low" if read.recognized else "high",
            "camera": cam.camera_id,
            "name": display if read.recognized else None,
            "category": read.category if read.recognized else None,
            "similarity": read.similarity,
        })
        self._visits["known" if read.recognized else "unknown"] += 1
        if read.recognized and read.person_id:
            self._last_seen[read.person_id] = now_wall
        crop: bytes | None = None
        thumb: str | None = None
        sid: str | None = None
        if not read.recognized:
            # One full-frame decode: the 320 px crop, then the ~190 px wall
            # thumbnail from the crop — a face, not a whole porch, in a
            # tile 120 px wide.
            crop = _face_crop_jpeg(frame_bytes, read.face_bbox)
            thumb = _thumbnail_data_uri(crop if crop is not None else frame_bytes)
            if thumb is not None:
                self._stranger_seq += 1
                sid = f"s{self._stranger_seq}"
                if crop is not None:
                    self._stranger_crops[sid] = crop
                self._stranger_gallery.append({
                    "id": sid, "image": thumb, "label": cam.camera_id, "time": now_wall,
                })
                # Keep the crop store in step with the wall (deque drops the
                # oldest silently).
                live = {g["id"] for g in self._stranger_gallery}
                for old in [k for k in self._stranger_crops if k not in live]:
                    self._stranger_crops.pop(old, None)

        # Durable, and LAST: the alert has already gone out and the
        # dashboard is already right. A doorbell that failed to ring
        # because a key/value write timed out would be a worse doorbell
        # than one with a gap in its history.
        self._remember(cam, read, now_wall, visit_id=sid, thumb=thumb, crop=crop)
        # Wire the app-dispatched alert into the SDK contract counters
        # (/health's alerts_fired) — the base loop can't see it because
        # on_frame returns None.
        self._contract_note_alerts(1)
        return None

    def _remember(self, cam: Any, read: Any, at: float, *,
                  visit_id: str | None, thumb: str | None,
                  crop: bytes | None) -> None:
        """Record this visit. Never raises.

        The identity goes to the PLATFORM now, not to a log of our own.

        This app used to keep its whole visit history in the SDK's
        key/value store, and argued for it: a ``FrameApp`` has a frame
        and a face but no ``event_id``, so attaching a name to a
        platform visit meant guessing which visit by timestamp, and a
        guessed identity is indistinguishable afterwards from a
        measured one. The argument was right and the conclusion was
        wrong — indistinguishable is a property of the record, so
        RFC-0003 put the binding IN the record. ``visit_at`` says
        whether the instant fell inside a visit's own span (``window``)
        or merely near one (``nearest``), and refuses to choose when
        two visits could be meant.

        What stays local is the wall tile: a thumbnail, a camera and a
        time. That is presentation state, the same category as the
        ``_recent`` deque above it, and it carries no name.
        """
        if visit_id is None:
            self._visit_seq += 1
            visit_id = f"v{int(at)}-{self._visit_seq}"
        self._claim_identity(cam, read, at)
        try:
            self._tiles.record({
                "id": visit_id,
                "at": at,
                "camera": cam.camera_id,
                "recognized": bool(read.recognized),
                **({"thumb": thumb} if thumb and not read.recognized else {}),
            }, crop=crop if not read.recognized else None)
        except Exception:  # noqa: BLE001 — a tile is not worth a miss
            logger.debug("stranger tile not stored", exc_info=True)

    def _claim_identity(self, cam: Any, read: Any, at: float) -> None:
        """Attach the name to the platform's visit, saying how it bound.

        Only a RECOGNISED face produces a claim. "Somebody unknown was
        here" is already a visit in the store; writing ``face_id:
        unknown`` would turn the absence of an identity into an
        assertion about one, and every reader counting faces would
        count it.

        Best-effort, and last: an alert that already went out must not
        be undone by a write that timed out. But the failure is LOGGED
        at warning rather than swallowed, because "the name was not
        recorded" is not a quieter version of "there was no name".
        """
        if not (read.recognized and read.person_id):
            return
        name = (read.name or read.person_id or "").strip()
        if not name:
            return
        try:
            client = self.nvr
            bound = client.timeline.visit_at(
                cam.camera_id, dt.datetime.fromtimestamp(at, dt.timezone.utc),
                label="person", tolerance_s=self.config.bind_tolerance_seconds)
            if bound is None:
                logger.warning("face %s not recorded: core unreachable", name)
                self._claims["unreachable"] += 1
                return
            event_id = bound.get("event_id")
            if not event_id:
                # Ambiguous, or no visit at all. Both mean this frame
                # does not identify a visit, and a doorbell frame taken
                # while two people are at the door genuinely does not
                # say whose face it is.
                self._claims[bound.get("reason") or "unbound"] += 1
                return
            binding = bound.get("binding") or "nearest"
            client.timeline.add_claims(
                event_id,
                [{"kind": "face_id", "value": name,
                  "confidence": read.similarity,
                  "source_task": "face_recognition",
                  "source_adapter": self.config.recognition_adapter,
                  "correlation_id": read.correlation_id}],
                binding=binding)
            self._claims[binding] += 1
        except Exception:  # noqa: BLE001
            logger.warning("face %s not recorded", name, exc_info=True)
            self._claims["failed"] += 1

    def _enrolled_count(self) -> int | None:
        """How many faces the adapter holds, cached for a minute. None when
        the adapter cannot be reached — the dashboard says so rather than
        showing a zero that looks like an empty DB."""
        fetched_at, count = self._enrolled_cache
        if time.time() - fetched_at < 60.0:
            return count
        try:
            faces = self._face_admin(timeout_seconds=self._DIRECTORY_TIMEOUT_S).list_faces()
            rows = faces.get("faces", faces) if isinstance(faces, dict) else faces
            count = len(rows) if isinstance(rows, list) else None
            if isinstance(rows, list):
                self._people_meta = {
                    str(f.get("person_id") or f.get("id")): (
                        f.get("metadata") if isinstance(f.get("metadata"), dict) else {})
                    for f in rows if isinstance(f, dict)
                }
        except Exception as exc:
            logger.warning("enrolled-face count unavailable: %s", exc)
            count = None
        self._enrolled_cache = (time.time(), count)
        return count

    def _camera_rows(self) -> list[dict[str, Any]]:
        """One row per configured camera for the dashboard's table."""
        now = time.time()
        stall_after = max(30.0, 10 * float(self.config.poll_interval_seconds or 1.0))
        rows = []
        # The cameras actually being polled: the YAML list standalone, the
        # cameras picked in the catalog when connected.
        for camera_id in list(self._cameras):
            cam = SimpleNamespace(camera_id=camera_id)
            rec = self._camera_health.get(cam.camera_id) or {}
            last = rec.get("last_frame")
            age = int(now - last) if last else None
            if rec.get("error") and (last is None or rec.get("last_error", 0) > last):
                status = "error"
            elif last is None:
                status = "waiting"
            elif age is not None and age > stall_after:
                status = "stalled"
            else:
                status = "ok"
            rows.append({
                "camera_id": cam.camera_id,
                "status": status,
                "last_frame_age_s": age,
                "error": rec.get("error"),
            })
        return rows

    def state_snapshot(self) -> dict[str, Any]:
        """``GET /state`` — what the catalog's live views and the /ui
        dashboard render: counters, per-camera health, the stranger wall
        and the recent-visitor feed."""
        return {
            "cameras": list(self._cameras),
            "camera_health": self._camera_rows(),
            "deduped_visitors_tracked": len(self._last_fired),
            "enrolled_faces": self._enrolled_count(),
            "visits": dict(self._visits),
            # How the names this app wrote were attached (RFC-0003).
            # `nearest` climbing means it is seeing faces between
            # visits rather than during them, which is a tuning signal
            # long before it is a wrong name.
            "identity_binding": dict(self._claims),
            "since": self._started_at,
            "stranger_gallery": list(self._stranger_gallery),
            "recent": list(self._recent),
            "rings": self._rings,
            # The last decision, including a suppressed one. "Why didn't
            # it ring?" is the first question anyone asks a doorbell, and
            # it should not need a log file to answer.
            "last_chime": self._last_chime,
            "chime": {
                "enabled": self.config.chime_enabled,
                "quiet_hours": self.config.quiet_hours,
                "rechime_seconds": self.config.rechime_seconds,
                "tones": dict(self._chime.tones),
            },
        }

    def on_config_update(self, config: dict[str, Any]) -> None:
        """Live edits from the catalog's config form. Each knob is rebound
        as a whole value, so the run loop sees old-or-new, never half.
        Idempotent: the first delivery re-sends the boot config."""
        if "recognition_threshold" in config:
            thr = float(config["recognition_threshold"])
            if thr != self.config.recognition_threshold:
                self.pipeline.config = FaceRecognitionPipelineConfig(recognition_threshold=thr)
                self.config.recognition_threshold = thr
                logger.info("recognition_threshold updated live: %.2f", thr)
        if "dedup_window_seconds" in config:
            self.config.dedup_window_seconds = float(config["dedup_window_seconds"])
        if "attach_snapshot_for_unknowns" in config:
            self.config.attach_snapshot_for_unknowns = bool(config["attach_snapshot_for_unknowns"])
        if "snapshot_max_bytes" in config:
            self.config.snapshot_max_bytes = int(config["snapshot_max_bytes"])
        if any(k in config for k in
               ("chime_enabled", "quiet_hours", "rechime_seconds", "chime_tones")):
            self.config.chime_enabled = bool(
                config.get("chime_enabled", self.config.chime_enabled))
            self.config.quiet_hours = str(
                config.get("quiet_hours", self.config.quiet_hours) or "")
            self.config.rechime_seconds = int(
                config.get("rechime_seconds", self.config.rechime_seconds))
            if "chime_tones" in config:
                self.config.chime_tones = {
                    str(k): str(v) for k, v in (config["chime_tones"] or {}).items()}
            # Rebuilt whole rather than mutated field by field, so the
            # run loop never sees a policy with a new quiet window and
            # an old tone table. The last-rang clock is deliberately
            # NOT carried over: an operator who just changed the bell
            # is entitled to hear the next caller.
            self._chime = ChimePolicy(
                tones=self.config.chime_tones,
                quiet_hours=self.config.quiet_hours,
                rechime_seconds=self.config.rechime_seconds,
                enabled=self.config.chime_enabled,
            )
            logger.info("chime policy updated live (enabled=%s quiet=%r)",
                        self.config.chime_enabled, self.config.quiet_hours)

    def ui_html(self) -> str:
        """The app dashboard (RFC-0002 Phase 4): ONE static, self-contained
        HTML document, no scripts — core's catalog renders it sandboxed and
        refetches on an interval. Same shape as the ANPR dashboard."""
        snap = self.state_snapshot()
        now = time.time()
        esc = _html.escape

        def ago(ts: float | None) -> str:
            if not ts:
                return "—"
            m = max(0, int((now - ts) / 60))
            return "just now" if m == 0 else f"{m}m ago" if m < 60 else f"{m // 60}h ago"

        colour = {"ok": "#46a758", "waiting": "#8b8d98", "stalled": "#e5a000", "error": "#e5484d"}
        cam_rows = "".join(
            f"<tr><td>{esc(r['camera_id'])}</td>"
            f"<td style='color:{colour.get(r['status'], '#8b8d98')};font-weight:600'>{esc(r['status'])}</td>"
            f"<td>{'—' if r['last_frame_age_s'] is None else str(r['last_frame_age_s']) + 's'}</td>"
            f"<td class='dim'>{esc(r['error'] or '')}</td></tr>"
            for r in snap["camera_health"]
        ) or "<tr><td colspan='4' class='dim'>No cameras configured.</td></tr>"

        def _tile(item: dict[str, Any]) -> str:
            # A visit whose picture the history no longer keeps still
            # happened, and saying so beats a broken image: the caption
            # is the answer to "who came on Tuesday", the photo was only
            # ever the nicer half of it.
            if item.get("image"):
                face = f"<img src='{item['image']}' alt='stranger'>"
            elif item.get("photo_kept"):
                # The crop is in the platform's evidence store; the log
                # just does not carry a thumbnail this far back. Saying
                # "aged out" here would tell an operator the face is
                # gone when they can still enrol from it.
                face = "<div class='gone'>photo kept — open to enrol</div>"
            else:
                face = "<div class='gone'>snapshot aged out</div>"
            return (f"<figure>{face}<figcaption>{esc(str(item.get('label') or '?'))}"
                    f" · {ago(item.get('time'))}</figcaption></figure>")

        wall = "".join(_tile(item) for item in reversed(snap["stranger_gallery"]))
        wall_block = (f"<div class='wall'>{wall}</div>" if wall
                      else "<p class='dim'>No strangers seen yet.</p>")

        visit_rows = []
        for item in reversed(snap["recent"][-12:]):
            known = item.get("level") == "low"
            who = esc(item.get("name") or "Unknown visitor")
            cat = esc(item.get("category") or "")
            sim = item.get("similarity")
            visit_rows.append(
                f"<tr><td style='color:{'#46a758' if known else '#e5484d'};font-weight:600'>{who}</td>"
                f"<td>{cat}</td><td>{esc(str(item.get('camera', '')))}</td>"
                f"<td>{'' if sim is None else f'{float(sim):.2f}'}</td><td>{ago(item.get('time'))}</td></tr>"
            )
        visits_table = (
            "<table><tr><th>Visitor</th><th>Category</th><th>Camera</th><th>Match</th><th>When</th></tr>"
            + "".join(visit_rows) + "</table>" if visit_rows
            else "<p class='dim'>No visitors yet.</p>"
        )
        enrolled = snap["enrolled_faces"]
        enrolled_txt = "?" if enrolled is None else str(enrolled)
        enrolled_note = (" <span class='warn'>(face adapter unreachable)</span>"
                         if enrolled is None else "")
        # Why the last caller did or did not ring, in the operator's own
        # words rather than in a log they would have to go and find.
        last_chime = snap.get("last_chime") or {}
        if not snap["chime"]["enabled"]:
            chime_note = " <span class='warn'>(chime off)</span>"
        elif last_chime.get("ring"):
            chime_note = (f" <span class='dim'>(last: {esc(str(last_chime.get('tone')))}"
                          f")</span>")
        elif last_chime.get("reason"):
            chime_note = (f" <span class='dim'>(silent: "
                          f"{esc(str(last_chime['reason']))})</span>")
        else:
            chime_note = ""
        return f"""<title>Smart Doorbell</title>
<style>
 body {{ font: 14px system-ui, sans-serif; margin: 1.2rem; color: #1a1a1a;
        background: #fafafa; }}
 h1 {{ font-size: 1.1rem; margin: 0 0 .2rem }}
 h2 {{ font-size: .95rem; margin: 1.1rem 0 .4rem; color: #3a3d44 }}
 .dim {{ color: #6b6f76 }}
 .warn {{ color: #e5a000 }}
 .stats {{ display: flex; gap: 1.5rem; margin: .8rem 0; flex-wrap: wrap }}
 .stats b {{ font-size: 1.3rem; display: block }}
 table {{ border-collapse: collapse; width: 100% }}
 th, td {{ text-align: left; padding: .3rem .6rem;
          border-bottom: 1px solid #e0e0e0; font-size: .9rem }}
 th {{ color: #6b6f76; font-weight: 500 }}
 .wall {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: .6rem }}
 figure {{ margin: 0; border: 1px solid #e0e0e0; border-radius: 6px; overflow: hidden; background: #fff }}
 figure img {{ width: 100%; height: 96px; object-fit: cover; display: block }}
 .gone {{ height: 96px; display: flex; align-items: center; justify-content: center;
          background: #f2f3f5; color: #6b6f76; font-size: .72rem; text-align: center }}
 figcaption {{ font-size: .78rem; padding: .25rem .4rem; color: #3a3d44 }}
 .note {{ margin-top: 1rem; font-size: .85rem; color: #6b6f76 }}
</style>
<h1>Smart Doorbell</h1>
<div class="dim">Since {ago(snap["since"])} · threshold {self.config.recognition_threshold:.2f}
 · re-fire window {int(self.config.dedup_window_seconds)}s</div>
<div class="stats">
 <div><b>{enrolled_txt}</b><span class="dim">enrolled faces</span>{enrolled_note}</div>
 <div><b>{snap["visits"]["known"]}</b><span class="dim">known visitors</span></div>
 <div><b>{snap["visits"]["unknown"]}</b><span class="dim">strangers</span></div>
 <div><b>{snap["deduped_visitors_tracked"]}</b><span class="dim">visitors tracked</span></div>
 <div><b>{snap["rings"]}</b><span class="dim">rings</span>{chime_note}</div>
</div>
<h2>Cameras</h2>
<table><tr><th>Camera</th><th>Status</th><th>Last frame</th><th>Error</th></tr>{cam_rows}</table>
<h2>Latest strangers</h2>
{wall_block}
<h2>Recent visitors</h2>
{visits_table}
<div class="note">Enrol, list and remove faces with the Quick actions on this
page. Threshold and re-fire window are edited in the config form and apply live.</div>
"""

    # ── Operator actions (App Catalog face-enrollment UI) ──────────────

    def _face_admin(self, *, timeout_seconds: float | None = None) -> "_FaceAdminClient":
        return _FaceAdminClient(
            self.config.adapter_url,
            self.config.adapter_token,
            self.config.request_timeout_seconds if timeout_seconds is None else timeout_seconds,
        )

    # The directory refresh runs on the contract thread (/state) and, when
    # its minute is up, on the run loop at recognition time. Neither may
    # wait out the 30 s enrolment budget on a silent adapter: a dead
    # adapter would then freeze /state for the catalog and stall the
    # camera loop once a minute.
    _DIRECTORY_TIMEOUT_S = 3.0

    def _crop_for(self, stranger_id: str) -> bytes | None:
        """This stranger's full-size face crop, from memory or history.

        A tile the wall restored after a restart has no crop in this
        process — it was uploaded by the process that saw the face. The
        platform kept it, so fetch it back rather than refusing: "the
        doorbell was restarted" is not a reason an operator should have
        to care about when they click Enrol on a face from Tuesday.

        What is deliberately NOT done is enrolling from the wall
        thumbnail. That is a ~190px image kept for drawing the tile, and
        feeding it to the adapter would teach it a worse face than the
        operator believes they handed over — a silently poor enrolment
        is worse than an honest "no longer available", which is what a
        None here still produces.
        """
        sid = (stranger_id or "").strip()
        if not sid:
            return None
        crop = self._stranger_crops.get(sid)
        if crop is not None:
            return crop
        try:
            crop = self._tiles.crop(sid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("crop for %s unreadable (%s)", sid, exc)
            return None
        if crop:
            # Hold it for the rest of this process: enrolling usually
            # means looking at the face first, so the operator has
            # already fetched it once through stranger_image.
            self._stranger_crops[sid] = crop
        return crop

    def on_action(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """enroll_face / list_faces / delete_face — the catalog's
        face-DB management, previously CLI-only. Runs on the contract
        server's thread; talks to the InsightFace adapter's /faces/*
        routes via the same client the CLI uses. ValueError → 400 in
        the SDK dispatcher, KeyError → 404, adapter errors → 500."""
        if name == "list_faces":
            faces = self._face_admin().list_faces()
            rows = faces.get("faces", faces) if isinstance(faces, dict) else faces
            out = []
            for f in rows if isinstance(rows, list) else []:
                pid = f.get("person_id") or f.get("id")
                meta = f.get("metadata") if isinstance(f.get("metadata"), dict) else {}
                out.append({
                    "person_id": pid,
                    "name": f.get("name"),
                    "category": f.get("category"),
                    "notes": meta.get("notes", ""),
                    "valid_until": meta.get("valid_until", ""),
                    "expired": _pass_expired(meta.get("valid_until")),
                    "thumbnail": meta.get("thumbnail"),
                    "samples": f.get("samples", 1),
                    "registered_at": f.get("registered_at"),
                    "last_seen": self._last_seen.get(pid) if pid else None,
                })
            return {"results": out, "categories": list(PERSON_CATEGORIES)}

        if name == "stranger_image":
            sid = str(params.get("stranger_id") or "").strip()
            crop = self._crop_for(sid)
            if crop is None:
                raise KeyError(sid or "stranger_id")
            return {"stranger_id": sid, "image": base64.b64encode(crop).decode("ascii"),
                    "mime": "image/jpeg"}

        if name == "enroll_stranger":
            sid = str(params.get("stranger_id") or "").strip()
            crop = self._crop_for(sid)
            if crop is None:
                raise KeyError(sid or "stranger_id")
            # A capture assigned to someone already enrolled is an added
            # sample; the wall tile then disappears (it is no longer a stranger).
            add_to = bool(str(params.get("person_id") or "").strip()) and not str(params.get("name") or "").strip()
            out = self._enroll(image_bytes=crop, params=params, append=add_to or bool(params.get("append")))
            self._stranger_crops.pop(sid, None)
            self._stranger_gallery = deque((g for g in self._stranger_gallery if g.get("id") != sid),
                                           maxlen=self._stranger_gallery.maxlen)
            # And out of the durable history, or the tile reappears on
            # the next restart for somebody who is now enrolled.
            try:
                self._tiles.forget(sid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("enrolled stranger %s not removed from "
                               "history (%s)", sid, exc)
            return out

        if name == "update_face":
            person_id = str(params.get("person_id") or "").strip()
            if not person_id:
                raise ValueError("'person_id' is required")
            changes: dict[str, Any] = {}
            if str(params.get("name") or "").strip():
                changes["name"] = str(params["name"]).strip()
            cat = str(params.get("category") or "").strip().lower()
            if cat:
                if cat not in PERSON_CATEGORIES:
                    raise ValueError(f"'category' must be one of {', '.join(PERSON_CATEGORIES)}")
                changes["category"] = cat
            meta: dict[str, Any] = {}
            if "notes" in params and params.get("notes") is not None:
                meta["notes"] = str(params.get("notes") or "").strip()
            if "valid_until" in params and params.get("valid_until") is not None:
                meta["valid_until"] = _parse_date(str(params.get("valid_until") or ""))
            if meta:
                changes["metadata"] = meta
            if not changes:
                raise ValueError("nothing to change")
            result = self._face_admin().update(person_id, **changes)
            return {"updated": person_id, "adapter": result}

        if name == "delete_face":
            person_id = str(params.get("person_id") or "").strip()
            if not person_id:
                raise ValueError("'person_id' is required")
            self._face_admin().delete_face(person_id)
            return {"deleted": person_id}

        if name == "enroll_face":
            if not params.get("append") and not str(params.get("name") or "").strip():
                raise ValueError("'name' is required")
            image_b64 = str(params.get("image") or "").strip()
            if not image_b64:
                raise ValueError("'image' is required (a base64 JPEG/PNG)")
            # The UI sends raw base64 (data: prefix stripped client-side);
            # be tolerant and strip it here too.
            if "," in image_b64 and image_b64.lstrip().startswith("data:"):
                image_b64 = image_b64.split(",", 1)[1]
            try:
                image_bytes = base64.b64decode(image_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"'image' is not valid base64: {exc}") from None
            if not image_bytes:
                raise ValueError("'image' decoded to empty bytes")
            return self._enroll(image_bytes=image_bytes, params=params)

        raise KeyError(name)

    def _enroll(self, *, image_bytes: bytes, params: dict[str, Any],
                append: bool | None = None) -> dict[str, Any]:
        """Shared by enroll_face (a photo the operator supplied) and
        enroll_stranger (a crop the door camera took).

        Two shapes. A NEW person needs a name; the id derives from it
        unless pinned. ADDING to a person (``append``, or a stranger
        capture with a ``person_id``) needs only the id: the sample joins
        their set and the adapter matches against the best of them —
        which is how the porch camera's own angle and light get learnt,
        one "this is Alice" at a time. Name / category / notes / expiry
        are then optional and only change what is given."""
        display = str(params.get("name") or "").strip()
        pinned = str(params.get("person_id") or "").strip()
        if append is None:
            append = bool(params.get("append"))
        if append and not pinned:
            raise ValueError("'person_id' is required when adding a photo to an existing person")
        if not append and not display:
            raise ValueError("'name' is required")
        category = str(params.get("category") or "").strip().lower()
        if not append and not category:
            category = "family"
        if category and category not in PERSON_CATEGORIES:
            raise ValueError(f"'category' must be one of {', '.join(PERSON_CATEGORIES)}")
        person_id = pinned or _slug(display)
        metadata: dict[str, Any]
        if append:
            # Adding a sample never wipes what was written about the person:
            # blank notes / expiry mean "unchanged", and the avatar stays.
            metadata = {}
            if str(params.get("notes") or "").strip():
                metadata["notes"] = str(params["notes"]).strip()
            if str(params.get("valid_until") or "").strip():
                metadata["valid_until"] = _parse_date(str(params["valid_until"]))
        else:
            metadata = {
                "notes": str(params.get("notes") or "").strip(),
                "valid_until": _parse_date(str(params.get("valid_until") or "")),
                "enrolled_at": time.time(),
            }
            thumb = _thumbnail_data_uri(image_bytes)
            if thumb is not None:
                metadata["thumbnail"] = thumb
        result = self._face_admin().register(
            image_bytes=image_bytes,
            person_id=person_id,
            name=display,
            category=category,
            metadata=metadata,
            append=append,
        )
        face = result.get("face") if isinstance(result, dict) else None
        return {
            "enrolled": {
                "person_id": person_id,
                "name": display or (face or {}).get("name"),
                "category": category or (face or {}).get("category"),
                "notes": metadata.get("notes", ((face or {}).get("metadata") or {}).get("notes", "")),
                "valid_until": metadata.get("valid_until", ((face or {}).get("metadata") or {}).get("valid_until", "")),
                "samples": (face or {}).get("samples"),
                "appended": append,
            },
            "adapter": result,
        }

    def _build_alert(
        self,
        cam: CameraConfig,
        read: FaceRead,
        snapshot: bytes | None,
        chime: Any = None,
    ) -> Alert:
        kind = "unknown_visitor"
        if read.recognized:
            category = (read.category or "").lower()
            display = read.name or read.person_id or "?"
            # The adapter's match reply carries no metadata; the directory
            # refresh that feeds the enrolled count keeps a copy per person.
            meta = read.raw.get("metadata") if isinstance(read.raw, dict) else None
            if not isinstance(meta, dict) and read.person_id:
                self._enrolled_count()
                meta = self._people_meta.get(read.person_id)
            valid_until = (meta or {}).get("valid_until") if isinstance(meta, dict) else None
            if category == "watchlist":
                kind, severity = "watchlist_visitor", "high"
                title = f"Watchlist match at {cam.camera_id}: {display}"
                description = (
                    f"{display!r} is on the watchlist (similarity "
                    f"{read.similarity:.2f}) on {cam.camera_id}."
                )
            elif _pass_expired(valid_until):
                kind, severity = "expired_pass", "high"
                title = f"Expired pass at {cam.camera_id}: {display}"
                description = (
                    f"{display!r} ({category}) was recognised on {cam.camera_id} but their "
                    f"pass expired on {valid_until}."
                )
            else:
                kind = "known_visitor"
                severity = _CATEGORY_SEVERITY.get(category, "info")
                title = f"Known visitor at {cam.camera_id}: {display}"
                description = (
                    f"Recognised {display!r} (similarity "
                    f"{read.similarity:.2f}) on {cam.camera_id}."
                )
        else:
            severity = "high"
            title = f"Unknown visitor at {cam.camera_id}"
            description = (
                f"Unrecognised face on {cam.camera_id}. "
                "Check the snapshot below."
            )

        evidence: dict[str, Any] = {
            "kind": kind,
            "recognized": read.recognized,
            "person_id": read.person_id,
            "name": read.name,
            "category": read.category,
            "similarity": read.similarity,
            "face_bbox": list(read.face_bbox) if read.face_bbox else None,
            "threshold": read.threshold,
        }
        if snapshot is not None:
            evidence["snapshot_b64"] = base64.b64encode(snapshot).decode("ascii")
            evidence["snapshot_mime"] = "image/jpeg"

        # What to ring, or why nothing did. In the envelope rather than
        # acted on here: an app that owned the speaker would work on
        # exactly one deployment, and a suppressed ring with no stated
        # reason is indistinguishable from a broken doorbell.
        tags: list[str] = []
        if chime is not None:
            evidence["chime"] = chime.as_dict()
            tags.append(f"chime:{chime.tone}" if chime.ring else "chime:silent")

        return Alert(
            severity=severity,
            title=title,
            description=description,
            camera_id=cam.camera_id,
            source=AlertSource(),
            correlation_id=read.correlation_id,
            evidence=evidence,
            tags=tags,
        )


# ── enroll / list-faces / get-face / delete-face subcommands ─────


class _FaceAdminClient:
    """Direct HTTP client for the adapter's /faces/* CRUD routes.
    KAI-C does NOT proxy these (they're not part of the contract);
    the enroll flow talks to the adapter directly."""

    def __init__(self, base_url: str, token: str, timeout_seconds: float) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = timeout_seconds

    def register(
        self,
        *,
        image_bytes: bytes,
        person_id: str,
        name: str,
        category: str,
        metadata: dict[str, Any] | None = None,
        append: bool = False,
    ) -> dict[str, Any]:
        files = {"frame": ("face.jpg", image_bytes, "image/jpeg")}
        data = {
            "person_id": person_id,
            "name": name,
            "category": category,
            "metadata": json.dumps(metadata or {}),
        }
        if append:
            data["append"] = "true"
        resp = httpx.post(
            f"{self._base}/faces/register",
            files=files, data=data,
            headers=self._headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def list_faces(self, category: str | None = None) -> dict[str, Any]:
        params = {"category": category} if category else {}
        resp = httpx.get(
            f"{self._base}/faces",
            params=params,
            headers=self._headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_face(self, person_id: str) -> dict[str, Any]:
        resp = httpx.get(
            f"{self._base}/faces/{person_id}",
            headers=self._headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def update(self, person_id: str, **changes: Any) -> dict[str, Any]:
        """PATCH /faces/{id} — name / category / metadata without a new
        photo. Adapters older than the route answer 404 or 405; say so
        rather than letting the operator think the edit failed for a
        reason of their own."""
        resp = httpx.patch(
            f"{self._base}/faces/{person_id}",
            json=changes,
            headers=self._headers,
            timeout=self._timeout,
        )
        if resp.status_code in (404, 405):
            detail = ""
            try:
                detail = str(resp.json().get("detail", ""))
            except Exception:
                pass
            if resp.status_code == 405 or "Not Found" in detail or not detail:
                raise ValueError(
                    "the InsightFace adapter does not support editing yet — "
                    "update the adapter image (ghcr.io/open-nvr/insightface-adapter) "
                    "or re-enrol with a photo"
                )
            raise KeyError(person_id)
        resp.raise_for_status()
        return resp.json()

    def delete_face(self, person_id: str) -> dict[str, Any]:
        resp = httpx.delete(
            f"{self._base}/faces/{person_id}",
            headers=self._headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()


# Soft cap so we fail client-side before shipping a 50 MB photo over
# the network only to get a 413 back. Matches the adapter-side
# ``MAX_IMAGE_BYTES`` cap.
_ENROLL_MAX_IMAGE_BYTES: int = 8 * 1024 * 1024


def _print_http_error(action: str, exc: httpx.HTTPStatusError) -> None:
    """Translate a 4xx/5xx response into a one-line operator-friendly
    error. Avoids dumping a full httpx traceback for predictable
    failure modes (no face detected, bad token, file too large)."""
    try:
        detail = exc.response.json().get("detail", "")
    except Exception:
        detail = exc.response.text[:200]
    print(
        f"{action} failed (HTTP {exc.response.status_code}): {detail}",
        file=sys.stderr,
    )


def _cmd_enroll(config: AppConfig, args: argparse.Namespace) -> int:
    image_path = Path(args.image).expanduser()
    if not image_path.is_file():
        print(f"image not found: {image_path}", file=sys.stderr)
        return 2
    size = image_path.stat().st_size
    if size > _ENROLL_MAX_IMAGE_BYTES:
        print(
            f"image {image_path} is {size / 1_000_000:.1f} MB — over the "
            f"{_ENROLL_MAX_IMAGE_BYTES / 1_000_000:.0f} MB upload limit. "
            "Resize / re-encode before enrolling.",
            file=sys.stderr,
        )
        return 2
    image_bytes = image_path.read_bytes()
    client = _FaceAdminClient(
        config.adapter_url, config.adapter_token, config.request_timeout_seconds,
    )
    try:
        out = client.register(
            image_bytes=image_bytes,
            person_id=args.person_id,
            name=args.name,
            category=args.category,
        )
    except httpx.HTTPStatusError as exc:
        _print_http_error("enroll", exc)
        return 1
    print(json.dumps(out, indent=2))
    return 0


def _cmd_list_faces(config: AppConfig, args: argparse.Namespace) -> int:
    client = _FaceAdminClient(
        config.adapter_url, config.adapter_token, config.request_timeout_seconds,
    )
    try:
        out = client.list_faces(category=args.category)
    except httpx.HTTPStatusError as exc:
        _print_http_error("list-faces", exc)
        return 1
    print(json.dumps(out, indent=2))
    return 0


def _cmd_delete_face(config: AppConfig, args: argparse.Namespace) -> int:
    client = _FaceAdminClient(
        config.adapter_url, config.adapter_token, config.request_timeout_seconds,
    )
    try:
        out = client.delete_face(args.person_id)
    except httpx.HTTPStatusError as exc:
        _print_http_error("delete-face", exc)
        return 1
    print(json.dumps(out, indent=2))
    return 0


# ── daemon ─────────────────────────────────────────────────────────


def _cmd_daemon(config: AppConfig, args: argparse.Namespace) -> int:
    pipeline = FaceRecognitionPipeline(
        client=KaicRecognitionClient(
            config.kaic_url, config.kaic_api_key,
            config.recognition_adapter, config.request_timeout_seconds,
        ),
        config=FaceRecognitionPipelineConfig(
            recognition_threshold=config.recognition_threshold,
        ),
    )
    dispatcher = build_dispatcher(
        webhook_url=config.webhook_url,
        webhook_timeout_seconds=config.request_timeout_seconds,
        nats_alerts_url=config.nats_alerts_url,
        nats_alerts_token=config.nats_alerts_token,
        nats_alerts_subject_prefix=config.nats_alerts_subject_prefix,
    )

    doorbell = SmartDoorbell(config, pipeline, dispatcher)

    if args.once:
        try:
            doorbell.step()
        finally:
            dispatcher.close()
        return 0

    if not config.cameras and not config.opennvr_url:
        # Connected to OpenNVR the cameras are PICKED in the App Catalog,
        # so an empty YAML list is normal. Standalone there is nowhere
        # else to get them from.
        raise SystemExit(
            "config: at least one camera is required for the daemon "
            "(or set opennvr_url and select cameras in the App Catalog)"
        )

    # The SDK FrameApp loop is async; drive it the same way the SDK
    # AppRunner drives a Detector. SIGINT / SIGTERM trigger a clean exit.
    loop = asyncio.new_event_loop()

    def _handle_signal(signum, _frame):
        logger.info("received signal %s; stopping", signum)
        loop.call_soon_threadsafe(doorbell.stop)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        loop.run_until_complete(doorbell.run())
    finally:
        try:
            dispatcher.close()
        except Exception:
            logger.exception("dispatcher.close() failed")
        loop.close()
    return 0


# ── CLI ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="OpenNVR smart-doorbell example",
    )
    parser.add_argument("--config", required=True, help="path to config.yml")
    parser.add_argument(
        "--log-level", default="INFO",
        help="DEBUG / INFO / WARNING / ERROR (default: INFO)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # daemon
    p_daemon = sub.add_parser("daemon", help="poll cameras, recognise faces, fire alerts")
    p_daemon.add_argument(
        "--once", action="store_true",
        help="run one pass over every camera then exit",
    )
    p_daemon.set_defaults(func=_cmd_daemon)

    # enroll
    p_enroll = sub.add_parser("enroll", help="register a known face")
    p_enroll.add_argument("--person-id", required=True)
    p_enroll.add_argument("--name", required=True)
    p_enroll.add_argument("--image", required=True, help="path to a JPEG/PNG face crop")
    p_enroll.add_argument("--category", default="family")
    p_enroll.set_defaults(func=_cmd_enroll)

    # list-faces
    p_list = sub.add_parser("list-faces", help="list registered faces")
    p_list.add_argument("--category", default=None)
    p_list.set_defaults(func=_cmd_list_faces)

    # delete-face
    p_del = sub.add_parser("delete-face", help="delete a registered face")
    p_del.add_argument("--person-id", required=True)
    p_del.set_defaults(func=_cmd_delete_face)

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    return args.func(config, args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
