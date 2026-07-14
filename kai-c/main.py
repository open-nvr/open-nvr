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

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import AnyHttpUrl, BaseModel, Field
from typing import Dict, Any, Optional
import hmac
import ipaddress
import logging
import socket
import time
import uvicorn
import requests
import os
import base64
from urllib.parse import urlparse
from fastapi import Header

from kai_c.audit import AuditEventType, AuditStore, new_correlation_id
from kai_c.connector import KaiConnector
from kai_c.correlation import CORRELATION_ID_HEADER, CorrelationIdMiddleware
from kai_c.events import InferenceCompletedEvent
from kai_c.nats_publisher import NatsPublisher
from kai_c.registry import AdapterRegistry
from kai_c.schemas import KAIRequest
from kai_c.sovereignty import SovereigntyViolation
from kai_c.stream_proxy import (
    CLOSE_MODEL_ERROR,
    CLOSE_POLICY_REFUSED,
    StreamProxy,
)

logger = logging.getLogger("kai-c")


# ============================================================
# AI sovereignty policy (V-022)
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


# Internal API key for authentication between opennvr and kai-c.
# Defined here (before lifespan + before FastAPI()) so it's a normal
# module-level binding by the time lifespan, the Depends-protected
# routes, and the dependencies all read it. Originally lived halfway
# down the file with the route handlers — leaving it there worked at
# runtime (lifespan reads it only at uvicorn startup, by which point
# the whole module has been evaluated) but was a footgun for any
# future refactor that calls lifespan directly.
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")

