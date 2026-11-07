# Should a small local LLM sit between the user and search? Research brief (2026-11-07)

Question from the field: typed and spoken questions reach search with typos and misheard words, and
free text does not always map to the filters search has. Should a small on-box LLM correct the query
and turn it into a structured, semantic search? Facts below come from pages opened on 2026-11-07;
items marked *(unverified)* came only from search snippets.

**Short answer:** yes for the residue (paraphrase, intent), no for the filters and the spelling. Build
the deterministic parser out first, let the LLM propose only what the parser cannot, and validate
everything the LLM says against the box's own vocabulary before it runs.

## 1. Query understanding with small LLMs on-device

**The self-querying pattern.** LangChain's SelfQueryRetriever and Elastic's write-up have an LLM read
the query plus typed attribute fields, emit a structured filter, then run semantic search on the
residue. Both demos use GPT-4-class cloud models; the Elastic post reports no accuracy or latency, no
output validation, no fallback, and passes the model's text straight to `json.loads()`. One useful
prompt trick from Elastic's query-DSL post: enumerate the legal values of small-cardinality fields
(colours, classes, camera names) in the prompt and ask for lenient matching so a hallucinated
criterion does not empty the result.

**Measured small-model accuracy.** A 2026 study of 0.6B–4B models on 14 structured-output tasks:
unconstrained schema validity 78.6 % for Llama-3.2-1B/3B; with XGrammar constrained decoding validity
reaches 100 % and content accuracy 0.986 (1B) / 0.991 (3B), with a 4–8 ms grammar compile and a
1.6–3.7 % throughput cost (arXiv 2609.23742). The trap, from "The Constraint Tax" (Qwen2.5-0.5B–3B,
arXiv 2605.26128): on a calendar tool-call task hard-schema decoding kept 100 % validity while
executable accuracy fell from 91.5 % to 48.0 %, almost all from one wrong-but-valid field. Its advice:
track the *wrong-valid-field* rate, reason free and constrain late, treat validity as an interface
guarantee rather than correctness. ZoomInfo reports about 97 % per-field accuracy for natural language
to search fields, with Llama-3-8B fine-tuned on 500+ labelled queries (arXiv 2411.05048). No published
latency for 1–3B text-to-filter on edge hardware was found; the Gemma 4 database paper (arXiv
2606.31808) shows constrained decoding adds negligible latency.

**Guardrail pattern.** The LLM proposes a JSON filter; a deterministic validator checks every value
against the live vocabulary (camera names, people, labels, colours) and every time expression against
the date parser; any field that fails is dropped and its words returned to the free-text residue; if
nothing survives, plain search runs. Show the user what survived.

**Spelling: fuzzy beats an LLM.** For typos in known tokens, Postgres `pg_trgm` (`word_similarity`,
a GIN index, thresholds around 0.3/0.6/0.5) or RapidFuzz against a words table built with `ts_stat`
is the documented pattern: deterministic, sub-millisecond, and it cannot invent a camera that does
not exist. Apply automatically only when the original query returns nothing. An LLM is only worth it
for phrase-level rewrites with no vocabulary anchor.

**Plates.** Genetec's plate matcher uses OCR-equivalence groups (0≡O,D,Q; 1≡I; 2≡Z; 5≡S; 8≡B; 6≡G),
allows up to two substitutions, and a contiguous-characters fuzzy mode. Normalise stored and queried
plates to a canonical class, then rank by edit distance. No LLM.

## 2. Voice and natural-language search in surveillance products

| vendor | input | mechanism | shows its interpretation? |
|---|---|---|---|
| Verkada | text | CLIP text encoder against crop embeddings; camera, object and time as separate filters | not described |
| Axis ACS Pro | text, English only | a >2.5B zero-shot text-image model on the local server, query moderation, time as a separate step; weak on actions and counting | no |
| Hikvision AcuSeek | text or voice (Hik-Connect 6, 30+ languages) | "Guanlan" models; infers time and place from camera names | not described |
| Genetec Security Center SaaS | text | LLM-powered; refine by location and before/after | not described |
| Rhombus | text | ChatGPT integration in beta, marks events on the timeline | no |
| Coram | text conversation | on-appliance, no external LLM; the user confirms each match | implicitly, by confirmation |
| Eagle Eye | text | indexes video; date and camera as separate filters | no |
| Frigate | text | Jina CLIP v1/v2 locally; no query parsing | no |
| Avigilon Alta, Spot AI | filter UI / text | *(unverified)* | results only |

