# Enrichment — captions, descriptors and embeddings on the event store

Search answers from the **event store**: every visit Tier-0 records is a row,
and three *enrichers* add words and vectors to it after the fact — off the
ingest path, in the background, best-effort.

| Enricher | Writes | Needs | Env flag | Skill on the camera |
|---|---|---|---|---|
| **Descriptors** | colour / type claims (`visit_descriptors`) | the plate reader and/or a VLM registered | `EVENTS_DESCRIPTOR_ENRICHMENT` | *(plan-driven; none)* |
| **Captions** | a sentence per visit (`event_text`) | a captioner advertising `scene_caption` (BLIP, Moondream, or `ollamavlm`) | `EVENTS_CAPTION_ENRICHMENT` | `image_captioning` |
| **Embeddings** | one 512-d vector per visit (`event_embeddings`) | the CLIP adapter (`embed` task) | `EVENTS_EMBED_ENRICHMENT` | `embed` |

## The gate that surprises everyone

Captions and embeddings are **per-camera opt-in**. The flag being on and the
adapter being registered is not enough: a camera must carry the skill, or
nothing is produced — deliberately, so a thirty-camera site does not pay for
inference on thirty cameras to describe one gate. Since this document, the
backend logs a warning the first time a qualifying visit is skipped for this
reason, and once per thousand after.

Assign the skills in **camera settings → Skills** (*Image Captioning*,
*Visual Embedding*), or by API — the operator's assignments are sent as a
full list; app-owned assignments are untouched:

```bash
curl -s -X PUT localhost:8000/api/v1/cameras/1 \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"assignments":[{"skill":"image_captioning"},{"skill":"embed"}]}'
```

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
4. **Assign the skills** to the cameras that should pay for them (above).
5. **Back-fill history.** The enrichers run on *new* visits only. Every visit
   recorded before is a row with no words and no vector, so the first search
   an operator runs — against yesterday — returns nothing. Set
   `EVENTS_ENRICHMENT_BACKFILL=true`; the sweep starts three minutes after
   boot, walks history newest-first, hands each qualifying visit to the same
   enricher, and stops when it is done. It is a one-off; turn it off after.

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
