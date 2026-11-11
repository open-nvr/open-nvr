# Ventuno Q — the caption arm on a sentence encoder (text-embed), 2026-11-10

What the `text-embed` adapter (`BAAI/bge-small-en-v1.5`, 384-d, ONNX on the
CPU) does for search's caption arm on the Ventuno Q, measured against the
box's own captions. Companion to `ventuno-q-npu-embedder-2026-11-10.md`, which
found that SigLIP2's text tower could not separate relevant from irrelevant
caption/query pairs (0.76–0.87 against 0.59–0.75).

**Setup.** The adapter image built on an arm64 Mac (`text-embed-adapter:local`,
808 MB) loaded on the box and run as a throwaway container with `--cpus 2`, not
registered with KAI-C. Every distinct caption in `event_text` (805 of them,
written by the NPU Gemma captioner over the past days) embedded as documents in
batches of 64; fifteen hand-written queries embedded as queries; cosine of each
query against every caption, in plain Python on the host.

## Cost

| | measured |
|---|---|
| 805 captions, batched 64, two ARM cores | 30.3 s = 37.6 ms a caption |
| 15 queries in one batch | 144 ms = 10 ms a query |
| container RSS | ~300 MB |

At the box's rate (tens of captions an hour) this is under two seconds of one
core an hour. Re-embedding the whole store is half a minute.

## Does the floor separate hits from noise

For each query: the best score over the 805 captions, and what that caption
said. The first ten queries describe things the captions contain; the last five
describe things nobody saw on this site.

| query | best | the caption it chose | reads as |
|---|---|---|---|
| person in a blue shirt | 0.80 / 0.78 / 0.78 / 0.75 | "…light-colored t-shirt…", "…light blue shirt…standing with their back to the camera", "A man wearing a light blue shirt…" | hit |
| someone carrying a bag | 0.83 / 0.82 | "A person with a black backpack…" | hit |
| someone sitting down | 0.81 / 0.78 / 0.77 / 0.76 | "…patterned top and dark jeans is sitting down", "…sitting on a surface, possibly a bench" | hit |
| two people together | 0.77 / 0.75 | "…with another person's arm visible near them", "Two men are visible in the photo…" | hit |
| a person on a phone | 0.76 | "…standing near a window, holding a smartphone" | hit (the second phone caption, "sitting on a bench…looking at a sma[rtphone]", scored 0.70) |
| person wearing a cap | 0.74 | "a close-up of a person's shoulder and upper arm" | noise (no cap on the site) |
| a person in a red jacket | 0.72 | "A person with dark clothing…" | noise |
| a child | 0.71 | "A person with a black backpack…" | noise |
| a man with a bicycle | 0.70 | "A man wearing glasses and a dark shirt…" | noise |
| a red truck at the dock | 0.70 | "A pink car is visible in the photo." | near miss (a vehicle, wrong colour) |
| a delivery van | 0.69 | "A pink or reddish-purple SUV is parked outdoors." | near miss |
| snow on the ground | 0.68 | "…a blurry, low-resolution depiction of a person…" | noise |
| a dog | 0.64 | "A pink car is visible in the photo." | noise |
| a woman walking | 0.66 | "A person with a black backpack…" | noise (captions never say "woman") |
| a cat on the roof | 0.57 | "The image shows a black car parked outdoors." | noise |

The medians over all 805 captions sit at 0.42–0.61 and the 90th percentiles at
0.50–0.70, for every query alike: most of the store is far below any floor, and
the arm's decision happens in the top one or two percent.

**The line.** A caption that answers the query scores 0.75–0.83. The best
unrelated caption scores 0.57–0.74. `SEARCH_CAPTION_MIN_SIMILARITY=0.75` keeps
every hit above and cuts every noise row in this set, with one near-boundary
pair: "person wearing a cap" at 0.74 (cut, correctly) and the fourth
blue-shirt caption at 0.75 (kept, correctly). A query phrased further from the
captioner's own vocabulary ("a woman walking" against captions that only ever
say "a person") lands below the floor and the arm sits out, which is the
intended outcome: the words arm and the frame arm still run.

By luck the same 0.75 is where SigLIP2's text tower put the top of ITS noise
(0.59–0.75), so a box without the adapter — the fallback — gets a usable
floor from the same default; its hits (0.76–0.87) sit above it, with less
margin.

