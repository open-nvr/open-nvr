# RFC-0002: Skills — one registry, one event fabric, apps that compose

- **Status:** Draft — architecture agreed between the authors 2026-08-29
  (including the point-by-point review of the same date, which added
  decisions 7–8); phases open for scheduling.
- **Author:** Varun Pratap Singh (xpertvoip)
- **Date:** 2026-08-29
- **Scope:** open-nvr, ai-adapter, first-party example apps
- **Related:** RFC-0001 (architecture consolidation), PR #269
  (apps-consume-tier0), issue #344 (the observability incident that
  motivated skill *status*), the 2026-08-29 LPR install finding
  (gap 1 observed live: app installed cleanly, could never work).

## Summary

The platform's extension model is: cameras → RTSP → MediaMTX →
detect-pipeline (Tier-0) → events on NATS → adapters/models through KAI-C
(Tier-1) → apps. Apps both *consume* capabilities and *contribute* them.

A **skill** is a named capability of the system at a point in time —
"detect vehicles", "read license plates", "recognise faces at the door" —
with a defined provider (an app or adapter), the event contract it
publishes, the configuration it accepts (chiefly: which cameras), and a
live status. Apps discover what the system can do by querying the skill
registry, and consume what it does by subscribing to events.

Installing the LPR app is the canonical walkthrough:

1. Install enables the app **and provisions its adapter dependency**
   (`fast_plate_ocr`) — the system now *has* the capability.
2. The skill is **dormant**: capable, but doing nothing, until cameras
   are assigned to it.
3. Once cameras are assigned, Tier-0's existing vehicle detections on
   those cameras (car/bus/truck are already in its vocabulary — nothing
   is added to the base detector) escalate through **Tier-1 dispatch**,
   which runs the OCR adapter once per track on the best-frame crop,
   through KAI-C — the app writes no inference code at all.
4. KAI-C publishes the plate read as a **contracted event** on the bus
   (adapters themselves stay bus-unaware request/response).
5. Any other app — the camera agent first — discovers the skill in the
   registry, subscribes to its events, and can read/write its camera
   assignment. The LPR app may additionally ship its own UI; the agent
   shows plates in chat and history without any point-to-point wiring.

This RFC is consolidation in RFC-0001's sense: no new frameworks. Every
piece below already exists somewhere in the codebase as a fragment; the
work is unifying the fragments and closing seven gaps.

## Design decisions (agreed)

1. **The registry is an index, never a broker.** Discovery, contracts,
   assignment, and status live in the registry; *data never routes
   through it*. Detections stay on NATS, inference stays on KAI-C. A
   registry outage must not stop a single running skill. (The classic
   plugin-platform failure is a registry that becomes a broker and then
   a bottleneck and then a single point of failure.)

2. **Unify the four existing sources of truth; do not add a fifth.**
   The registry is a *view over*: KAI-C's adapter registry (models),
   app `/manifest` surfaces (apps), the Tier-0 assignment table
   (cameras), and the camera agent's skill derivation (the most
   developed prototype of the idea, including `suggested_apps` — the
   greyed "install this to get that" on-ramp). Writes happen through
   manifests and the assignment API; the registry never drifts from its
   sources because it *is* its sources.

3. **Contracts before registry.** Discovery without a schema is a
   catalogue of things you cannot safely build on. Event contracts are
   versioned subjects with defined, additive-only payloads
   (`opennvr.events.plate.recognized.v1`), carrying the
   `correlation_id` KAI-C's audit chain already threads. Contracts are
   Phase 0 because they are cheap, unblock everything else, and
   immediately fix today's duplicate plate producers.

4. **Camera assignment is declarative.** The registry holds *desired
   state* — "skill `plate.recognition` → cam-2, cam-4" — and pipeline
   and apps **reconcile** against it, Kubernetes-style. Not imperative
   RPC between apps: reconciliation makes restarts free, conflicts
   visible, and the existing Assignments editor the single UI for every
   skill rather than one of three competing surfaces.

5. **Budgets at KAI-C.** Once any app can subscribe to any skill, one
   busy camera can fan out into N apps × Tier-1 calls. Tier-0 already
   solves this shape (bounded load, region shedding); the same budget
   concept lands at KAI-C, which sees every inference and is therefore
   the natural metering point.

6. **Scopes on subscription.** Plates and faces are PII. "Any app can
   query the registry" must never mean "any app can receive plate
   events." Skills carry scopes granted at install (mobile-permission
   style), enforced at the bus/API boundary, audited like inference.
   For the compliance-driven deployments this project targets, "plate
   data has an ACL and an audit trail" is a feature, not plumbing.

