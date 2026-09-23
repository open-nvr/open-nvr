# footage-search

**Search your recorded footage in plain language — "show me every red
truck at the dock yesterday."** Natural-language queries answered from
the footage OpenNVR has already remembered, fully on your hardware, no
cloud and no API keys.

```
$ python footage_search.py --config config.yml search "red truck at the dock yesterday"
2 match(es):
  [Loading Dock] 2026-06-13 14:22:08  truck red
      "a red truck parked near a loading dock"
      event #8140 · plate KA01AB1234 · photo kept
```

Two query paths: the CLI above, and the **App Catalog's "Search
footage" form** — the manifest declares a `search` action, so the
catalog renders the form and proxies it to this app's contract surface
(user-JWT only) with zero frontend code in this repo folder.

| | |
|---|---|
| Pattern | Contract-only app: no stream, no store — parses the sentence, asks core |
| Adapters | (searches remembered visits — no direct call) |
| Difficulty | ⭐⭐ intermediate |
| Best for learning | NL→filter parsing, and what belongs in an app vs. the platform |

## 2.0.0 deleted this app's database

Until version 2 this app kept its own SQLite index. It subscribed to
KAI-C's inference bus and wrote every searchable keyframe into a local
file, then searched that. It worked. It was also the wrong shape, and
the reasons are worth reading if you are about to build something
similar:

- **One row per analyzed frame, not per event.** Tier-0 publishes
  continuously, so a person sitting still was thousands of identical
  rows — which is why the old store carried a 60-second coalescing
  hack, and why a search could return 25 consecutive frames instead of
  25 distinct episodes. The platform's store is one row per *visit*.
- **No camera scoping.** It indexed whatever came past on the bus, and
  an operator's view of it was whatever the app chose to show.
  `timeline.find` is scoped server-side by the same predicate as
  everything else in OpenNVR.
- **A second retention policy on a second store.** Footage deleted
  from OpenNVR stayed described here for up to 30 more days, in a file
  nobody was auditing. That is the kind of thing you discover during
  an audit rather than before one.
- **It could not carry what mattered.** No evidence photo, no plate,
  none of the claims enrichment skills make about a visit — because
  none of that exists on the bus at the moment a frame is analyzed. It
  is all on the visit.

So the index is gone, along with the indexer daemon, the NATS
subscription, the retention loop and the coalescing window. What
remains is the part that was always this app's own: turning a sentence
into a query.

**Upgrading?** Nothing to migrate — the results come from core now, and
they go further back than your index did. The old Docker volume is left
on disk rather than deleted for you; reclaim it with `docker volume rm
opennvr_footage_search_data` once you are happy.

## Why the parsing stays here

The app-facing search route does no sentence parsing on purpose. An app
has usually already decided what it is looking for, and two parsers
disagreeing about one query is a bug that is very hard to see.

Here a *human* typed the sentence. So the parsing belongs to whoever
took the human's input — this app — and what goes to core is a
structured query: labels, search text, a camera, a window.

## Run it

```bash
cd examples/footage-search && uv sync --extra dev
cp config.example.yml config.yml      # edit opennvr_url and camera aliases

python footage_search.py --config config.yml search "people near the gate in the last hour"
python footage_search.py --config config.yml search "anyone in a yellow jacket today"
python footage_search.py --config config.yml search "suitcase left in the lobby"

python footage_search.py --config config.yml serve    # the app contract
```

Each result carries the event id, and the plate and evidence photo when
the visit has them, so you can jump straight to the clip.

`search` exits **3**, not 0, when core did not answer. "Nothing
matched" and "nobody was able to look" are different answers, and a
script reading this output has to be able to tell them apart.

## How queries are parsed

By default a **heuristic parser** (no LLM, deterministic) extracts:

- **labels** — object classes named in the query (COCO vocabulary plus
  your `extra_labels`);
- **search text** — leftover descriptive words (colours, clothing)
  matched against captions and attributes;
- **time window** — `yesterday`, `today`, `this morning`, `tonight`,
  `last 30 minutes`, `past hour`, …;
- **camera** — via `camera_aliases` ("the dock" → "Loading Dock").

Set `ollama.enabled: true` to parse with a local Ollama model instead —
better at messy phrasing, and it **falls back to the heuristic parser on
any error**, so turning it on never makes search worse. It parses the
sentence and nothing else; what the results mean is the store's answer.

An alias naming a camera this app has not been given is **refused**
rather than widened to every camera. The operator asked about the gate;
answering with the loading dock would be worse than saying there is no
gate here.

## Configure

Everything is in [`config.example.yml`](config.example.yml):
`opennvr_url` (required — the store *is* the app), `extra_labels`,
`camera_aliases`, and the optional `ollama` block.

## What it does NOT do (yet)

- **No bounding-box/colour grounding.** "Red" matches caption text and
  colour claims, not a verified red region — a captioner can miss or
  misname colours.
- **No frame thumbnails.** Results say whether an evidence photo was
  kept; rendering it is the catalog's job, not this app's. (Follow-up.)
- **No semantic embeddings.** Matching is keyword/label based, not
  vector similarity, so paraphrases ("lorry" for "truck") won't match
  unless you add the synonym. That upgrade now belongs in the
  platform's search, where every app and the operator UI would get it
  at once — which is rather the point of having deleted the index.

## Tests

```bash
uv run pytest          # or: PYTHONPATH=. python -m pytest tests/ -q
```

Covers the NL→filter parser, the parsed query reaching core intact, the
two failures that are *not* "nothing matched" (an unreachable store, a
camera the app does not hold), and the operator action — all without
NATS, a database, or an LLM.