# When INTERNAL_API_KEY is unset, the internal surface used to be fully
# anonymous — a silent, fail-OPEN default. That insecure mode is now explicit
# opt-in: set KAI_C_ALLOW_ANONYMOUS=true only for a local single-host dev box
# with no key. Otherwise an empty key fails CLOSED (all internal endpoints 401).
KAI_C_ALLOW_ANONYMOUS = os.getenv("KAI_C_ALLOW_ANONYMOUS", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def _internal_api_key_ok(supplied: Optional[str]) -> bool:
    """Return True if a request bearing X-Internal-Api-Key ``supplied`` may
    reach KAI-C's internal surface (v2 endpoints, /infer/cloud, the WS proxy).

    * Key set   -> constant-time match required (no timing oracle, no silent
      bypass when the key happens to be empty).
    * Key empty + KAI_C_ALLOW_ANONYMOUS -> allowed (explicit local-dev opt-in).
    * Key empty + no opt-in -> DENIED (fail closed).
    """
    if INTERNAL_API_KEY:
        return bool(supplied) and hmac.compare_digest(supplied, INTERNAL_API_KEY)
    return KAI_C_ALLOW_ANONYMOUS


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# local_only accepts loopback + the operator's own Docker bridge subnet (bridge
# traffic stays in-kernel, so it counts as "on this machine"), but not generic
# RFC1918. See V-022 and DESIGN_NOTES: KAI-C sovereignty & the Docker bridge.
_DOCKER_BRIDGE_SUBNET = os.getenv("OPENNVR_DOCKER_SUBNET", "172.28.0.0/16")


def _host_is_on_this_machine(host: str | None) -> bool:
    """Sovereignty-local host check for V-022 (AI_SOVEREIGNTY=local_only).

    Accepts:
      * loopback hosts (``localhost``, ``127.0.0.1``, ``::1``, anything
        resolving to ``is_loopback``);
      * hosts that resolve to an address inside OPENNVR_DOCKER_SUBNET —
        i.e. the operator's own Docker bridge network, where traffic
        stays inside this host's kernel networking stack.

    Rejects everything else, including non-bridge RFC1918 / ULA / LAN
    addresses (those represent peer hosts on the same LAN, which V-022
    is specifically about excluding from the AI plane).

    Replaces the original ``_host_is_loopback`` which only accepted
    loopback. Kept inline so KAI-C doesn't need to import from the
    server package.
    """
    if not host:
        return False
    h = host.strip("[]").lower()
    if h in _LOOPBACK_HOSTS:
        return True
    # Direct-IP check (caller passed e.g. ``127.0.0.1`` or ``172.28.0.5``).
    try:
        ip = ipaddress.ip_address(h)
        if ip.is_loopback:
            return True
        try:
            if ip in ipaddress.ip_network(_DOCKER_BRIDGE_SUBNET):
                return True
        except (ValueError, TypeError):
            pass
        return False
    except ValueError:
        pass
    # Hostname — resolve and check every returned address.
    saved = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(2.0)
        try:
            infos = socket.getaddrinfo(h, None)
        except (socket.gaierror, socket.timeout, OSError):
            return False
    finally:
        socket.setdefaulttimeout(saved)
    if not infos:
        return False
    try:
        bridge_net = ipaddress.ip_network(_DOCKER_BRIDGE_SUBNET)
    except (ValueError, TypeError):
        bridge_net = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback:
            continue
        if bridge_net is not None and ip in bridge_net:
            continue
        return False
    return True


# Back-compat alias — some tests / external callers may use the old name.
_host_is_loopback = _host_is_on_this_machine


def _validate_adapters_match_sovereignty() -> None:
    """Refuse to start if AI_SOVEREIGNTY=local_only but any registered
    adapter URL points off this machine. Called immediately after
    ADAPTER_REGISTRY is defined so a mis-set env var is caught before
    the server accepts a single request.

    "On this machine" includes loopback and the operator's Docker bridge
    subnet — see ``_host_is_on_this_machine`` and DESIGN_NOTES: KAI-C
    sovereignty & the Docker bridge.
    """
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
        elif not _host_is_on_this_machine(host):
            offenders.append(f"{name}={url!r} (host={host})")
    if offenders:
        details = "\n  - ".join(offenders)
        raise RuntimeError(
            "V-022: AI_SOVEREIGNTY=local_only requires every adapter URL "
            "to be on this machine (loopback or Docker bridge subnet "
            f"{_DOCKER_BRIDGE_SUBNET}). "
            "The following adapters violate that policy:\n"
            f"  - {details}\n"
            "Either set ADAPTER_URL (and any per-model overrides) to a "
            "127.0.0.1 / ::1 / localhost URL or a Docker service-DNS "
            "name resolving inside the bridge subnet, or set "
            "AI_SOVEREIGNTY=federated|cloud_allowed if you have explicitly "
            "accepted the sovereignty trade-off."
        )

# ============================================================
# Module-level singletons (populated by the lifespan handler)
# ============================================================
# The audit store is eager — it just opens an append-only file and
# can be constructed at import time. The registry and NATS publisher
# are built inside ``lifespan`` so their event-loop-bound resources
# (httpx.AsyncClient, the background poller task, the NATS connection)
# are tied to the running uvicorn loop.
_audit = AuditStore()
_registry: AdapterRegistry | None = None
_nats_publisher: NatsPublisher | None = None


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

# Fail-closed at import time if any adapter URL violates the sovereignty
# policy. Cannot run as a startup handler because we want this check to
# fire even when KAI-C is imported by tests, not just when uvicorn is the
# entry point.
_validate_adapters_match_sovereignty()


# ============================================================
# Lifespan — registry + NATS publisher startup/shutdown
# ============================================================
# Replaces the legacy ``@app.on_event("startup")`` / ``("shutdown")``
# handlers, which FastAPI has deprecated in favour of the lifespan
# context-manager pattern.

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the registry + register any adapters from the legacy
    ``ADAPTER_REGISTRY`` env-derived dict, then start the NATS publisher.

    Adapters that fail registration (sovereignty refusal, unreachable,
    etc.) are logged but do NOT block startup — operators may register
    them later via ``POST /api/v1/adapters/register``.

    NATS publisher failures are similarly non-fatal: a misconfig is
    logged and the publisher is left in a disabled state so KAI-C still
    serves HTTP/WS inference correctly without broadcast.
    """
    global _registry, _nats_publisher

    # Auth posture (finding #8): make the effective auth mode explicit in the
    # logs so an operator is never silently running fail-open.
    if INTERNAL_API_KEY:
        logger.info("Internal API key auth ENABLED for the internal surface.")
    elif KAI_C_ALLOW_ANONYMOUS:
        logger.warning(
            "INTERNAL_API_KEY is unset and KAI_C_ALLOW_ANONYMOUS is on: the "
            "internal surface is ANONYMOUS. Local-dev only — never production."
        )
    else:
        logger.warning(
            "INTERNAL_API_KEY is unset: the internal surface fails CLOSED (all "
            "calls 401). Set INTERNAL_API_KEY, or KAI_C_ALLOW_ANONYMOUS=true for "
            "a local dev box."
        )

    _registry = AdapterRegistry(
        sovereignty_mode=AI_SOVEREIGNTY,
        audit=_audit,
        # Same bearer token the /infer path uses -- keeps /capabilities +
        # /health polls authenticated past the adapter's 5-minute grace
        # window (otherwise every poll 401s).
        auth_token=INTERNAL_API_KEY or None,
    )
    for name, url in ADAPTER_REGISTRY.items():
        try:
            adapter = await _registry.register(name, url)
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
        else:
            # Contract §8.5 — config-as-consent. This adapter came from
            # the operator's OWN startup configuration (compose overlay /
            # ADAPTER_REGISTRY env), and writing it there IS the consent
            # act — so its declared permission keys are granted here
            # rather than parked in ``pending`` awaiting a UI click. The
            # grant is a first-class audit event (adapter_grant_id +
            # actor "system:startup-config"), keeping the receipt chain
            # intact. Only THIS seed loop auto-grants: adapters added at
            # runtime via POST /api/v1/adapters/register keep the human
            # gate, and permission DRIFT on a later poll still flips a
            # seeded adapter back to pending (see registry.refresh()).
            if adapter.pending_keys():
                _registry.approve_all(name, actor="system:startup-config")
    await _registry.start_polling()

    # NATS publisher for the event-bus broadcast surface. Starts AFTER
    # the registry so any sovereignty-refused adapters are already
    # audited, and a NATS misconfig surfaces as its own log line.
    # ``NATS_URL=""`` disables publishing entirely (no broadcast;
    # HTTP/WS inference paths unchanged) — see
    # ``kai_c.nats_publisher.NatsPublisher`` for the rationale.
    _nats_publisher = NatsPublisher(
        url=os.getenv("NATS_URL", "").strip() or None,
        # Reuse INTERNAL_API_KEY as the NATS token — operators already
        # manage one secret for KAI-C; tying broadcast auth to the same
        # value avoids a second knob.
        token=INTERNAL_API_KEY or None,
        sovereignty_mode=AI_SOVEREIGNTY,
    )
    try:
        await _nats_publisher.start()
    except Exception as exc:
        logger.warning("NATS publisher startup failed: %s", exc)
        _nats_publisher = NatsPublisher(
            url=None, token=None, sovereignty_mode=AI_SOVEREIGNTY,
        )

    try:
        yield
    finally:
        if _registry is not None:
            await _registry.aclose()
            _registry = None
        if _nats_publisher is not None:
            await _nats_publisher.close()
            _nats_publisher = None


app = FastAPI(
    title="KAI-C (Kavach AI Connector)",
    description="Middleware connector between OpenNVR Kavach and AI Adapters",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS. KAI-C is an internal, server-to-server service: the OpenNVR backend
# reaches it over loopback with X-Internal-Api-Key, and no browser calls it
# directly (it has no host port and no nginx route). So cross-origin browser
# access is CLOSED by default instead of the previous "*" + credentials (which
# is spec-invalid anyway). Set KAI_C_CORS_ORIGINS=https://a,https://b to allow
# specific origins if a browser ever needs direct access.
_cors_origins = [
    o.strip() for o in os.getenv("KAI_C_CORS_ORIGINS", "").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

# correlation_id middleware. Runs on every request so the audit log
# lines below can pull the id off request.state.
app.add_middleware(CorrelationIdMiddleware)


def get_registry() -> AdapterRegistry:
    if _registry is None:  # pragma: no cover — guarded by lifespan
        raise RuntimeError("registry not initialized; lifespan did not run")
    return _registry


def get_audit() -> AuditStore:
    return _audit


def get_nats_publisher() -> NatsPublisher:
    if _nats_publisher is None:  # pragma: no cover — guarded by lifespan
        raise RuntimeError("NATS publisher not initialized; lifespan did not run")
    return _nats_publisher


def get_adapter_url(model_name: str = "default") -> str:
    """Get AI Adapter URL from internal registry."""
    return ADAPTER_REGISTRY.get(model_name, ADAPTER_REGISTRY["default"])


def enforce_legacy_serving_gate(model_name: str) -> None:
    """§8/§11 approval gate for the LEGACY ``/infer`` + ``/infer/local``
    passthroughs, which resolve adapters via the static ``ADAPTER_REGISTRY``
    dict and so bypass ``registry.proxy_infer``'s gate. Fail closed: if the
    live registry knows this adapter and it is not fully approved, refuse
    with 403 and audit ``inference.refused_permission`` — matching the
    governed path. If the live registry has no entry (an adapter that never
    registered with the v2 registry), the legacy escape hatch is preserved.
    """
    registry = _registry
    if registry is None:  # pragma: no cover — guarded by lifespan
        return
    adapter = registry.get(model_name)
    if adapter is None:
        return
    if not adapter.is_serving_allowed:
        pending = adapter.pending_keys()
        get_audit().emit(
            AuditEventType.INFERENCE_REFUSED_PERMISSION,
            adapter=model_name,
            approval_status=adapter.approval_status,
            pending_permissions=pending,
            reason=(
                "legacy inference path refused; adapter is not fully "
                f"approved ({len(pending)} permission(s) await operator grant)"
            ),
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"adapter {model_name!r} is {adapter.approval_status}: "
                f"{len(pending)} declared permission(s) await operator "
                "approval before it may serve inference"
            ),
        )


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


# INTERNAL_API_KEY is defined at module top (next to AI_SOVEREIGNTY) so
# lifespan and the Depends-protected routes share a single binding.


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
    # §8/§11 fail-closed gate (outside the try so its 403 isn't swallowed by
    # the generic 500 handler below): a pending/not-approved adapter must not
    # serve via the legacy path either.
    enforce_legacy_serving_gate(request.model_name)

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

    Accepts TWO request shapes:

      * contract-v1 (current backend default, OPENNVR_ADAPTER_CONTRACT=v1)::

            {"task": "...", "frame_b64": "<base64 jpeg>", **params}

        The frame bytes are already inline, so KAI-C forwards the body to
        the adapter as-is. This is what ``server/services/kai_c_service.py``
        sends via ``build_infer_payload``.

      * legacy URI (OPENNVR_ADAPTER_CONTRACT=legacy)::

            {"task": "...",
             "input": {"frame": {"uri": "opennvr://frames/camera_1/latest.jpg"},
                       "params": {...}}}

        KAI-C resolves the frame URI to local bytes and sends them as
        ``frame_b64``.

    Either way it then:
      * attaches the adapter bearer token (INTERNAL_API_KEY) so the
        adapter's auth stays enforced, and
      * translates the adapter's DetectionResult back into the flat
        {label, confidence, bbox, count} shape the backend stores.

    Flow: OpenNVR Backend -> KAI-C -> AI Adapter -> KAI-C -> OpenNVR Backend
    """
    # §8/§11 fail-closed gate for the legacy local path (outside the try so
    # its 403 survives the generic 500 handler). ``/infer/local`` always
    # resolves the "default" adapter (get_adapter_url() with no arg).
    enforce_legacy_serving_gate("default")

    try:
        adapter_url = get_adapter_url()

        # 1) Build the adapter body from whichever request shape we got.
        req = request or {}
        if "frame_b64" in req:
            # contract-v1: the frame bytes are already inline, so the body
            # is effectively in adapter-contract shape already. Forward
            # every top-level key (``task`` + params) as an inference
            # parameter per the contract; drop only the structural
            # ``input`` key if a caller sends a hybrid body.
            adapter_body = {k: v for k, v in req.items() if k != "input"}
            if not adapter_body.get("frame_b64"):
                raise HTTPException(
                    status_code=400,
                    detail="frame_b64 was provided but empty; nothing to infer on",
                )
            if "confidence" in adapter_body and "confidence_threshold" not in adapter_body:
                adapter_body["confidence_threshold"] = adapter_body.pop("confidence")
        else:
            # legacy URI path. Resolve opennvr://frames/... to a local
            # file KAI-C can read (core mounts shared_frames at
            # FRAMES_DIR), then base64-encode it.
            inp = req.get("input") or {}
            frame = inp.get("frame") or {}
            uri = frame.get("uri") or ""
            params = dict(inp.get("params") or {})

            frames_dir = os.getenv("FRAMES_DIR", "/app/AI-adapters/AIAdapters/frames")
            rel = uri
            if rel.startswith("opennvr://frames/"):
                rel = rel[len("opennvr://frames/"):]
            else:
                rel = rel.replace("opennvr://", "")
            # Resolve symlinks and normalize ``..`` segments before
            # checking containment. Otherwise a frame URI like
            # ``opennvr://frames/../../../etc/passwd`` would resolve to
            # ``/app/AI-adapters/AIAdapters/frames/../../../etc/passwd`` —
            # ``os.path.isfile`` accepts it, ``open()`` reads it, and we'd
            # ship arbitrary host files to the adapter base64-encoded as
            # if they were a camera frame. KAI-C is the security
            # middleware in the architecture; URI inputs from a
            # higher-trust caller (the backend) still get defense-in-depth
            # path validation here.
            frames_root = os.path.realpath(frames_dir)
            frame_path = os.path.realpath(os.path.join(frames_dir, rel))
            if not (
                frame_path == frames_root
                or frame_path.startswith(frames_root + os.sep)
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "frame URI resolves outside the configured frames "
                        "directory; rejecting"
                    ),
                )

            if not os.path.isfile(frame_path):
                raise HTTPException(
                    status_code=400,
                    detail=f"frame not found for inference: {frame_path}",
                )
            with open(frame_path, "rb") as fh:
                frame_b64 = base64.b64encode(fh.read()).decode("ascii")

            # SDK adapter wants frame_b64 + flat params; map the UI's
            # "confidence" to "confidence_threshold".
            if "confidence" in params and "confidence_threshold" not in params:
                params["confidence_threshold"] = params.pop("confidence")
            adapter_body = {"frame_b64": frame_b64, **params}

        headers = {"Content-Type": "application/json"}
        if INTERNAL_API_KEY:
            headers["Authorization"] = f"Bearer {INTERNAL_API_KEY}"

        # 4) Call the adapter
        response = requests.post(
            f"{adapter_url}/infer",
            json=adapter_body,
            headers=headers,
            timeout=60,
        )
        response.raise_for_status()
        adapter_json = response.json()

        # 5) Translate DetectionResult -> flat shape the backend stores.
        #    No detection -> confidence 0.0 placeholder so the backend's
        #    existing "skip zero-confidence" logic fires.
        det = adapter_json.get("result") or {}
        detections = det.get("detections") or []
        if detections:
            top = max(detections, key=lambda d: d.get("confidence") or 0.0)
            bb = top.get("bbox") or {}
            flat = {
                "label": top.get("label"),
                "confidence": top.get("confidence"),
                "bbox": [bb.get("x"), bb.get("y"), bb.get("w"), bb.get("h")],
                "count": len(detections),
                "latency_ms": adapter_json.get("inference_ms"),
            }
        else:
            flat = {
                "confidence": 0.0,
                "latency_ms": adapter_json.get("inference_ms"),
            }

        return {"status": "success", "response": flat}

    except HTTPException:
        raise
    except requests.HTTPError as e:
        raise HTTPException(
            status_code=e.response.status_code if e.response else 500,
            detail=f"AI Adapter error: {str(e)}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"KAI-C processing error: {str(e)}",
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
    # Defense-in-depth: refuse the cloud-provider proxy path in local_only mode
    # so a misconfigured/compromised caller can't route to a vendor by hitting
    # KAI-C directly (the server router already 403s its own routes). See V-022.
    if AI_SOVEREIGNTY == "local_only":
        raise HTTPException(
            status_code=403,
            detail=(
                "Cloud-provider inference disabled: "
                "AI_SOVEREIGNTY=local_only. Set AI_SOVEREIGNTY="
                "federated|cloud_allowed at boot to enable."
            ),
        )

    # Validate internal API key (constant-time; an empty key fails closed
    # unless KAI_C_ALLOW_ANONYMOUS is explicitly set).
    if not _internal_api_key_ok(x_internal_api_key):
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

    # Same bearer token as the /infer path; without it these probes 401
    # once the adapter's 5-minute registration grace window closes.
    cap_headers = (
        {"Authorization": f"Bearer {INTERNAL_API_KEY}"} if INTERNAL_API_KEY else {}
    )
    for name, url in ADAPTER_REGISTRY.items():
        try:
            response = requests.get(
                f"{url}/capabilities", headers=cap_headers, timeout=10
            )
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
    """Auth dependency for the v2 endpoints.

    Delegates to ``_internal_api_key_ok``: with ``INTERNAL_API_KEY`` set a
    constant-time header match is required; with it empty the call is denied
    unless ``KAI_C_ALLOW_ANONYMOUS`` is explicitly set for local dev. (An empty
    key used to fail OPEN — silently disabling auth for the whole surface; it
    now fails closed.)

    All v2 endpoints depend on this so register/deregister/infer cannot
    be reached anonymously by an attacker who finds KAI-C's port open
    on a non-loopback interface.
    """
    if not _internal_api_key_ok(x_internal_api_key):
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


@app.get("/api/v1/adapters-metrics", dependencies=[Depends(require_internal_api_key)])
async def v1_fleet_metrics():
    """The FLEET rollup — every adapter's §05 snapshot in one call, so
    the UI's fleet strip (N ok / worst p95 / total rpm) doesn't fan out
    across N requests. Same payload per adapter as the single-adapter
    endpoint, keyed by name."""
    registry = get_registry()
    out: dict[str, Any] = {}
    for summary in registry.list_summaries():
        name = summary.get("name")
        if not name:
            continue
        adapter = registry.get(name)
        if adapter is None:
            continue
        snap = registry.metrics.snapshot(name)
        snap["max_inflight"] = adapter.capabilities.scheduling.max_inflight
        snap["status"] = summary.get("status")
        out[name] = snap
    return {"adapters": out}


@app.get("/api/v1/adapters/{name}/metrics", dependencies=[Depends(require_internal_api_key)])
async def v1_adapter_metrics(name: str):
    """Observability spec §05 — the windowed rollup for one adapter,
    fed by the /metrics scrape on the existing 60s registry poll.

    Serves p50/p95/p99 latency (ms, derived from the adapter's
    ``adapter_infer_latency_seconds`` histogram), per-outcome counts,
    the latest saturation gauges against the declared
    ``scheduling.max_inflight`` ceiling, and the fingerprint-change
    timeline. All-null fields until the first scrape lands."""
    registry = get_registry()
    adapter = registry.get(name)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"unknown adapter: {name}")
    snapshot = registry.metrics.snapshot(name)
    # The declared ceiling comes from /capabilities, not /metrics — the
    # decision view pairs it with the live inflight gauge (§06).
    snapshot["max_inflight"] = adapter.capabilities.scheduling.max_inflight
    return snapshot