7. **Shared dependencies are refcounted.** A required adapter that
   already exists is *reused*, never duplicated — and uninstall
   *releases* a dependency, never removes it while another app holds
   it. If LPR and smart-doorbell both depend on yolov8, uninstalling
   LPR leaves yolov8 running. Versioning rule: one pinned adapter set
   per platform release; an app requiring outside that set fails
   install loudly rather than fragmenting the pins (the
   CAPTION_ADAPTER_TAG lesson, made a rule).

8. **Camera assignment has union semantics, refcounted per consumer.**
   The assignment table records *which consumer wants a skill on which
   camera*; the skill runs on the union. LPR wants plates on cam-2 and
   the agent wants them on cam-4 → active on both; the agent unassigns
   cam-4 → the skill *shrinks* to cam-2, it does not switch off. A
   skill goes dormant only when its last assignment is released. The
   activation itself need not provision anything: enabling an
   already-capable path (Tier-0 forwards vehicle detections for a
   camera) and provisioning a new adapter are the same registry event —
   a skill is a **capability path activated** (detector class × cameras
   × consumer), not a container started.

9. **Standard chains are declarative routes, executed by Tier-1
   dispatch — not by apps.** The mechanism already exists
   (`detect-pipeline/dispatch.py`): a class→adapter routing map; on a
   gate escalation the dispatcher runs the mapped adapter **once, on
   the track's best-frame crop, through KAI-C**, bounded
   (`DETECT_DISPATCH_MAX_INFLIGHT`, per-camera share) and best-effort
   (a dead adapter never stalls Tier-0); KAI-C publishes the result.
   "Detect X, then analyse the crop with Y" is therefore one routing
   row (`vehicle → fast_plate_ocr`, `person → insightface`), bound to
   the assignment table, run once however many apps consume — the
   app-orchestrated alternative runs the same inference once per
   interested app, or couples apps to each other. Apps orchestrate
   inference themselves only for genuinely custom logic (the escape
   hatch), and plate/face routes stay opt-in rows, never defaults —
   they are privacy-sensitive by jurisdiction.

## The lifecycle (normative)

```
installed → provisioned (deps resolved or reused, refcounted)
          → dormant     (capable; zero marginal cost)
          → active      (cameras assigned — union across consumers)
          → degraded    (a dependency stopped answering; status says which)
```

Dormant is a first-class state, not an accident of config: an installed
skill consumes nothing until a consumer assigns it work, because
default-on-everywhere burns CPU no app benefits from.

## Current state (grounded)

What exists today, file-verified:

- **Bus + subjects**: NATS, `opennvr.inference.*`, alerts fanned back
  onto the bus (`nats_alerts_url` in app configs). RFC-0001 principle 1
  holds.
- **Adapter contract + KAI-C**: one `/infer` shape, registration,
  audit, per-call `correlation_id`. Eight adapters published to GHCR.
- **Tier-0 per-camera assignment**: capability assignments, the
  Assignments editor, opt-in skip for unassigned cameras.
- **App contract surface**: `/health /manifest /state` on every
  first-party app (internal-only ports 92xx).
- **Event-driven consumption**: PR #269 converted occupancy-counting
  and footage-search to consume Tier-0 events.
- **Tier-1 dispatch (the chain executor, decision 9)**: gate
  escalation → declarative class→adapter route → one audited KAI-C
  call on the best-frame crop → result published to
  `opennvr.inference.<adapter>.<cam>.completed`. Off by default,
  budgeted, best-effort. `car → caption` ships as a default row;
  plate/face are deliberate opt-ins.
- **Skill registry (agent-local)**: the camera agent derives skills,
  maps each to `suggested_adapters`/`suggested_apps`, greys out what an
  install would enable, and honours enabled skills in its tool
  advertising.
- **Plate enrichment**: core's `events_plate_enrichment` OCRs vehicle
  visits through KAI-C and stores `plate_text` on event rows — plates
  are already searchable from the agent's history path.

## The seven gaps

1. **No dependency provisioning on install.** Observed live 2026-08-29:
   `--profile apps up license-plate-recognition` succeeds; no compose
   file runs or registers `fast-plate-ocr-adapter`; every OCR call is
   doomed. Manifests declare nothing actionable.
2. **LPR duplicates detection.** `subscribes=None  # FrameApp: drives
   inference itself via KAI-C` — it polls frames and runs its own
   YOLOv8 calls while Tier-0 detects the same vehicles. Double CPU for
   identical work. PR #269 stopped at occupancy + footage-search.
3. **Skills are agent-local.** No platform endpoint answers "what can
   this system do right now"; each consumer would have to re-derive it.
4. **Three camera-assignment surfaces.** Tier-0's editor, the agent's
   assignable skills, and per-app YAML (LPR ships a placeholder camera
   at a placeholder IP). No shared source of truth.
