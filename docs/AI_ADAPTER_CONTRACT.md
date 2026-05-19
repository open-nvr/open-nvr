# AI Adapter Contract — design (A1)

> **Status.** Draft for review. Locks the HTTP/WebSocket surface every
> AI Adapter must implement so OpenNVR / KAI-C / example apps can speak
> to any model — first-party or community-contributed — without
> coordinating on a per-model basis. No code in this revision is wired
> up; this document is the load-bearing artefact reviewers should
> critique before any adapter migration begins.
>
> **What lives outside this doc.** NATS event bus (B1), Redis caches
> (B2), MCP server (D1), pgvector + RAG (D2/D3), example apps
> (C1/C2). Each is named in the milestone plan and will get its own
> design doc when its turn comes.

---

## 0. TL;DR — what every reader gets in one screen

**If you're an operator/org:** existing cameras (Hikvision, Dahua,
Axis, Reolink, generic ONVIF / RTSP) integrate via the OpenNVR
camera wizard — auto-discovery, transport-security probe, one stable
UUID per camera that adapters and apps will use forever (§12.1).
Every adapter declares permissions up front (GPU? Network egress?
Filesystem paths?), needs your approval on first registration, runs
inside a sandbox that enforces the approved list, and writes an
immutable audit trail of every inference and every policy refusal
that you can search from the UI and forward to your Splunk/ELK/Syslog
(§11.2). The narrower subset that needs human attention — sovereignty
refusals, fingerprint mismatches, app-fired "person in restricted
zone" — routes to UI badges, Slack, Discord, PagerDuty, email
(§11.5). If a model gets swapped under you, KAI-C tells you. If
sovereignty says "local_only", no adapter that declared network
egress can register. This is the single trust contract.

**If you're a developer:** you implement six HTTP endpoints (with one
WebSocket) and you get sandboxing, fair queuing per camera, audit
logging, sovereignty enforcement, blue-green deploys, alerting
plumbing, and operator UX for free. The minimum viable adapter is
~30 lines of FastAPI (§3.7); a production adapter adds bearer-token
auth in another ~10 lines (§3.8). Point the conformance kit at your
service; if green, KAI-C accepts it (§11.4). Two SDKs are shipped:
`opennvr-adapter-sdk` for wrapping a model, `opennvr-app-sdk` for
*using* adapters to build an app (camera discovery, inference
streaming, alert emission). The contract is intentionally weakly
typed where it can be (free-form `tasks_advertised`, free-form
`result` shapes) so you're never bottlenecked on us. Adapters and
apps can live in your own repos — the community catalogue (§12.4)
lists conforming entries; you keep ownership and get a "Conforming
to OpenNVR Adapter Contract v1" badge.

**If you're a security reviewer:** §3.8 (auth + correlation),
§8 (permissions + sandbox), §11.1 (sovereignty enforcement),
§11.2 (audit trail + SIEM export), §11.3 (capability drift), and
§11.5 (alert routing) are the six contracts that turn "an adapter
ran" into "and here is who let it run, what it asked for, what it
actually did, and how the operator was notified." Hash-chained
audit integrity is v1.5.

**If you're an organization planning a deployment:** the use-case
catalogue in §12.2 lists 12 named examples covering intrusion
detection, package theft, LPR, loitering, PPE compliance, fall
detection, fire/smoke, crowd counting, camera health, forensic
search, and the watchman voice agent. C1/C2 ship first; the rest
are the contribution roadmap.

---

## 1. The bet

The single architectural bet we are making is: **one adapter wraps one
model and exposes that model's capabilities as a stable HTTP/WebSocket
surface.** Anyone — Anthropic, a research lab, a community hobbyist —
who can write a FastAPI service (or a Go service, or a Rust service)
can author an adapter. The contract is the only thing that binds them.

What is *not* in this bet: where the model runs, who hosts it, which
framework, what hardware. The HuggingFace adapter that proxies to a
remote endpoint, a YOLO adapter that loads weights into local GPU
memory, and a hypothetical Triton sidecar that fronts a 70B model on a
remote GPU server are all conformant adapters as long as their HTTP
surface is the same. Their internals are not our concern.

What we get for accepting this constraint: a single integration point
between OpenNVR and any model, a clear contribution lane for the
community, and a stable target that example apps can build against.
What we give up: the fastest possible inference path for in-process
plugin models. The shared-memory fast path described in §6 buys most
of that performance back when the adapter is co-located with its
caller.

## 2. Layers (mental model)

```
┌─────────────────────────────────────────────────────────────────┐
│                    Example apps & agents                         │
│      Each one is a separate program in examples/. They           │
│      consume adapter HTTP/WS, NATS subjects, and NVR APIs.       │
└──────────────┬──────────────────────────────┬───────────────────┘
               │                              │
       ┌───────▼──────────┐         ┌────────▼─────────┐
       │  OpenNVR backend │         │   MCP server     │
       │   + KAI-C        │◀────────│   (in-process    │
       │  (registry +     │         │    with backend) │
       │  policy + cache) │         └──────────────────┘
       └───────┬──────────┘
               │
       HTTP control plane (mandatory)        ┌───────────────┐
       WebSocket streaming  (mandatory)      │  NATS event   │
       Shared-memory frame transport         │  bus (B1)     │
        (optional, negotiated)               │  detections,  │
               │                             │  commands     │
   ┌───────────▼──────────────────────────┐  └───────────────┘
   │   AI Adapters                         │
   │  one model each; HTTP+WS contract;    │
   │  self-hosted local OR cloud-proxy     │
   │  OR service-mesh fan-out;             │
   │  language-agnostic by contract        │
   └───────────────────────────────────────┘
```

## 3. The contract — mandatory endpoints

Every adapter MUST expose the following HTTP endpoints. The shapes
below are the v1 wire format; later revisions of this contract will
bump a version number and adapters can advertise which versions they
speak (see §10).

### 3.1 `GET /health`

Liveness + identity. No auth. Adapter is expected to return a 200
within 1 second of being asked.

```json
{
  "status": "ok",
  "adapter_name": "yolov8-person-detection",
  "adapter_version": "1.4.0",
  "model_name": "yolov8n.onnx",
  "model_version": "8.0.196",
  "started_at": "2026-05-18T03:00:00Z",
  "uptime_seconds": 12345
}
```

