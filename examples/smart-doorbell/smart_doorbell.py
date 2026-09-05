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
import json
import logging
import re
import signal
import sys
import time
import uuid
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
from frame_sources import FrameSource, FrameSourceError, build_frame_source
from opennvr_app_sdk import (
    Action, AlertType, AppManifest, FrameApp, KaiCClient, Param, StateView,
)
from opennvr_app_sdk.frame_sources import DictFrameSource

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
        "Recognises faces at the door via InsightFace through KAI-C; "
        "known visitors ride low-severity, strangers high with a snapshot."
    ),
    requires_tasks=["face_recognition"],  # checked vs GET /api/v1/adapters
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
    ],
    emits=[
        AlertType("known_visitor", severity="low"),
        AlertType("unknown_visitor", severity="high",
                  description="Unrecognised face; carries a snapshot when enabled."),
    ],
    state_schema=[
        StateView(name="deduped", label="Visitors tracked", kind="metric",
                  path="deduped_visitors_tracked"),
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
                      description="A clear, front-facing photo of the person."),
                Param("category", str, default="known",
                      description="known / family / staff / … (drives alert severity)."),
            ],
            description="Register a known face so the doorbell greets them "
                        "instead of flagging a stranger.",
        ),
        Action(
            "list_faces", "Enrolled faces", params=[],
            description="Show everyone currently enrolled.",
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


def _slug(name: str) -> str:
    """A stable person_id from a display name: lowercase, non-alnum runs
    collapsed to single hyphens. Re-enrolling the same name upserts the
    face (the adapter keys on person_id)."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "face"


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

        super().__init__(
            config,
            dispatcher,
            # By-reference bridge: swapping an entry in
            # ``self._frame_sources`` (test stubs, camera reconfig) is
            # picked up on the next tick.
            frame_source=DictFrameSource(self._frame_sources),
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
        cam = self._cameras_by_id[camera_id]
        correlation_id = uuid.uuid4().hex

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
        alert = self._build_alert(cam, read, snapshot_bytes)
        self.dispatcher.dispatch(alert)
        self._recent.append({
            "message": (f"{read.person_id} recognised" if read.recognized
                        else "Unknown visitor")
                       + f" at {cam.camera_id}",
            "time": time.time(),
            "level": "low" if read.recognized else "high",
        })
        # Wire the app-dispatched alert into the SDK contract counters
        # (/health's alerts_fired) — the base loop can't see it because
        # on_frame returns None.
        self._contract_note_alerts(1)
        return None

    def state_snapshot(self) -> dict[str, Any]:
        """``GET /state`` — the dedup ledger size per camera."""
        return {
            "cameras": [cam.camera_id for cam in self.config.cameras],
            "deduped_visitors_tracked": len(self._last_fired),
            "recent": list(self._recent),
        }

    # ── Operator actions (App Catalog face-enrollment UI) ──────────────

    def _face_admin(self) -> "_FaceAdminClient":
        return _FaceAdminClient(
            self.config.adapter_url,
            self.config.adapter_token,
            self.config.request_timeout_seconds,
        )

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
                out.append({
                    "person_id": f.get("person_id") or f.get("id"),
                    "name": f.get("name"),
                    "category": f.get("category"),
                })
            return {"results": out}

        if name == "delete_face":
            person_id = str(params.get("person_id") or "").strip()
            if not person_id:
                raise ValueError("'person_id' is required")
            self._face_admin().delete_face(person_id)
            return {"deleted": person_id}

        if name == "enroll_face":
            display = str(params.get("name") or "").strip()
            if not display:
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
            category = str(params.get("category") or "known").strip() or "known"
            # Deterministic id from the name (the CLI does the same); the
            # adapter upserts, so re-enrolling a name refreshes the face.
            person_id = _slug(display)
            result = self._face_admin().register(
                image_bytes=image_bytes,
                person_id=person_id,
                name=display,
                category=category,
            )
            return {
                "enrolled": {"person_id": person_id, "name": display, "category": category},
                "adapter": result,
            }

        raise KeyError(name)

    def _build_alert(
        self,
        cam: CameraConfig,
        read: FaceRead,
        snapshot: bytes | None,
    ) -> Alert:
        if read.recognized:
            severity = "low" if (read.category or "").lower() == "family" else "info"
            display = read.name or read.person_id or "?"
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

        return Alert(
            severity=severity,
            title=title,
            description=description,
            camera_id=cam.camera_id,
            source=AlertSource(),
            correlation_id=read.correlation_id,
            evidence=evidence,
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
    ) -> dict[str, Any]:
        files = {"frame": ("face.jpg", image_bytes, "image/jpeg")}
        data = {
            "person_id": person_id,
            "name": name,
            "category": category,
            "metadata": json.dumps(metadata or {}),
        }
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

    if not config.cameras:
        raise SystemExit(
            "config: at least one camera is required for the daemon"
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