5. **No app-UI convention.** Apps that want a UI (LPR should have one)
   have no routing/hosting convention; the agent got one only by
   owning its own port.
6. **No canonical event contracts.** Two independent plate producers
   (core enrichment; LPR alerts) with no shared schema a subscriber
   can rely on; app-level events are ad hoc.
7. **No dormant state.** Installed apps start working immediately on
   whatever their config names (LPR: the placeholder). "Installed →
   capable → idle until assigned" is not a platform lifecycle.
8. **The flagship app is invisible to its own platform.** 12 of 13
   first-party examples extend an SDK base class (`Detector`,
   `FrameApp`, or `AlertSubscriber`) and therefore serve the §03
   contract (`/health /manifest /state`) and self-register. The one
   exception is the **camera agent**: it imports SDK utilities but
   extends no base, serves no `/manifest` or `/state`, and never
   registers with the App Catalog — so the registry's app-manifest
   source (decision 2) cannot see the platform's most important app.

## The SDK rule (conformance)

Every first-party example app extends an SDK base class — that is what
makes "copy an example, write your predicate" a real contributor path
rather than folklore. The rule is CI-enforced
(`server/tests/test_apps_ride_the_sdk.py`): an app that extends no base
fails the build unless it is on the explicit allowlist, and an
allowlist entry for an app that HAS since conformed also fails — the
allowlist may only shrink. The camera agent is the sole allowlisted
debt, retired by the Phase 1 contract-parity task below (full
base-class migration is not the goal for it; serving the contract and
registering is).

## Phases

Ordering rule: contracts → registry → assignment → provisioning →
reference implementation → budgets/scopes. Each phase is independently
shippable and useful.

### Phase 0 — Event contracts (docs + convergence; no new services)

- [ ] `docs/EVENT_CONTRACTS.md`: envelope (id, `correlation_id`,
      camera_id, ts, producer, schema version), subject naming
      (`opennvr.events.<domain>.<event>.v<N>`), additive-only rule.
- [ ] v1 schemas for what already flows: `detection.observed`,
      `visit.recorded`, `plate.recognized`, `alert.fired`.
- [ ] Decide the domain/adapter event split: Tier-1 dispatch results
      land today on adapter-shaped subjects
      (`opennvr.inference.fast_plate_ocr.<cam>.completed`); either a
      thin normaliser maps them to domain events
      (`plate.recognized.v1`) or the domain schema rides in the
      completion payload. Pick one; both is drift.
- [ ] Converge the plate producers on that answer: Tier-1 dispatch is
      the producer; core's `events_plate_enrichment` stops running its
      own OCR and consumes the dispatched event instead (it is the
      duplicate, not the canonical path).
- [ ] Conformance check in CI: subjects used by first-party apps must
      appear in the contracts doc.

**Accept when:** a subscriber can consume plates without knowing which
producer fired them.

### Phase 1 — Registry as index

- [ ] `GET /api/v1/skills` on core: id, provider (app|adapter), the
      event subjects it publishes, config surface, status
      (`available | dormant | active | degraded | missing-dependency`),
      derived live from the four sources in decision 2.
- [ ] Status derives from real signals, not process-up (issue #344's
      lesson): adapter reachable via KAI-C, last event published,
      app `/health`.
- [ ] The camera agent's skills panel consumes this endpoint instead of
      its private derivation (its `suggested_apps` on-ramp becomes a
      registry field, so every consumer gets it).
- [ ] **Agent contract parity (retires gap 8):** the agent mounts
      `/manifest` and `/state` in its existing FastAPI, self-registers
      via the `AppRegistryClient` it already carries, and comes off the
      conformance allowlist. Not a base-class rewrite — contract
      parity only, so the registry and App Catalog finally see the
      flagship.
- [ ] App Catalog shows the skills each installed app provides.

**Accept when:** the agent's skills panel renders from the platform
endpoint with no behaviour change, and a second consumer (App Catalog)
renders the same truth.

### Phase 2 — Declarative camera assignment

- [ ] One assignment table: (skill, camera_id, **consumer**, params) —
      desired state with union semantics per decision 8, exposed under
      `/api/v1/skills/{id}/cameras`. Releasing one consumer's
      assignment shrinks the union; the last release makes the skill
      dormant.
