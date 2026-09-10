# Quickstart

Ten minutes, no broker, no Docker, no camera.

## 1. Scaffold

```bash
pip install opennvr-app-sdk
opennvr-app new driveway-watch
cd driveway-watch
uv sync
```

You get a runnable app, a config template, a Dockerfile, a smoke test
that already passes, and — with `--repo` — the CI and publish workflows
the App Catalog expects.

## 2. Watch it fire

```bash
uv run opennvr-app dev
```

`dev` walks a simulated person across the frame and prints every alert
your rule fires, annotating zone entry and exit:

```
opennvr-app dev — driveway-watch 0.1.0 (perimeter)
  camera cam-1 · person walking left → right · 1 event/s · zones: driveway

  t=   3.0s  person  conf 0.80  at (0.30, 0.50)
  t=   4.0s  person  conf 0.80  at (0.38, 0.50)  in driveway
  t=   4.0s  ALERT [HIGH] Person loitering on cam-1
```

Events go through the same code path a real subscription uses, so what
fires here fires in production. `--still` parks the object, `--label car`
changes what it sees, `--rate` and `--count` change the pace.

## 3. Write the rule

The rule is one decorated function:

```python
@app.on_detection("person", zone="driveway", dwell=30, cooldown=60)
def loitering(event):
    if event.confidence < event.config.min_confidence:
        return
    event.alert(f"Person loitering on {event.camera}", severity="high")
```

The filters do what rules used to hand-roll:

| Filter | Effect |
|---|---|
| `"person"`, `"car"` | Which labels. None given means every label. |
| `zone="driveway"` | Only inside a zone the operator drew — and declaring one adds the zone editor to the catalog. |
| `dwell=30` | Only after 30 seconds of continuous presence, then once per episode. |
| `cooldown=60` | At most one alert a minute for the same object. |
| `camera="cam-1"` | One camera, or a list. |
| `min_confidence=0.6` | A floor on detector confidence. |

The `event` carries `camera`, `label`, `confidence`, `track_id`, `zone`,
`zones`, `dwell_s`, `first_seen`, `bbox`, `center`, `count("car")`,
`detections`, `config`, `remember()` / `recall()`, and `alert()` — which
fills in the whole alert envelope from what it already knows.

## 4. Test it

```bash
uv run pytest -q
```

The generated smoke test is the parity bar: it fires on the thing, it
stays quiet on the near-miss, and the envelope carries what a downstream
consumer needs. See [Testing](guides/testing.md).

## 5. Check it, then ship it

```bash
uv run opennvr-app validate .   # what a reviewer would check
uv run opennvr-app spec         # your app's OpenAPI 3.1 document
```

Then list it: [CONTRIBUTING_APPS.md](https://github.com/open-nvr/open-nvr/blob/main/docs/CONTRIBUTING_APPS.md).
