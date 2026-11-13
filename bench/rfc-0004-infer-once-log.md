# RFC-0004 — infer once, publish once: the plan, the measurements, the record

The design is [`docs/rfcs/RFC-0004-infer-once-publish-once.md`](../docs/rfcs/RFC-0004-infer-once-publish-once.md) (merged as PR #908).
This file is its ledger: what was planned, what was measured before and
after each step, and what actually shipped. Every number here is a
measurement on the Ventuno Q (QCS8275, Hexagon HTP) unless a line says
otherwise. Nothing is added to the "measured" columns from a datasheet
or an estimate.

## The plan, in order

| # | Item | PR | State |
|---|---|---|---|
| 0 | this ledger | — | written 2026-11-13 |
| 1 | one structured call per best frame (caption + attributes in one answer; per-question fallback) | this PR | code done 2026-11-13, flag off; box measurement next |
| 2 | `visit.enriched.v1` on the bus; result cache (frame hash, model fingerprint, prompt) | — | planned |
| 3 | description channel: live tracks + freshest result; one shared gated call on a new track or a stale result | — | planned |
| 4 | agent live look and Mirror onto 2 and 3; direct adapter calls removed | — | planned |
| 5 | SDK frame-consumer rule (credits, latest frame wins, measured cadence, tracker continuity, skipped counter); KAI-C stream proxy adopts it | — | planned |
| 6 | KAI-C capability tokens (RFC-0001): adapters accept only platform-minted calls | — | planned |
| 7 | durable record subjects (visits, enriched visits, plates, alerts) with acks and replay | — | planned, last |
| — | the brake: detector latency vs its floor, device busy share (governor) | #905 | merged 2026-11-13; not yet measured on the box |

## Baseline, before any step (measured 2026-11-11 and 2026-11-12)

Office camera (cam23, CP Plus H.265 substream) plus the looping plate
clip (cam26, 864×576), the agent on all cameras, descriptor people
questions on, accurate plate mode.

| Measurement | Value | Where |
|---|---|---|
| Detector inference on the HTP, alone | 8.6 ms | `ventuno-q-benchmark-2026-10-30.md` |
| Detector in the stack, quiet floor | 24–26 ms | `ventuno-q-anpr-htp-2026-11-11.md` |
| Detector while Gemma held the HTP | 67–119 ms (2.7–4.8× the floor) | soak notes 2026-11-12 |
| Gemma calls, ten busy minutes (11:07–11:17 UTC) | 325: 225 question calls, 100 captions | adapter counters |
| Gemma NPU time in that window | 290 s of 600 (48 %) | adapter latency sums |
| Gemma calls per person visit (people questions on) | up to 6: 1 caption + 5 questions | `descriptor_enrichment.KIND_QUESTIONS` |
| Gemma calls per vehicle visit | 3: 1 caption + 2 questions | same |
| One caption or one question | 1.6–2.0 s wall, 96 output tokens cap | `ventuno-q-npu-captioner-2026-11-05.md` |
| One SigLIP embedding | 6–8 ms | `ventuno-q-npu-embedder-2026-11-10.md` |
| Captioned visits in a busy office hour | 600–660 | soak notes |
| Search eval (33 labelled cases, caption arm on bge-small) | MRR 0.71, hit@5 0.82 | `ventuno-q-text-embed-2026-11-10.md` |
| Direct adapter callers outside the gate | the agent's live look; the Mirror's loop | code |
| Enrichment results published on the bus | none (stored only) | `docs/EVENT_CONTRACTS.md` |

## Step 1 — one structured call

**Planned.** `caption_enrichment.enrich_event_caption` delegates, when
`EVENTS_ENRICHMENT_SINGLE_CALL` is on and the visit's class has
descriptor kinds, to one call that asks for a one-sentence caption and
the kinds' answers in a JSON object. The caption lands in `event_text`
and the answers in `visit_descriptors` through the same normalisation
as the per-question path. A model that returns no usable JSON is given
the per-question path for that visit, and a counter says how often.
`descriptor_enrichment` leaves a visit alone when the single call will
carry it, and asks as before for a visit that already has its caption.

**To measure (before the default flips):**
- Gemma calls per visit, by class, from the adapter's counters over the
  same ten-minute window shape as the baseline.
- Gemma share of the NPU and the detector's mean latency in that window.
- Caption quality: the 33-case search eval on the box (MRR, hit@5),
  which needs an operator login on the box.
- Attribute coverage: `visit_descriptors` rows per visit by kind, and
  the share of answers the vocabulary could not reduce, single call
  against per-question on the same hour of traffic.
- JSON failure rate of the structured answer.

**Measured.** _pending — the box runs the flag off until the two
ten-minute windows below are taken._

Method for the measurement, so it is the same both times:
1. Same traffic shape as the baseline: cam23 (office) + cam26 (plate clip
   loop), the agent on cam23 only, people questions on, accurate plate mode
   off (fast) so the plate reader does not sit in the window.
2. Window A, flag off: ten minutes; note the captioner's call count and
   latency sum, the detector's mean latency, `visit_descriptors` rows by
   kind for visits in the window, captions written.
3. Flip `EVENTS_ENRICHMENT_SINGLE_CALL=true`, recreate core, wait for
   `STABLE_S`; Window B, same ten minutes, same counters, plus the
   `structured_enrichment.STATS` counters (calls, json_ok, json_failed,
   claims, captions).
4. Search eval (33 cases) after an hour on each setting, operator login
   needed on the box.

**Done.** 2026-11-13, branch `feat/rfc-0004-single-call`:
- `server/services/structured_enrichment.py` — prompt (caption + the
  kinds' questions with their vocabularies, JSON-only answer, `unknown`
  allowed), parser (fenced / prose-wrapped object, strict on shape),
  `claims_from` through `normalise_answer`, `kinds_for_visit` with the
  descriptor enricher's three filters plus "the adapter must be one the
  plan lists for VQA", `ask_structured` through the caption gate
  (`kind="caption"`, VQA-shaped body, no `task`), `STATS` counters.
- `caption_enrichment.enrich_event_caption` — with the flag on, reads
  the answered kinds in Phase 1, makes the one call in Phase 2, writes
  caption + descriptors in Phase 3; VQA recorded as ran only on a JSON
  answer; a prose answer is the caption and the questions wait.
- `descriptor_enrichment._single_call_carries` — the descriptor task
  returns early when the caption task will carry the visit (flag on,
  caption lane enabled, captionable class, camera assigned
  `image_captioning`, no caption yet); a visit with its sentence or a
  camera without captioning is asked as before.
- Setting `events_enrichment_single_call` (default False), env knob,
  CHANGELOG entry, 16 tests in `server/tests/test_structured_enrichment.py`;
  the eight enrichment suites pass (178).
- Not done: the box measurement; the default stays off until it is in.
