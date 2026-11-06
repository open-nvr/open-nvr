# Natural-language event search: research brief (2026-11-06)

Why: on the Ventuno Q, "person blue" matched "blue jeans", "person in blue shirt" matched "green t-shirt and
blue jeans", and "did you see the 3 persons together" matched nothing and fell back to every visit. This brief
is the survey behind the fixes (counts from the tracker, proximity text queries, a loud fallback notice) and the
order of what comes after. Facts come from pages opened on 2026-11-06; items marked *(unverified)* came only
from search snippets.

## Techniques

**Why plain word matching and plain CLIP both fail on these queries.** CLIP-family models behave cross-modally
like a bag of words: they carry attribute-object binding but lose it in cosine alignment, so "blue shirt" and
"blue jeans + red shirt" score alike (arXiv 2502.03566, ICLR 2026). CLIP cannot count; "Teaching CLIP to Count
to Ten" needed a counting-contrastive finetune (arXiv 2302.12066). SOVABench (WACVW 2026) found embeddings
built from MLLM-generated descriptions beat contrastive VLMs on counting and spatial retrieval (arXiv
2601.04824), which supports "caption then embed" over "embed the frame" for counts and attribute pairs.

**Hybrid lexical + vector with reciprocal rank fusion.** FTS rank list plus vector rank list, fused with the
sum of 1/(k+rank). Supabase publishes a pure-SQL `hybrid_search` for Postgres (tsvector + pgvector, `rrf_k`
default 50, per-lane weights). Postgres already covers attribute+object pairs: `websearch_to_tsquery` turns
`"blue shirt"` into `'blue' <-> 'shirt'`, `<N>` tolerates a gap, and `ts_rank_cd` rewards proximity.

**Query parsing vs. an LLM writing the filter.** The "self-querying retriever" pattern maps the query to
metadata filters plus a semantic residue; its stated limits are filter accuracy on ambiguous queries and
per-query LLM cost (Elastic). On an edge box a deterministic parser for counts, colours, object nouns and time
words, with the on-box VLM only for the residue, is the cheaper and more predictable form.

**Edge text-image embedding models.**

| Model | Params / size | Licence | NPU |
|---|---|---|---|
| SigLIP2-base-p16-224 (Qualcomm AI Hub) | image encoder 93 MB w8a16, text 461 MB w8a16 | Apache-2.0 | QNN/ONNX; 5.3 ms image, 2.2 ms text on Dragonwing IQ-9075; 54 ms image on Q-6690 |
| OpenAI CLIP ViT-B/16 (Qualcomm AI Hub) | ~150 M, 571 MB float | MIT | 22–31 ms on IQ-8275 NPU |
| MobileCLIP2-S0 / S2 (Apple, 2025) | 11.4 M + 63.4 M / 35.7 M + 63.4 M | code MIT, weights under Apple ML Research terms (not OSI) | no official ONNX/QNN export |
| Jina CLIP v1 | ~0.2 B | Apache-2.0 | ONNX; CPU-capable (Frigate "small") |
| Jina CLIP v2 | 0.9 B, 512 px, 89 languages | CC BY-NC 4.0 | needs a discrete GPU per Frigate |

**Re-ranking with a VLM.** RANKVIDEO (2026) gains about 31 % nDCG@10 as a second stage (arXiv 2602.02444);
UniRank (2026) scores candidates directly with a VLM (arXiv 2603.29897). Both assume a GPU; at ~2 s a photo on
the NPU a re-rank is affordable only for a top-5 to top-10 window.

**Person-attribute recognition.** MSP60K (60,122 images, 57 attributes, 8 scenarios) with an LLM-augmented
framework (arXiv 2408.09720); PETA/PA-100K are the field's benchmarks. Useful as a vocabulary for the attribute
claims, not a model to train now. Venus (INFOCOM 2026) is the closest "edge memory + keyframe retrieval for VLM
queries" system; it embeds keyframes, no captions (arXiv 2512.07344).

## Competitors

- **Frigate** (0.18.2, Oct 2026): semantic search = Jina CLIP v1 (small quantised CPU / large GPU) or v2;
  text-to-image over stored thumbnail embeddings, image-to-image "Find Similar", GenAI object descriptions
  embedded with the v1 text model. No reranker documented. Docs state text-to-image triggers are unsupported
  "due to fundamental limitations of CLIP-based similarity search". Counts and attributes: nothing; users
  combine Explore filters by hand. 0.18 added a tool-calling chat agent, remote embeddings and OpenVINO NPU
  acceleration for embeddings.
