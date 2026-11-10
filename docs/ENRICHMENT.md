# Enrichment — captions, descriptors and embeddings on the event store

Search answers from the **event store**: every visit Tier-0 records is a row,
and three *enrichers* add words and vectors to it after the fact — off the
ingest path, in the background, best-effort.

| Enricher | Writes | Needs | Env flag | Skill on the camera |
|---|---|---|---|---|
| **Descriptors** | colour / type claims (`visit_descriptors`) | the plate reader and/or a VLM registered | `EVENTS_DESCRIPTOR_ENRICHMENT` | *(plan-driven; none)* |
| **Captions** | a sentence per visit (`event_text`) | a captioner advertising `scene_caption` (BLIP, Moondream, or `ollamavlm`) | `EVENTS_CAPTION_ENRICHMENT` | `image_captioning` |
| **Embeddings** | one vector per visit (`event_embeddings`; 768-d from SigLIP 2, 512-d from the earlier CLIP) | the embedding adapter (`embed` task; the `clip` container) | `EVENTS_EMBED_ENRICHMENT` | `embed` |

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

1. **Start the adapters.** They are compose profiles, off by default because
   they are CPU-heavy. In `.env`:
   ```
   OPENNVR_EXAMPLE_COMPOSE=docker-compose.camera-agent.yml,docker-compose.apps.yml
   OPENNVR_EXAMPLE_PROFILE=descriptions,embeddings     # or: enrichment (both)
   ```
   `descriptions` is the captioner (`CAPTION_ADAPTER` picks the image;
   `ollamavlm` needs a vision model pulled into Ollama — `OLLAMA_VLM_MODEL`).
   `embeddings` is the CLIP adapter: CPU, weights baked in, ~3 minutes on
   first boot, no volume.
2. **Set the flags.** `EVENTS_CAPTION_ENRICHMENT=true`,
   `EVENTS_EMBED_ENRICHMENT=true`.
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

## Changing the embedding model

`event_embeddings.model` records the **producer**: the adapter and the
model behind it, name and version as its `/capabilities` report them
(`clip:open_clip/ViT-B-16-SigLIP2-256/webli`). Vectors
from two models are not comparable, and search already treats a
dimension mismatch as a non-match, so a store of old vectors would go
quietly dark under a new model. Two things stop that:

- the enricher replaces a vector from another producer instead of
  treating the visit as already embedded (a retry on the same producer
  still does nothing);
- the backfill, once history is done, checks on every catch-up pass
  whether the store holds another producer's vectors and, if so, walks
  history again from the top. `GET /search`'s `semantic.note` says
  "Mixed producers" while that runs, and settles once the walk is over.

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

