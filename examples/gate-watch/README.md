# Gate Watch

Tells you when a gate has been left open. Every look, the app fetches a
frame of the camera through core, crops it to the gate the operator drew,
and asks the site's vision model one question through KAI-C: *is the gate
open or closed?* A gate seen open for longer than `open_seconds` raises
`gate_left_open`; the first "closed" after that raises `gate_closed` and
clears it.

It is the simplest shape of an AI step on a frame: no detector, no
tracking, one question whose answer the app holds to a timer. The same
pattern works for a door, a barrier, a shutter or a hatch by changing the
question.

## What you see

The app's page in the catalog shows gates open now, the longest open,
open events and alerts today, a per-gate table, a recent-events log and
a gallery of the frame at each left-open alert. `active_hours` sets when
alerts fire (the gate is still watched and counted outside it);
`escalate_after_seconds` raises `gate_still_open` (high) once if nobody
has closed the gate that long after the alert.

## What it emits

| Alert | When | Severity |
|---|---|---|
| `gate_left_open` | seen open for `open_seconds` | `alert_severity` |
| `gate_closed` | the first "closed" after a left-open alert | low |
| `gate_unknown` | `unsure_tolerance` looks in a row the model could not judge | low |

Looks the model cannot judge neither extend nor reset the open timer.
After an alert the same gate stays quiet for `cooldown_seconds` unless it
closes.

## What it needs

A registered adapter that advertises `visual_qa`: the shipped moondream
adapter (`CAPTION_ADAPTER=moondream` in the camera-agent overlay, or the
caption adapter row in the AI Adapters page). On CPU one look is a few
seconds, so the poll interval defaults to 30 s; on a GPU box 10 s is
fine. `vqa_adapter` names the adapter explicitly when several qualify.

Draw the gate on each camera (App Catalog → Gate Watch → Configure →
Cameras → the gate polygon). With no drawing the whole frame is asked
about, which works for a camera that looks at nothing but the gate.

## Quick start (compose)

```bash
docker compose -f docker-compose.yml -f docker-compose.apps.yml \
  --profile gate-watch up -d gate-watch
```

## Standalone

```bash
cp config.example.yml config.yml      # list cameras with a frame_url and a gate each
python gate_watch.py --config config.yml
python gate_watch.py --config config.yml --once
```

## Testing

```bash
uv sync --extra dev && uv run pytest -q
```

The tests script the model's answers and drive the app with a fake clock,
so they need no broker, no core and no model. For an end-to-end run,
`tools/usecase_eval.py` with a gate clip under `data/fake-cameras/gate/`.
