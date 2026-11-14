# Enrichment — captions, descriptors and embeddings on the event store

Search answers from the **event store**: every visit Tier-0 records is a row,
and three *enrichers* add words and vectors to it after the fact — off the
ingest path, in the background, best-effort.

| Enricher | Writes | Needs | Env flag | Skill on the camera |
|---|---|---|---|---|
| **Descriptors** | colour / type claims (`visit_descriptors`) | the plate reader and/or a VLM registered | `EVENTS_DESCRIPTOR_ENRICHMENT` | *(plan-driven; none)* |
| **Captions** | a sentence per visit (`event_text`) | a captioner advertising `scene_caption` (BLIP, Moondream, or `ollamavlm`) | `EVENTS_CAPTION_ENRICHMENT` | `image_captioning` |
| **Embeddings** | one frame vector per visit (`event_embeddings`, kind `image`; 768-d from SigLIP 2, 512-d from the earlier CLIP) and one caption vector (kind `caption`; 384-d from bge-small where the `text-embed` container runs, else the embedder's text tower) | the embedding adapter (`embed` task; the `clip` container) and the sentence encoder (`text_embed` task; the `text-embed` container, same profile) | `EVENTS_EMBED_ENRICHMENT` | `embed` (frames); captions follow the caption |

## The gate: skills follow apps

Captions, embeddings and descriptors are **per-camera**, and a camera
carries a skill only because an enabled app that brings it was pointed at
the camera (`docs/CAMERA_ASSIGNMENTS.md` → *Skills follow apps*). The flag
being on and the adapter being registered is not enough — deliberately,
so a thirty-camera site does not pay for inference on thirty cameras to
describe one gate. The backend logs a warning the first time a qualifying
visit is skipped for this reason, and once per thousand after.

| Skill in the camera's set | Brought by |
|---|---|
| `image_captioning`, `embed` | **Footage Search** — select the cameras to make searchable |
| `image_captioning`, `vqa` | **OpenNVR Agent** — every camera, while the agent is enabled |
| `license_plate_recognition` | **ANPR** — its selected cameras (a gate role on the Vehicles page selects too) |
| `face_recognition` | **Smart Doorbell** — its selected cameras |

There is no skills editor on the camera page. Select the camera in the app
(Applications → the app → Configure → Cameras → Select cameras); the
enricher picks the change up on its next visit.

## Turning it on, in order

1. **Set the flags.** `EVENTS_CAPTION_ENRICHMENT=true` (captions and the
   colour/type questions, with `EVENTS_DESCRIPTOR_ENRICHMENT=true`) and
   `EVENTS_EMBED_ENRICHMENT=true`.
2. **The adapters start with them.** Both live in `docker-compose.apps.yml`,
   which every stock install carries. The captioner (`caption-adapter`;
   `CAPTION_ADAPTER` picks the image, `ollamavlm` needs a vision model
   pulled into Ollama — `OLLAMA_VLM_MODEL`) is enabled by `start.sh` on
   every `up` where either description flag is true: profile
   `descriptions`, the captioner and its registration job and nothing
   else. No Camera Agent is needed; until 2026-11-14 the captioner was
   defined in the agent's file, and an install without the agent had none.
   The embedders (`clip` / `siglip` and the sentence model) are the
   `embeddings` profile: add it to `OPENNVR_EXAMPLE_PROFILE` in `.env`
   (the qualcomm flavour does this itself), or `enrichment` for all of them.
3. `./start.sh up`, then check the adapters registered:
   `GET /api/v1/skills` lists `image_captioning` and `embed` with a provider.
4. **Select the cameras** in the app that brings the skill (Footage Search for captions + embeddings; the Agent covers every camera) — see *The gate* above.
5. **Back-fill history.** The enrichers run on *new* visits only. Every visit
   recorded before is a row with no words and no vector, so the first search
   an operator runs — against yesterday — returns nothing. Sweeping history
   is a choice, off by default: **Apps → Footage Search → Configure →
   Backfill history**, then Save. From then on the sweep walks history
   newest-first when the box is idle (see *When the box cannot keep up*),
   hands each qualifying visit to the same enricher, and — once history is
   done — keeps catching up on live visits the gate dropped under load.
   Turning the switch off stops it at the next pass; the cursor is kept,
   so turning it back on resumes where it stopped. Off, only new visits
   are described as they happen. `EVENTS_ENRICHMENT_BACKFILL=true` in
   `.env` forces the sweep on regardless of the app (a headless install).

## When the box cannot keep up

Enrichment never slows recording or detection. When it cannot keep up,
it yields — it does not queue without bound (#583: a CPU captioner that
takes seconds per image was outrun by one busy camera, every call timed
out on core's side while the model finished anyway, and the box lost two
cores for zero captions saved).

**Admission, per adapter.** Each adapter takes `scheduling.max_inflight`
calls at once (its line in `server/config/adapters_index.yml`); up to
`EVENTS_ENRICHMENT_QUEUE_DEPTH` (8) more visits may wait for a slot, and
the next is **dropped** — described later by the backfill — rather than
queued. `EVENTS_ENRICHMENT_TIMEOUT_S` (90) is a hung-adapter guard, not
load control; `EVENTS_ENRICHMENT_BREAKER_TIMEOUTS` (5) timeouts in a row
pause that adapter for 60 s, doubling to 10 min, and raise a system event.
A timeout is logged as what it is ("still answering after 90 s"), not
"failed".

**The governor.** Can the box afford enrichment *right now*? Read from the
host monitor's CPU sample and each adapter's average call time:

| State | When | What runs |
|---|---|---|
| **NORMAL** | CPU under `EVENTS_ENRICHMENT_GOVERNOR_CPU_PERCENT` (65) and no adapter over `EVENTS_ENRICHMENT_SLOW_CALL_S` (20) per call | everything; the backfill after `EVENTS_ENRICHMENT_BACKFILL_IDLE_S` (120) of quiet, optionally only inside `EVENTS_ENRICHMENT_BACKFILL_WINDOW` (`HH:MM-HH:MM`) |
| **LIVE-ONLY** | over either line | captions and embeddings only when a slot is free; colour/type questions keep a short line (the filters' claims outrank the sentence); backfill held |
| **PAUSED** | the monitor's own `cpu_high` alert, or an open breaker | nothing; visits stay for later |

Each change is one `enrichment_throttled` system event with the reason.
Busy is load, not clock: busy hours throttle themselves.

## On demand: "not yet" is not "no"

A search whose question needs a caption or a claim the matching visits
lack — "blue car on the gate camera between two and four" — answers from
what exists and **queues the rest for description now**, newest first,
up to `EVENTS_ENRICHMENT_REQUEST_CAP` (50) per question; narrow the window
past that. This runs even with the two description flags **off**: the
operator asked. The answer carries `pending` — how many are missing, how
many this call queued, and roughly how long — and the Search page shows
it with a *Check again* button; the agent says "N visits in that window
have not been described yet — about T; ask me again then." The App SDK
returns it on the rows (`SearchResult.pending`, `EventsClient.last_pending`).
`describe=false` on `GET /api/v1/search` queues nothing. A visit the
captioner looked at and had nothing to say about is marked (`enriched_by`)
so nobody asks about it forever; a call that *failed* is not.

## CPU installs

Without a CUDA GPU the installer defaults `EVENTS_CAPTION_ENRICHMENT` and
`EVENTS_DESCRIPTOR_ENRICHMENT` to **false** and asks. Both together cost
one captioner call plus two VQA questions per finished visit. To keep them
on on CPU (or an M-series Mac), use `CAPTION_ADAPTER=moondream` — the
0.5B in-container build, ~10× faster than `ollamavlm` on CPU — and a light
camera; never `ollamavlm` there. Nothing recorded while they are off is
lost: the on-demand lane describes what a question needs, and the backfill
honours the same flags when you turn them back on.

A Qualcomm box (Ventuno Q and other Dragonwing / Snapdragon boards with the
Hexagon NPU) is the exception: `start.sh` fills `CAPTION_ADAPTER=litertlm`,
Gemma 4 E2B on the NPU at ~2.5 s a photo (docs/HARDWARE.md), and the
installer defaults both flags to **true** there.

## Checking it worked

- `docker logs opennvr_core | grep -i 'enrichment'` — a warning naming the
  camera means step 4 was skipped.
- The captioner's own log should show `POST /infer`, not only `/health`.
- `SELECT count(*) FROM event_text;` and `FROM event_embeddings;` climb.
- `GET /api/v1/search?q=red truck` returns ranked results with `total > 0`.

## What it costs

One captioner call and one embedding per *qualifying* visit (people and
common vehicles), on the best frame only — never per frame. A visit on a
camera without the skill costs nothing. No adapter registered is a silent
no-op, not an error: search matches labels and plates, which is what a stock
install does today.

## Two arms, two models

Search's **frame arm** compares the query with each visit's frame vector:
one image-text model (`embed` — SigLIP 2 on the clip or siglip-qnn
container) embeds the frame at enrichment and the words at search time,
and the two land in one space. Its **caption arm** compares the query
with each visit's caption vector, text against text — and an image-text
model's text tower is a poor judge of that: on the Ventuno Q SigLIP2's
text-to-text cosines for relevant and irrelevant caption/query pairs
overlap (0.76–0.87 against 0.59–0.75, 2026-11-10), so no floor separates
a hit from noise. The caption arm therefore has its own model: the
`text-embed` container (`text_embed` task; `BAAI/bge-small-en-v1.5`,
384-d, MIT, CPU, a few milliseconds a caption), in the same compose
profile as the embedder. Caption vectors and the caption arm's query both
go through it; where none is registered, the embed adapter's text tower
carries on as before. `event_embeddings.kind` keeps the two apart
(`image` / `caption`), each kind is judged against its own producer, and
`SEARCH_CAPTION_MIN_SIMILARITY` is that arm's floor on that model's
scale.

## Changing the embedding model

`event_embeddings.model` records the **producer**: the adapter and the
model behind it, name and version as its `/capabilities` report them
(`clip:open_clip/ViT-B-16-SigLIP2-256/webli`; `text-embed:BAAI/bge-small-en-v1.5/cls-onnx`
for caption vectors). Vectors
from two models are not comparable, and search already treats a
dimension mismatch as a non-match, so a store of old vectors would go
quietly dark under a new model. Two things stop that:

- the enricher replaces a vector from another producer instead of
  treating the visit as already embedded (a retry on the same producer
  still does nothing);
- the backfill, once history is done, checks on every catch-up pass
  whether the store holds another producer's vectors (each kind against
  its own producer) and, if so, walks history again from the top.
  `GET /search`'s `semantic.note` says "Mixed producers" while that runs,
  and settles once the walk is over. The walk is the backfill's, so it
  runs under the same switch (Footage Search → Configure → Backfill
  history): with it off, only new captions get the new model's vector.

The October 2026 move from CLIP ViT-B/32 (512-d) to SigLIP 2 B/16
(768-d) is what this was written for; it costs one walk of history at
the backfill's own pace, with nothing to run by hand.

## People attributes, and what they cost

With `EVENTS_DESCRIPTOR_PEOPLE=true` a person visit is asked two questions:
the colour of the top (`clothing_top`) and what is carried (`carrying`).
`EVENTS_DESCRIPTOR_PEOPLE_EXTENDED=true` adds three more, `gender` (man,
woman, child), `clothing_bottom` (the colour below the waist) and
`headwear` (helmet, cap, hat, hood, turban, scarf, nothing), so a search
for "woman in a red top with a helmet" has claims to match. That is five
inferences a person visit instead of two; on a CPU-only box with the 0.5B
vision model each is several seconds, so turn it on for the cameras that
need it and let the per-camera assignment keep the rest cheap.

