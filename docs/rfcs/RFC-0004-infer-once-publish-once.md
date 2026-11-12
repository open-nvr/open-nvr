# RFC-0004 — Infer once, publish once: the costly models as a shared tier

**Status:** PROPOSED (2026-11-12). Design first; implementation is the
first work after 0.1.11. The brake that stops any of this from slowing
the detector (#905, the enrichment governor reading the detector's own
latency) ships in 0.1.11 independently of this RFC.
**Blocks:** the Mirror's own VLM loop, the camera agent's direct live
look, any app that would run a caption or question model itself
**Author:** OpenNVR

## The design this restates

OpenNVR's architecture has two tiers and one rule between them.

The **detection tier** runs one cheap detector per camera, all the time,
and turns frames into tracks, visits and a best frame per visit. It
publishes those once on the bus. Apps subscribe; none of them runs a
detector of its own. This tier is built as designed and is not the
subject of this RFC.

The **enrichment tier** runs the costly models — a vision-language model,
an embedder, a plate reader — on the best frame of a visit, **once**, and
makes the result available to **every** consumer: search, the agent, the
Mirror, any app. The rule is the one every subscriber of the detection
tier already lives by: *the platform computes, the app consumes*. An app
that calls the caption model itself is a raw app, however good it is; it
pays for the model a second time, outside the budget that protects the
detector, and the next app pays a third time.

## Where the code stands, measured

Ventuno Q, 2026-11-12, a busy office hour plus a looping plate clip
(bench numbers in `bench/ventuno-q-anpr-htp-2026-11-11.md` and the
soak notes of that day):

| What | Follows the rule? | Evidence |
|---|---|---|
| Detector, tracks, visits, best frame, published on the bus | yes | occupancy, the plate app and alerts consume events; none runs a model |
| Caption, attributes, embedding computed once per visit in core | yes, for the computation | the three enrichers run on the best frame behind one gate per adapter |
| **One inference per best frame** | **no** | a person visit is one caption **plus up to five separate questions** to the same model on the same frame: six inferences at ~2 s each on the Hexagon |
| **Result published to subscribers** | **no** | core publishes `plate.recognized`, `occupancy.*`, `alert.fired`, `overlay.boxes`; captions and attributes are only written to the store, so a consumer must poll the API |
| The camera agent's live look | **no** | fetches a frame and calls the VLM directly, outside the pipeline and outside the gate |
| The Mirror | **no** | runs its own loop of VLM calls on crops it fetches itself |

The cost of the two "no" rows inside core was 325 Gemma calls in ten
minutes (225 of them questions), 48 % of the NPU, and a detector running
at four times its quiet latency. The agent and Mirror rows add to that
whenever a person is using them.

## What changes

### 1. One structured inference per best frame

The caption enricher and the descriptor enricher merge into one call per
visit. The prompt asks for a JSON object — a one-sentence caption plus the
attribute fields the vocabulary already defines (vehicle: colour, type;
person: top colour, carrying, and the people fields when enabled) — and
the answer is parsed into the same rows the two enrichers write today:
`event_text.caption` and `visit_descriptors` claims. Nothing downstream
changes shape.

Cost: a person visit goes from two to six model calls to one. The answer
is a few more tokens than a caption; measured first on the Ventuno Q
before the default flips (acceptance below). Models that cannot follow a
JSON instruction (the smallest CPU captioners) keep the per-question path
behind the same interface; the adapter's answer decides.

### 2. Publish what was computed

A new domain event, `opennvr.events.visit.enriched.v1`, the sibling of
`visit.recorded.v1` (which says a visit exists), published by core when a
visit's enrichment finishes (or when a field of it does, if
the embedding lands later than the caption):

```json
{
  "schema": "visit.enriched.v1",
  "camera_id": 23,
  "event_id": 20046,
  "ts": "2026-11-12T11:08:49Z",
  "payload": {
    "caption": "a white hatchback entering from the left",
    "attributes": {"vehicle_colour": "white", "vehicle_type": "hatchback"},
    "embedding_id": "clip:siglip2/w8a16-qnn:20046",
    "evidence_path": "af/af206e…jpg",
    "producer": "litertlm:gemma-4-e2b"
  }
}
```

Subscribers get the result pushed, in the SDK's `DomainEventSubscriber`
like every other event. The store stays the memory for anything that
asks later. The dashboard's accelerator tile and the governor's
`enrichment_throttled` events already say when enrichment is yielding;
a subscriber that sees no `visit.enriched` for a visit knows why from
those, and the backfill delivers it later.

### 3. A shared "latest description" for live askers

Some askers want *now*, not the last visit: the agent answering "what do
you see on the gate camera", the Mirror greeting a person who just
walked up. For them core offers one path:

`GET /api/v1/cameras/{id}/description?max_age_s=N` — the newest
enriched description of that camera if it is younger than N seconds;
otherwise **one** gated call on the current best frame, whose answer is
handed to every caller that asked within the same second (single-flight),
written to the store, and published as `visit.enriched` like any other.

The call goes through the enrichment gate and the governor, so it pays
into the same budget and yields to the detector like everything else.
An interactive caller still never waits behind the backfill (the
governor's `interactive` rule stands).

### 4. The agent and the Mirror become subscribers

- The camera agent's live look calls the description endpoint instead
  of the adapter. Its scene summaries already read the store.
- The Mirror subscribes to `visit.enriched` for its camera for the
  running commentary, and uses the description endpoint, with a short
  `max_age_s`, only for the confirmation look when a new person appears.
  Its own VLM loop goes.
- `docs/APP_PLATFORM.md` states the rule for app authors: consume
  `visit.enriched`; an app that needs a question answered about a frame
  asks core, never an adapter. The adapter endpoints stay reachable for
  the one legitimate direct user, KAI-C.

## What this does not decide

- Which fields the structured answer carries beyond today's vocabulary.
  The vocabulary is RFC-0003's concern; this RFC only moves its filling
  into one call.
- Whether the people questions default on. That is a per-flavour
  default (`EVENTS_DESCRIPTOR_PEOPLE`), and with one call per visit the
  cost argument against them mostly disappears.
- Plate reading. It already runs once per vehicle track with its own
  consensus and publishes `plate.recognized`; it is the model for the
  other enrichers, not a thing to change here.

## Acceptance

Measured on the Ventuno Q, same office hour and the same looping clip
as the 2026-11-12 numbers:

1. Gemma calls per visit: one (people questions on), where it is two to
   six today, with the caption and the claims in the store unchanged in
   kind and the labelled search eval (`tools/search_eval.py`, 33 cases)
   within noise of today's MRR 0.71.
2. `visit.enriched` observed by a test subscriber for every visit that
   got a caption, within the governor's pacing.
3. The agent's live look and the Mirror make zero direct adapter calls
   (the adapter's `adapter_infer_total{task=""}` counter does not move
   while both are in use), and the Mirror's commentary still updates
   within one enrichment interval of a visit.
4. With all three running, the detector's mean latency on camera 23
   stays under twice its floor across the hour; today it is four times.

## Order of work

1. The structured single call (core only; largest saving, no API change).
2. `visit.enriched` on the bus and in the SDK (core, SDK, docs).
3. The description endpoint with single-flight (core).
4. Agent live look, then the Mirror, onto 2 and 3; remove their direct
   calls.