Status values: `ok`, `degraded` (working but slow / partial), `loading`
(model loading, not ready yet), `error` (alive but broken).

### 3.2 `GET /capabilities`

The most important endpoint. What this adapter *can do*, structured
enough for KAI-C and OpenNVR UI to render, free-form enough for exotic
models to fit. See §4 for the full shape.

### 3.3 `GET /hardware/evaluation`

Verdict + reasoning for "can this adapter serve from where it is
deployed". The adapter decides how to compute the verdict — local
hardware probe, ping a cloud endpoint, check service-mesh health,
look at its own model load status. The contract only standardizes the
response shape.

```json
{
  "verdict": "ok",                  // "ok" | "warn" | "blocked"
  "reasoning": "GPU detected, model loaded, 4.2GB VRAM free",
  "checked_at": "2026-05-18T03:00:00Z",
  "details": {
    "gpu_available": true,
    "gpu_name": "NVIDIA RTX 3060",
    "vram_total_mb": 12288,
    "vram_free_mb": 4307,
    "cuda_version": "12.1"
  }
}
```

The `details` field is free-form per adapter. A cloud-proxying adapter
would put `endpoint_reachable`, `auth_valid`, `measured_latency_ms`,
`rate_limit_headroom_pct` there. OpenNVR UI shows `verdict` +
`reasoning` to the operator and only drills into `details` on request.

### 3.4 `GET /metrics`

Prometheus exposition format. Mandatory because we should never have
to retrofit observability. Minimum baseline metrics:

- `adapter_infer_total{outcome}` (`outcome ∈ {ok, model_error, provider_error, transport_error, refused}`)
- `adapter_infer_latency_seconds` (histogram)
- `adapter_model_loaded` (gauge, 0/1)
- `adapter_stream_connections_active` (gauge)
- `adapter_inflight_requests` (gauge)
- `adapter_queue_depth` (gauge) — number of requests waiting for the model

Adapter-specific metrics are encouraged. KAI-C will scrape from each
registered adapter; OpenNVR-side dashboards aggregate.

### 3.5 `POST /infer`

Single-shot inference, request/response. Default content-type:
`multipart/form-data` because most inference inputs include binary
data (images, audio).

```
POST /infer
Content-Type: multipart/form-data; boundary=...

--...
Content-Disposition: form-data; name="frame"; filename="capture.jpg"
Content-Type: image/jpeg

<bytes>
--...
Content-Disposition: form-data; name="params"
Content-Type: application/json

{
  "confidence_threshold": 0.45,
  "classes": ["person", "vehicle"]
}
--...
```

Adapters MAY additionally accept `application/json` with base64-encoded
binary fields for clients that can't do multipart. They MUST accept
multipart.

Response is always JSON. Successful shape:

```json
{
  "status": "ok",
  "model_name": "yolov8n",
  "model_version": "8.0.196",
  "inference_ms": 23,
  "result": { /* free-form per adapter, see §5 for guidance */ }
}
```

Error shape: see §7 (failure envelope).

### 3.6 `POST /infer/stream` (WebSocket)

Continuous bidirectional inference for camera feeds / audio streams /
LLM token streaming. Required because the agent and intrusion-detection
example apps depend on it; adapters whose models genuinely don't
support streaming MUST refuse the WebSocket upgrade with HTTP 501
(*before* the socket opens) and MUST declare
`endpoints.infer_stream.supported = false` in `/capabilities`. The
4xxx close codes in §6.5 apply only to adapters that *accepted* the
upgrade and are terminating mid-stream.

See §6 for the full protocol including the optional shared-memory
fast path.

### 3.7 Minimum viable adapter

The smallest legal adapter is ~30 lines. Drop this into any FastAPI
project and you have something KAI-C will accept:

```python
# my_adapter.py
from datetime import datetime, timezone
from fastapi import FastAPI

app = FastAPI()
STARTED_AT = datetime.now(timezone.utc)

@app.get("/health")
def health():
    return {
        "status": "ok",
        "adapter_name": "hello-adapter",
        "adapter_version": "0.1.0",
        "model_name": "hello-echo",
        "model_version": "1",
        "started_at": STARTED_AT.isoformat(),
        "uptime_seconds": int((datetime.now(timezone.utc) - STARTED_AT).total_seconds()),
    }

@app.get("/capabilities")
def capabilities():
    return {
      "adapter": {"name": "hello-adapter", "version": "0.1.0",
                  "vendor": "you", "license": "MIT",
                  "supported_contract_versions": ["1"]},
      "model":   {"name": "hello-echo", "version": "1",
                  "framework": "none", "modalities_in": ["text"],
                  "modalities_out": ["text"]},
      "endpoints": {
        "infer":        {"supported": True,
                         "input_content_types": ["application/json"]},
        "infer_stream": {"supported": False, "max_concurrent_streams": 0},
      },
      "tasks_advertised": ["echo"],
      "scheduling": {"max_inflight": 1},
    }

@app.get("/hardware/evaluation")
def hwe():
    return {"verdict": "ok", "reasoning": "no hardware required",
            "checked_at": datetime.now(timezone.utc).isoformat(), "details": {}}

@app.get("/metrics")
def metrics():
    return "adapter_infer_total 0\nadapter_model_loaded 1\n"

@app.post("/infer")
def infer(payload: dict):
    return {"status": "ok", "model_name": "hello-echo", "model_version": "1",
            "inference_ms": 0, "result": {"echoed": payload}}
```

Everything else (permissions, cost, fair queuing, streaming) you opt
*into* — defaults are safe. Streaming is unsupported here by design;
KAI-C accepts it.

### 3.8 Authentication + correlation_id

**Authentication.** Every HTTP request from KAI-C carries an
`Authorization: Bearer <token>` header. The token is minted by KAI-C
at adapter registration and rotated on operator request. Adapters
MUST validate the token on `/infer`, `/infer/stream`, and `/metrics`
and MAY skip it on `/health` (so KAI-C can probe liveness before
re-issuing a token). `/capabilities` and `/hardware/evaluation` MUST
accept *either* the registered token *or* an unauthenticated probe
during the initial registration window (5 minutes after the adapter
URL first becomes reachable). After the window closes, all endpoints
except `/health` require the token.

