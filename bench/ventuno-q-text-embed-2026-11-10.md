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

## What changed because of this

- `SEARCH_CAPTION_MIN_SIMILARITY` default 0.22 → 0.75 (the earlier value was
  never measured against real vectors).
- The caption arm's query and the caption vectors go through `text_embed`
  where an adapter advertises it; the frame arm keeps SigLIP2 on the NPU.

## Not measured yet

- Recall across a labelled query set (the next step: a relevance-labelled
  eval over the box's visits, so the floor and the fusion are judged by a
  number, not by reading tables like the one above).
- The adapter under the governor's pacing during a re-embed walk of the full
  store, and its effect on the detector loop (expected: none visible, 37 ms a
  caption on two of eight cores at the backfill's own pace).
