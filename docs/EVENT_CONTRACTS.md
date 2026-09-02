# OpenNVR Event Contracts

Status: **normative** (RFC-0002 Phase 0). Every subject a first-party
app or service publishes or subscribes to on the bus MUST appear in
this document — CI enforces that (`server/tests/test_event_contracts.py`).

This document is the answer to "what can I subscribe to, and what will
I get?" — without reading any producer's source code. RFC-0002
decision 3: contracts come before the registry, because a registry
that indexes unversioned, producer-shaped events indexes drift.

## The envelope

Every **domain event** (the `opennvr.events.*` tree, defined below)
carries these fields at the top level of a JSON object. Producers MUST
set all of them; consumers MUST tolerate extra fields (additive-only
rule, next section).

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Unique per event. `evt_` + 12 hex chars. |
| `schema` | string | The versioned schema name, e.g. `plate.recognized.v1`. Redundant with the subject on purpose — payloads get stored, forwarded, and read without their subject. |
| `correlation_id` | string \| null | Joins the event to the KAI-C audit trail and to every other event in the same causal chain (alert → inference → adapter audit line). Thread it, never mint a new one mid-chain. |
| `camera_id` | string | The camera this event is about. |
| `ts` | string | ISO-8601 UTC wall-clock time of the *observation* (not of publish). Monotonic clocks never appear in contracts — see the `wall_ts` lesson in `detect_pipeline/bus.py`. |
| `producer` | string | Who published: `tier1-dispatch`, `kai-c`, `core`, or `app:<name>`. Consumers MUST NOT branch on it (that is the whole point); it exists for audit and debugging. |
| `payload` | object | The schema-specific body, defined per event below. |

## Subject naming

```
opennvr.events.<domain>.<event>.v<N>.<camera_id>
```

* `<domain>.<event>` — noun.verb-in-past-tense: `plate.recognized`,
  `visit.recorded`. The domain is the *subject matter* (plate, visit,
  detection, alert), never the producer or the adapter.
* `.v<N>` — major version, part of the subject so a v2 can run beside
  v1 during migration and subscribers pick explicitly.
* `.<camera_id>` — last token, so `opennvr.events.plate.recognized.v1.>`
  subscribes to all cameras and `...v1.cam-front` to one.

### Versioning: additive-only

Within a version, changes may only **add** optional fields. Renaming,
removing, retyping, or changing the meaning of an existing field
requires `v<N+1>` — published in parallel until consumers migrate,
then the old version is retired in a minor release with notice.
Consumers MUST ignore unknown fields (`extra="ignore"` in Pydantic
terms). This is the same rule KAI-C's `InferenceCompletedEvent`
already follows; it is now the rule everywhere.

## The two trees: domain events vs adapter completions

Two event families exist on the bus, on purpose:

1. **Adapter completions** — `opennvr.inference.<adapter>.<camera_id>.completed`
   (and `.gate`). Published by KAI-C after every successful infer call
   and by Tier-0 for its own frames. Shaped by the *adapter's*
   response. These are **plumbing**: correct for consumers that care
   about the inference machinery itself (metrics, audit, the
   inference-listener example). They are versioned by the adapter's
   own schema field (`opennvr.tier0.v1`) and are NOT the surface apps
   should build business logic on.

2. **Domain events** — `opennvr.events.<domain>.<event>.v<N>.<camera_id>`.
   Producer-independent, envelope-carrying, versioned by contract.
   **This is the surface for apps.**

### The split decision (RFC-0002 Phase 0, decided)

Tier-1 dispatch results land today on adapter-shaped subjects only.
The decided rule: **the completion publish site also emits the domain
event** — a thin normaliser at the source, not a new service.
Concretely, KAI-C's `_publish_inference_completed` consults a small
static map (adapter name → domain schema + payload extractor) and,
when the adapter has a domain mapping, publishes the domain event
right after the completion. Both events carry the same
`correlation_id`.

Why this over the alternatives considered:

* *A standalone normaliser service* is a new moving part whose only
  job is to re-publish; on a platform that runs on one box, that is
  cost without isolation benefit.
* *Domain schema riding inside the completion payload* leaves
  subscribers needing to know adapter subjects — which fails Phase
  0's acceptance test ("a subscriber can consume plates without
  knowing which producer fired them").

The map lives in KAI-C beside the publisher, is part of this contract
(the table below), and growing it is a docs-reviewed change:

| Adapter | Domain event emitted |
|---|---|
| `fast_plate_ocr` | `plate.recognized.v1` |

## Contracted events (v1)

What already flows, now with names. Payload field tables list required
fields; producers may add optional ones under the additive-only rule.

### `detection.observed.v1`

Subject: `opennvr.events.detection.observed.v1.<camera_id>`
Producer: Tier-0 (`detect-pipeline`).

One event per published Tier-0 frame result that contains at least one
track. This is the domain rendering of the existing
`opennvr.inference.tier0.<camera_id>.completed` / `opennvr.tier0.v1`
payload — that adapter-tree subject continues unchanged for plumbing
consumers.

`payload`:

| Field | Type | Meaning |
|---|---|---|
| `frame` | object | `{w, h}` in pixels — lets consumers normalise boxes. |
| `calibrating` | bool | Tier-0 still calibrating; treat tracks as provisional. |
| `tracks` | array | Same track shape as `opennvr.tier0.v1` (`id`, `label`, `conf`, `bbox`, motion fields). Normative source: `detect_pipeline/bus.py::build_payload`. |

### `visit.recorded.v1`

Subject: `opennvr.events.visit.recorded.v1.<camera_id>`
Producer: core (timeline ingestion), at the moment a finished visit
row is persisted (`services/timeline_service.py::record_track_visit`).

Today visits are DB rows only; this contract puts the fact on the bus
so apps stop polling the timeline API for "something happened".

`payload`:

| Field | Type | Meaning |
|---|---|---|
| `event_id` | int | The timeline row id — join key for the REST API. |
| `label` | string | Track label (`person`, `car`, ...). |
| `started_at` / `ended_at` | string | ISO-8601 UTC visit bounds. |
| `evidence_path` | string \| null | Best-frame evidence reference, resolvable via the evidence API (not a raw filesystem path promise). |

### `plate.recognized.v1`

Subject: `opennvr.events.plate.recognized.v1.<camera_id>`
Producer: the KAI-C normaliser (see the split decision) — regardless
of whether the OCR was initiated by Tier-1 dispatch or by core's
enrichment during the transition.

One event per **accepted** OCR read (a plate was actually extracted —
empty/failed reads do not fire).

`payload`:

| Field | Type | Meaning |
|---|---|---|
| `plate_text` | string | The normalised read, uppercase, no separators. |
| `confidence` | number \| null | Adapter-reported confidence for the read, when available. |
| `vehicle_label` | string \| null | Upstream detection label (`car`/`truck`/`bus`) when the chain knows it. |
| `event_id` | int \| null | Timeline visit row this read enriches, when initiated from a visit. |
| `plate_box` | `[x1,y1,x2,y2]` \| absent | Optional. Where the adapter localised the plate, in the pixel space of the crop it was given. Consumers use it to reject **partial** reads: a crop whose edge cuts through the plate still OCRs the surviving characters at high confidence, so `confidence` cannot distinguish `K884` (a fragment of `K884RS`) from a whole plate — only the geometry can. Absent when the adapter reports no localisation. |
| `plate_box_confidence` | number \| absent | Optional. How sure the adapter's localiser was that it had found a plate at all — distinct from `confidence`, which scores the characters. Consumers reject **false localisations** with it: a manufacturer badge reads as plausible characters (the Audi four rings OCR as `C00D`) at plausible read confidence, from a box in the middle of the crop, so neither `confidence` nor `plate_box` can catch it — but the localiser scored it 0.38 where genuine plates score 0.85+. Absent from OCR-only adapters and from producers that predate the field (consumers then have no opinion to apply). |
| `plate_box_image` | `[width,height]` \| absent | Optional. The size of the image `plate_box` is measured in — exactly the bytes the adapter OCR'd. Multi-frame OCR sends plate *candidate* crops whose size differs from the visit's evidence frame, so a consumer judging the box against the evidence file would measure in the wrong image; when this field is present it is the only correct denominator. Absent from adapters that predate it (consumers then fall back to the evidence file's size, correct for single-evidence producers). |

#### Producer convergence (Phase 0 exit criterion)

Two callers currently initiate plate OCR: core's
`services/plate_enrichment.py` (per persisted vehicle visit) and
Tier-1 dispatch (per escalated track, best-frame). Convergence:

1. The normaliser ships first — both initiators immediately produce
   the same `plate.recognized.v1`, because the event fires at KAI-C,
   the one place both paths already meet. Nothing breaks on installs
   where Tier-1 routes are disabled.
2. Core's enrichment then becomes a *consumer*: it subscribes to
   `plate.recognized.v1` and writes `plate_text` onto the matching
   visit row. Its own OCR call is demoted to fallback for events the
   dispatch path did not cover, and retires with Phase 4 (one OCR per
   visit, dispatch-initiated, best-frame).

Accept: a subscriber consumes plates without knowing which producer
fired them.

### `alert.fired.v1`

Subject: `opennvr.events.alert.fired.v1.<camera_id>`
Producer: `app:<name>` via the App SDK's NATS alert dispatcher.

The existing wire shape (`opennvr_app_sdk/alerts.py::Alert.to_wire`:
`alert_id`, `fired_at`, `title`, `description`, `severity`, `source`,
`camera_id`, `correlation_id`, `evidence`, `tags`) **is** the v1
payload — it already satisfies the envelope's spirit and every
first-party app publishes it.

Transition note: apps publish today on
`opennvr.alerts.{source.kind}.{source.name}.{camera_id}`. That tree
remains supported as the *plumbing* address (subscribers filtering by
which app fired, e.g. the alerts-subscriber example). The SDK
dispatcher dual-publishes to the domain subject so consumers that
only care "an alert fired on this camera" stop encoding app names in
subscriptions. New consumers use the domain subject.

### `access.decided.v1`

Subject: `opennvr.events.access.decided.v1.<camera_id>`
Producer: `app:license-plate-recognition` (or any app making gate
decisions — consumers must not branch on the producer, per the
envelope contract).

One event per plate read **on a gate-in camera while the publishing
app's barrier mode is enabled** — the app's admission decision as a
fact on the bus. Actuation is a separate concern on purpose: a
gateway app (the `gate-controller` example) subscribes and drives the
site's relay/barrier hardware, so decision policy and hardware wiring
evolve independently, and an audit trail of every decision exists
whether or not a barrier is attached.

`payload`:

| Field | Type | Meaning |
|---|---|---|
| `plate_text` | string | The normalised read (uppercase, no separators). |
| `decision` | string | `allow` or `deny`. |
| `reason` | string | Why: `registered`, `allowlisted`, `monitored`, `expired_pass`, `uncertain_read` (a fuzzy near-miss read — actuation is exact-match only), `unknown`. Additive: consumers must not branch on unrecognised reasons. |
| `owner` | string \| null | Registry owner, when the plate is registered (helps gate displays/logs). |
| `unit` | string \| null | Registry unit/flat, when registered. |
| `confidence` | number \| null | OCR confidence carried from the read, when available. |

Consumers MUST treat any decision other than `allow` as "do not
actuate" — unknown future decision values fail closed.

### `occupancy.changed.v1`

Subject: `opennvr.events.occupancy.changed.v1.<camera_id>`
Producer: `app:occupancy-counting` (or any app measuring zone
occupancy — consumers must not branch on the producer).

Fired when a zone's head-count CHANGES — sampled, not per-frame: the
producer publishes on every committed level transition and otherwise
at most once per ~10s per camera while the count moves. This is the
history feed behind the Occupancy page's charts; core persists the
samples (90-day retention).

`payload`:

| Field | Type | Meaning |
|---|---|---|
| `count` | int | Entities currently in the zone. |
| `level` | string | The committed alerting band: `normal`, `over`, `under`. Additive: consumers must tolerate new bands. |
| `max_occupancy` | int \| null | The zone's configured ceiling at publish time (charts scale against it). |
| `min_occupancy` | int \| null | The configured floor, when set. |

## Out of contract (deliberately)

* `opennvr.inference.>` payload internals beyond what each adapter's
  own schema field declares — plumbing, versioned per producer.
* Recording, playback, and media subjects — explicitly out of
  RFC-0002 scope; nothing in this phase touches them.
* Agent/UI websocket traffic — not bus events.

## Checklist for adding an event

1. Add the schema table here, under a `v1` heading, with subject and
   producer.
2. Envelope fields all present; payload additive-only from day one.
3. If a Tier-1 adapter produces it, add the adapter → domain row to
   the normaliser map table.
4. CI (`test_event_contracts.py`) will fail any first-party publish or
   subscribe on an `opennvr.events.*` subject that this document does
   not list — that failure is the review prompt, not an obstacle.