The minimum viable adapter in §3.7 omits auth for brevity. A
production adapter looks like:

```python
from fastapi import Depends, Header, HTTPException

EXPECTED_TOKEN = os.environ["OPENNVR_ADAPTER_TOKEN"]  # set by KAI-C

def require_token(authorization: str = Header(None)):
    if authorization != f"Bearer {EXPECTED_TOKEN}":
        raise HTTPException(401, "invalid token")

@app.post("/infer", dependencies=[Depends(require_token)])
def infer(...): ...
```

KAI-C delivers the token to the adapter container via the
environment variable `OPENNVR_ADAPTER_TOKEN`. Adapters that prefer
mTLS over bearer tokens MAY declare
`adapter.auth = "mtls"` in capabilities; the KAI-C registry flow
provisions a per-adapter client certificate.

**correlation_id wire spec.** Every request from KAI-C carries an
`X-Correlation-Id: <uuid>` header (HTTP) or `correlation_id` field
(WebSocket handshake). The adapter:

- Echoes the value in every response (HTTP response header or
  per-message field in WS `result`/`result_ack`/`close`).
- Logs the value with every internal log line tied to that request.
- If propagating downstream (e.g., cloud-fronting adapter making
  outbound calls), passes it through.

This is the single identifier that joins audit-log lines across
KAI-C, the adapter, NATS subscribers, and example apps. Without it
the §11.2 audit story is unimplementable.

**Body/frame size limits.** Default request body limit is 32 MiB for
`/infer` and 8 MiB per WS frame. Adapters MAY advertise lower
limits via `endpoints.infer.max_body_bytes` and
`endpoints.infer_stream.max_frame_bytes`; KAI-C enforces.

## 4. The `/capabilities` shape

This is the only endpoint reviewers should scrutinize hardest, because
it is what every consumer learns the adapter from. The shape needs to
be expressive enough for exotic models, structured enough for the UI.

```json
{
  "adapter": {
    "name": "yolov8-person-detection",
    "version": "1.4.0",
    "vendor": "open-nvr",
    "license": "AGPL-3.0",
    "model_card_url": "https://github.com/ultralytics/ultralytics",
    "supported_contract_versions": ["1"]
  },
  "model": {
    "name": "yolov8n",
    "version": "8.0.196",
    "framework": "ultralytics",
    "size_mb": 6.2,
    "modalities_in": ["image"],
    "modalities_out": ["bbox_classes"],
    "fingerprint": "sha256:c4f3a1...e7"   // optional but strongly recommended
  },
  "endpoints": {
    "infer": {
      "supported": true,
      "input_content_types": ["multipart/form-data", "application/json"],
      "input_schema_ref": "/schema/infer",
      "output_schema_ref": "/schema/infer/response"
    },
    "infer_stream": {
      "supported": true,
      "max_concurrent_streams": 16,
      "supports_shared_memory": true,
      "shared_memory_protocol_version": 1
    },
    "extra": [
      { "path": "/track", "method": "POST",  "purpose": "multi-object tracking across frames" },
      { "path": "/classes", "method": "GET", "purpose": "list known class labels" }
    ]
  },
  "tasks_advertised": ["object_detection"],
  "permissions": {
    "gpu": true,
    "network_egress": [],
    "host_filesystem": [],
    "shared_memory_paths": ["/dev/shm/opennvr/frames"]
  },
  "scheduling": {
    "max_inflight": 8,
    "preferred_batch_size": 4,
    "fair_queuing": "per_camera"
  },
  "cost": {
    "currency": "USD",
    "estimated_per_call": 0.0,
    "estimated_per_hour": 0.0,
    "rate_limit_per_minute": null,
    "is_metered": false
  }
}
```

A cloud-fronted adapter would look the same shape with values that
reflect its reality:

```json
"permissions": {
  "gpu": false,
  "network_egress": ["api-inference.huggingface.co"],
  "host_filesystem": [],
  "shared_memory_paths": []
},
"cost": {
  "currency": "USD",
  "estimated_per_call": 0.0008,
  "rate_limit_per_minute": 60,
  "is_metered": true
}
```

A few notes on the shape:

- `tasks_advertised` is the closest thing to a "Task enum" — a small,
  optional, free-text vocabulary so consumers can answer "I want
  any adapter that does X." It's intentionally weak typing: an adapter
  can declare a brand-new task name that nobody has heard of, and the
  UI just renders it. We do not constrain the vocabulary in v1. If
  the community converges on common names, we can canonicalize them
  later.
- `permissions` is the **sandboxing declaration** (§8). KAI-C reads
  this and applies the declared scope as container constraints when
  managing the adapter's lifecycle.
- `scheduling.fair_queuing: "per_camera"` opts the adapter in to
  KAI-C's per-camera fair-queuing (§9). Default `"none"` lets KAI-C
  forward requests as fast as they arrive; `"per_camera"` makes KAI-C
  apply a token bucket per camera_id header.
- `cost` lets OpenNVR show a running estimate of cloud-adapter spend
  to the operator and refuse to schedule inference when a budget is
  exhausted. `null` rate-limit means "unlimited / unknown". For
  free / local adapters everything is zero.
- `model.fingerprint` is an opaque adapter-chosen string (typically
  a content hash of the weights file like `sha256:...`). If the
  adapter advertises one, KAI-C records it at registration and on
  every subsequent `/capabilities` poll. A mismatch is a **tamper
  signal** — KAI-C alerts the operator and records the change to the
  audit log (§10.5). Adapters that can't compute a meaningful
  fingerprint (e.g., cloud-fronting adapters) omit the field; KAI-C
  surfaces "model identity not verifiable" in the UI rather than
  silently trusting.

## 5. Inference output — guidance, not strict schema

Different models return different shapes. We do not try to standardize
the *content* of the `result` field. We do strongly recommend the
following conventions for the four most common modalities, so that
consumers (UI, agents, downstream adapters) can reason about results
without per-adapter parsers:

### 5.1 Detection (bounding boxes)

```json
{
  "detections": [
    {
      "label": "person",
      "confidence": 0.92,
      "bbox": { "x": 0.21, "y": 0.34, "w": 0.18, "h": 0.55 },
      "track_id": null,
      "attributes": {}
    }
  ],
  "frame_dimensions": { "w": 1920, "h": 1080 }
}
```