- [ ] Tier-0 reconciles its capability assignments from it (the
      existing editor becomes this table's UI).
- [ ] Apps reconcile: the app SDK grows a watcher so an app's active
      camera set follows the table; per-app YAML camera lists become a
      seed/fallback, not the source of truth.
- [ ] The agent's assignable-skills flow reads/writes the same table.

**Accept when:** assigning a camera to a skill in ONE place changes
what Tier-0, the app, and the agent all do — and unassigning makes the
skill dormant (gap 7 closes here).

### Phase 3 — Install-time provisioning

- [ ] Manifest grows `requires: {adapters: [...]}`.
- [ ] Compose wiring per app: adapter service + one-shot KAI-C register
      sidecar (the caption-adapter-register pattern, already proven in
      the camera-agent overlay).
- [ ] **Fast-track, independently shippable now:** LPR's compose gains
      `ghcr.io/open-nvr/fast-plate-ocr-adapter` + register sidecar, so
      the app installed today can actually work.
- [ ] Provisioning is refcounted per decision 7: an existing adapter is
      reused; uninstall releases, never removes while held; versions
      resolve against the release's pinned set or fail loudly.
- [ ] Install refuses (or clearly warns) when a required adapter cannot
      be provisioned — the smart-doorbell NOTE becomes machine-checked.

**Accept when:** a fresh `--profile apps up license-plate-recognition`
yields a working chain with zero manual adapter steps.

### Phase 4 — LPR as the reference implementation

- [ ] The chain moves into the platform (decision 9): enable the
      Tier-1 route `car/truck/bus → fast_plate_ocr` bound to the
      skill's assigned cameras, with the gate in enforce there. One OCR
      per vehicle **visit** (best-frame), not per frame or per app.
- [ ] Convert LPR from FrameApp (poller) to pure consumer: subscribe
      to the plate event, apply watchlist/dedup/severity logic, fire
      alerts. Zero inference code in the app. (Closes gap 2; the CPU
      win is the entire redundant YOLO pass plus never running OCR
      more than once per visit.)
- [ ] Give LPR the app UI the model promises (watchlist editor, recent
      reads, per-camera stats), behind an app-surface routing
      convention (core proxies `/apps/{app}/ui` to the app's contract
      port — smallest workable convention; alternatives welcome).
- [ ] The camera agent consumes `plate.recognized.v1` (in addition to
      today's enrichment path) so "what plates today?" answers from
      the contract, proving cross-app consumption end to end.

**Accept when:** the walkthrough in the Summary is literally true on a
fresh install.

### Phase 5 — Budgets and scopes

- [x] Per-skill Tier-1 budgets at KAI-C (calls/min per camera per
      skill), with shed-and-report semantics like Tier-0's region
      shedding — never silent drops. *(shipped — `kai_c/budgets.py`:
      sliding per-(adapter, camera) windows, env-configured
      (`KAIC_BUDGET_PER_CAMERA_PER_MIN` + per-adapter overrides),
      429 + `inference.refused_budget` audit +
      `kaic_budget_shed_total` metric + rate-limited WARNING; calls
      without a camera_id are exempt.)*
- [x] Subscription scopes: manifests request event scopes
      (`events:plate.recognized`); install grants; grants visible in
      App Catalog; every grant audited. *(shipped —
      `AppManifest.requires_scopes`; registration auto-grants and
      writes one `app.scope_granted` audit row per scope; the catalog
      renders the granted scopes on the app page; LPR declares
      `events:plate.recognized`.)*
- [ ] **Bus-side scope enforcement** *(staged follow-up — the one
      Phase 5 piece not shipped)*: today every bus client shares the
      deployment token, so a granted scope is policy + audit, not a
      wire-level barrier. The enforcement design: NATS authorization
      config with one user per installed app, whose `subscribe`
      permissions are exactly its granted scopes' subjects; core
      renders that config from the grant table on install/uninstall
      and reloads the broker. Requires per-app credential delivery to
      app containers — a deployment-surface change big enough to want
      its own review.

**Accept when:** an app without the plate scope cannot receive plate
events, and a runaway subscriber degrades predictably instead of
starving the stack.

## Non-goals

- No new message broker, no service mesh, no rewrite of KAI-C or the
  app SDK — extensions only.
- The registry does not proxy data; see decision 1.
- Tier-0's base detector vocabulary is not app-extensible in this RFC;
  apps extend at Tier-1 (crop → specialist adapter). A future RFC may
  revisit pluggable Tier-0 heads (the RF-DETR eval harness is the
  groundwork).

## Open questions

1. Registry write-path for third-party apps: manifest-only, or an
   explicit register call with review? (Leaning manifest-only + App
   Catalog approval, mirroring adapter approval in KAI-C.)
2. Where does the assignment table live — core DB (leaning yes: core
   already owns cameras and the Assignments editor) or its own service?
3. UI convention for apps: core reverse-proxy (leaning yes) vs
   published ports vs iframe embedding in App Catalog.
4. Do contracted events get persisted centrally (RFC-0001 C1's event
   store) or stay bus-only with per-app persistence? (Leaning: the
   canonical store subscribes to contracted subjects — one more
   consumer, no special path.)
