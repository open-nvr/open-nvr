# KAI-C in OpenNVR — where it is used, and what it makes possible

OpenNVR records video and detects objects without KAI-C. Everything else
it knows — a plate, a face, a colour, a caption, a spoken word, the
identity that ties one visit to another across the site — arrives through
KAI-C. This document is the register of that dependency: every place the
platform calls it, and what each call buys.

It exists because the dependency is easy to under-record. KAI-C is not a
feature with a page in the product; it is a gate that other features are
built on top of, so its contribution shows up as *their* capability and
disappears from the story. When somebody later asks why a claim on a
report is trustworthy, or why a skill greyed out, or why an inference was
refused, the answer is always in KAI-C, and the code that asks the
question should be findable from here.

## 1. What KAI-C is, in one screen

KAI-C is the orchestrator that sits between OpenNVR and every AI adapter.
Adapters implement the wire contract in
[AI_ADAPTER_CONTRACT.md](AI_ADAPTER_CONTRACT.md) — `/health`,
`/capabilities`, `/hardware/evaluation`, `/metrics`, `/infer`,
`/infer/stream` — and KAI-C is the only thing OpenNVR talks to. Core
never holds an adapter URL (`settings.kai_c_url`,
`server/core/config.py`), and that single indirection is what gives the
platform four properties it would otherwise have to build four times:

**A registry.** `GET /capabilities` is the list of what this deployment
can actually do, expressed in the canonical task taxonomy of contract
§4.1. Every "can we?" question in the product resolves to it.

**A health signal.** `GET /adapters/health` says which of those
registered skills are answering right now. Registered and healthy are
different facts, and the product is careful to keep them different.

**A gate.** Sovereignty enforcement (§11.1), permission scopes and the
fail-closed approval lifecycle (§8.3), weight-fingerprint governance,
and fair queuing across cameras (§9) all live inside KAI-C, on the one
path every model call takes. A control on that path is a control; a
control in a caller is a convention.

**An audit trail.** §11.2: every inference gets a correlation id and an
audit line naming the adapter, the model fingerprint and the outcome.
That id is the reason a downstream assertion can be walked back to the
computation that produced it.

Layer 2 of the contract's mental model (§2) is KAI-C. This register is
the view from layer 3 — everything in OpenNVR that leans on it.

## 2. The register

### 2.1 The client and the gate

| Where | What it does |
| --- | --- |
| `server/services/kai_c_service.py` | The single client. `get_capabilities()`, `check_kai_c_health()`, `capture_frame_bytes()` (the shared capture pool), `extract_frame_from_video()`, and the HTTP client every other caller borrows. It also encodes the governed/direct mode decision: in governed mode, contract-v1 calls go through KAI-C's gate, which applies sovereignty and fingerprint governance and seeds its v1 registry from the adapters reachable at boot. |
| `server/core/config.py` | `kai_c_url`, `kai_c_ip`, the sovereignty mode (`local_only` refuses non-local adapters and 403s cloud inference), and the plate-OCR toggle that routes `fast_plate_ocr` through KAI-C. |
| `server/core/client_ip.py`, `server/routers/streams.py` | KAI-C is a sibling service, not a user: it is inside the internal-CIDR set for trust decisions, and MediaMTX's addressing exists partly so KAI-C's capture path resolves in host-mode compose. |
| `server/services/app_keys.py` | The site key that detect-pipeline and KAI-C hold is the same secret apps authenticate with; the trust boundary is shared rather than duplicated. |

### 2.2 Inference paths

