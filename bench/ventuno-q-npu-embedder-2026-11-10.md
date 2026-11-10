# Ventuno Q — search's embedder on the NPU: SigLIP2 on the Hexagon (measured 2026-11-10)

Board and runtime as in [`ventuno-q-npu-agent-llm-2026-11-07.md`](ventuno-q-npu-agent-llm-2026-11-07.md):
Arduino Ventuno Q (QCS8275, Hexagon v75), the deployed stack (open-nvr main at #856, ai-adapter main at #86),
one CP Plus camera in an office during the morning. The embedder is ai-adapter's `siglip_qnn`:
`google/siglip2-base-patch16-224` as Qualcomm AI Hub's w8a16 ONNX export, both towers compiled for the HTP by
ONNX Runtime's QNN provider at the first start, serving the `embed` task on the clip adapter's port. It replaced the
CPU `clip-adapter` (OpenCLIP, SigLIP2-256) that start.sh had brought up by mistake forty minutes earlier (#856).

## Start-up and latency

| | measured |
|---|---|
| first start: both graphs compiled for the HTP | ~11 s (image tower 07:50:55, text tower 07:51:06); container healthy in about a minute |
| CPU fallback for the text tower | not needed — both towers on `QNNExecutionProvider:htp` |
| text embedding | 4.0–6.3 ms encode, 23–28 ms wall over HTTP in-container |
| frame embedding (640×360 JPEG) | 6.3–8.0 ms encode, 31–45 ms wall |
| adapter RSS | 2.69 GiB (the 1.1 GB text-tower externals are mapped) |
| vectors after 25 min with camera 23 picked for Footage Search | 103 visits, each with a frame vector and a caption vector, all from producer `clip:siglip2-base-patch16-224/w8a16-qnn` (the backfill re-embedded the rows the CPU adapter had written) |

Qualcomm's published figures for the IQ-8275 are 5.1 ms (image) and 2.1 ms (text); ours include dequantisation and
normalisation in Python.

## Does the ranking make sense

Five queries that describe things on the site and five that do not, each embedded by the text tower and ranked
against the 103 visits' frame vectors (the *frame arm*) and their caption vectors (the *caption arm*), cosine on
unit vectors. Frame-arm top hits, with the visit's own caption:

| query | top frame score | the visit it chose |
|---|---|---|
| a person sitting at a desk working on a computer | 0.139 | "A person with dark hair … seated in front of a computer monitor" |
| a man in a patterned shirt | 0.131 | "A man wearing a light-colored, short-sleeved, patterned shirt" |
| an empty office desk with a chair | 0.134 | "a security camera view of a wooden desk with a black office chair" |
| a person wearing a backpack | 0.121 | "A young man … with a blue backpack" |
| a car parked outside | 0.111 | "a dark-colored pickup truck parked near a building" |

Right every time. The scores are what a SigLIP-family model produces: it is trained with a sigmoid loss whose
learned scale and bias put a confident match around 0.1–0.2 cosine, not the 0.3+ of contrastive CLIP.

| | frame arm: top | frame arm: median | caption arm: top | caption arm: median |
|---|---|---|---|---|
| relevant queries (5) | 0.111–0.139 | −0.003–0.113 | 0.761–0.870 | 0.32–0.53 |
| irrelevant queries (5: fire truck, cat on a sofa, snowy mountain, bowl of fruit, football crowd) | 0.025–0.056 | −0.06–−0.01 | 0.592–0.752 | 0.31–0.36 |

## Two findings

1. **The frame arm's floor was wrong.** `SEARCH_VECTOR_MIN_SIMILARITY` defaulted to 0.22, a number never measured
   against real vectors (no embedder had run on a Qualcomm box before today). Every correct frame match above is
   under it; the arm was returning nothing. **0.08** separates the two populations cleanly here (best irrelevant
   0.056, worst relevant top-3 0.081) and is the new default; a box with the old value in `.env` must change it.
2. **The caption arm through this text tower cannot be floored.** Text-to-text cosines from an image-text model's
   text tower are high for everything — "a snowy mountain landscape" scores 0.73 against an office caption — so
   the relevant and irrelevant ranges overlap and 0.22 filters nothing. The arm still ranks sensibly at the top
   (its best hits were the right captions) and rank fusion with the word match keeps it useful, but a proper
   text-embedding model is the next step for it: Qualcomm AI Hub ships Nomic-Embed-Text and MiniLM for the same
   NPU. Until then the caption floor stays as it is, a no-op.

## How it was run

`/hardware/evaluation` and timed `/infer` calls from inside the adapter container (the bearer is its own token);
the ranking probe inside the core container through `services.embed_enrichment.embed_text` and
`services.embedding_store.rank_by_similarity` over every embedded visit (scratchpad `embed_probe2.py`). Nothing
on the box was changed for the measurement.