`bbox` coordinates are normalized [0,1] (resolution-independent).
`track_id` is null unless a tracking-capable adapter set it.

### 5.2 Classification

```json
{
  "predictions": [
    { "label": "cat",   "confidence": 0.81 },
    { "label": "dog",   "confidence": 0.12 }
  ]
}
```

### 5.3 ASR

```json
{
  "transcript": "the room is clear",
  "language": "en",
  "segments": [ { "start_ms": 0, "end_ms": 1800, "text": "the room is clear" } ]
}
```

### 5.4 LLM chat

```json
{
  "completion": "I see one person near the gate.",
  "finish_reason": "stop",
  "usage": { "prompt_tokens": 24, "completion_tokens": 9 }
}
```

These are conventions for consistency. An adapter that needs to deviate
should — for example, an OCR adapter would return polygons not boxes,
and that's fine. The contract is the envelope (§3.5), not the content.

## 6. Streaming protocol

`POST /infer/stream` opens a WebSocket. The protocol is JSON-framed
control messages with optional binary frame payloads. Every connection
opens with a `handshake` exchange that negotiates:

- which inputs the client will send (frames, audio, JSON)
- whether shared-memory frame transport is in play
- per-client inflight limits
- whether the adapter should publish results to NATS (B1) instead of
  back over the socket

### 6.1 Handshake

Client → Adapter (first WS message):

```json
{
  "type": "handshake",
  "client_id": "opennvr-core-1",
  "camera_id": "cam-7",
  "frame_transport": "websocket",   // or "shared_memory"
  "shared_memory_root": "/dev/shm/opennvr/frames/cam-7",
  "result_sink": "websocket",       // or "nats:detections.cam-7.object_detection"
  "expected_input_rate_hz": 15
}
```

Adapter → Client:

```json
{
  "type": "handshake_ack",
  "frame_transport": "shared_memory",   // accepted offer; falls back if can't
  "result_sink": "websocket",
  "max_inflight": 8,
  "session_id": "ws-9f3e..."
}
```

### 6.2 Frame messages

If `frame_transport == "websocket"`, the client sends:

```json
{ "type": "frame", "seq": 142, "ts_ms": 1716000000123,
  "content_type": "image/jpeg" }
<binary frame bytes immediately follow as the next WS message>
```

If `frame_transport == "shared_memory"`, the client writes the frame
into the negotiated shared-memory path and sends only metadata:

```json
{ "type": "frame_ref", "seq": 142, "ts_ms": 1716000000123,
  "shm_path": "/dev/shm/opennvr/frames/cam-7/000142.bin",
  "content_type": "image/jpeg", "size_bytes": 87432 }
```

The adapter reads from the shared-memory path, runs inference, and the
client is responsible for unlinking the file (or wrapping in a ring
buffer — implementation-defined, documented).

### 6.3 Result messages

Adapter → Client (for each completed inference):

```json
{
  "type": "result",
  "seq": 142,                            // echoes the frame seq
  "ts_ms": 1716000000123,
  "inference_ms": 18,
  "result": { /* per §5 */ }
}
```

If `result_sink` was set to a NATS subject in the handshake, the
adapter publishes the same payload to that subject and the WebSocket
sees only periodic `result_ack` heartbeats. This is the path the
agent layer will use — the agent subscribes to NATS for detections
without holding open one WebSocket per camera.

### 6.4 Control messages

Either side can send:

- `{"type": "pause"}` / `{"type": "resume"}` — flow control
- `{"type": "stats"}` → result includes inflight, queue depth, fps
- `{"type": "close", "reason": "..."}` — graceful shutdown

### 6.5 Close codes

Beyond the standard 1000 close, the adapter uses:

- `4001` policy refused (sovereignty / permissions / etc.)
- `4002` model error (OOM, weights missing, runtime crash)
- `4003` provider error (cloud endpoint failure for proxy adapters)
- `4004` overloaded (back off and retry)

## 7. Failure envelope

Every error response — `/infer`, `/infer/stream`, `/health`,
`/capabilities` — uses the same JSON shape:

```json
{
  "status": "error",
  "error": {
    "category": "model_error",         // see below
    "code": "out_of_memory",
    "message": "GPU OOM at batch size 4",
    "transient": false,
    "retry_after_ms": null,
    "details": {}
  }
}
```

Categories:

| Category | Meaning | Example |
|---|---|---|
| `model_error` | Inference itself failed | OOM, bad weights, NaN output |
| `provider_error` | Upstream / cloud failure | HF 429, OpenAI 503, auth expired |
| `transport_error` | Network or framing | Truncated multipart, malformed JSON |
| `permission_denied` | Sandbox or policy refused | Tried to write outside declared paths |
| `not_supported` | Endpoint not implemented | Adapter doesn't do streaming |
| `overloaded` | Backpressure | Queue full, ask later |

Consumers (KAI-C, agents) use `category` to decide retry policy —
`transient: true` errors are safe to retry with backoff;
`transient: false` are operator-actionable.

### 7.1 Canonical error codes

`error.code` is a stable adapter-chosen identifier. To keep audit
logs searchable across adapters we canonicalize the most common ones
— adapters SHOULD use these names where applicable and MAY define
new ones for adapter-specific failures (`my_adapter.weights_corrupt`
prefix-namespacing is encouraged for non-canonical codes).

| Code | Category | Meaning |
|---|---|---|
| `out_of_memory` | model_error | GPU/CPU OOM |
| `weights_missing` | model_error | Adapter started without weights present |
| `inference_runtime_crash` | model_error | Model produced NaN, crashed, or hard-faulted |
| `quota_exceeded` | provider_error | Cloud provider rate/quota hit |
| `auth_expired` | provider_error | Cloud credential rotated / expired |
| `provider_unavailable` | provider_error | Cloud endpoint 5xx |
| `malformed_input` | transport_error | Multipart truncated, JSON invalid |
| `unsupported_content_type` | transport_error | Adapter doesn't accept this MIME |
| `permission_egress_denied` | permission_denied | Adapter tried to call out outside declared egress list |
| `permission_path_denied` | permission_denied | Adapter tried to read/write outside declared filesystem scope |
| `stream_not_supported` | not_supported | WS upgrade refused (alias for HTTP 501) |
| `queue_full` | overloaded | KAI-C inflight cap hit (transient: true) |
| `backpressure` | overloaded | Adapter signalled overload mid-stream |