## The floor against the labelled eval (added the same evening)

The fifteen probe queries above are phrased the way the captioner writes.
The relevance-labelled eval (`bench/search-eval-cases-ventuno-q-2026-11-10.yml`,
33 cases from the same captions, operator phrasing) was run before and after
the caption arm moved to this adapter, all 1,076 caption rows re-embedded:

| | SigLIP text tower, floor 0.22 | bge-small, floor 0.75 |
|---|---|---|
| MRR | 0.462 | 0.508 |
| hit@5 | 0.571 | 0.607 |
| P@5 | 0.234 | 0.302 |
| recall@10 | 0.446 | 0.467 |
| attribute / caption / paraphrase / typo MRR | 0.44 / 0.61 / 0.40 / 0.07 | 0.54 / 0.66 / 0.40 / 0.06 |

A gain, but at 0.75 the caption arm admitted one or two rows a query — the
eval's phrasing scores lower than the probe's ("hooded sweatshirt" best
relevant 0.63, "light blue kurta" 0.67, "person wearing a striped shirt"
0.69). A sweep of the caption arm ALONE over the labelled pool (646 bge
caption vectors, the arm's own ranking, no fusion):

| floor | MRR | hit@5 | cases with the arm on | wrong rows in the top 5, summed |
|---|---|---|---|---|
| 0.55 | 0.81 | 0.93 | 28/28 | 72 |
| 0.60 | 0.79 | 0.89 | 28/28 | 69 |
| 0.65 | 0.74 | 0.82 | 26/28 | 61 |
| 0.68 | 0.70 | 0.79 | 24/28 | 54 |
| 0.70 | 0.68 | 0.75 | 24/28 | 52 |
| 0.72 | 0.64 | 0.71 | 22/28 | 46 |
| 0.75 | 0.60 | 0.64 | 21/28 | 32 |

"Wrong" here is strict — the other white cars under "white car parked in
front of a building" count as wrong — so the rightmost column overstates
the noise. Against the probe's nonsense queries ("a cat on the roof" 0.57,
"a dog" 0.64, "snow on the ground" 0.68), 0.65 cuts the first two and lets
the third through. Fusion ranks by agreement between arms, so a looser
caption floor costs little precision and buys the arm back for a quarter of
the cases: **the default moved to 0.65** (the 0.75 above stood for one
afternoon). A box running the fallback text tower should raise it to 0.75.

## The eval after the floor and the parser fix (17:35 UTC)

Core at the merge of #864, #866 (the vector arms embed the whole corrected
question; typos corrected against the captions' own words) and #867 (floor
0.65), every caption row on the bge producer, all three arms on:

| | baseline (SigLIP text tower, 0.22) | bge, 0.75 | words only, #866 | all arms, 0.65, #866 |
|---|---|---|---|---|
| MRR | 0.462 | 0.508 | 0.594 | **0.711** |
| hit@5 | 0.571 | 0.607 | 0.679 | **0.821** |
| P@5 | 0.234 | 0.302 | **0.575** | 0.441 |
| recall@10 | 0.446 | 0.467 | 0.508 | **0.510** |
| negatives passed | 2/5 | 2/5 | 3/5 | 3/5 |
| attribute MRR | 0.439 | 0.535 | 0.609 | **0.843** |
| caption MRR | 0.614 | 0.659 | **0.727** | 0.712 |
| paraphrase MRR | 0.400 | 0.400 | 0.400 | **0.500** |
| typo MRR | 0.074 | 0.056 | 0.389 | **0.667** |
| median latency | 287 ms | 300 ms | 118 ms | 342 ms |

The "words only" column was an accident worth keeping: that run happened
in the minutes after core was recreated, while KAI-C had not yet probed
the re-registered adapters healthy, so every search ran the word arm alone
(`embedder-unreachable`). It shows what #866 alone buys (typos 0.07 → 0.39,
"pink car" found) and that the vector arms, as fused today, cost top-5
precision (0.575 → 0.441) while buying rank and recall: the frame arm at its
0.08 floor admits fifty rows for most queries, and reciprocal-rank fusion
rewards any row two arms agree on. Weighting the arms, or a tighter frame
floor, is the next measured change.

Also noted: for the first minutes after a core restart the embedders are
skipped because their health is not yet "ok" in KAI-C's registry, and
semantic search quietly runs words-only. The eval's `arms` column is what
made that visible.

