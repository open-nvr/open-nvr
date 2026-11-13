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
your rule fires, annotating zone entry and exit. A zone your app
declares but nobody has drawn yet gets a stand-in polygon across the
middle of the frame, so a `zone=` rule can be seen firing before any
operator touches the catalog:

```
opennvr-app dev — driveway-watch 0.1.0 (perimeter)
  camera cam-1 · person walking left → right · 1 event/s · zones: driveway
  drew a stand-in polygon across the middle of the frame for driveway —
  the operator draws the real one in the App Catalog (--no-zones to skip).

  t=   3.0s  person  conf 0.80  at (0.30, 0.50)
  t=   4.0s  person  conf 0.80  at (0.38, 0.50)  in driveway
  t=   7.0s  ALERT [HIGH] Person loitering on cam-1
```

Events go through the same code path a real subscription uses, so what
fires here fires in production. `--still` parks the object, `--label car`
changes what it sees, `--rate` and `--count` change the pace.

## 3. Write the rule

The rule is one decorated function:

```python
app.param("dwell_s", float, default=30.0)

@app.on_detection("person", zone="driveway", dwell="$dwell_s", cooldown=60)
def loitering(event):
    event.alert(f"Person loitering on {event.camera}", severity="high")
```

The filters do what rules used to hand-roll:

| Filter | Effect |
|---|---|
| `"person"`, `"car"` | Which labels. None given means every label. |
| `zone="driveway"` | Only inside a zone the operator drew. Declaring it adds a per-camera polygon param **named `driveway`**, which is how the operator knows which zones this app expects. |
| `dwell=30` | Only after 30 seconds of continuously satisfying *this rule's filters*, then once per presence episode. With a zone, that is 30 seconds **in the zone**. |
| `cooldown=60` | At most one alert a minute for the same object. |
| `forget=120` | How long an object may go unseen before the episode ends and `dwell` re-arms. Defaults to `max(30s, dwell)`. |
| `camera="cam-1"` | One camera, or a list. |
| `min_confidence=0.6` | A floor on detector confidence. There is no hidden default — omit it and every detection reaches the rule. |

Any numeric filter can read a config value instead of a literal:
`dwell="$dwell_s"` (or `dwell=setting("dwell_s")`) resolves from the
param above when the app starts, so the operator tunes the rule from the
catalog without a code change.

The `event` carries `camera`, `label`, `confidence`, `track_id`, `zone`,
`zones`, `dwell_s`, `first_seen`, `bbox`, `center`, `count("car")`,
`detections`, `config`, and `remember()` / `recall()`. It is also how you
act:

| Call | Reaches |
|---|---|
| `event.alert(title, …)` | an operator, in the alert inbox |
| `event.publish(schema, payload)` | other apps, as a contracted domain event |
| `event.nvr` | the platform — cameras, snapshots, recordings, timeline, durable state, inference |
| `event.snapshot()` | the current frame from this event's camera |

## 4. Give it a dashboard

Declare a tile, keep a number, and the catalog renders it — no frontend:

```python
app.metric("alerted", label="Alerts fired")
app.log("recent", label="Recent sightings", limit=20)

@app.on_setup()
def prepare(config):
    app.store.update(alerted=0, recent=[])
```

`app.store` is merged into `GET /state` automatically; `@app.state()`
adds computed keys. `@app.action(...)` adds an operator button with a
generated form, `@app.ui()` an embedded HTML page, `@app.on_license()`
the licence gate for a paid app, and `@app.on_config()` /
`@app.on_shutdown()` the rest of the lifecycle. See
[App surfaces](guides/surfaces.md).


## 5. Test it

```bash
uv run pytest -q
```

The generated smoke test is the parity bar: it fires on the thing, it
stays quiet on the near-miss, and the envelope carries what a downstream
consumer needs. See [Testing](guides/testing.md).

## 6. Check it, then ship it

```bash
uv run opennvr-app validate .   # what a reviewer would check
uv run opennvr-app spec         # your app's OpenAPI 3.1 document
```

Then list it: [CONTRIBUTING_APPS.md](https://github.com/open-nvr/open-nvr/blob/main/docs/CONTRIBUTING_APPS.md).