## 8. Permission declaration + sandbox enforcement

Adapters declare what they need in `/capabilities.permissions`. KAI-C
reads this on adapter registration and applies the declared scope as
container constraints. The operator sees the requested permissions
before enabling the adapter — like an app-store permission prompt.

| Permission | Enforcement |
|---|---|
| `gpu: true` | Container gets GPU device passthrough; if false, denied |
| `network_egress: ["host", ...]` | nftables rule limiting egress to listed hosts; empty = no internet |
| `host_filesystem: ["/path", ...]` | Bind-mounts limited to listed paths; default deny |
| `shared_memory_paths: ["/path"]` | tmpfs mount writable only at listed paths |
| `host_metadata: false` | Block AWS/GCP/Azure IMDS endpoints + `/proc/host*` |

**Operator approval is mandatory** for any permission outside the
safe-by-default set. Specifically, KAI-C MUST refuse to register an
adapter that declares any of the following until the operator
explicitly approves it from the OpenNVR UI:

- `gpu: true`
- `network_egress` non-empty
- `host_filesystem` non-empty
- `shared_memory_paths` non-empty
- `host_metadata: true`

The UI prompt looks like an app-store permission dialog: the operator
sees the adapter name, version, fingerprint (§4), declared permission
list, and a single approve/reject button per permission. Approvals
are recorded to the audit log (§10.5) with a stable
`adapter_grant_id` so a future incident review can answer "who
approved this adapter to call out to api.openai.com on 2026-04-12."

This is the **biggest security upgrade** the contract delivers and the
single best argument for why HTTP-adapter-per-container beats
in-process plugins for community-contributed code.

## 9. Fair queuing inside KAI-C

When multiple cameras want the same adapter, fairness matters. If
camera 1 is publishing 30fps continuous inference and camera 2 wants
one ad-hoc call, camera 2 should not wait behind 30 frames every
second.

KAI-C implements a per-camera token bucket for any adapter that
declares `scheduling.fair_queuing: "per_camera"` in its capabilities.
The bucket is sized from `scheduling.max_inflight` and refilled at
the adapter's measured `inference_ms` rate. Adapters that declare
`fair_queuing: "none"` get FIFO — useful for cloud adapters where
backpressure happens at the provider.

The fair-queuing layer also enforces a **global max-inflight per
adapter** so KAI-C never opens more concurrent requests than the
adapter advertised. Adapters can rely on never seeing more than N
inflight, no matter how many cameras are pushing.

## 10. Versioning + blue-green

Adapters declare which contract versions they speak:

```json
"adapter": {
  ...
  "supported_contract_versions": ["1", "2"]
}
```

KAI-C uses this to route requests. The contract itself is versioned at
the document level (this is v1). Breaking changes bump the version;
adapters can declare support for multiple versions for a transition
period.

Adapter *implementations* are also versioned. Operators can register
two versions of the same adapter (`face_recognition:v1.2` and
`face_recognition:v1.3`) side-by-side. KAI-C routes new traffic to the
newer version, drains the old, retires it. Versions are part of the
adapter URL in the registry (`/api/v1/adapters/face_recognition/v1.3`).

This sounds heavy but it falls out almost for free if we plan for it
now and is painful to retrofit. Implementation lands in milestone B
or later.

## 11. KAI-C aggregator behaviour

KAI-C polls each registered adapter's `/capabilities` and
`/hardware/evaluation` on:

- adapter registration
- every 60s thereafter (configurable)
- on demand via `POST /kaic/refresh`

It maintains an in-memory + Redis-backed cache of:

- adapter → capabilities (latest)
- adapter → health (latest)
- task → list of adapters that advertise it (derived index)
- adapter → permissions granted (declared at registration)

OpenNVR backend gets a single aggregated view via
`GET /api/v1/ai/capabilities` (which KAI-C serves) so the UI never
fans out across N adapter calls.

### 11.1 Sovereignty enforcement

KAI-C is also the sovereignty enforcement point (V-022 from the
security work). The existing `ai_sovereignty` setting still applies:

- `local_only` — KAI-C refuses to register any adapter whose
  registration URL is non-loopback AND any adapter whose declared
  `permissions.network_egress` is non-empty (because that's a
  cloud-proxy adapter). This is stricter than the current V-022
  check, which only looked at the URL.
- `federated` — adapters may have egress to declared peer endpoints,
  but `permissions.network_egress` must list them explicitly; KAI-C
  refuses wildcards.
- `cloud_allowed` — anything goes.

**For adapter authors:** to be sovereignty-clean for `local_only`
deployments, advertise `permissions.network_egress: []` and bind your
HTTP server on loopback. If your adapter is a cloud-proxy by nature
(HuggingFace, OpenAI, etc.), declare every egress host you'll call
out to in `network_egress` — KAI-C will admit you under `federated`
or `cloud_allowed` and reject under `local_only`. The adapter
declaration is the source of truth; KAI-C will not infer.

This is a meaningful tightening of V-022 and lands in milestone B
when KAI-C is rewritten as the registry. Until then, the existing
loopback check stays in place as a baseline.

### 11.2 Audit trail

Audit logging is the load-bearing trust contract. KAI-C writes an
append-only audit record to OpenNVR's audit log (V-013 store)
for every event below. The log is queryable from the OpenNVR UI by
time range, adapter, camera, outcome, and category — that's the
"audit any breach" affordance the platform promises to operators.