Only Hikvision verifiably offers voice. No vendor page fetched shows "here is how I understood you"
chips. The pattern exists outside surveillance (the natural-language filter pattern used by Airbnb,
Amazon, Booking): parse text into editable chips, show ignored words, ask with a chip on ambiguity,
never apply a silent wrong filter. OpenNVR's chips already follow this.

## 3. Semantic quality levers, cheapest first

1. **Vocabulary synonym expansion** (free): car/vehicle/sedan, guy/person/man, at index or query time.
2. **Hybrid full-text + embedding with reciprocal rank fusion**: reported recall gains from +1.7 % to
   +24 % depending on dataset and tuning; untuned fusion can lose to dense-only. Small text encoders,
   all 384-dimensional, 512 tokens, about 33M parameters, MIT, with ONNX on the model card:
   bge-small-en-v1.5 (MTEB 62.2), e5-small-v2 (needs `query:` / `passage:` prefixes), gte-small
   (61.4, English only). nomic-embed-text-v1.5 is 137M, Apache-2.0, and Qualcomm AI Hub ships ONNX/QNN
   builds at 7.9 ms (QCS8550) and 9.9 ms (IQ-9075) on the NPU; MiniLM-L6-v2 (22.7M) at 1.0–1.3 ms.
   jina-embeddings-v3 is CC-BY-NC-4.0 and unusable commercially.
3. **LLM query expansion**: chain-of-thought expansion needs 3B or more to match BM25 with classic
   expansion and hurts on domain-specific corpora (arXiv 2305.03653); a 2023 meta-study of eleven
   techniques found expansion helps weaker retrievers and harms stronger ones (arXiv 2309.08541). No
   benchmark on short event captions was found.
4. **LLM re-rank of the top k**: 0.5B pointwise rerankers reach BEIR about 55 (arXiv 2603.16411), with
   no CPU latency figures published; on CPU it costs k model calls per search.

## 4. Recommendation for OpenNVR

**Typed search, build now.**
1. The deterministic parser, extended: `pg_trgm` or RapidFuzz correction against the box's own
   vocabulary (camera names, people, labels, colours), the date parser, plate-class normalisation, a
   synonym table. Covers typos and most structure at about 0 ms with no NPU dependency.
2. Interpretation chips as they are, plus: show dropped words, a dotted border for a low-confidence
   correction, a one-click way back to the literal words.
3. Hybrid fusion with bge-small (CPU) or nomic-embed on the NPU, measured on our own captions before
   shipping as a lane.

**Typed search, build when Gemma 4 on the NPU lands.** An LLM-proposed filter for the residue only,
constrained-decoded, validator-gated, with the wrong-valid-field rate in the logs.

**Voice.** Reuse the camera agent's Parakeet; run the transcript through the same parser plus an
entity-recovery pass (fuzzy and phonetic candidates from the name list with a strict replacement
threshold; RECOVER-style passes report 8–46 % entity-WER gains); read the chips back before searching.

**Skip.** LLM spelling correction; LLM re-rank on CPU; jina v3; any design that executes LLM output
without a validator.

## Sources opened

- https://www.elastic.co/search-labs/blog/self-querying-retrievers · https://www.elastic.co/search-labs/blog/elastic-query-dsl-structured-datasets
- arXiv 2609.23742 · 2605.26128 · 2606.31808 · 2411.05048 · 2305.03653 · 2309.08541 · 2603.16411
- https://www.postgresql.org/docs/current/pgtrgm.html
- https://techdocs.genetec.com/r/en-US/Security-Center-Administrator-Guide-5.13/ALPR-matcher-technique-OCR-equivalence
- https://verkada.com/blog/scaling-natural-language-video-search-inside-verkadas-breakthrough · https://whitepapers.axis.com/en/freetext-search-in-acs-pro
- https://www.aap.com.au/aapreleases/cision20250918ae76745 (Hikvision AcuSeek) · https://itbrief.co.uk/story/genetec-adds-case-investigation-tools-to-security-centre-saas
- https://manilatimes.net/2025/01/30/tmt-newswire/globenewswire/rhombus-launches-new-ai-capabilities-for-faster-smarter-security-investigations/2047031 · https://www.coram.ai/ai-security-camera-search · https://docs.frigate.video/configuration/semantic_search
- https://ai-design-patterns.pages.dev/pattern/nl-filter
- https://huggingface.co/BAAI/bge-small-en-v1.5 · https://huggingface.co/intfloat/e5-small-v2 · https://huggingface.co/thenlper/gte-small · https://huggingface.co/qualcomm/MiniLM-v2 · https://huggingface.co/qualcomm/Nomic-Embed-Text
- https://weaviate.io/blog/hybrid-search-explained
