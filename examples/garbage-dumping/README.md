# Garbage Dumping

Tells you when garbage has been dumped in a watched zone. Every look, the
app fetches a frame of the camera through core, crops it to the
no-dumping zone the operator drew, and asks the site's open-vocabulary
detector through KAI-C for what the operator calls garbage: `"garbage
bag"`, `"pile of rubbish"`, `"cardboard box"`. Phrases, not classes, so a
site adds `"construction debris"` or `"tyre"` by editing the list.

What the zone held when the app first looked is the baseline, not an
alert. More than that, seen in `confirm_looks` looks in a row, raises
`garbage_dumped` with the count; the zone going back to empty raises
`garbage_cleared` and clears it. A second dump on top of the first alerts
again.

## What you see

The app's page in the catalog shows items in the zones now, zones with
an open alert, dumps and cleared today, a per-zone table, a
recent-events log and a before-and-after gallery: the app keeps the
previous look, so each dump shows the zone before and after, and the
alert carries both pictures. `active_hours` sets when alerts fire; the
zone is still watched and counted outside it.

## What it emits

| Alert | When | Severity |
|---|---|---|
| `garbage_dumped` | more garbage than the zone held before, confirmed | `alert_severity` |
| `garbage_cleared` | the zone is empty again after an alert | low |

## What it needs

A registered adapter that advertises `open_vocab_detection`: the shipped
open-vocab adapter (`--profile open-vocab` in the apps overlay brings it
up and registers it). On CPU one look is about ten seconds, so the poll
interval defaults to 60 s; on a GPU box 15 s is fine. `detector_adapter`
names the adapter explicitly when several qualify.

Draw the zone on each camera (App Catalog → Garbage Dumping → Configure →
Cameras → the zone polygon). With no drawing the whole frame is checked.

## Quick start (compose)

```bash
docker compose -f docker-compose.yml -f docker-compose.apps.yml \
  --profile open-vocab --profile garbage-dumping up -d open-vocab-adapter garbage-dumping
```

## Standalone

```bash
cp config.example.yml config.yml      # list cameras with a frame_url and a zone each
python garbage_dumping.py --config config.yml
python garbage_dumping.py --config config.yml --once
```

## Testing

```bash
uv sync --extra dev && uv run pytest -q
```

The tests script the detector and drive the app with a fake clock, so
they need no broker, no core and no model. For an end-to-end run,
`tools/usecase_eval.py` with a dumping clip under
`data/fake-cameras/abandoned-garbage/`.
