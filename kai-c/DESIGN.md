# KAI-C — the capability authority for OpenNVR's AI layer

KAI-C is the component that decides **which code may run which AI model on
which camera — and proves, afterwards, that nothing else did.**

OpenNVR's AI layer is plug-and-play: adapters (YOLOv8, Whisper, BLIP,
llama.cpp, community-built) are separate containers that anyone can add. In
a security product, that flexibility is also the threat model — an adapter
is third-party code asking for your camera streams. KAI-C is the trust
boundary that makes the plug-and-play promise safe to keep.

## What KAI-C owns

| Responsibility | What it means |
|---|---|
| **Adapter registry** | Every adapter announces itself; KAI-C tracks identity, contract version, declared tasks and capabilities |
| **Approval gating** | New adapters are `pending` until an admin approves them — a pending adapter gets no streams and serves no inference (trust-on-first-use, same philosophy as the device firewall) |
| **Policy** | Which caller (app, agent, tier0 dispatch) may use which adapter, for which cameras, for which task |
| **Capability tokens** | Short-lived signed grants that carry the policy decision to the adapter (see below) |
| **Audit** | Every inference is attributable: who asked, which adapter, which camera, what trigger, how long it took |

## What KAI-C deliberately does **not** own

- **The inference data path.** Callers talk to adapters directly. KAI-C is
  a *control plane*: it grants and records, it does not proxy. This is a
  design decision (RFC-0001, Challenge 2): a proxy hop in the data path
  either breaks the streaming voice latency budget or gets bypassed — and a
  governance layer that busy callers bypass audits everything except what
  matters.
- **Event distribution.** That is NATS. KAI-C *emits* audit events onto the
  bus like any other producer; the canonical event store ingests them.
- **Model execution.** Adapters run models. KAI-C never touches tensors.

## How it works — the capability-token flow

The pattern is the one OpenNVR already trusts for video: MediaMTX tap URLs
carry signed tokens minted by core. KAI-C applies it to inference.

```
┌────────┐ 1. mint                ┌────────────────┐
│ caller │ ─────────────────────▶ │ KAI-C          │
│ (app / │   POST /capabilities   │ (authority)    │
│ agent /│   {adapter, camera,    │ policy check → │
│ tier0) │    task, ttl}          │ signed token   │
│        │ ◀───────────────────── │                │
│        │ 2. token (JWT)         └───────┬────────┘
│        │                                │ publishes JWKS
│        │ 3. POST /infer + token ┌───────▼────────┐
│        │ ─────────────────────▶ │ adapter        │
│        │                        │ verifies sig + │
│        │ ◀───────────────────── │ scope offline  │
│        │ 4. result              └───────┬────────┘
└───┬────┘                                │
    │ 5. audit event                      │ 5. audit event
    ▼                                     ▼
        NATS  opennvr.audit.inference.*  →  core event store
```

1. **Mint.** The caller asks KAI-C for a capability:
   `{adapter: "blip", camera_id: 3, task: "caption", ttl_s: 300}`. KAI-C
   checks the adapter is approved and policy allows this caller/camera/task,
   then returns a signed JWT whose claims are exactly that scope.
   Denials are audit events too.
2. **Call.** The caller invokes the adapter's `/infer` **directly** — zero
   added hops, so streaming pipelines (voice, live VLM) pay nothing.
3. **Verify.** The adapter SDK validates signature, scope, and expiry
   offline against KAI-C's JWKS (the same key-distribution mechanism
   MediaMTX already uses). Out-of-scope or expired → `403` with reason.
4. **Audit.** Both sides emit an audit event; the event store makes the
   trail queryable next to the product timeline.

**Why short-lived tokens instead of the shared `INTERNAL_API_KEY`:** one
static key shared by every caller means no attribution (any caller is every
caller) and no revocation (rotating it breaks everything at once). Scoped
5-minute tokens mean un-approving an adapter or revoking an app takes
effect within one TTL, and every audit row names its true caller.

## Interfaces

- `POST /api/v1/capabilities` — mint (authenticated caller → token or
  policy denial)
- `GET  /api/v1/adapters` — registry with approval state, tasks,
  capabilities (apps use this for `requires_tasks` checks)
- `POST /api/v1/adapters/{id}/approve|block` — admin approval (UI-backed)
- `GET  /.well-known/jwks.json` — verification keys (shared with the
  stream-token infrastructure)
- Bus: emits `opennvr.audit.inference.*`; consumes nothing on the hot path

## Failure semantics

- **KAI-C down:** callers keep using cached unexpired tokens (≤ TTL of
  runway); new mints fail closed. Adapters keep verifying offline — the
  data path has no runtime dependency on KAI-C being up.
- **Clock skew:** tokens carry `iat`/`exp` with the same leeway rules as
  the stream tokens.
- **Adapter without verification (legacy):** during migration, adapters
  dual-accept the legacy shared key and log which path was used; the
  conformance suite flips to require tokens one minor version later
  (RFC-0001 migration steps).

## Design history — why not a proxy?

KAI-C v0.1 sat in the data path as a request/response proxy. Reality
diverged from the diagram: the streaming voice path bypassed it for
latency, tier0 dispatched through it only when configured, and the audit
trail had holes exactly where the call volume was. The proxy also lived in
the core container, so it bought no isolation for its cost. The
capability-token redesign keeps everything the proxy promised — gating,
policy, attribution — and deletes the reason to bypass it: there is nothing
in the path to bypass. See RFC-0001 (Challenge 2) for the full decision
record and the rejected alternatives.

## Relation to the rest of the platform

- **Device firewall** gates which *devices* may use OpenNVR; KAI-C gates
  which *code* may use which *model* on which *camera*. Same philosophy
  (default-deny for the untrusted, TOFU enrollment, admin approval, break-
  glass), different subjects.
- **MediaMTX stream tokens** are the sibling mechanism for video access;
  KAI-C tokens are the same idea for inference. One JWKS serves both.
- **The event store** (RFC-0001, Challenge 1) is where audit events become
  queryable history — "which model looked at camera 3 yesterday and why"
  is one SQL query, not a log grep.