| Where | What it does |
| --- | --- |
| `server/services/inference_manager.py` | Batch/offline inference over recorded video: extracts the frame through KAI-C, posts to `/infer/local`. The comment there is the design in one line — heavy models live behind KAI-C's governed dispatch, and what remains in core is orchestration. |
| `server/services/cloud_inference_service.py` | `_call_kai_c()` → `/infer/cloud`. Cloud models are not a second integration: they are the same gate with a different sovereignty verdict. |
| `sdk/opennvr-app-sdk/opennvr_app_sdk/client.py` | `OpenNVR.ai` — what adapters exist, and running one on a frame. An app that wants a model asks the platform, which asks KAI-C; apps never learn adapter URLs. |
| `sdk/opennvr-app-sdk/opennvr_app_sdk/infer_stream.py` | The persistent WebSocket session of contract §6, for apps that infer per frame rather than per event. All frames of a session share one KAI-C audit correlation id. |
| `sdk/opennvr-app-sdk/opennvr_app_sdk/contract.py`, `manifest.py` | Adapters self-register with KAI-C and expose `/health` + `/capabilities`; a manifest's required-adapters list names the specific KAI-C adapters an app cannot run without. |

### 2.3 Identity and enrichment

| Where | What it does |
| --- | --- |
| `server/services/plate_enrichment.py` | The vehicle-visit OCR sweep: one attempt per visit through `POST {kai_c_url}/api/v1/infer/{PLATE_MODEL}`, with a client reference KAI-C echoes back, and error handling that distinguishes "adapter not approved" from "KAI-C unreachable" — an operator-actionable difference the UI surfaces. |
| `server/services/plate_event_consumer.py` | KAI-C's normaliser publishes `plate.recognized.v1` for every accepted read, including reads core did not ask for; this consumer is the convergence point so one plate becomes one row whoever triggered it. |
| `server/services/skills_registry.py` | The index over the four sources that name a skill; the first is the KAI-C adapter registry. Its domain-event map mirrors KAI-C's normaliser. |
| `server/routers/skills.py` | `_kai_c_view()` — the TTL-cached `(health, capabilities)` pair, cheap enough for UIs to poll without turning KAI-C probes into load. Every "which skills does this site have?" answer in the product comes from here. |
| `server/routers/cameras.py` | Assignable skills per camera. Note the rule it enforces: KAI-C unreachable means *unknown*, never *empty* — the UI must not grey out a skill because a probe failed. |
| `server/routers/internal_camera_agent.py` | The camera agent's view of the same derivation (same `_kai_c_view`, same TTL), plus descriptor and text ingestion from enrichment runs. |

### 2.4 Apps that ask KAI-C what the box can do

| Where | What it does |
| --- | --- |
| `examples/package-delivery` | The Deliveries app rides Tier-0 for *who and when* and asks KAI-C for *what*: COCO has no package class, so on each trigger it reads `ai.capabilities()` and counts the doorstep with the best registered skill — a `package_detection` adapter, an object detector with a box class, or a VQA model — through `ai.infer(adapter, jpeg, task=…, camera_id=…)`, falling back to the Tier-0 bag classes when none is registered. The choice is re-made every five minutes and shown on the page as good / fair / proxy / none, because it decides how far the counts can be trusted. A model call happens only when somebody left the doorstep or on the re-count cadence, never per frame. |

### 2.5 Models, catalog, operator surfaces

| Where | What it does |
| --- | --- |
| `server/routers/ai_models.py`, `ai_model_management.py` | `check_kai_c_health()` as the operator-facing health endpoint. The stated principle: users need to know about KAI-C, not about individual adapter URLs. |
| `server/routers/adapters_catalog.py` | Whether an adapter computes a weights fingerprint at all — a governance property KAI-C acts on. |
| `server/routers/apps.py`, `app_platform.py` | Apps authenticate against KAI-C-shared secrets; app frames come from the KAI-C capture pool; required adapters are checked at install. |
| `app/src/lib/kaic.ts`, `kaiCService.ts` | The frontend's read of the `/capabilities` shape and the KAI-C request format. |
| `app/src/views/AIAdapters.tsx`, `AIModelsBYOM.tsx`, `AppCatalog.tsx`, `AppView.tsx`, `Cameras.tsx` | Where an operator sees adapters, their health, and what each camera can therefore do. |
| `docs/grafana/opennvr-ai-health.json`, `docs/OBSERVABILITY.md` | KAI-C and adapter health as an operable dashboard rather than a log line. |

## 3. What KAI-C makes possible in search and cross-camera journeys

