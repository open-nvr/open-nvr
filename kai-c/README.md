# KAI-C (Kavach AI Connector)

KAI-C is the middleware layer between the OpenNVR NVR backend and the AI Adapters engine. The backend never talks to AIAdapters directly — KAI-C handles routing, URL management, authentication, response standardization, **audit logging, sovereignty enforcement, and fingerprint-drift detection**.

> **Design direction:** the target architecture for KAI-C — control plane
> with capability tokens, no proxy in the inference data path — is specified
> in [DESIGN.md](DESIGN.md) (decision record: RFC-0001, Challenge 2). This
> README documents the CURRENT v2.x behaviour.

## v2.0 — registry, audit, and the trust contract

As of v2.0 KAI-C is the registry and audit layer per §11 of the [AI Adapter Contract v1](../docs/AI_ADAPTER_CONTRACT.md). It still works as a thin proxy for the legacy endpoints; the new behaviour is layered on top.

**What's new:**

- **Adapter registry** (`kai_c/registry.py`) — polls each adapter's `/capabilities` on registration + every 60s. Caches capabilities, fingerprint, health. Detects drift between polls.
- **Audit log** (`kai_c/audit.py`) — append-only JSONL store. Every registration, every inference, every sovereignty refusal, every fingerprint mismatch is recorded. Queryable from `/api/v1/audit`.
- **Sovereignty v2** (`kai_c/sovereignty.py`) — under `local_only`, refuses adapters whose declared `permissions.network_egress` is non-empty (cloud-proxy adapters). Under `federated`, refuses wildcard egress entries. Re-checks on every poll so a runtime drift de-registers the adapter.
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
| `permissions.*` adds a new permission | **BLOCKING** — adapter flips to `pending`, stops serving, awaits operator re-approval (kept visible in the registry) |
| `permissions.network_egress` violates `local_only` | De-register + `inference.refused_sovereignty` audit |
| `endpoints.*` | Audit, no action |
| `scheduling.*` | Apply silently |

**Audit event vocabulary (subset for v1)**: `adapter.registered`, `adapter.deregistered`, `adapter.fingerprint_mismatch`, `adapter.capability_drift`, `adapter.unavailable`, `inference.completed`, `inference.failed`, `inference.refused_sovereignty`. JSONL format on disk at `$KAI_C_AUDIT_LOG` (defaults to `/var/log/opennvr/kai-c-audit.jsonl`).

**Deferred to a future release** (tracked against the [AI Adapter Contract](../docs/AI_ADAPTER_CONTRACT.md) §11): NATS / SIEM forwarding, hash-chained audit integrity, fair-queuing per-camera token bucket implementation, full WS streaming proxy.

## Permission approval — the §8 operator gate (shipped)

The A2.4b operator approval flow is implemented, **fail-closed**:

* An adapter that declares any permission (`gpu`, `network_egress:<host>`,
  `host_filesystem:<path>`, `shared_memory_paths:<path>`, `host_metadata`)
  registers into **`pending`** — stored, polled, visible — and cannot serve
  a single inference until an operator grants. Enforced on *every* serving
  path: governed `/api/v1/infer/{name}`, the WS stream (close 4001), and
  the legacy `/infer` + `/infer/local` passthroughs.
* Grants are **per-scope keys** (one per egress host / fs path), issued via
  `GET/POST /api/v1/adapters/{name}/permissions[/grant|/revoke|/approve-all]`,
  each audited with a unique `adapter_grant_id`, the actor, and a timestamp —
  incident review answers "who allowed this adapter to reach the internet?"
  with a receipt.
* **Model-tamper containment:** if a running, approved adapter starts
  declaring a new permission on a later poll (the signature of a swapped or
  compromised model service), it flips back to `pending` and stops serving
  immediately; removed permissions prune their stale grants so a re-added
  scope needs fresh approval.
* **Zero-friction default:** bundled adapters that declare no permissions
  (blip, insightface, bytetrack, fast-plate-ocr, whisper, piper) auto-approve.
  yolov8 currently declares `gpu` + a weights path, so it meets the gate —
  covered by the startup-config auto-grant (config-as-consent, audited as
  actor `system:startup-config` — shipped, contract §8.5), plus a
  build-accurate declaration fix in ai-adapter (the CPU build should declare
  `gpu: false`). The gate binds exactly where risk enters: third-party /
  cloud / elevated-scope adapters added at runtime.

To our knowledge this per-scope, fail-closed, operator-receipted permission
model for AI inference services is unique among IP-camera / NVR platforms —
it is a core part of the sovereignty story (see
`docs/SECURITY_ARCHITECTURE.md` §2 and the paper).

### Known limitation — legacy `/infer` is unaudited

The legacy endpoints `/infer`, `/infer/local`, and `/infer/cloud` (kept for OpenNVR backend back-compat) **bypass the new audit + registry pipeline**. They read the static `ADAPTER_REGISTRY` env-derived dict, not the live registry; they don't thread `X-Correlation-Id`; they don't emit `inference.completed` / `inference.failed` events.

**Consequence**: the legacy path lacks correlation IDs and completed/failed audit events. (The §8 permission gate DOES cover it — a pending adapter is refused on legacy `/infer` + `/infer/local` too, with an `inference.refused_permission` audit event.) The startup-time loopback check still applies, so the legacy path is safe for the URL-loopback story — it just doesn't get the audit story.

**Fix path**: migrate OpenNVR backend onto `POST /api/v1/infer/{adapter_name}`, which has full audit + sovereignty + correlation_id. Once nothing uses legacy `/infer`, those endpoints retire.

**Status (Jul 2026)**: the OpenNVR backend now defaults to the governed path (`OPENNVR_ADAPTER_CONTRACT=governed` in `server/services/kai_c_service.py`); `v1` and `legacy` remain as opt-out escape hatches. The legacy endpoints stay until those escape hatches are retired.





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
