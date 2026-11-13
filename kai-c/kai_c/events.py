# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
NATS event schema for the B1 event-bus surface.

§11.4 of the contract design (and the §11.2 audit vocabulary it
extends) reserves these subject names for KAI-C's broadcast surface
so monitoring apps can subscribe instead of polling. A subscribed
client gets every inference result in real time without making its
own KAI-C call — one adapter inference fans out to N consumers, GPU
load drops N×.

Subject naming convention
-------------------------

::

    opennvr.inference.{adapter}.{camera_id}.completed

* ``adapter``    — adapter name as registered with KAI-C (yolov8,
                   piper-tts, whisper-asr, …).
* ``camera_id``  — operator-supplied camera identifier. The literal
                   string ``unknown`` is used when KAI-C couldn't
                   derive one from the request payload (e.g.,
                   conformance probes).

Subscribers can use NATS wildcards:

* ``opennvr.inference.>``               — every inference event
* ``opennvr.inference.yolov8.>``        — YOLOv8 results only
* ``opennvr.inference.*.cam-front.>``   — every adapter, one camera

Schema is JSON over the wire — human-debuggable, no codegen needed,
~1 KB per event for typical detection results. Subscribers consume
via the ``opennvr-inference-listener`` example (or roll their own;
see ``examples/inference-listener/README.md`` for the 30-line
template).

Failure mode
------------

Publishing is best-effort. If the NATS broker is down or the publish
times out, KAI-C logs a warning and continues — the HTTP/WS request
path is NEVER blocked by the publisher. Subscribers see a gap in
the event stream; KAI-C's audit log still has the full record (so
the audit guarantee §11.2 makes is unchanged). When the broker
comes back, publishing resumes for new inferences; we do not replay
the gap. Replay is a separate event-store concern (see §B2 in the
design doc roadmap).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


# Subject prefix used by every KAI-C-broadcast event. Subscribers
# anchoring on this constant avoid hard-coding the literal in their
# code. Exported via ``kai_c.events.SUBJECT_PREFIX``.
SUBJECT_PREFIX: str = "opennvr"
INFERENCE_COMPLETED_SUFFIX: str = "completed"


def inference_completed_subject(adapter: str, camera_id: str | None) -> str:
    """Build the topic name for an ``InferenceCompletedEvent``.

    ``camera_id=None`` is normalized to ``"unknown"`` so wildcard
    subscriptions never miss events from conformance probes /
    adapter health checks where the contract doesn't require a
    camera_id.
    """
    cam = camera_id or "unknown"
    return f"{SUBJECT_PREFIX}.inference.{adapter}.{cam}.{INFERENCE_COMPLETED_SUFFIX}"


class InferenceCompletedEvent(BaseModel):
    """Published by KAI-C after every successful ``/api/v1/infer/{adapter}``
    call AND every successful WS-streaming ``result`` message.

    Carries enough information for a subscriber to:

    * Filter by adapter / camera_id (subject-level wildcards).
    * Join back to KAI-C's audit log via ``correlation_id``.
    * Verify the inference was produced by the expected weights via
      ``model_fingerprint`` (§11.3 drift detection).
    * Apply downstream business logic on ``result`` — same shape as
      the §3.5 ``InferResponse.result`` field, so existing parsers
      work unchanged.

    The schema is forward-compatible: extra fields a future KAI-C
    version adds will be ignored by Pydantic-strict subscribers (the
    model uses ``extra="ignore"``).
    """

    model_config = {"extra": "ignore"}

    correlation_id: str = Field(
        description="X-Correlation-Id threaded from the inbound request. "
                    "Same value the adapter saw and the audit log records."
    )
    adapter: str = Field(description="Registered adapter name (e.g., 'yolov8').")
    adapter_version: str | None = Field(
        default=None,
        description="Adapter version from /capabilities.adapter.version.",
    )
    camera_id: str | None = Field(
        default=None,
        description="Operator-supplied camera identifier, if any.",
    )
    track_id: int | None = Field(
        default=None,
        description="Tier-0 track the request was about, when Tier-1 "
                    "dispatch sent it (echoed from the request, never "
                    "interpreted). Joins the result back to the person or "
                    "vehicle the pipeline is following.",
    )
    label: str | None = Field(
        default=None,
        description="Tier-0 class of that track (e.g. 'person'), echoed "
                    "like track_id.",
    )
    model_name: str = Field(description="Model name from the §3.5 InferResponse.")
    model_version: str = Field(description="Model version from the §3.5 InferResponse.")
    model_fingerprint: str | None = Field(
        default=None,
        description="sha256 of weights at inference time. Subscribers can "
                    "verify against the last /capabilities snapshot to catch "
                    "mid-flight weight rotation (§11.3).",
    )
    inference_ms: int = Field(
        ge=0,
        description="Per-inference latency in ms (from the adapter's response).",
    )
    seq: int | None = Field(
        default=None,
        description="WS-streaming sequence number (§6.3). Monotonically "
                    "increasing per WS session; absent on the HTTP /infer "
                    "path. Subscribers can use it to detect dropped frames "
                    "or dedupe redelivered messages.",
    )
    result: dict[str, Any] = Field(
        default_factory=dict,
        description="The adapter's §5.x result body (DetectionResult, "
                    "AsrResult, etc.). Shape is task-specific; the contract "
                    "doesn't constrain it.",
    )
    completed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC instant KAI-C received the adapter's response.",
    )
