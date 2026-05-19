# KAI-C (Kavach AI Connector)

KAI-C is the middleware layer between the OpenNVR NVR backend and the AI Adapters engine. The backend never talks to AIAdapters directly — KAI-C handles routing, URL management, authentication, response standardization, **audit logging, sovereignty enforcement, and fingerprint-drift detection**.

## v2.0 (A2.4) — registry, audit, and the trust contract

As of A2.4 KAI-C is the registry and audit layer per §11 of the [AI Adapter Contract v1](../docs/AI_ADAPTER_CONTRACT.md). It still works as a thin proxy for the legacy endpoints; the new behaviour is layered on top.

**What's new:**

- **Adapter registry** (`kai_c/registry.py`) — polls each adapter's `/capabilities` on registration + every 60s. Caches capabilities, fingerprint, health. Detects drift between polls.
- **Audit log** (`kai_c/audit.py`) — append-only JSONL store. Every registration, every inference, every sovereignty refusal, every fingerprint mismatch is recorded. Queryable from `/api/v1/audit`.
- **Sovereignty v2** (`kai_c/sovereignty.py`) — V-022 tightening: under `local_only`, refuses adapters whose declared `permissions.network_egress` is non-empty (cloud-proxy adapters). Under `federated`, refuses wildcard egress entries. Re-checks on every poll so a runtime drift de-registers the adapter.
- **Correlation IDs** (`kai_c/correlation.py`) — every inbound request gets a `X-Correlation-Id` (minted if absent), threaded through to the adapter, echoed in the response, and stamped on every audit event for the request. One id joins logs across KAI-C, the adapter, and downstream consumers.

**New v1 endpoints** (legacy endpoints unchanged for back-compat):

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/adapters/register` | Register an adapter URL → polls /capabilities → runs sovereignty check → stores |
| `DELETE /api/v1/adapters/{name}` | Deregister |
| `GET  /api/v1/adapters` | Lightweight adapter summaries for the UI |
| `GET  /api/v1/ai/capabilities` | Aggregated capabilities (§11 — single call for the UI to render every adapter) |
| `POST /api/v1/adapters/refresh` | Force-refresh /capabilities + /health |
| `GET  /api/v1/audit` | Query the audit log (filters: adapter, event_type, camera_id, since, limit) |
| `POST /api/v1/infer/{adapter}` | Contract-compliant proxy with correlation_id threading + audit emission |

**Drift handling matches §11.3:**

| Field that changed | Action |
|---|---|
| `model.fingerprint` | `adapter.fingerprint_mismatch` audit, keep serving (operator decides) |
| `model.version` | `adapter.capability_drift` audit, keep serving |
| `permissions.*` adds a new permission | **BLOCKING** — de-register adapter, await re-approval |
| `permissions.network_egress` violates `local_only` | De-register + `inference.refused_sovereignty` audit |
| `endpoints.*` | Audit, no action |
| `scheduling.*` | Apply silently |

**Audit event vocabulary (subset for v1)**: `adapter.registered`, `adapter.deregistered`, `adapter.fingerprint_mismatch`, `adapter.capability_drift`, `adapter.unavailable`, `inference.completed`, `inference.failed`, `inference.refused_sovereignty`. JSONL format on disk at `$KAI_C_AUDIT_LOG` (defaults to `/var/log/opennvr/kai-c-audit.jsonl`).

**Deferred to follow-up** (documented in the design doc §11): NATS / SIEM forwarding, operator-UI approval flow, hash-chained audit integrity (v1.5), fair-queuing per-camera token bucket implementation, full WS streaming proxy.

### Known limitation — legacy `/infer` is unaudited

The legacy endpoints `/infer`, `/infer/local`, and `/infer/cloud` (kept for OpenNVR backend back-compat) **bypass the new audit + registry pipeline**. They read the static `ADAPTER_REGISTRY` env-derived dict, not the live registry; they don't thread `X-Correlation-Id`; they don't emit `inference.completed` / `inference.failed` events.

**Consequence**: an adapter de-registered by the new registry (e.g., for permission drift in §11.3) is still reachable via the legacy `/infer` path because that endpoint only sees the static config. The startup-time loopback check (V-022 M1a) still applies, so the legacy path is safe for the URL-loopback story — it just doesn't get the audit story.

**Fix path**: migrate OpenNVR backend onto `POST /api/v1/infer/{adapter_name}`, which has full audit + sovereignty + correlation_id. Once nothing uses legacy `/infer`, those endpoints retire. Tracked as A2.5.





## Why KAI-C Exists

```
Without KAI-C:     Backend ──(must know adapter URL)──> AIAdapters
With KAI-C:        Backend ──> KAI-C ──(manages URLs internally)──> AIAdapters
```

- Backend developers don't need to know or configure AI adapter URLs
- KAI-C can route to multiple adapters (different models on different ports)
- Single place to add auth, logging, and response normalization
- Cloud inference (HuggingFace) is handled transparently

## Project Structure

```
kai-c/
├── main.py              # FastAPI server (entry point, runs on port 8100)
├── kai_c/               # Python package
│   ├── __init__.py
│   ├── connector.py     # KaiConnector -- sends requests to AI Adapters
│   └── schemas.py       # KAIRequest Pydantic model (data validation)
├── test/
│   └── test.py          # Demo script: webcam -> KAI-C -> AI Adapter
├── start.py             # Development server launcher (with reload)
├── start_no_reload.py   # Production server launcher
├── pyproject.toml       # Python dependencies (uv)
├── Dockerfile           # Container build
├── .env.example         # Example environment variables
└── START_INSTRUCTIONS.md
```

## Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Make sure AIAdapters is running on port 9100

# 3. Start KAI-C
python main.py
# Runs on http://localhost:8100
# API docs at http://localhost:8100/docs
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Service info and configured adapters |
| `GET` | `/health` | KAI-C liveness check |
| `GET` | `/adapters/health` | Check health of all registered AI adapters |
| `GET` | `/capabilities` | Get capabilities from all adapters |
| `GET` | `/schema` | Get response schemas (proxied from adapter) |
| `POST` | `/infer` | Run inference via connector (structured request) |
| `POST` | `/infer/local` | Forward raw request to adapter (pass-through) |
| `POST` | `/infer/cloud` | Cloud inference (HuggingFace) with auth |

## How It Works

### Local Inference Flow

```
1. Backend sends POST /infer to KAI-C (port 8100)
   {
     "camera_id": "cam_1",
     "stream_url": "opennvr://frames/cam_1/latest.jpg",
     "model_name": "yolov8",
     "task": "person_detection",
     "options": {}
   }

