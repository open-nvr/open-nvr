# App surfaces

Declare a surface and the App Catalog renders it. There is no
app-specific UI code anywhere in the platform, and there should be none
in your app either.

## The config form

Every `Param` becomes a form field an operator fills in, and a key in
`config.yml`. `per_camera=True` collects it once per camera; the type
`"geometry.polygon"` becomes a zone editor drawn on a camera still.

```python
Param("dwell_s", float, default=30.0, description="Seconds before it counts.")
Param("zones", "geometry.polygon", per_camera=True)
Param("watch_labels", list, default=["person"], suggestions=list(DETECTION_LABELS[:8]))
```

Config is **live**: core re-delivers it on a poll, so an app that applies
changes in `on_config_update` follows the operator without a restart.
Make that method idempotent — the first call usually restates what boot
already applied.

## The dashboard

`state_schema` declares how to render whatever `state_snapshot()`
returns. Five kinds, all with a dot-path into that dict:

| Kind | Shows |
|---|---|
| `metric` | one scalar as a stat chip |
| `gauge` | a number between `min` and `max`, amber past `warn`, red past `danger` |
| `table` | a list, with `columns` when the rows are dicts |
| `log` | a recent-events feed, newest `limit` first |
| `gallery` | thumbnails — plate crops, doorbell snapshots; `data:` URIs allowed |

A missing path renders as an em-dash, never an error: `/state` is live
data and may not have filled in yet.

For anything the five kinds cannot express, `has_ui=True` serves an HTML
dashboard at `GET /ui`, proxied by core and rendered sandboxed. Use
`ui_mode="external"` for an app that is a full application with its own
web UI; the catalog then shows an "Open app" button instead of
embedding.

## Actions

An `Action` becomes a button with a generated form. The governance
boundary is deliberate: core's proxy is **user-JWT only**, so actions
are operator verbs and the OpenNVR Agent's service key can read your
state but can never invoke one. `confirm=True` makes the catalog ask
first.

Implement them in `on_action`, and respect its error contract: raise
`KeyError(name)` for names you don't handle (→ 404), `ValueError` for
bad params (→ 400). Anything else becomes a 500 without taking the app
down.

`current_user()` inside an action returns the operator — a real
`UserContext` with their per-camera permissions, so an action can scope
what it touches to what that person may see.

## The contract server

You never construct it; every archetype starts one when
`cfg.contract_port` is set. What it serves:

```
GET  /health   /manifest   /state   /ui
GET  /openapi.json   /asyncapi.json
POST /actions/{name}   /entitlement/verify
```

Both specs are [generated from the manifest](../specs.md).

Full example:
[`09_manifest_and_surfaces.py`](https://github.com/open-nvr/open-nvr/blob/main/sdk/opennvr-app-sdk/cookbook/09_manifest_and_surfaces.py).