| Event | When | Fields recorded |
|---|---|---|
| `adapter.registered` | Adapter passes registration checks | adapter_name, adapter_version, model_name, model_version, model_fingerprint, declared_permissions, registration_url, contract_version |
| `adapter.permission_granted` | Operator approves a non-default permission (§8) | adapter_grant_id, adapter_name, permission_kind, permission_value, operator_user_id |
| `adapter.permission_revoked` | Operator or system revokes a grant | adapter_grant_id, reason |
| `adapter.deregistered` | Adapter removed (operator action / drift / shutdown) | reason |
| `adapter.capability_drift` | `/capabilities` poll shows changed values (§11.3) | field_path, previous_value, current_value, action_taken |
| `adapter.fingerprint_mismatch` | `model.fingerprint` changed between polls | previous_fingerprint, current_fingerprint |
| `inference.completed` | Every successful `/infer` or per-frame stream result | correlation_id, adapter, camera_id, model_version, request_received_at, response_sent_at, inference_ms, result_size_bytes |
| `inference.failed` | Every error envelope returned | correlation_id, adapter, camera_id, error.category, error.code, transient |
| `inference.refused_sovereignty` | Sovereignty policy rejected the call | correlation_id, adapter, sovereignty_mode, refusal_reason |
| `inference.refused_permission` | Sandbox denied an attempted side-effect | correlation_id, adapter, permission_kind, attempted_value |
| `inference.refused_budget` | Cost budget exhausted | correlation_id, adapter, budget_window, spend_at_refusal |
| `stream.opened` / `stream.closed` | WS lifecycle | session_id, adapter, camera_id, close_code, reason |

`correlation_id` is the stable UUID defined in §3.8. KAI-C mints it
at request-receive time and threads it through every layer: the
adapter, the audit log, NATS subjects (B1), and any example app
that subscribes. A single correlation_id lets an incident reviewer
pull the full causal chain — "this 3am alert came from app `X`,
which called adapter `Y` v1.4, which got a stream frame from camera
`cam-7` at 03:04:17" — out of a single audit query.

Retention defaults to 90 days; operators can extend via the existing
audit-log retention settings.

**SIEM / external forwarding.** The audit log is append-only inside
OpenNVR and additionally MAY be forwarded to external sinks
configured by the operator:

| Sink | Format | Notes |
|---|---|---|
| `syslog` | RFC 5424 JSON | Default-available for any org running a Splunk/ELK/Sumo collector |
| `webhook` | POST `application/json` per event | Generic — useful for Slack/Discord/PagerDuty via existing receivers |
| `file` | JSON Lines, rotating | Local audit archive for air-gapped deployments |
| `splunk_hec` | Splunk HTTP Event Collector | v1.5 |
| `datadog` | Datadog Logs API | v1.5 |

Operators configure the sink in OpenNVR settings. Forwarding is
best-effort; failures to forward are themselves audited
(`audit.export_failed`) but do not block the local append-only
write — local audit is the source of truth.

**Integrity.** v1 audit log is append-only via the existing
OpenNVR audit store. v1.5 will add hash-chained integrity (each
record's hash includes the previous record's hash) so tampering is
detectable. Tracked as a security roadmap item.

### 11.3 Capability drift detection

`/capabilities` is polled on the cadence in §11. Drift between two
polls is treated as follows:

| Field that changed | Action |
|---|---|
| `adapter.version` or `adapter.name` | Treat as new adapter; require re-registration. Old registration de-registered. |
| `model.fingerprint` | Audit `adapter.fingerprint_mismatch`. Alert operator. Keep serving (operator may decide it's a legitimate update); UI offers "re-approve" + "de-register" actions. |
| `model.version` | Audit `adapter.capability_drift`. Inform operator. Keep serving. |
| `permissions.*` adding a new permission | **Blocking.** De-register adapter. Operator must re-approve from registration flow. |
| `permissions.*` removing a permission | Allow; record audit event. |
| `endpoints.*` | Audit; no action. |
| `scheduling.*` | Apply new values on next request. No audit. |
| `cost.*` | Apply; recompute budget. |

Drift in `tasks_advertised` is benign (just updates the index).

### 11.4 Conformance kit + SDKs

Three artefacts ship alongside the KAI-C registry to make the
contract easy to adopt:

**1. `opennvr-adapter-conformance`** (Python CLI). Point it at any
adapter URL; it exercises every endpoint, asserts wire shapes
(using the Pydantic models in
`ai-adapter/app/interfaces/contract.py`), runs a streaming
roundtrip, and reports pass/fail/warn:

```bash
pip install opennvr-adapter-conformance
opennvr-adapter-conformance http://localhost:9001
```

Green run = KAI-C will accept. Community contributors run locally
before opening a PR; CI runs it on every reference adapter.

**2. `opennvr-adapter-sdk`** (Python). For *adapter authors* — a
minimal FastAPI starter that wires up `/health`, `/capabilities`,
`/hardware/evaluation`, `/metrics`, bearer-token validation,
correlation_id plumbing, and the WS protocol skeleton. Author
implements only the `infer()` method:

```python
from opennvr_adapter_sdk import Adapter, infer

adapter = Adapter(
    name="my-detector", version="0.1.0",
    model_name="my-model", model_version="1",
    tasks=["object_detection"],
)

@adapter.infer
async def detect(image_bytes, params, correlation_id):
    ...
    return {"detections": [...]}

adapter.run()  # starts FastAPI on $ADAPTER_PORT
```

**3. `opennvr-app-sdk`** (Python + TypeScript). For *example-app
authors* — wraps adapter discovery, invocation, camera lookup,
recording trigger, and alert emission. Generated from the same
Pydantic contract so types stay in sync:

```python
from opennvr_app_sdk import OpenNVR

nvr = OpenNVR()  # auto-discovers from env

# Find an adapter that does object detection
adapter = await nvr.adapters.find(task="object_detection")

# Stream camera 7 through it; alert if person in zone
async with adapter.stream(camera_id="cam-7") as session:
    async for result in session:
        for det in result["detections"]:
            if det["label"] == "person" and in_zone(det["bbox"]):
                await nvr.alerts.fire(
                    title="Person in restricted zone",
                    camera_id="cam-7",
                    severity="high",
                )
```

Both SDKs ship in A2 alongside the conformance kit. They are the
single biggest lever for "popular": writing an adapter or an app
becomes a 30-line affair, not a contract-reading exercise.

### 11.5 Alerts

Audit logging is "always on, always recorded." Alerting is the
narrower set of events that *demand operator attention right now*.
Both flow through KAI-C; alerts are a strict subset of the audit
stream tagged for routing.

**Built-in system alerts** (KAI-C emits these without operator
configuration):

| Trigger | Severity | Default channels |
|---|---|---|
| `adapter.unavailable` (≥ 3 consecutive `/health` failures) | high | UI + webhook |
| `adapter.fingerprint_mismatch` | critical | UI + webhook + email |
| `adapter.permission_drift_blocking` | critical | UI + webhook + email |
| `inference.refused_sovereignty` (any) | high | UI + webhook |
| `inference.refused_permission` (≥ 5/min from one adapter) | high | UI + webhook |
| `inference.refused_budget` (any) | medium | UI |
| `audit.export_failed` (≥ 1/min) | medium | UI |

**App-emitted alerts** (example apps call
`nvr.alerts.fire(...)` via the SDK):

```python
await nvr.alerts.fire(
    title="Person in restricted zone",
    description="Detected at gate camera, after-hours.",
    camera_id="cam-7",
    severity="high",          # low | medium | high | critical
    correlation_id=session.correlation_id,
    evidence={
        "snapshot_uri": "opennvr://snapshots/...",
        "recording_clip_uri": "opennvr://recordings/...",
        "detection": det.dict(),
    },
    tags=["intrusion", "after-hours"],
)
```

The alert shape on the wire:

```json
{
  "alert_id": "alrt_...",
  "fired_at": "2026-05-18T03:04:17Z",
  "title": "Person in restricted zone",
  "description": "...",
  "severity": "high",
  "source": {
    "kind": "app|adapter|kai-c",
    "name": "intrusion-detection",
    "version": "1.2.0"
  },
  "camera_id": "cam-7",
  "correlation_id": "<uuid>",
  "evidence": { ... },
  "tags": ["intrusion", "after-hours"]
}
```

**Alert channels** (configured per-deployment by the operator from
the OpenNVR UI):

| Channel | Notes |
|---|---|
| UI badge | Always-on; in-app inbox with ack/resolve |
| Webhook (POST JSON) | Generic; works for Slack/Discord/Teams/PagerDuty via their incoming-webhook endpoints |
| Email (SMTP) | Configured at OpenNVR install time |
| Slack incoming-webhook | First-class with severity → colour mapping |
| Discord incoming-webhook | Same |
| Native push | Mobile app (B+) |
| PagerDuty Events API | Direct integration (v1.5) |

**Routing rules** are operator-defined: "alerts of severity ≥ high
go to PagerDuty + UI; severity = medium go to Slack only; severity =
low go to UI only." Default rules ship with a sensible profile so
day-one operators get useful pings without configuration.

**Acknowledge / resolve flow** is operator-facing in the UI: each
alert is a row that can be acked (snoozes channel re-fires for 1h),
resolved (closes the alert), or escalated (re-fires on the
next-severity-up channel).

**Audit relationship**: every alert is also an audit event
(`alert.fired`, `alert.acked`, `alert.resolved`). The audit log
remains the source of truth; alerts are the *delivery mechanism*
for the subset that needs immediate human attention.

## 12. Examples + the integration contract for app authors

This section is the **app developer's manual.** Adapter authors can
skim; example-app authors live here.

### 12.1 Camera identifiers — how to integrate existing cameras

Operators register cameras once in OpenNVR (via UI or
`POST /api/v1/cameras`). OpenNVR talks to existing cameras over
RTSP/RTSPS/ONVIF through MediaMTX; common vendors are supported
out-of-the-box (Hikvision, Dahua, Axis, Reolink, Amcrest, Foscam,
Uniview, plus any generic ONVIF / RTSP source). Once registered, a
camera has a stable UUID — `camera_id` — that flows through every
adapter call and audit event.

**App author discovery flow:**

```python
# 1. List cameras visible to this app's role
cameras = await nvr.cameras.list()
# → [{"id": "cam-7", "name": "Front gate", "manufacturer": "Hikvision",
#     "resolution": [1920, 1080], "fps": 15, "has_audio": true,
#     "rtsp_internal": "rtsps://mediamtx:8322/cam-7", "tags": ["outdoor"]}]

# 2. Open a stream through any object-detection adapter
adapter = await nvr.adapters.find(task="object_detection")
async with adapter.stream(camera_id="cam-7") as session:
    async for result in session:
        ...
```

**Important guarantees for app authors:**

- The `camera_id` is stable across reboots, IP changes, and credential
  rotations. An app written today still works when the camera moves
  to a new VLAN tomorrow.
- The app never sees raw RTSP URLs or camera credentials. KAI-C
  fetches frames; the app sees decoded, pre-processed frames over WS.
- Camera capabilities (resolution, fps, audio, PTZ, two-way audio)
  are queryable via `nvr.cameras.get(camera_id)`; an app can refuse
  to run if a camera lacks audio.
- New camera registration triggers `camera.registered` audit event;
  apps that auto-attach to new cameras can subscribe via NATS (B1).

**For new-camera onboarding (operator UX):** the OpenNVR UI's
"Add camera" wizard auto-discovers ONVIF devices on the LAN,
probes transport security (M1c work), and gives the operator
defaults for username/password and resolution. Apps don't see this
flow — they only see the resulting stable `camera_id`.

### 12.2 Use-case catalog (what we plan to ship)

The intent is: by milestone end, OpenNVR ships with 6–8 first-party
examples covering the most-asked-for NVR + AI use cases. Each is a
template community contributors can copy. The full target catalogue:

| Slug | Use case | Adapters needed | Status |
|---|---|---|---|
| `intrusion-detection` | Person/vehicle in zone after-hours | object_detection | **C1 (first to ship)** |
| `watchman-agent` | Voice-interactive watchman ("what's at the gate?") | ASR + TTS + LLM | **C2** |
| `package-detect` | Porch package arrival + theft detection | object_detection + classification | A3 candidate |
| `camera-health` | Detect blurry/obstructed/offline cameras | classification | A3 candidate |
| `lpr` | License-plate recognition (whitelist / denylist) | LPR adapter (new) | B |
| `loitering` | Person stays in zone > N minutes | object_detection + tracking | B |
| `crowd-count` | Headcount in zone (retail, public safety) | counting | B |
| `ppe-compliance` | Hardhat/vest detection (construction sites) | classification | B |
| `fall-detect` | Slip-and-fall (elder care, retail) | pose | B+ |
| `fire-smoke` | Early fire/smoke warning | classification | B+ |
| `forensic-search` | "Show me everyone in red between 2-4pm" | CLIP + pgvector | D3 |
| `audit-replay` | Semantic search across recorded events | embeddings + RAG | D3 |

Each example ships as a runnable Docker container with a README and
screenshots. Community contributors fill the gaps — an example
becomes "first-party" only after it passes the conformance kit and
ships with tests.

### 12.3 Example app structure

```
examples/intrusion-detection/
├── README.md           ← problem + screenshots + how to run
├── Dockerfile          ← drop-in run-this
├── pyproject.toml      ← or package.json / Cargo.toml
├── intrusion_detection.py
├── config.example.yml  ← what an operator configures
└── tests/
```

Examples consume:

- Adapter HTTP/WS contract (this doc) — for inference
- OpenNVR REST APIs — for cameras, recordings, alerts
- NATS subjects (once B1 ships) — for detection-event streams
- `opennvr-app-sdk` (§11.4) — wraps the above

Each example's README answers: "what problem does this solve, what
adapters does it need, how do I run it, what's the operator UX." No
special integration with OpenNVR core — examples are first-class
clients.

### 12.4 Publishing your own adapter or app (community flow)

The contract is designed for community contribution. Three lanes:

**1. Adapter in your own repo.** An adapter only needs to be a
reachable HTTPS endpoint that passes the conformance kit. Host it
wherever — your GitHub repo, your own Docker registry, your laptop.
KAI-C registers any URL. To get *listed* in the OpenNVR community
catalogue (UI marketplace surface, planned B+), submit a one-line PR
to `open-nvr/community/adapters.yml` with your name + repo + image
URL + conformance-kit run output.

**2. Example app in your own repo.** Same model — host wherever,
submit to `open-nvr/community/apps.yml`. The catalogue listing
links to your repo; you keep ownership.

**3. Reference adapter / first-party example.** PR directly into
`ai-adapter/app/adapters/` or `open-nvr/examples/`. Must pass
conformance kit + have tests + be maintained. Reserved for adapters
that solve a foundational need (the existing 8) or examples that
fill a high-priority slot in the catalogue.

**Recognition.** First-party authors are credited in the example's
README + the community catalogue. Adapter and app authors who pass
conformance get a "Conforming to OpenNVR Adapter Contract v1" badge
they can show on their repo.

**Version safety.** Contract versions are forward-compatible within
a major. An adapter built against v1 keeps working through v1.x;
v2 will bump only after a deprecation window. The
`supported_contract_versions` field lets adapters declare which
versions they accept so KAI-C can route correctly during transitions.

## 13. Migration matrix — the 8 existing adapters

Each adapter in `ai-adapter/app/adapters/` needs to be brought up to
the contract. Ordered by complexity (simplest first to validate the
design with low risk):

| Adapter | `/health` | `/capabilities` | `/hardware/evaluation` | `/infer` | `/infer/stream` | `/metrics` | Notes |
|---|---|---|---|---|---|---|---|
| PiperAdapter (TTS) | partial | needs full | new | partial | optional (not real-time) | new | Smallest model, simplest endpoints. **First migration.** |
| WhisperAdapter (ASR) | partial | needs full | new | yes | new (real-time ASR) | new | Streaming valuable. |
| YOLOv8Adapter | partial | needs full | new | yes | new (continuous detection) | new | **Most impactful** — shared-memory fast path makes most sense here. |
| YOLOv11Adapter | partial | needs full | new | yes | new | new | Same as YOLOv8. |
| BLIPAdapter (caption) | partial | needs full | new | yes | optional | new | One-shot per frame; streaming optional. |
| InsightFaceAdapter | partial | needs full | new | yes | new | new | Face DB needs to move to pgvector later (D2). |
| HuggingFaceAdapter (vision) | partial | needs full | needs cloud-aware verdict | yes | new | new | First cloud-proxying adapter; reference for cost/quota fields. |
| OllamaAdapter (LLM) | partial | needs full | new | yes | yes (LLM streaming is natural) | new | Token streaming over WS. |

Migration order (one commit per adapter): **Piper → Whisper → YOLOv8 → YOLOv11 → BLIP → InsightFace → HuggingFace → Ollama.** Each lands behind a feature flag (`ENABLE_CONTRACT_V1`) so we can roll back if a migration breaks something.

## 14. What is intentionally NOT in v1

Each of these will get its own design doc when its milestone arrives.

- **NATS event bus.** Adapters publish detection events as
  `result_sink: "nats:..."` in streaming handshakes, but the bus
  itself, JetStream persistence, and subject naming come in B1.
- **Redis cache layer.** The capability cache + idempotency cache
  are KAI-C concerns that ride alongside the contract; B2.
- **MCP server.** Tools to expose, auth shape, and rate-limiting
  in D1.
- **pgvector + RAG.** D2/D3. The face DB migration off the
  in-memory dict is the trigger.
- **Triton-style multi-model inference servers.** Not ruled out —
  a Triton-fronting adapter is a perfectly valid contract
  implementation — but not in v1's reference set.
- **Adapter image signing + SBOM.** Tracked under the V-011 security
  roadmap; lands in parallel.

## 15. Open questions for reviewer

1. Is the `tasks_advertised` field too weak (free-text strings, no
   canonical vocabulary)? My take: leave it weak for v1, formalize
   only if the community converges on names organically.
2. Does the shared-memory protocol need a per-frame lifecycle owner
   (who unlinks)? My take: client owns it (the side that wrote it),
   adapter reads and acks. Document explicitly so adapter authors
   don't try to clean up.
3. Should `permissions` be declarative-only, or should KAI-C
   *enforce* it as a v1 requirement? My take: declarative only in
   the contract spec, enforcement is a v1.5 KAI-C feature. Otherwise
   we can't ship the contract without finishing the sandbox plumbing.

## 16. References

- Zenodo paper, DOI [10.5281/zenodo.17261761](https://doi.org/10.5281/zenodo.17261761) — §3.4 / §4.1 customer-sovereignty principles inform §8 and §11.1.
- Existing adapter implementations: `/Users/varunpratapsingh/cv/ai-adapter/app/adapters/`.
- KAI-C registry behaviour today: `/Users/varunpratapsingh/cv/open-nvr/kai-c/main.py`.
- OpenNVR security architecture: [docs/SECURITY_ARCHITECTURE.md](./SECURITY_ARCHITECTURE.md), §2.4 V-022 row covers the sovereignty interaction.