The search and journey work added in this change is the clearest case of
the pattern this document exists to record, so it is written out rather
than listed.

**Enrichment is planned from the live registry, not from configuration.**
`server/services/enrichment_plan.py` intersects `GET /capabilities` with
`GET /adapters/health` and turns the result into the set of claims this
deployment can make about a visit: a site with an LPR adapter gets
plates, one with a face adapter gets identities, one with a VQA model
gets colour and clothing, one with none gets detections and nothing
more. Nobody declares this; the plan changes the day an adapter is
installed or fails. The plan is cached for 30 seconds
(`PlanCache`) for the same reason `_kai_c_view` is — the registry is
cheap to ask and expensive to ask constantly. Adapters that report no
health are treated as available rather than absent, following the
cameras-router rule that an unreachable probe is ignorance, not a
negative.

**Every claim carries its KAI-C correlation id.**
`visit_descriptors.correlation_id` (migration `d2e3f4a5b6c7`) is there
so a line on a report joins back to KAI-C's audit line — the exact
inference, adapter and model fingerprint behind it. Without it a
descriptor is an assertion. With it, it is evidence, and a skill later
found to be wrong can have its rows identified and re-run. The unique
key `(event_id, kind, source_task)` means a re-run replaces what that
source said rather than stacking a second opinion.

**The camera graph is learned from KAI-C's exact identities.**
`server/services/journey.py` and migration `e3f4a5b6c7d8` treat a plate
read by the LPR adapter and a face recognised by the face adapter as
anchors: the same string on two cameras is one object, observed
travelling. Thousands of those pairs give each A→B edge a median and a
p90 transit with no survey and no configuration, and the graph re-learns
itself when a gate is chained shut or a camera is re-aimed.

**That graph is what makes the general case work.** An object with no
exact identity — a person in a crowd, an unplated van, a trolley — is
followed by asking the topology where it could have gone and in what
time, then asking the descriptors which candidate fits. Both halves are
KAI-C's: the topology was taught by its identity adapters, and the
descriptors were produced by its attribute adapters. Without it,
"follow this" means scanning every camera for the whole window and
comparing nothing.

**More skills, strictly better answers — and the honesty rules that keep
that true.** A visit is described by whatever is registered and healthy,
so capability grows monotonically with the adapter set. Three rules stop
that from becoming overconfidence: a skill that did not run is recorded
in `ran_tasks` so *missing* never scores as *mismatch*; the weight of an
attribute value is measured from what this deployment actually sees
(`ix_descriptor_kind_value`), so "red" is weak at a depot of red vans and
nearly an identifier where there is one; and a journey states its method
— identity, evidence, time-only or none — with a caveat, so an operator
knows whether they are being shown a fact or a plausible reconstruction.

## 4. The rules this register encodes

Three conventions recur at almost every site above, and new code touching
KAI-C should follow them.

Registered, healthy and unreachable are three states, not two. The
product distinguishes "this site cannot do that", "it can and the adapter
is down", and "we could not ask" — and never collapses the third into the
first. `cameras.py` says it plainly: unreachable means not greyed out.

Probes are cached, not avoided. `_kai_c_view` and `PlanCache` exist so
that polling UIs and per-visit enrichment do not translate into a probe
per request.

Provenance travels with the claim. Correlation id, source task, source
adapter and model fingerprint are carried from the inference to the row
to the report, because the value of an answer from a model is bounded by
the ability to say where it came from.

## 5. Related documents

[AI_ADAPTER_CONTRACT.md](AI_ADAPTER_CONTRACT.md) is the wire spec —
§4.1 for the task taxonomy, §8 for permissions and the approval
lifecycle, §9 for fair queuing, §11 for aggregator behaviour,
sovereignty and the audit trail. [EVENT_CONTRACTS.md](EVENT_CONTRACTS.md)
covers the normalised domain events KAI-C publishes.
[CONTRIBUTING_ADAPTERS.md](CONTRIBUTING_ADAPTERS.md) is the path for
adding a skill. [SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md)
holds the `V-###` controls referenced from the code sites above.
