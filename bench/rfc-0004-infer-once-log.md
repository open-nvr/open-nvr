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

**Measured — paired frame comparison, run 1 (2026-11-13 01:47–01:56 UTC).**
`tools/rfc0004_paired_compare.py` on the box, as a one-off container from
the merged core image, calls sequential, through KAI-C's infer route like
core itself. 70 evidence frames from the last 24 h: 27 car, 13 truck
(all cam26, the plate clip), 30 person (11 cam23 office, 19 cam26).
Arm A = plain caption call, B = one question per kind (2 per frame),
C = the one structured call. The box was under its normal load (agent on,
plate reader in accurate mode), so absolute times are a busy box's.

| Measurement | Vehicle frames (40) | Person frames (30) |
|---|---|---|
| A, plain caption (adapter inference_ms, mean) | 1069 ms | 1418 ms |
| B, two questions together | 1446 ms (723 each) | 1446 ms |
| Old path A + B | 2515 ms, 3 calls | 2864 ms, 3 calls |
| C, one structured call | 1782 ms | 1813 ms |
| Saving per visit | 29 % of Gemma time, 2 calls fewer | 37 %, 2 calls fewer |

Over all 70 frames: old 2665 ms, new 1796 ms, ratio 1.48. JSON parsed on
70 of 70 (Gemma wraps it in a code fence; the parser takes that). No reply
hit the 96-token cap (mean reply 120 characters). No call failed.

| Kind | Frames | Agree | Differ | B only | C only | Neither |
|---|---|---|---|---|---|---|
| colour | 40 | 34 | 6 | 0 | 0 | 0 |
| vehicle_type | 40 | 31 | 9 | 0 | 0 | 0 |
| clothing_top | 30 | 30 | 0 | 0 | 0 | 0 |
| carrying | 30 | 26 | 1 | 3 | 0 | 0 |

Looked at the disputed frames: all six colour disagreements are one
scene, a white van in front of a yellow ambulance, where B said white
(right) and C said yellow; the type disagreements are a goods carrier
(B truck, right; C bus), a white crossover (B suv, C van) and a black
Maruti SUV the detector had labelled truck (B truck, C pickup, both
wrong). The three "B only" carrying answers are C saying "unknown" where
B said "nothing" — conservative, not wrong. Pattern: C's answers drift
to the whole scene. The cause is order: the caption is generated first,
and every attribute value is conditioned on it ("A yellow and white
vehicle…" → colour yellow).

Captions: A averages 17 words with a preamble ("A security camera view
shows…"), C 9.7 words and terser — in one office frame A said "a man in
a red shirt sitting in a chair", C said "a man is sitting at a desk with
a laptop", losing the colour search would want.

**Decision from run 1:** cost saving real but smaller than the call
count suggests (1.5×, not 3×: a question is 0.7 s, a caption 1.1–1.4 s,
the structured reply 1.8 s). Accuracy: attributes 90 % agreement on
vehicles, 97 % on people, and where they differ the per-question answer
is the right one. Not good enough to flip the default. Run 2 changes the
prompt only: attribute keys first and caption last (so the answers are
conditioned on the image, not on the caption), the subject named ("the
main vehicle, the largest or nearest"), and the caption asked for with
the plain prompt's instruction (who or what, what they wear, what they
do) so search loses no words.


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

**Measured — paired frame comparison, run 2 (2026-11-13 01:58–02:07 UTC),
same 70 frames, prompt revised as decided above.** The per-question arm
answered identically in both runs (140 of 140), so the model is
deterministic and the difference is the prompt.

| Kind | Frames | Agree run 1 → run 2 | Differ | B only | What the differences are |
|---|---|---|---|---|---|
| colour | 40 | 34 → **40** | 0 | 0 | — |
| vehicle_type | 40 | 31 → **36** | 4 | 0 | one white crossover, four frames: B suv, C van (toss-up on the frame) |
| clothing_top | 30 | 30 → 29 | 1 | 0 | two people in the crop: B red (the one behind), C white (the nearer, as asked) |
| carrying | 30 | 26 → 27 | 2 | 1 | a bag on the chair, not carried: B backpack, C nothing; B nothing, C unknown |

Cost unchanged: A 1223 ms, a question 718 ms, C 1789 ms; old 2659 ms vs
new 1789 ms, ratio 1.49; 70 of 70 JSON, none cut by the token cap.

Captions, run 2: 10.8 words against A's 17, but A spends its words on a
preamble ("A security camera view shows…", "The image is a blurry,
low-resolution shot of…"). What search wants is there more often: a
colour word in **70 of 70** C captions against 45 of 70 A captions (run 1's
terse prompt: 59). The red-shirt frame now reads "A man wearing a red
shirt is sitting in a chair and looking at a laptop"; the van-and-ambulance
frame "A white van is parked on a dirt road".

**Decision from run 2:** attributes match the per-question path on 132 of
140 answers, and none of the 8 differences is a case where the
per-question answer is clearly right and the structured one wrong. The
caption carries the words search filters on more often than the plain
prompt does. The prompt revision ships; the flag can go on for the
traffic windows. The default flips only after window B and the 33-case
search eval.

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
- 2026-11-13, branch `bench/rfc-0004-paired-compare`: `tools/rfc0004_paired_compare.py`
  (the paired comparison above), `tools/rfc0004_window.sh` (one counter
  snapshot for a traffic window), and the prompt revision from run 1
  (attributes first, subject named, caption asked for with the plain
  prompt's detail). Raw per-frame answers of both runs kept off the repo
  (they describe people in the office).