# ── A2.4b: adapter permission-approval endpoints (§8 / §11) ─────────
# The operator-UI approval flow the KAI-C README deferred. All behind
# require_internal_api_key like the rest of the v1 surface. The
# ``actor`` recorded in KAI-C's audit trail is the OpenNVR user, threaded
# through the ``X-Actor`` header the server proxy sets; falls back to a
# generic label when absent (e.g. a direct operator curl).


class PermissionKeysRequest(BaseModel):
    """Body for the grant / revoke endpoints — a list of permission
    keys (see ``registry.permission_keys``)."""
    keys: list[str] = Field(default_factory=list)


def _actor_from_header(x_actor: Optional[str]) -> str:
    return x_actor.strip() if x_actor and x_actor.strip() else "operator"


@app.get(
    "/api/v1/adapters/{name}/permissions",
    dependencies=[Depends(require_internal_api_key)],
)
async def v1_adapter_permissions(name: str):
    """§8 / §11 — the permission view for one adapter: declared keys
    (with human labels, kind, and sovereignty-conflict flags), the
    granted set, the still-pending set, and the derived approval_status.
    404 for an unknown adapter."""
    view = get_registry().permissions_view(name)
    if view is None:
        raise HTTPException(status_code=404, detail=f"unknown adapter: {name}")
    return view