## The fusion, swept (17:50 UTC)

With the three arms' rankings pulled from core's own functions over the
labelled pool (28 positive cases; text arm present for 24, frame for 26,
caption for 26), fused offline with per-arm weights and floors:

| setting | MRR | hit@5 | P@5 | recall@10 |
|---|---|---|---|---|
| as deployed: frame 0.08, caption 0.65, votes 1/1/1 | 0.755 | 0.857 | 0.486 | 0.626 |
| words only | 0.750 | 0.786 | 0.414 | 0.529 |
| words + caption (0.65) | 0.758 | 0.857 | 0.514 | 0.628 |
| words + frame (0.08) | 0.697 | 0.857 | 0.457 | 0.648 |
| **frame 0.12, caption 0.65, caption at half a vote** | **0.815** | **0.893** | **0.536** | **0.676** |
| frame 0.14, caption 0.60–0.65, caption half | 0.815 | 0.893 | 0.536 | 0.663 |
| frame 0.12, caption 0.60, votes 1/1/1 | 0.792 | 0.893 | 0.529 | 0.668 |
| frame 0.10, caption 0.60–0.65, caption half | 0.807 | 0.893 | 0.500 | 0.653 |

(Offline numbers run a little above the API's because the offline text
arm takes the parser's output directly; the comparisons are what matter.)

Two things the table says. The frame arm at 0.08 was the arm costing
precision: words + caption beat words + frame on MRR and P@5, and raising
the frame floor to 0.12 — the bottom of the measured match band, 0.11–0.14
— is worth more than any weight. And with two vector arms, a full vote
each let them agree on a near-miss and outvote the words that named the
thing; the caption arm at half a vote keeps its recall and gives the words
their say. **Deployed defaults: frame floor 0.12, caption floor 0.65, votes
text 1 / frame 1 / caption 0.5** (`SEARCH_*_ARM_WEIGHT`).

## The LLM re-rank, measured (2026-11-11 03:30 UTC)

Each labelled case's live first ten results, re-ranked by Gemma (the
litertlm captioner's chat, on the Hexagon) exactly as
`services/search_rerank.py` does, scored before and after:

| prompt | MRR | hit@5 | P@5 | chat median / p90 |
|---|---|---|---|---|
| none (as deployed) | 0.762 | 0.821 | 0.450 | — |
| "a caption matches when it shows what the search asks for" | 0.731 | 0.786 | 0.443 | 378 / 524 ms |
| strict: every part of the search, when unsure not a match | 0.775 | 0.821 | 0.457 | 234 / 455 ms |
| rank the best three, best first | 0.777 | 0.821 | 0.464 | 256 / 374 ms |

The first prompt had the model say yes to nearly every caption (ten of
ten for "person in camouflage") and moved a right answer from first to
seventh ("man sitting on the floor with a laptop"). The strict prompt
gains a little and never loses a first place. The paraphrase cases it
was meant for ("two men looking at a laptop", MRR 0) did not move:
the right caption was not in the first ten, and a re-rank cannot find
what fusion did not bring. **Shipped off by default** (`SEARCH_RERANK_TOP=0`,
`?rerank=N` per request); the next lever for paraphrases is recall, not
re-ranking.

## What changed because of this

- `SEARCH_CAPTION_MIN_SIMILARITY` default 0.22 → 0.75 → 0.65 (the first value
  was never measured against real vectors; the second was measured against
  the captioner's phrasing; the third against operators').
- `SEARCH_VECTOR_MIN_SIMILARITY` 0.08 → 0.12 and the fusion's per-arm votes
  (`SEARCH_TEXT_ARM_WEIGHT` 1, `SEARCH_VECTOR_ARM_WEIGHT` 1,
  `SEARCH_CAPTION_ARM_WEIGHT` 0.5), from the sweep above.
- The caption arm's query and the caption vectors go through `text_embed`
  where an adapter advertises it; the frame arm keeps SigLIP2 on the NPU.

## Not measured yet

- Recall across a labelled query set (the next step: a relevance-labelled
  eval over the box's visits, so the floor and the fusion are judged by a
  number, not by reading tables like the one above).
- The adapter under the governor's pacing during a re-embed walk of the full
  store, and its effect on the detector loop (expected: none visible, 37 ms a
  caption on two of eight cores at the backfill's own pace).
