# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
# 
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
KAI-C HTTP Service - Middleware between OpenNVR NVR and AI Adapters

This service runs as a standalone HTTP server that:
1. Receives requests from OpenNVR Backend
2. Forwards them to AI Adapters
3. Returns standardized responses
"""

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import AnyHttpUrl, BaseModel, Field
from typing import Dict, Any, Optional
import httpx
import ipaddress
import logging
import socket
import time
import uvicorn
import requests
import os
from urllib.parse import urlparse
from fastapi import Header

from kai_c.audit import AuditEventType, AuditStore, new_correlation_id
from kai_c.connector import KaiConnector
from kai_c.correlation import CORRELATION_ID_HEADER, CorrelationIdMiddleware
from kai_c.registry import AdapterRegistry
from kai_c.schemas import KAIRequest
from kai_c.sovereignty import SovereigntyViolation

logger = logging.getLogger("kai-c")


# ============================================================
# V-022 (M1a): AI sovereignty policy
# ============================================================
# Mirrors the server-side `settings.ai_sovereignty` field. KAI-C cannot
# import from `core.config` (it is a separate sub-project that runs in its
# own process and may live on a different host) so the policy is duplicated
# here via env var. The two SHOULD be set to the same value at deploy time;
# the architecture doc calls this out as a known operational coupling.
#
# Values:
#   local_only       - default. Every adapter URL in ADAPTER_REGISTRY must
#                      resolve to loopback (127.0.0.1, ::1, localhost), and
#                      /infer/cloud (HuggingFace proxy) is refused outright.
#   federated        - adapters may live off-host but raw frame data is
#                      refused; only anonymised parameter exchange is okay.
#                      The federated runtime is responsible for honouring
#                      that distinction.
#   cloud_allowed    - no boundary checks; suitable for hosted deployments
#                      that have explicitly accepted the sovereignty
#                      trade-off.
AI_SOVEREIGNTY = os.getenv("AI_SOVEREIGNTY", "local_only").lower()
if AI_SOVEREIGNTY not in {"local_only", "federated", "cloud_allowed"}:
    raise RuntimeError(
        f"AI_SOVEREIGNTY env var must be one of "
        f"'local_only' / 'federated' / 'cloud_allowed' "
        f"(got {AI_SOVEREIGNTY!r})."
    )


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _host_is_loopback(host: str | None) -> bool:
    """Mirror of server/core/config.py:_host_is_loopback, kept narrow to
    avoid pulling the server codebase into KAI-C."""
    if not host:
        return False
    h = host.strip("[]").lower()
    if h in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        pass
    saved = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(2.0)
        try:
            infos = socket.getaddrinfo(h, None)
        except (socket.gaierror, socket.timeout, OSError):
            return False
    finally:
        socket.setdefaulttimeout(saved)
    return bool(infos) and all(
        ipaddress.ip_address(info[4][0]).is_loopback for info in infos
    )


def _validate_adapters_match_sovereignty() -> None:
    """Refuse to start if AI_SOVEREIGNTY=local_only but any registered
    adapter URL is non-loopback. Called immediately after ADAPTER_REGISTRY
    is defined so a mis-set env var is caught before the server accepts a
    single request."""
    if AI_SOVEREIGNTY != "local_only":
        return
    offenders: list[str] = []
    for name, url in ADAPTER_REGISTRY.items():
        host = urlparse(url).hostname
        if host is None:
            offenders.append(f"{name}={url!r} (unparseable host — missing scheme?)")
        elif host == "0.0.0.0":
            offenders.append(
                f"{name}={url!r} (host is 0.0.0.0, the wildcard bind, not loopback)"
            )
        elif not _host_is_loopback(host):
            offenders.append(f"{name}={url!r} (host={host})")
    if offenders:
        details = "\n  - ".join(offenders)
        raise RuntimeError(
            "V-022: AI_SOVEREIGNTY=local_only requires every adapter URL "
            "to be loopback. The following adapters violate that policy:\n"
            f"  - {details}\n"
            "Either set ADAPTER_URL (and any per-model overrides) to a "
            "127.0.0.1 / ::1 / localhost URL, or set AI_SOVEREIGNTY="
            "federated|cloud_allowed if you have explicitly accepted the "
            "sovereignty trade-off."
        )

app = FastAPI(
    title="KAI-C (Kavach AI Connector)",
    description="Middleware connector between OpenNVR Kavach and AI Adapters",
    version="2.0.0"  # A2.4: bumped — added registry / audit / correlation_id
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# A2.4: correlation_id middleware (§3.8). Runs on every request so the
# audit log lines below can pull the id off request.state.
app.add_middleware(CorrelationIdMiddleware)

# A2.4: audit store + adapter registry singletons. The audit store is
# eager; the registry is built at startup so the poller can begin
# polling once known adapters are registered.
_audit = AuditStore()
_registry: AdapterRegistry | None = None


def get_registry() -> AdapterRegistry:
    if _registry is None:  # pragma: no cover — guarded by startup
        raise RuntimeError("registry not initialized; startup did not run")
    return _registry


def get_audit() -> AuditStore:
    return _audit


@app.on_event("startup")
async def _kaic_startup() -> None:
    """Build the registry + register any adapters from the legacy
    ``ADAPTER_REGISTRY`` env-derived dict. Adapters that fail
    registration (sovereignty refusal, unreachable, etc.) are logged
    but do NOT block startup — operators may register them later via
    POST /api/v1/adapters/register."""
    global _registry
    _registry = AdapterRegistry(
        sovereignty_mode=AI_SOVEREIGNTY,
        audit=_audit,
    )
    for name, url in ADAPTER_REGISTRY.items():
        try:
            await _registry.register(name, url)
        except SovereigntyViolation as exc:
            logger.warning("sovereignty refused %s@%s: %s", name, url, exc)
            _audit.emit(
                AuditEventType.INFERENCE_REFUSED_SOVEREIGNTY,
                adapter=name,
                reason=str(exc),
                sovereignty_mode=AI_SOVEREIGNTY,
                registration_url=url,
            )
        except Exception as exc:
            # Adapter unreachable / malformed /capabilities — log and
            # continue. Operators can re-register via the v2 endpoint
            # once the adapter is up.
            logger.info("registration deferred for %s@%s: %s", name, url, exc)
    await _registry.start_polling()


@app.on_event("shutdown")
async def _kaic_shutdown() -> None:
    global _registry
    if _registry is not None:
        await _registry.aclose()
        _registry = None

# ============================================================
# ADAPTER REGISTRY - KAI-C manages all AI Adapter URLs
# Users NEVER see or configure these URLs
# ============================================================
ADAPTER_REGISTRY = {
    "default": os.getenv("ADAPTER_URL", "http://localhost:9100"),  # Default AI Adapter
    # Add more adapters here as needed:
    # "yolov8": "http://localhost:9100",
    # "blip": "http://localhost:9101",
    # "insightface": "http://localhost:9102",
}

# V-022 (M1a): fail-closed at import time if any adapter URL violates the
# sovereignty policy. Cannot run as a startup handler because we want this
# check to fire even when KAI-C is imported by tests, not just when uvicorn
# is the entry point.
_validate_adapters_match_sovereignty()


def get_adapter_url(model_name: str = "default") -> str:
    """Get AI Adapter URL from internal registry."""
    return ADAPTER_REGISTRY.get(model_name, ADAPTER_REGISTRY["default"])


class InferenceRequest(BaseModel):
    """Request model for inference endpoint"""
    camera_id: str
    stream_url: str
    model_name: str
    task: str
    options: Optional[Dict[str, Any]] = {}


class InferenceResponse(BaseModel):
    """Response model for inference endpoint"""
    status: str
    camera_id: Optional[str] = None
    model_used: Optional[str] = None
    event_type: Optional[str] = None
    response: Optional[Dict[str, Any]] = None
    message: Optional[str] = None


class CloudInferenceRequest(BaseModel):
    """Request model for cloud inference endpoint"""
    provider: str
    model_name: str
    task: str
    inputs: Dict[str, Any]
    parameters: Optional[Dict[str, Any]] = {}
    credential_token: str


class CloudInferenceResponse(BaseModel):
    """Response model for cloud inference endpoint"""
    status: str
    task: str
    model_name: str
    result: Optional[Any] = None
    latency_ms: int
    executed_at: str
    error: Optional[str] = None


# Internal API key for authentication between opennvr and kai-c
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "KAI-C (Kavach AI Connector)",
        "version": "1.0.0",
        "status": "running",
        "configured_adapters": list(ADAPTER_REGISTRY.keys())
    }


@app.post("/infer", response_model=InferenceResponse)
async def process_inference(request: InferenceRequest):
    """
    Process inference request through KAI-C connector.
    
    This endpoint:
    1. Receives request from OpenNVR Backend (NO adapter URL from user!)
    2. KAI-C looks up the correct AI Adapter from internal registry
    3. Forwards request to AI Adapter
    4. Returns standardized response
    
    Flow: OpenNVR Backend → KAI-C → AI Adapter (from registry) → KAI-C → OpenNVR Backend
    """
    try:
        # Get AI Adapter URL from internal registry (user never provides this!)
        adapter_url = get_adapter_url(request.model_name)
        
        # Create KAI-C connector for the adapter
        connector = KaiConnector(adapter_url=adapter_url)
        
        # Create KAI request
        kai_request = KAIRequest(
            camera_id=request.camera_id,
            stream_url=request.stream_url,
            model_name=request.model_name,
            task=request.task,
            options=request.options
        )
        
        # Process through connector (forwards to AI Adapter)
        result = connector.process_stream(kai_request)
        
        # Check if there's an error
        if result.get("status") == "error":
            return InferenceResponse(
                status="error",
                message=result.get("message", "Unknown error from AI Adapter")
            )
        
        # Return success response
        return InferenceResponse(
            status="success",
            camera_id=result.get("camera_id", request.camera_id),
            model_used=result.get("model_used", request.model_name),
            event_type=result.get("event_type", "INFERENCE_COMPLETE"),
            response=result.get("response", result)
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"KAI-C processing error: {str(e)}"
        )


@app.post("/infer/local")
async def process_local_inference(request: dict):
    """
    Process local AI inference request through KAI-C.
    
    This endpoint accepts the task/input format from the backend
    and forwards it to the AI Adapter.
    
    Request format:
    {
        "task": "person_detection",
        "input": {
            "frame": {"uri": "kavach://frames/camera_1/latest.jpg"}
        }
    }
    
    Flow: OpenNVR Backend → KAI-C → AI Adapter → KAI-C → OpenNVR Backend
    """
    try:
        adapter_url = get_adapter_url()
        
        # Forward request directly to AI Adapter
        response = requests.post(
            f"{adapter_url}/infer",
            json=request,
            timeout=60
        )
        response.raise_for_status()
        result = response.json()
        
        # Return AI Adapter response wrapped in standard format
        return {
            "status": "success",
            "response": result
        }
        
    except requests.HTTPError as e:
        raise HTTPException(
            status_code=e.response.status_code if e.response else 500,
            detail=f"AI Adapter error: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"KAI-C processing error: {str(e)}"
        )


@app.post("/infer/cloud", response_model=CloudInferenceResponse)
async def process_cloud_inference(
    request: CloudInferenceRequest,
    x_internal_api_key: Optional[str] = Header(None)
):
    """
    Process cloud AI inference request.

    This endpoint:
    1. Validates internal API key from opennvr
    2. Routes to cloud provider handler (e.g., HuggingFace)
    3. Returns unified response format

    Flow: OpenNVR Backend → KAI-C → Cloud Provider API → KAI-C → OpenNVR Backend
    """
    # V-022 (M1a): refuse the entire cloud-provider proxy path when the
    # operator has set AI_SOVEREIGNTY=local_only. The server-side router
    # already 403s its own /cloud-inference/* endpoints, but this is the
    # defence-in-depth at the KAI-C side: a misconfigured or compromised
    # caller cannot route to HuggingFace by hitting KAI-C directly.
    if AI_SOVEREIGNTY == "local_only":
        raise HTTPException(
            status_code=403,
            detail=(
                "Cloud-provider inference disabled: "
                "AI_SOVEREIGNTY=local_only. Set AI_SOVEREIGNTY="
                "federated|cloud_allowed at boot to enable."
            ),
        )

    # Validate internal API key
    if INTERNAL_API_KEY and x_internal_api_key != INTERNAL_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Invalid internal API key"
        )
    
    try:
        # Route to appropriate cloud provider
        if request.provider == "huggingface":
            result = await _process_huggingface_inference(request)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported provider: {request.provider}"
            )
        
        return CloudInferenceResponse(**result)
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Cloud inference error: {str(e)}"
        )


async def _process_huggingface_inference(request: CloudInferenceRequest) -> Dict[str, Any]:
    """
    Process Hugging Face inference via AI Adapter.
    
    Routes request to AI Adapter's HuggingFaceHandler.
    """
    adapter_url = get_adapter_url()
    
    # Prepare payload for AI Adapter's /infer endpoint
    payload = {
        "task": request.task,
        "input": {
            "model_name": request.model_name,
            "inputs": request.inputs,
            "parameters": request.parameters,
            "api_token": request.credential_token
        }
    }
    
    try:
        response = requests.post(
            f"{adapter_url}/infer",
            json=payload,
            timeout=60
        )
        response.raise_for_status()
        
        result = response.json()
        
        # Transform to unified format
        return {
            "status": result.get("status", "success"),
            "task": request.task,
            "model_name": request.model_name,
            "result": result.get("result") or result.get("response"),
            "latency_ms": result.get("latency_ms", 0),
            "executed_at": result.get("executed_at", ""),
            "error": result.get("error")
        }
    
    except requests.HTTPError as e:
        raise HTTPException(
            status_code=e.response.status_code if e.response else 500,
            detail=f"AI Adapter error: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to call AI Adapter: {str(e)}"
        )


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "ok",
        "service": "kai-c",
        "message": "KAI-C is running and ready to process requests"
    }


@app.get("/adapters/health")
async def check_adapters_health():
    """
    Check health of all configured AI Adapters.
    
    Flow: Backend → KAI-C → (checks internal adapters)
    
    Returns status of all adapters in the registry.
    """
    results = {}
    for name, url in ADAPTER_REGISTRY.items():
        try:
            response = requests.get(f"{url}/health", timeout=5)
            if response.status_code == 200:
                results[name] = {"status": "ok", "url": url}
            else:
                results[name] = {"status": "error", "url": url, "message": f"Returned {response.status_code}"}
        except Exception as e:
            results[name] = {"status": "error", "url": url, "message": str(e)}
    
    return {
        "kai_c_status": "ok",
        "adapters": results
    }


@app.get("/capabilities")
async def get_all_capabilities():
    """
    Get capabilities from all configured AI Adapters.
    
    Flow: Backend → KAI-C → (queries all internal adapters)
    
    Returns combined capabilities from all adapters.
    """
    all_capabilities = {
        "kai_c": {
            "version": "1.0.0",
            "service": "kai-c"
        },
        "adapters": {}
    }
    
    for name, url in ADAPTER_REGISTRY.items():
        try:
            response = requests.get(f"{url}/capabilities", timeout=10)
            response.raise_for_status()
            all_capabilities["adapters"][name] = {
                "url": url,
                "capabilities": response.json()
            }
        except Exception as e:
            all_capabilities["adapters"][name] = {
                "url": url,
                "error": str(e)
            }
    
    return all_capabilities


@app.get("/schema")
async def get_schemas(task: Optional[str] = None):
    """
    Get schemas from AI Adapters.
    
    Flow: Backend → KAI-C → (queries internal adapters)
    """
    adapter_url = get_adapter_url()
    try:
        params = {"task": task} if task else {}
        response = requests.get(
            f"{adapter_url}/schema",
            params=params,
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get schema: {str(e)}"
        )


# ============================================================
# A2.4: v2 registry / audit / aggregated-capabilities endpoints
# ============================================================
# These live under /api/v1/* so future versioning (v2, v3) is cheap.
# The legacy /infer, /infer/local, /infer/cloud, /health, /capabilities,
# /adapters/health, /schema endpoints above are kept for back-compat
# until OpenNVR backend migrates onto the v1 surface.


def require_internal_api_key(x_internal_api_key: Optional[str] = Header(None)) -> None:
    """Auth dependency for the v2 endpoints (peer-review SR-NEW-7).

    Same dev-mode-bypass pattern as the legacy ``/infer/cloud`` endpoint:
    if ``INTERNAL_API_KEY`` is empty (dev / single-host loopback
    deployments) all calls pass. In production the operator sets the
    env var and OpenNVR backend MUST send the matching
    ``X-Internal-Api-Key`` header.

    All v2 endpoints depend on this so register/deregister/infer cannot
    be reached anonymously by an attacker who finds KAI-C's port open
    on a non-loopback interface.
    """
    if INTERNAL_API_KEY and x_internal_api_key != INTERNAL_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: missing or invalid X-Internal-Api-Key",
        )


class RegisterAdapterRequest(BaseModel):
    """Payload for POST /api/v1/adapters/register.

    ``url`` is validated as a real HTTP/HTTPS URL — malformed strings
    return 422 (Unprocessable Entity) before they hit the sovereignty
    layer, where they'd otherwise look like a sovereignty refusal.
    """
    name: str = Field(min_length=1)
    url: AnyHttpUrl


@app.post("/api/v1/adapters/register", dependencies=[Depends(require_internal_api_key)])
async def v1_register_adapter(payload: RegisterAdapterRequest):
    """Register an adapter at runtime. Polls /capabilities, runs the
    sovereignty + permission checks, stores in the registry."""
    try:
        adapter = await get_registry().register(payload.name, str(payload.url))
    except SovereigntyViolation as exc:
        get_audit().emit(
            AuditEventType.INFERENCE_REFUSED_SOVEREIGNTY,
            adapter=payload.name,
            reason=str(exc),
            sovereignty_mode=AI_SOVEREIGNTY,
            registration_url=payload.url,
        )
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"adapter unreachable: {exc}")
    return {
        "status": "ok",
        "adapter": {
            "name": adapter.name,
            "url": adapter.url,
            "adapter_version": adapter.capabilities.adapter.version,
            "fingerprint": adapter.fingerprint,
        },
    }


@app.delete("/api/v1/adapters/{name}", dependencies=[Depends(require_internal_api_key)])
async def v1_deregister_adapter(name: str):
    """Remove an adapter from the registry. Emits adapter.deregistered."""
    registry = get_registry()
    if registry.get(name) is None:
        raise HTTPException(status_code=404, detail=f"unknown adapter: {name}")
    await registry.deregister(name, reason="operator_action")
    return {"status": "ok"}


@app.get("/api/v1/adapters", dependencies=[Depends(require_internal_api_key)])
async def v1_list_adapters():
    """Lightweight adapter summaries — what the OpenNVR UI lists."""
    return {"adapters": get_registry().list_summaries()}


@app.get("/api/v1/ai/capabilities", dependencies=[Depends(require_internal_api_key)])
async def v1_aggregated_capabilities():
    """The §11 aggregated capabilities view — a single call so the UI
    doesn't fan out across N adapter URLs."""
    return get_registry().aggregated_capabilities()