- **Scrypted**: AI summaries per event via a cloud LLM or local llama.cpp; not documented as searchable.
- **Blue Iris, ZoneMinder**: no natural-language or semantic search found *(snippets only)*.
- **Verkada** (2024): free-text search on a public image-text model plus an in-house pre-indexing model;
  OpenAI moderation on queries; predefined clothing and vehicle-colour filters remain.
- **Rhombus** (beta 2025): plain-English prompts, cloud; no colour or count examples.
- **Coram AI**: on-site appliance, "video is never sent to an external LLM"; example "person in a yellow jacket
  by the east entrance on Tuesday afternoon"; counts in search not described.
- **Spot AI**: NL prompts on an NVIDIA-GPU recorder; counts as reports, not queries.
- **Axis** (ACS Pro, 2025): free-text search on an open image-text model fine-tuned by Axis, on the local
  server; time/area/direction/size filters; a search log.
- **Genetec** (2026): NL search by clothing detail or approximate time, cloud.
- **Milestone** (2026): AI search in natural language; on-prem via BriefCam for air-gapped sites.
- **Hanwha BLAZE** (2026): generative "semantic search" plus similarity search; hybrid on-prem/cloud.

Nobody documents "N people together" as a search query. Clothing colour is either a structured filter (Axis,
Verkada legacy, Hanwha cameras) or thrown at the embedding model.

## Ranked plan for OpenNVR

1. **Fix the lexical lane first, no new model** — done in the PR this brief ships with: proximity queries for
   colour + thing (`blue <2> shirt`, `blue <2> (shirt | top | …)`), cover-density ranking, a warning rather
   than a footnote when the words were dropped.
2. **Deterministic parser + per-visit people count** — done in the same PR: "3 persons", "two men", "a group",
   "together" become *at least N people in frame*, answered from the tracker's own count. No competitor
   offers it; CLIP cannot.
3. **Caption-then-embed + RRF hybrid.** Embed the Gemma captions and attribute claims with a text encoder and
   fuse with FTS. Captions already exist on every box; description embeddings handle synonyms ("in a hurry"
   vs "running").
4. **Frame-embedding lane with SigLIP2-base on the Qualcomm NPU** (Apache-2.0; packaged by Qualcomm AI Hub);
   CPU boxes skip it or use Jina v1 small. One fused lane, never the sole ranker.
5. **VLM yes/no re-rank of the top 5–10** with Gemma 4 E2B, asynchronous, with a deadline.

**Skip**: Jina CLIP v2 (non-commercial licence, GPU); MobileCLIP2 until the Apple model terms are confirmed
compatible with AGPL distribution; cloud LLM query moderation or embedding; training an attribute model
without labelled data; text-to-image triggers (Frigate abandoned them for this model class).

## Sources opened

- https://docs.frigate.video/configuration/semantic_search · https://docs.frigate.video/configuration/genai/genai_objects · https://github.com/blakeblackshear/frigate/releases
- https://docs.scrypted.app/scrypted-nvr/ai-summaries.html
- https://www.verkada.com/blog/ai-powered-search/ · https://www.rhombus.com/blog/introducing-ai-search · https://coram.ai/ai-security-camera-search
- https://siliconangle.com/2024/10/31/spot-ai-reels-31m-ai-powered-video-processing-platform/
- https://newsroom.axis.com/news/acs-pro-free-text-search · https://securitybrief.co.uk/story/genetec-adds-ai-tools-to-speed-video-incident-review
- https://security.world/milestone-systems-announces-ai-built-for-security-operations/ · https://security.world/hanwha-vision-unveils-blaze-hybrid-ai-vms-platform/
- https://huggingface.co/qualcomm/SigLIP2 · https://huggingface.co/qualcomm/OpenAI-Clip · https://github.com/apple/ml-mobileclip · https://huggingface.co/jinaai/jina-clip-v1 · https://huggingface.co/jinaai/jina-clip-v2
- https://supabase.com/docs/guides/ai/hybrid-search.md · https://www.postgresql.org/docs/current/textsearch-controls.html · https://www.elastic.co/search-labs/blog/self-querying-retrievers
- arXiv 2302.12066, 2502.03566, 2601.04824, 2606.19682, 2602.02444, 2603.29897, 2512.07344, 2408.09720
