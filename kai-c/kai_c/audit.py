# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
KAI-C audit event store.

§11.2 of the contract design defines the audit-trail contract: every
registration, every inference, every refusal, every drift event is
recorded with a stable correlation_id so an operator can pull the
causal chain for any incident.

This module is the v1 implementation: an append-only JSON-Lines file.
Each line is a single audit event. The file is queryable via tail-read
plus simple in-process filtering — fine up to ~1M events, after which
we'll want a real datastore. The shape is forward-compatible with the
"SIEM forwarding" hook in §11.2 (syslog / webhook / Splunk HEC) —
those land in a follow-up commit; this module just exposes the events.

What we do NOT implement here:
* Hash-chained integrity (§11.2 v1.5 roadmap).
* SIEM export sinks (the design doc names syslog/webhook/file/splunk_hec/
  datadog — file is implicit here; the rest follow-up).
* Retention enforcement (operators rotate the file manually in v1).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

logger = logging.getLogger(__name__)


class AuditEventType(str, Enum):
    """§11.2 event vocabulary. Adapter-related + inference-related.

    String mixin so events serialize as plain strings in JSON.
    """

    # Adapter lifecycle
    ADAPTER_REGISTERED = "adapter.registered"
    ADAPTER_DEREGISTERED = "adapter.deregistered"
    ADAPTER_PERMISSION_GRANTED = "adapter.permission_granted"
    ADAPTER_PERMISSION_REVOKED = "adapter.permission_revoked"
    ADAPTER_CAPABILITY_DRIFT = "adapter.capability_drift"
    ADAPTER_FINGERPRINT_MISMATCH = "adapter.fingerprint_mismatch"
    ADAPTER_UNAVAILABLE = "adapter.unavailable"

    # Inference
    INFERENCE_COMPLETED = "inference.completed"
    INFERENCE_FAILED = "inference.failed"
    INFERENCE_REFUSED_SOVEREIGNTY = "inference.refused_sovereignty"
    INFERENCE_REFUSED_PERMISSION = "inference.refused_permission"
    INFERENCE_REFUSED_BUDGET = "inference.refused_budget"

    # Stream lifecycle (logged via /infer/stream; KAI-C currently
    # proxies HTTP /infer only, so these are placeholders for A2.5)
    STREAM_OPENED = "stream.opened"
    STREAM_CLOSED = "stream.closed"


class AuditEvent(dict):
    """A single audit record. Inherits from dict so it serializes
    cleanly via json.dumps and so callers can stash extra fields
    without subclassing."""


def new_correlation_id() -> str:
    """KAI-C mints a fresh correlation_id when a client doesn't supply
    one. Format matches the §3.8 wire spec: a hex UUID."""
    return uuid.uuid4().hex


class AuditStore:
    """Append-only JSONL audit log.

    Thread-safe for concurrent emit() calls — every event flushes
    immediately so a crash mid-write loses at most the latest record.
    The file is opened in line-buffered mode; on POSIX the kernel
    flushes after every newline.
    """

    def __init__(self, path: str | None = None) -> None:
        """
        ``path`` defaults to ``$KAI_C_AUDIT_LOG`` or
        ``/var/log/opennvr/kai-c-audit.jsonl``. The parent directory is
        created on first write so a missing dir doesn't crash startup
        — the file is opened lazily on the first emit.
        """
        self._path: str = (
            path
            or os.environ.get("KAI_C_AUDIT_LOG")
            or "/var/log/opennvr/kai-c-audit.jsonl"
        )
        self._lock = threading.Lock()
        self._opened: bool = False

    @property
    def path(self) -> str:
        return self._path

    def emit(
        self,
        event_type: AuditEventType | str,
        *,
        correlation_id: str | None = None,
        adapter: str | None = None,
        camera_id: str | None = None,
        **fields: Any,
    ) -> AuditEvent:
        """Write one audit record. Returns the event so callers can
        attach the ``correlation_id`` to whatever they emit next."""
        event = AuditEvent({
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": str(event_type.value if isinstance(event_type, AuditEventType) else event_type),
            "correlation_id": correlation_id or new_correlation_id(),
        })
        if adapter is not None:
            event["adapter"] = adapter
        if camera_id is not None:
            event["camera_id"] = camera_id
        event.update(fields)

        line = json.dumps(event, default=_json_safe_default) + "\n"
        with self._lock:
            try:
                if not self._opened:
                    os.makedirs(os.path.dirname(self._path), exist_ok=True)
                    self._opened = True
                with open(self._path, "a", buffering=1) as fh:
                    fh.write(line)
            except OSError as exc:
                # Audit-log failure is itself an event (§11.2 says
                # forwarding failures are audited and don't block the
                # local write — here the LOCAL write failed, which is
                # serious; we surface it via stderr/logger but do NOT
                # raise: the caller's main path should not be broken
                # by audit-disk issues.
                logger.error("audit-log write failed: %s line=%r", exc, line.rstrip())
        return event

    def read_all(self) -> list[AuditEvent]:
        """Read the entire audit log into memory. Used by the
        ``/api/v1/audit`` endpoint with subsequent in-process filter.
        Fine up to ~1M events; future work lands a real store."""
        events: list[AuditEvent] = []
        try:
            with open(self._path, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(AuditEvent(json.loads(line)))
                    except json.JSONDecodeError:
                        # Skip corrupt lines but don't crash — operators
                        # may have hand-edited the file.
                        continue
        except FileNotFoundError:
            return []
        return events

    def filter(
        self,
        *,
        adapter: str | None = None,
        event_type: AuditEventType | str | None = None,
        camera_id: str | None = None,
        since: str | None = None,  # ISO-8601 string
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Read + filter. Limit is applied AFTER filtering, so callers
        get up to ``limit`` matching events from the END of the log
        (most recent first)."""
        events = self.read_all()
        et_value = (
            event_type.value
            if isinstance(event_type, AuditEventType)
            else event_type
        )
        filtered: list[AuditEvent] = []
        for ev in events:
            if adapter is not None and ev.get("adapter") != adapter:
                continue
            if et_value is not None and ev.get("type") != et_value:
                continue
            if camera_id is not None and ev.get("camera_id") != camera_id:
                continue
            if since is not None and ev.get("ts", "") < since:
                continue
            filtered.append(ev)
        return filtered[-limit:]


def _json_safe_default(value: Any) -> Any:
    """Fallback for json.dumps on objects it can't serialize natively."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return repr(value)