@app.post("/api/v1/adapters/refresh", dependencies=[Depends(require_internal_api_key)])
async def v1_refresh_adapters(name: Optional[str] = None):
    """Force a /capabilities + /health re-poll. Without ``name`` refreshes
    every registered adapter; with it, just the one. §11."""
    registry = get_registry()
    if name is not None:
        if registry.get(name) is None:
            raise HTTPException(status_code=404, detail=f"unknown adapter: {name}")
        await registry.refresh(name)
        return {"status": "ok", "refreshed": [name]}
    names = registry.list_names()
    for n in names:
        await registry.refresh(n)
    return {"status": "ok", "refreshed": names}


@app.post("/api/v1/infer/{adapter_name}", dependencies=[Depends(require_internal_api_key)])
async def v1_infer(
    adapter_name: str,
    payload: Dict[str, Any],
    request: Request,
):
    """Contract-compliant proxy: forwards JSON payload to the named
    adapter's /infer, threading the request's X-Correlation-Id, and
    emitting inference.completed / inference.failed audit events.

    For v1 we only proxy ``application/json`` requests — multipart
    proxying lands when KAI-C starts brokering binary frame payloads
    itself (A2.4b alongside the WS streaming bridge)."""
    registry = get_registry()
    audit = get_audit()
    adapter = registry.get(adapter_name)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"unknown adapter: {adapter_name}")

    correlation_id = getattr(request.state, "correlation_id", None)
    if correlation_id is None:  # belt-and-braces
        correlation_id = new_correlation_id()

    camera_id = payload.get("camera_id") if isinstance(payload, dict) else None
    started = time.monotonic()

    try:
        status_code, body = await registry.proxy_infer(adapter_name, payload, correlation_id)
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        audit.emit(
            AuditEventType.INFERENCE_FAILED,
            correlation_id=correlation_id,
            adapter=adapter_name,
            camera_id=camera_id,
            latency_ms=latency_ms,
            error_category="transport_error",
            error_code="adapter_unreachable",
            error_message=str(exc),
        )
        raise HTTPException(status_code=502, detail=f"adapter unreachable: {exc}")

    latency_ms = int((time.monotonic() - started) * 1000)

    if status_code == 200:
        audit.emit(
            AuditEventType.INFERENCE_COMPLETED,
            correlation_id=correlation_id,
            adapter=adapter_name,
            camera_id=camera_id,
            latency_ms=latency_ms,
            model_version=adapter.capabilities.model.version,
        )
        return body

    error_info = body.get("error", {}) if isinstance(body, dict) else {}
    audit.emit(
        AuditEventType.INFERENCE_FAILED,
        correlation_id=correlation_id,
        adapter=adapter_name,
        camera_id=camera_id,
        latency_ms=latency_ms,
        error_category=error_info.get("category", "unknown"),
        error_code=error_info.get("code", "unknown"),
        transient=error_info.get("transient", False),
    )
    raise HTTPException(status_code=status_code, detail=body)


@app.get("/api/v1/audit", dependencies=[Depends(require_internal_api_key)])
async def v1_audit_query(
    adapter: Optional[str] = None,
    event_type: Optional[str] = None,
    camera_id: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
):
    """Query recent audit events. Filters: adapter, event_type,
    camera_id, since (ISO-8601). Returns up to ``limit`` newest matches."""
    if limit < 1 or limit > 10_000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 10000")
    events = get_audit().filter(
        adapter=adapter,
        event_type=event_type,
        camera_id=camera_id,
        since=since,
        limit=limit,
    )
    return {"count": len(events), "events": events}


if __name__ == "__main__":
    print("=" * 60)
    print("Starting KAI-C (Kavach AI Connector) Service")
    print("=" * 60)
    print("Running on: http://localhost:8100")
    print("API Docs: http://localhost:8100/docs")
    print("=" * 60)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8100,
        log_level="info"
    )
