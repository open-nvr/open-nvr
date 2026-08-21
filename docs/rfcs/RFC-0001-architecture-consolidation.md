# RFC-0001: Architecture consolidation — one contract, one event store, supervised runtime

- **Status:** Accepted in part — C1 parts A+B shipped (#213, #215); C4/C6
  first installments shipped with the Tier-0 PR (gate-mode in UI, tier0
  function-based health). Remaining phases open for discussion.
- **Author:** Varun Pratap Singh (xpertvoip)
- **Date:** 2026-08-04
- **Scope:** open-nvr, ai-adapter
- **Related:** PR #148 (tier0 detect-pipeline), PR #135 (vendor drivers), QA position paper "Why OpenNVR does not need the detect-pipeline"

## Summary

OpenNVR's extension architecture — the adapter contract, the NATS event bus,
and the app SDK — is sound and is the project's real moat. The complexity QA
is reporting does not come from that architecture; it comes from **parallel
paths that predate or sidestep it**: five places events live, two agent
codebases for one product, an audit layer most calls bypass, four
configuration surfaces with no rule about what goes where, and a Tier-0
service that half-follows the contract it sits next to.

This RFC proposes consolidation, not reinvention: fold the parallel paths
onto the architecture we already have. No new frameworks, no rewrite.

## What we explicitly keep (principles)

1. **The bus is the sharing mechanism.** Producers publish
   `opennvr.inference.*` / `opennvr.alerts.*`; consumers subscribe. No
   point-to-point integrations between features.
2. **The adapter contract v1 is the unit of AI capability.** One `/infer`
   shape, `/health`, GHCR image, conformance tests.
3. **Slim-ish core.** Core owns ingest, recording, serving, auth, and (per
   this RFC) the canonical event store. Intelligence lives in adapters and
   apps.
4. **Zero-required-config.** Every knob has a working default; `docker
   compose up` must produce a working, observable system (established in
   PR #148's defaults commits — this RFC extends it to a rule).

## Challenge 1 — Canonical event & evidence store (highest priority)

### Current state

"What happened" is answered by at least five stores with five schemas:

| Store | Contents | Persistence |
|---|---|---|
| `camera_events` table (PR #135) | driver alarms (motion/tamper from cameras) | DB, no retention policy |
| NATS `opennvr.inference.*` | adapter + tier0 detections | ephemeral |
| footage-search app SQLite | keyframes: labels/captions/time | app-private file |
| tier0 best-frame cache | per-track best crop | RAM, TTL-bounded |
| recordings | raw video | disk, retention-managed |

The QA paper proposes a sixth (lazy playback-analysis cache). Every consumer
reinvents timeline queries; no store links an event to its evidence frame;
tier0's best frames — the most valuable evidence we produce — evaporate.

### Target state

One `events` store in core:

```
events(id, camera_id, source, type, label, score, track_id,
       occurred_at, recording_ref, evidence_ref, payload_json)
evidence blobs: content-addressed JPEG crops under the recordings root,
                retention-managed with the recordings
```

- A single **bus→DB ingestor** in core subscribes to `opennvr.inference.>`
  and `opennvr.alerts.>` and writes rows. Apps keep publishing exactly as
  today — nothing changes for producers.
- Tier-0's per-track best frame is persisted (small JPEG + timestamp +
  `recording_ref`) when a track ends or escalates. This directly enables
  "which car entered at 15:00" answers **with an evidence image**, and the
  LPR crop (fast-plate-ocr) attaches `plate_text` to the same row when
  enabled.
- `camera_events` (driver alarms) migrates into the same table
  (`source='camera'`).
- footage-search becomes a thin query app over the core store (its indexer
  is deleted); the agent's footage tools query core instead of a sidecar
  SQLite.
- Retention: one policy, tied to recordings retention.

### Acceptance

- One API (`GET /api/v1/events?camera=&from=&to=&label=`) answers every
  timeline question in the product, with evidence URLs.
- Deleting the footage-search indexer removes no user-visible capability.

## Challenge 2 — KAI-C: choke point or optional proxy, not both

### Current state

Stated design: adapter calls flow through KAI-C for governance/audit.
Reality: the camera agents call whisper/ollama/piper **directly** ("bypasses
KAI-C in v0.1"), tier0 dispatches through KAI-C only when configured, apps
call adapters directly. The audit trail is therefore partial — the costs of
an extra hop without the guarantee that makes it worth having.

### Decision — this RFC recommends (C): dissolve the proxy, keep the authority

KAI-C's *responsibilities* (adapter approval, policy, audit) are correct and
necessary — a plug-and-play adapter is untrusted code asking for camera
streams, and a security-first product must gate and attribute that. KAI-C's
*form* (a Python proxy in the inference data path, co-located in the core
container, bypassed by the streaming voice path "in v0.1") is the misfit:
the audit trail has holes exactly where the volume is, and the extra hop is
why callers route around it.

Options considered:

- (A) Make KAI-C the single inference path (streaming passthrough, <10 ms
  p95 overhead budget). Rejected: fights the voice path's latency budget
  forever, and every future high-volume caller re-fights the same battle.
- (B) Demote KAI-C to an optional proxy and drop the "governed" claim.
  Rejected: gives up attribution, which is a product differentiator.
- **(C) KAI-C becomes a control plane; the data path goes direct.**
  Chosen. This is the same pattern the product already uses for streams
  (MediaMTX tap URLs carry signed tokens minted by core): centralized
  authority, decentralized data path.

### The capability-token flow (C)

```
1. MINT      app/agent → core (KAI-C authority):
             POST /api/v1/capabilities
             {adapter: "blip", camera_id: 3, task: "caption", ttl_s: 300}
             → signed capability token (JWT; claims: caller id, adapter,
               camera scope, task, exp). Denied if the adapter is not
               approved or policy forbids the caller/camera pair.
2. CALL      app/agent → adapter /infer, directly, token attached.
             Zero extra hops; streaming works because nothing is in the path.
3. VERIFY    adapter SDK verifies signature + scope + expiry offline
             (public key fetched from core's JWKS — already exposed for
             MediaMTX). Reject = 403 with reason.
4. AUDIT     both sides emit an audit event to NATS
             (opennvr.audit.inference.*): caller, adapter, camera, task,
             latency, bytes. The Challenge-1 event store ingests these —
             the audit trail and the product timeline share one store.
```

Consequences:

- The shared static `OPENNVR_ADAPTER_TOKEN`/`INTERNAL_API_KEY` (one key,
  no attribution) is retired for inference calls — a security upgrade, not
  just a refactor.
- The streaming voice path needs no exemption: there is nothing to bypass.
- KAI-C's registry/approval/permissions move into core proper as the
  capability authority; the request/response proxy code path is retired
  once audit coverage from (4) matches today's.
- Adapters gain ~5 lines (SDK verify helper); callers gain a mint-and-cache
  helper in the app SDK.

### Migration (graceful, no flag day)

1. Core mints tokens + SDK verify helper ships; adapters accept BOTH the
   legacy shared key and tokens (log which was used).
2. Agents and apps switch to minted tokens (SDK upgrade, no logic change).
3. Adapter conformance suite requires token verification; legacy shared-key
   acceptance is removed one minor version later.
4. KAI-C proxy endpoints are deleted; its approval/policy API and audit
   remain as core's capability authority (the KAI-C name can stay for the
   authority component).

### Acceptance

- No inference call carries the shared static key (grep + conformance test).
- Every `/infer` served by any adapter has a matching audit event with a
  resolvable caller identity.
- The voice pipeline's latency is unchanged from direct calls (benchmark).

## Challenge 3 — One agent, two backends (not two agents)

### Current state

`examples/camera-agent` (torch family) and `examples/camera-agent-lite`
(llama.cpp family) duplicate tools, context management, camera resolution,
routing, demo UI. They drift: lite received the small-LLM hardening first;
full received best-frame first; the tier0-starvation incident was partly a
"which agent is affected" confusion.

### Target state

One `camera-agent` package with a **backend profile**:

- `profile: full` → whisper/ollama/blip adapters, richer prompts
- `profile: lite` → whispercpp/llamacpp/smolvlm adapters, small-LLM prompt
  hardening, tighter token budgets

Tools, context, routing, serializer, demo UI: one implementation. The
existing SDK (`opennvr_app_sdk`) is the natural home for the shared agent
runtime. Compose overlays select the profile; the adapter images stay as
they are.

### DECISION (2026-08, maintainer): merge REJECTED — agents stay separate

The two agents target different hardware tiers and quality bars, evolve at
different cadences, and small-LLM prompting/tooling genuinely diverges from
the full-model agent's. A forced merge would couple their release trains
and turn every lite-specific hardening into a conditional in shared code.
The "target state" above is kept for the record; it is not the plan.

**What we do instead — drift containment, not unification:**

- Shared primitives move **down into the SDK**, never across between
  agents: anything both need (best-frame client, Tier-0 snapshot helpers,
  alert dispatch, event-store queries) lives in `opennvr_app_sdk` and both
  import it. An agent-to-agent copy/paste is the smell reviews reject.
- A parity table in each agent's README (capability × full/lite ×
  since-version) so drift is *visible* and deliberate, not accidental.
- Fixes applying to both are labelled `both-agents` in the tracker.

### Acceptance (revised for the decision)

- No module is duplicated between the two agent trees; shared logic exists
  once, in the SDK.
- The parity tables exist and reviews reference them.

## Challenge 4 — Configuration layering rule

### Current state

Settings live in: `.env` (~40 vars), envsubst-templated YAML via init
containers, compose overlays/profiles, DB-backed settings (device firewall,
WebRTC), per-app YAML. No rule says which layer owns what;
`DETECT_GATE_MODE` — a product decision — currently requires an `.env` edit
and redeploy.

### Target rule

| Layer | Owns | Examples |
|---|---|---|
| env / compose | infrastructure facts | ports, URLs, credentials, hardware (threads, hwaccel) |
| DB + admin UI | product behavior | gate mode (shadow/enforce), firewall enforcement, retention, per-camera toggles |
| per-app YAML | app-local tuning | zones, thresholds, labels |

Migrations this implies (each small, independent):

1. Gate mode → Compute-gated panel toggle, persisted in DB, read by tier0
   from core at startup and on change (poll or bus signal). Env remains as
   bootstrap override only.
2. `DETECT_*` expert knobs collapse behind `DETECT_PROFILE=auto|low-power|quality`
   presets (`auto` reads core count); raw vars stay as overrides.
3. Every service prints its **effective config** (one block, what's on/off
   and why) at startup — half of QA's confusion was not knowing system
   state.

### Acceptance

- A fresh user flips shadow→enforce in the UI without touching a file.
- `.env.example` shrinks.

## Challenge 5 — Tier-0 is an adapter (delete the special cases)

### Current state

detect-pipeline publishes to inference subjects but is not an adapter: it
lives in open-nvr (adapters live in ai-adapter), has its own publish
workflow, its own metrics bridge into core (`tier0_metrics.py` scraping
Prometheus text), and a bespoke `/best_frame` endpoint.

### Target state

Tier-0 conforms to the adapter contract as a **self-triggering adapter**:
`/health` like every adapter, capabilities declared the standard way, and
its metrics flow through the same path the app already uses for adapter CPU
charts (FOLLOWUPS #11 agrees). `/best_frame` stays (it is the evidence
producer for Challenge 1) but is documented in the contract as an optional
capability rather than a special case. Repo location: move to ai-adapter's
matrix or formally document why it is core-adjacent — either ends the
ambiguity.

### Acceptance

- The Compute-gated panel is a generic adapter-metrics panel with tier0
  selected; `tier0_metrics.py`'s bespoke scraper is deleted.

## Challenge 6 — Runtime supervision: zombies, not corpses

### Current state

Role-per-container is the right architecture for this product (adapter
isolation from camera credentials, conflicting ML dependency trees, blast-
radius containment) — but it created a supervision obligation that plain
docker-compose does not fulfill, and the gap is precise:

- `restart:` policies exist on nearly every service, so **crashed**
  containers recover. But Docker never acts on an **unhealthy** container —
  a healthcheck only changes the status string.
- Every camera-silently-blind incident on record was a zombie, not a crash:
  a model-download failure left a "healthy" container doing nothing; an
  expired stream ticket had ffmpeg self-restarting into the same dead
  credential forever; a NATS auth rejection left the process alive with
  publishing silently dropped. Restart policies never fired because nothing
  died.
- `docker-compose.apps.yml` defines **zero healthchecks for 20 app
  containers**, even though the SDK already serves `/health` with
  `last_event_age_s` in every app — the signal exists; compose doesn't look
  at it.
- `depends_on` conditions apply at startup only; there is no runtime
  dependency recovery.

### Target state

Four pieces, in dependency order:

1. **Health means function, not liveness.** Every service's healthcheck
   verifies its job, not its process: tier0 = frames flowed in the last N
   seconds AND bus connected AND model loaded; adapters = model actually
   loaded and one self-inference passed; apps = SDK `/health` wired into
   compose (all 20). A service that cannot do its job reports unhealthy —
   never "healthy but idle."
2. **Act on unhealthy.** An autoheal watcher (Docker-events sidecar behind
   a socket **proxy** — never raw `docker.sock` in a reachable container)
   restarts unhealthy containers with backoff; production deployments on
   swarm/k8s get this natively and the docs say so.
3. **Self-heal credential-class failures in-process** — restart cannot fix
   a stale credential. On stream 401: re-fetch the tap URL from core before
   restarting ffmpeg. On bus auth failure: bounded reconnects + unhealthy
   status (shipped in PR #148 fixes). On model missing: retry download,
   report unhealthy meanwhile.
4. **Surface everything.** A system-health panel in core (every component:
   state, last-event age, restart count) fed by the same `/health`
   endpoints, plus an `opennvr.alerts.system.*` bus event on any
   degradation — ingested by the Challenge-1 event store like everything
   else. A camera must never be blind *silently*.

### Acceptance

- Kill the model file inside any adapter: its container goes unhealthy
  within 60 s, is restarted by the watcher, and the health panel + an alert
  show the episode.
- Expire a stream ticket manually: tier0 re-mints and resumes within one
  restart cycle, with an event recording the interruption.
- `grep healthcheck docker-compose.apps.yml` matches 20 times.

## Sequencing

Deliberately ordered so each phase pays for the next and nothing blocks
PR #148 (which merges first — its fixes stand alone):

1. **Phase 1 (unblocks QA's real complaints, ~2 wk):** Challenge 4 items
   1–3 (UI toggle, presets, effective-config print) + Challenge 6 items
   1 and 3 (function-based healthchecks incl. the 20 apps, ticket re-mint,
   honest model-failure state — these subsume the two bugs from QA's
   paper). Challenge 6 items 2 and 4 (autoheal watcher, health panel)
   follow in Phase 2 alongside the event store, which the panel's alerting
   feeds into.
2. **Phase 2 (the missing piece, ~2–3 wk):** Challenge 1 event store +
   best-frame persistence + footage-search migration. This also delivers
   the "which car entered at 15:00 with picture and plate" capability.
3. **Phase 3 (debt removal, ~1–2 wk):** Challenge 3 drift containment
   (shared primitives down into the SDK, parity tables), then Challenge 5
   adapter-ization.
4. **Phase 4 (capability tokens, ~2 wk + one deprecation cycle):**
   Challenge 2 (C): token minting in core, SDK verify/mint helpers,
   dual-accept migration, then retire the proxy path and the shared
   static adapter key.

## Adoption guardrails — why this ordering, in product terms

This RFC is not only engineering hygiene; it is an adoption bet. The market
context: Frigate's community proves the demand and its top complaints
(config overwhelm, hardware hunger) prove the appetite for something
easier; ZoneMinder is aging out; commercial NVRs lock users to their
cameras. OpenNVR's differentiators are real — multi-vendor auto-
configuration (the driver layer), a fully-offline talking camera agent
(no competitor has one), and the app ecosystem. The risk is equally real:
our own QA — the most patient users we will ever have — needed documents
to understand tier0 and wrote a paper arguing to remove it. External users
give a project five minutes, not two days.

Rules this RFC commits us to:

1. **Adoption-critical before architecture-critical.** Challenge 6
   (nothing dies silently) and Challenge 4 (zero-required-config, UI over
   env) are what users experience; Challenges 2/3/5 are engineering health
   users never see. The phasing reflects this and MUST keep reflecting it.
2. **The first-fifteen-minutes funnel is a CI test, not a hope.** Fresh
   machine, 4 cores, no GPU: `docker compose up` → first camera discovered
   → first recording → first agent answer. Timed, asserted, run on every
   release. If it exceeds 10 minutes or anything reports healthy while
   broken, the release is blocked.
3. **Complexity budget.** Every new component either ships invisible at
   defaults or visibly earns its keep in the UI on day one. (Tier0's arc —
   shipped invisible, QA revolted, shadow-mode value made visible — is the
   lesson, generalized.)
4. **Lead with the wedge.** The README and first demo lead with "talk to
   your cameras, fully offline" and "point it at any camera brand and it
   configures itself" — not with the architecture. Depth (apps, tier0,
   firewall) is for the second visit.
5. **The Home Assistant channel is strategic.** Frigate grew through HA;
   the home-assistant-relay app is a distribution investment, not a demo,
   and gets roadmap priority accordingly.
6. **Reliability circuit-breaker.** If a future QA cycle finds another
   silent-blindness class of bug, feature work pauses until the
   supervision layer (Challenge 6) closes the class — recording
   reliability is the one non-negotiable in this product category.

## Non-goals

- Not merging everything into core (the QA paper's implicit proposal): that
  is Frigate's architecture for Frigate's product; it caps the app
  ecosystem that differentiates OpenNVR.
- Not adopting new infrastructure (no Kafka, no k8s-operator, no plugin
  framework). NATS + compose + the contract are sufficient.
- Not renaming or rebranding components.

## Open questions

1. Event store growth: per-event rows at tier0 rates need a rollup policy —
   persist per-track (recommended: one row per track lifecycle + state
   changes), not per-frame.
2. Capability-token TTL and revocation: 5-minute TTLs with mint-and-cache
   cover the common case; does adapter un-approval need instant revocation
   (short TTL is the answer) or a push signal on the bus as well?
3. Where does the lazy playback-analysis feature (QA's proposal — worth
   building) live? Recommended: an app on the SDK using the same detector
   adapter, writing to the Challenge-1 event store — which makes it ~300
   lines, not a subsystem.

## Appendix: incidents this RFC would have prevented or shortened

- **Lite starvation (QA, Aug 2026):** Challenge 4's presets + effective-config
  print would have surfaced the collision; Challenge 3 would have halved the
  diagnosis surface.
- **NATS auth silent drop:** Challenge 5 (adapter conformance includes a
  bus-publish check) + Challenge 4's effective-config print.
- **"Healthy but did nothing" model-download failure:** Challenge 6's
  function-based healthchecks + autoheal (and Challenge 5's standard
  adapter health semantics).
- **Hourly stream-ticket expiry silently blinding a camera:** Challenge 6
  items 3 and 4 — credential re-mint in-process, and "silently" becomes
  impossible once the health panel and system alerts exist.
- **"Which agent got the fix?" drift:** Challenge 3's containment —
  SDK-shared primitives + parity tables + `both-agents` labels.
- **Five timeline stores answering one question:** Challenge 1.