2. KAI-C looks up adapter URL from ADAPTER_REGISTRY
   (backend never sees this URL)

3. KAI-C reformats and forwards to AIAdapters (port 9100)
   POST http://localhost:9100/infer

4. AIAdapters returns JSON result

5. KAI-C wraps in standard response and returns to backend
```

### Cloud Inference Flow

```
1. Backend sends POST /infer/cloud
   {
     "provider": "huggingface",
     "model_name": "google/vit-base-patch16-224",
     "task": "image-classification",
     "inputs": {"image": "opennvr://frames/cam_1/latest.jpg"},
     "credential_token": "hf_token_here"
   }

2. KAI-C validates internal API key

3. KAI-C formats and sends to AIAdapters' cloud handler

4. AIAdapters calls HuggingFace API

5. Result flows back: HF -> AIAdapters -> KAI-C -> Backend
```

## Key Files Explained

### main.py

The FastAPI server. Contains:
- **ADAPTER_REGISTRY** -- maps model names to adapter URLs (internal, never exposed to users)
- `/infer` -- structured inference via `KaiConnector`
- `/infer/local` -- raw pass-through to adapter
- `/infer/cloud` -- cloud inference with provider routing and auth

### kai_c/connector.py

`KaiConnector` class that takes a `KAIRequest`, formats the payload, and POSTs to the AI adapter's `/infer` endpoint. Handles errors and returns standardized responses.

### kai_c/schemas.py

Pydantic model `KAIRequest` with fields: `camera_id`, `stream_url`, `model_name`, `task`, `options`. Validates data before sending to the adapter.

## Configuration

### Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `ADAPTER_URL` | Default AI Adapter URL | `http://localhost:9100` |
| `INTERNAL_API_KEY` | Auth key for cloud inference | (empty = no auth) |

### Adapter Registry

In `main.py`, the `ADAPTER_REGISTRY` dict maps model names to URLs:

```python
ADAPTER_REGISTRY = {
    "default": "http://localhost:9100",
    # "yolov8": "http://localhost:9100",
    # "blip": "http://localhost:9101",
}
```

To add a second adapter running on a different port, add an entry here. The model_name in the request selects which adapter to use.
