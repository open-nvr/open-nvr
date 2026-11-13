# PPE Check

Tells you when someone on camera is missing required safety kit. Tier-0
already finds every person and keeps each track's best crop, so the app
runs no detector of its own on the stream: for every person who is big
enough and wholly inside the frame it fetches that crop from the
detect-pipeline and asks the site's open-vocabulary detector through
KAI-C for the phrases that describe each required item — `"hard hat"`,
`"safety helmet"`; `"safety vest"`, `"high-visibility vest"`. An item none
of whose phrases scores `min_item_score` is missing. A person found
missing an item in `confirm_checks` checks in a row raises `ppe_missing`
with the item names and the crop as evidence; a compliant person is
counted and checked again after `check_interval_seconds`.

People cut off by the frame edge are not judged — a head above the top of
the frame is not a missing helmet — and nor are people shorter than
`min_person_height` of the frame, where the kit is a few pixels. Put the
camera where people pass at a good size: a gate, a door, a turnstile.

## What you see

The app's page in the catalog shows today's compliance (a gauge of
checks that found the full kit), people checked, violations, a per-camera
table, which items go missing most, a recent-events log and a gallery of
the crop behind each alert. `active_hours` sets the shift during which
alerts fire; off shift, people are still checked and counted, quietly.

## What it emits

| Alert | When | Severity |
|---|---|---|
| `ppe_missing` | a person is missing a required item, confirmed | `alert_severity` |

## What it needs

- Tier-0 (the detect-pipeline) on the camera, which every install runs.
- A registered adapter that advertises `open_vocab_detection`: the
  shipped open-vocab adapter (`--profile open-vocab` in the apps overlay
  brings it up and registers it). On CPU one check is about ten seconds;
  checks queue and run one at a time, so a busy gate is checked at that
  rate, not faster.

`required_items` is the kit; `item_phrases` names the phrases behind
each item, so a site adds `"welding mask"` as an item by listing its
phrases. Both apply live from the App Catalog.

## Quick start (compose)

```bash
docker compose -f docker-compose.yml -f docker-compose.apps.yml \
  --profile open-vocab --profile ppe-check up -d open-vocab-adapter ppe-check
```

## Standalone

```bash
cp config.example.yml config.yml      # NATS, the pipeline's best-frame port and KAI-C
python ppe_check.py --config config.yml
python ppe_check.py --config config.yml --once
```

## Testing

```bash
uv sync --extra dev && uv run pytest -q
```

The tests script the detector and the crop store and drive the app with
Tier-0 events and a fake clock, so they need no broker, no pipeline and
no model. For an end-to-end run, `tools/usecase_eval.py` with a clip of
people with and without the kit under `data/fake-cameras/ppe/`.