@app.post(
    "/api/v1/adapters/{name}/permissions/grant",
    dependencies=[Depends(require_internal_api_key)],
)
async def v1_adapter_permissions_grant(
    name: str,
    payload: PermissionKeysRequest,
    x_actor: Optional[str] = Header(None),
):
    """Grant a set of declared permission keys. Only keys the adapter
    actually declares take effect. Returns the updated permission view."""
    registry = get_registry()
    try:
        _, grant_id = registry.grant_permissions(
            name, payload.keys, _actor_from_header(x_actor)
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown adapter: {name}")
    view = registry.permissions_view(name)
    view["grant_id"] = grant_id
    return view


@app.post(
    "/api/v1/adapters/{name}/permissions/revoke",
    dependencies=[Depends(require_internal_api_key)],
)
async def v1_adapter_permissions_revoke(
    name: str,
    payload: PermissionKeysRequest,
    x_actor: Optional[str] = Header(None),
):
    """Revoke a set of granted permission keys. Revoking any key flips
    the adapter back to ``pending`` and stops it serving. Returns the
    updated permission view."""
    registry = get_registry()
    try:
        _, grant_id = registry.revoke_permissions(
            name, payload.keys, _actor_from_header(x_actor)
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown adapter: {name}")
    view = registry.permissions_view(name)
    view["grant_id"] = grant_id
    return view


@app.post(
    "/api/v1/adapters/{name}/permissions/approve-all",
    dependencies=[Depends(require_internal_api_key)],
)
async def v1_adapter_permissions_approve_all(
    name: str,
    x_actor: Optional[str] = Header(None),
):
    """Grant every declared permission key — the "approve" button.
    Returns the updated permission view (approval_status="approved"
    unless the adapter re-declares more scope before this lands)."""
    registry = get_registry()
    try:
        _, grant_id = registry.approve_all(name, _actor_from_header(x_actor))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown adapter: {name}")
    view = registry.permissions_view(name)
    view["grant_id"] = grant_id
    return view


# ── B1 NATS publishing helper ──────────────────────────────────────


async def _publish_inference_completed(
    *,
    adapter_name: str,
    adapter: Any,
    camera_id: Optional[str],
    correlation_id: str,
    latency_ms: int,
    body: Dict[str, Any],
) -> None:
    """Build an ``InferenceCompletedEvent`` from the response body the
    adapter returned and publish it on NATS. Shared by HTTP and WS
    success paths so the broadcast surface is identical regardless of
    transport.

    Always best-effort — the underlying ``NatsPublisher.publish_inference_
    completed`` swallows all errors and logs at WARNING. Calling this
    function never raises.
    """
    publisher = _nats_publisher
    if publisher is None or not publisher.enabled:
        return
    # ``body`` is the §3.5 InferResponse the adapter returned. Pull
    # out the model fields with defensive defaults — a misbehaving
    # adapter could omit them, in which case we publish what we have
    # rather than dropping the event.
    if not isinstance(body, dict):
        return
    try:
        model_name = str(body.get("model_name") or "")
        model_version = str(body.get("model_version") or "")
        # Adapter capabilities snapshot — fingerprint is the most
        # operationally valuable field (§11.3 drift detection).
        fingerprint = None
        try:
            fingerprint = adapter.capabilities.model.fingerprint
        except Exception:  # noqa: BLE001
            pass
        adapter_version = None
        try:
            adapter_version = adapter.capabilities.adapter.version
        except Exception:  # noqa: BLE001
            pass

        # ``inference_ms`` is the ADAPTER's measurement (§3.5). Don't
        # silently fall back to KAI-C's round-trip ``latency_ms`` —
        # that's a different semantic (includes network) and would
        # confuse subscribers doing latency analytics. Missing → 0.
        # (Peer review L4.)
        event = InferenceCompletedEvent(
            correlation_id=correlation_id,
            adapter=adapter_name,
            adapter_version=adapter_version,
            camera_id=camera_id,
            model_name=model_name,
            model_version=model_version,
            model_fingerprint=fingerprint,
            inference_ms=int(body.get("inference_ms") or 0),
            result=body.get("result") or {},
        )
    except Exception as exc:  # noqa: BLE001
        # Pydantic validation can fail if the adapter returned a
        # malformed shape. Don't break the request — log + skip.
        logger.warning(
            "NATS broadcast skipped — event-build failure: %s "
            "[correlation_id=%s adapter=%s]",
            exc, correlation_id, adapter_name,
        )
        return
    await publisher.publish_inference_completed(event)


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
    except PermissionError as exc:
        # §8 / §11 approval gate — fail closed. The registry already
        # emitted inference.refused_permission (it owns the pending-key
        # detail); we just translate to a 403 for the caller.
        raise HTTPException(status_code=403, detail=str(exc))
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
        # B1: broadcast on NATS. Best-effort — publish failures are
        # logged + counted but never raise. Fire-and-forget via
        # ``asyncio.create_task`` so a slow NATS connect doesn't
        # delay the HTTP response. (Peer review H1 — earlier we
        # ``await``ed the publish which could add up to 5 s of
        # latency on the first request after a NATS outage.)
        asyncio.create_task(_publish_inference_completed(
            adapter_name=adapter_name,
            adapter=adapter,
            camera_id=camera_id,
            correlation_id=correlation_id,
            latency_ms=latency_ms,
            body=body,
        ))
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


# ── /api/v1/infer/{adapter_name}/stream — §6 WebSocket proxy ───────


@app.websocket("/api/v1/infer/{adapter_name}/stream")
async def v1_infer_stream(websocket: WebSocket, adapter_name: str):
    """KAI-C WebSocket streaming proxy (§6) — bridges a monitoring app
    to a registered adapter's ``/infer/stream``.

    Auth (inbound): ``X-Internal-Api-Key`` header on the WS upgrade.
    FastAPI's ``BaseHTTPMiddleware`` doesn't run on WS upgrades and
    the ``Depends(require_internal_api_key)`` pattern raises
    ``HTTPException`` which doesn't translate cleanly to a WS close —
    so we check the header explicitly and close 4001 on failure.

    Correlation_id is read from the ``X-Correlation-Id`` header (the
    HTTP middleware doesn't run on WS upgrades either) and minted if
    absent. Threaded to the adapter via the upstream WS connect
    headers; the adapter's SDK echoes it back on the HTTP control
    plane and includes it in all log lines.
    """
    # Inbound auth — same allow-open-in-dev pattern as the HTTP path.
    # Defensive: require both INTERNAL_API_KEY to be set AND the
    # supplied header to non-empty-match. (Peer review H3 — guards
    # against a future code path that sets INTERNAL_API_KEY=None.)
    supplied = websocket.headers.get("x-internal-api-key")
    if not _internal_api_key_ok(supplied):
        await websocket.close(
            code=CLOSE_POLICY_REFUSED,
            reason="unauthorized",
        )
        return

    registry = get_registry()
    adapter = registry.get(adapter_name)
    if adapter is None:
        # Reject the upgrade with policy_refused. We don't accept then
        # close because the client gets a cleaner error this way.
        await websocket.close(
            code=CLOSE_POLICY_REFUSED,
            reason=f"unknown adapter: {adapter_name}",
        )
        return
    if not adapter.capabilities.endpoints.infer_stream.supported:
        # The adapter declared no streaming support. §3.6 + §6 say the
        # right behaviour is HTTP 501 from the HTTP probe, but we're
        # already on WS — close with model_error.
        await websocket.close(
            code=CLOSE_MODEL_ERROR,
            reason="adapter does not support streaming",
        )
        return

    correlation_id = (
        websocket.headers.get(CORRELATION_ID_HEADER.lower())
        or new_correlation_id()
    )

    if not adapter.is_serving_allowed:
        # §8 / §11 approval gate — fail closed on the streaming path too.
        # A pending / not-fully-approved adapter never streams. Audit the
        # refusal (same event type as the HTTP path) and close with
        # policy_refused.
        pending = adapter.pending_keys()
        get_audit().emit(
            AuditEventType.INFERENCE_REFUSED_PERMISSION,
            correlation_id=correlation_id,
            adapter=adapter_name,
            approval_status=adapter.approval_status,
            pending_permissions=pending,
            reason="adapter is not fully approved; streaming refused",
        )
        await websocket.close(
            code=CLOSE_POLICY_REFUSED,
            reason=f"adapter {adapter_name} awaiting operator approval",
        )
        return

    proxy = StreamProxy(
        client_ws=websocket,
        adapter_name=adapter_name,
        adapter_url=str(adapter.url),
        correlation_id=correlation_id,
        audit=get_audit(),
        # B1 — pass the broadcast publisher + adapter so per-frame
        # results fan out on NATS as they're relayed back to the
        # client. ``nats_publisher`` may be a disabled publisher (no
        # URL configured), in which case the proxy skips the broadcast.
        nats_publisher=_nats_publisher,
        adapter_info=adapter,
    )
    await proxy.run()


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
