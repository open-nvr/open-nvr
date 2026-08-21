# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Vendored Pydantic copies of the AI Adapter Contract v1 wire shapes.

KAI-C lives in the ``open-nvr`` repo; the source-of-truth Pydantic
models live in ``ai-adapter/app/interfaces/contract.py``. Because the
two are separate repos that may deploy on different release cadences,
KAI-C carries its own vendored copies of the shapes it consumes. On a
contract version bump the two sides MUST be updated in lockstep —
``CapabilitiesResponse.adapter.supported_contract_versions`` is the
canonical version-handshake field.

Only the shapes KAI-C actually parses are vendored — we do not need
the WebSocket-specific message types (HandshakeMessage / FrameMessage
/ etc.) because KAI-C does not currently proxy WebSocket streams.

Forward-compat note: every model uses ``extra="ignore"`` deliberately.
The adapter side uses ``extra="forbid"`` to keep its own wire shape
honest. KAI-C is the *consumer* — if an adapter's contract evolves
to add a new optional field on a future spec bump, KAI-C must keep
parsing the old fields without crashing on the new ones. Strict-on-
the-server, lenient-on-the-client is the classic Postel split.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


CONTRACT_VERSION: str = "1"


class HealthStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    LOADING = "loading"
    ERROR = "error"


class HardwareVerdict(str, Enum):
    OK = "ok"
    WARN = "warn"
    BLOCKED = "blocked"


class ErrorCategory(str, Enum):
    MODEL_ERROR = "model_error"
    PROVIDER_ERROR = "provider_error"
    TRANSPORT_ERROR = "transport_error"
    PERMISSION_DENIED = "permission_denied"
    NOT_SUPPORTED = "not_supported"
    OVERLOADED = "overloaded"


class FairQueuing(str, Enum):
    NONE = "none"
    PER_CAMERA = "per_camera"


# ── /health ────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: HealthStatus
    adapter_name: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    started_at: str  # ISO-8601; not parsed as datetime here — we just relay
    uptime_seconds: int = Field(ge=0)


# ── /capabilities ──────────────────────────────────────────────────


class AdapterInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    vendor: str = Field(min_length=1)
    license: str = Field(min_length=1)
    model_card_url: str | None = None
    supported_contract_versions: list[str] = Field(min_length=1)


class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    framework: str = Field(min_length=1)
    size_mb: float | None = Field(default=None, ge=0.0)
    modalities_in: list[str] = Field(default_factory=list)
    modalities_out: list[str] = Field(default_factory=list)
    fingerprint: str | None = Field(default=None, min_length=1)


class InferEndpointInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    supported: bool
    input_content_types: list[str] = Field(default_factory=list)
    input_schema_ref: str | None = None
    output_schema_ref: str | None = None


class StreamEndpointInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    supported: bool
    max_concurrent_streams: int = Field(default=0, ge=0)
    supports_shared_memory: bool = False
    shared_memory_protocol_version: int | None = Field(default=None, ge=1)


class ExtraEndpoint(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = Field(min_length=1)
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"]
    purpose: str = Field(min_length=1)


class EndpointsInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    infer: InferEndpointInfo
    infer_stream: StreamEndpointInfo
    extra: list[ExtraEndpoint] = Field(default_factory=list)


class Permissions(BaseModel):
    model_config = ConfigDict(extra="ignore")
    gpu: bool = False
    network_egress: list[str] = Field(default_factory=list)
    host_filesystem: list[str] = Field(default_factory=list)
    shared_memory_paths: list[str] = Field(default_factory=list)
    host_metadata: bool = False


class Scheduling(BaseModel):
    model_config = ConfigDict(extra="ignore")
    max_inflight: int = Field(default=1, ge=1)
    preferred_batch_size: int = Field(default=1, ge=1)
    fair_queuing: FairQueuing = FairQueuing.NONE


class Cost(BaseModel):
    model_config = ConfigDict(extra="ignore")
    currency: str = Field(default="USD", min_length=3, max_length=3)
    estimated_per_call: float = Field(default=0.0, ge=0.0)
    estimated_per_hour: float = Field(default=0.0, ge=0.0)
    rate_limit_per_minute: int | None = Field(default=None, ge=0)
    is_metered: bool = False


class CapabilityDescriptor(BaseModel):
    """Contract v1.1 (optional): rich self-description of one advertised task.

    ``task`` must match an entry in ``tasks_advertised``, which remains the
    canonical free-text key. Adapters that don't send descriptors keep
    working — consumers fall back to the bare task string.
    """

    model_config = ConfigDict(extra="ignore")
    task: str = Field(min_length=1)
    label: str | None = None
    summary: str | None = None
    categories: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    example_result: dict | None = None


class Accelerator(BaseModel):
    """Contract v1.1 (optional): compute backend an object-detector runs on.

    Vendored from the SDK (opennvr_adapter_sdk/contract.py). ``backend`` is an
    opaque string so a newer adapter's backend value never fails to parse here.
    """

    model_config = ConfigDict(extra="ignore")
    backend: str = Field(default="cpu", min_length=1)
    device: str | None = None


class InputSpec(BaseModel):
    """Contract v1.1 (optional): the tensor an object-detector expects. KAI-C
    shapes region crops to this so the adapter stays layout-simple."""

    model_config = ConfigDict(extra="ignore")
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    layout: str = "nhwc"          # nhwc | nchw
    dtype: str = "uint8"          # uint8 | float | float_denorm
    pixel_format: str = "rgb"     # rgb | bgr | yuv


class DetectorSpec(BaseModel):
    """Contract v1.1 (optional): marks the adapter as an object detector the
    Tier-0 detect pipeline can dispatch region crops to. Non-detector adapters
    omit it; KAI-C then does not use them in the detect loop."""

    model_config = ConfigDict(extra="ignore")
    input: InputSpec
    accelerator: Accelerator = Field(default_factory=Accelerator)
    labels: list[str] = Field(default_factory=list)
    max_detections: int = Field(default=20, ge=1)


class CapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    adapter: AdapterInfo
    model: ModelInfo
    endpoints: EndpointsInfo
    tasks_advertised: list[str] = Field(default_factory=list)
    capabilities: list[CapabilityDescriptor] = Field(default_factory=list)
    permissions: Permissions = Field(default_factory=Permissions)
    scheduling: Scheduling
    cost: Cost = Field(default_factory=Cost)
    # Contract v1.1 (optional): present only on object-detector adapters.
    detector: DetectorSpec | None = None


# ── Failure envelope ───────────────────────────────────────────────


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")
    category: ErrorCategory
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    transient: bool
    retry_after_ms: int | None = Field(default=None, ge=0)
    details: dict[str, Any] = Field(default_factory=dict)


class FailureEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["error"] = "error"
    error: ErrorDetail
